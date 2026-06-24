"""FHIR R4 EHR parsing service.

Turns a FHIR Bundle (or a single Patient resource) into our internal
``Patient`` + ``MedicalEvent`` rows so the matching engine can reason
over a clean, chronologically-ordered timeline.

What we extract
---------------
* **Patient** — demographics, identifiers, ``communication[].language``.
* **Condition** → ``DIAGNOSIS`` event with ICD-10 / SNOMED code.
* **MedicationStatement / MedicationRequest** → ``MEDICATION`` event with
  RxNorm code, dose text preserved in ``display_name``.
* **Observation** → ``LAB_RESULT`` or ``VITAL_SIGN`` (split by LOINC
  category if present, otherwise by category.code).  Quantitative
  observations have value + unit populated.
* **Procedure** → ``PROCEDURE`` event with CPT / SNOMED code.
* **AllergyIntolerance** → ``ALLERGY`` event.

Design notes
------------
* The parser is **pure**: no database writes happen here.  Two helpers
  are provided — :func:`parse_bundle` returns dataclass-style draft
  rows, and :func:`ingest_bundle` (which wraps it) commits them inside
  a transaction.  Splitting the two keeps unit tests trivial.
* **Idempotency.**  Each event is fingerprinted by
  ``(event_type, event_date, code, display_name)``.  Re-ingesting the
  same Bundle is a no-op; updates to existing events bump their status
  or value but never create duplicates.
* **Patient versioning.**  Every material change (new events, demographic
  edits) creates a :class:`PatientVersion` snapshot before mutating the
  patient row, so audit replay always works.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.patient import (
    EventStatusEnum,
    EventTypeEnum,
    MedicalEvent,
    Patient,
    PatientStatusEnum,
    PatientVersion,
    SexEnum,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Code-system URI → short name
# ---------------------------------------------------------------------------

_CODE_SYSTEM_MAP: dict[str, str] = {
    "http://hl7.org/fhir/sid/icd-10-cm": "ICD-10-CM",
    "http://hl7.org/fhir/sid/icd-10": "ICD-10",
    "http://snomed.info/sct": "SNOMED-CT",
    "http://loinc.org": "LOINC",
    "http://www.nlm.nih.gov/research/umls/rxnorm": "RxNorm",
    "http://www.ama-assn.org/go/cpt": "CPT",
    "urn:oid:2.16.840.1.113883.6.96": "SNOMED-CT",
    "urn:oid:2.16.840.1.113883.6.1": "LOINC",
}


def _short_code_system(uri: Optional[str]) -> Optional[str]:
    """Map a FHIR system URI to a short, display-friendly name."""
    if not uri:
        return None
    return _CODE_SYSTEM_MAP.get(uri, uri)


def _to_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Return ``value`` as a timezone-aware datetime in UTC.

    The business-time columns are now ``timestamptz`` (see Alembic
    revision 0f6f7433c1fd), so we hand Postgres aware values directly.
    Naive inputs are *assumed* to be UTC — that matches every callsite
    (FHIR parsers default to UTC when a date-only field is provided).
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _fingerprint_dt(value: Optional[datetime]) -> str:
    """Render a datetime as a stable ISO-format string for idempotency.

    Both freshly-parsed drafts and round-tripped DB values get
    normalised to aware UTC, so the resulting string is identical
    regardless of which session timezone Postgres reports them in.
    """
    aware = _to_utc(value)
    return aware.isoformat() if aware is not None else ""


# ---------------------------------------------------------------------------
# Draft dataclasses
# ---------------------------------------------------------------------------

@dataclass
class PatientDraft:
    """In-memory draft of a Patient extracted from a FHIR resource."""

    external_id: Optional[str]
    first_name: str
    last_name: str
    date_of_birth: date
    sex: SexEnum
    race: Optional[str] = None
    ethnicity: Optional[str] = None
    preferred_language: str = "en"
    status: PatientStatusEnum = PatientStatusEnum.ACTIVE
    fhir_data: Optional[dict[str, Any]] = None


@dataclass
class MedicalEventDraft:
    """In-memory draft of a single MedicalEvent."""

    event_type: EventTypeEnum
    event_date: datetime
    display_name: str
    end_date: Optional[datetime] = None
    code: Optional[str] = None
    code_system: Optional[str] = None
    value: Optional[str] = None
    unit: Optional[str] = None
    status: EventStatusEnum = EventStatusEnum.ACTIVE
    source_text: Optional[str] = None
    source_document: Optional[str] = None
    event_metadata: Optional[dict[str, Any]] = None

    def fingerprint(self) -> tuple[str, str, str, str]:
        """Stable tuple used for idempotent upsert.

        ``event_date`` is normalised to UTC and stripped of tzinfo before
        formatting.  Without this, a freshly-parsed timezone-aware draft
        would not match the round-tripped naive value Postgres returns
        from a ``timestamp without time zone`` column, breaking the
        re-ingest idempotency.
        """
        return (
            self.event_type.value,
            _fingerprint_dt(self.event_date),
            (self.code or "").lower(),
            self.display_name.lower(),
        )


@dataclass
class ParsedBundle:
    """Result of parsing one FHIR Bundle (or single Patient resource)."""

    patient: PatientDraft
    events: list[MedicalEventDraft] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Field-level helpers
# ---------------------------------------------------------------------------

def _safe_date(value: Optional[str]) -> Optional[date]:
    """Parse a FHIR ``date`` (YYYY-MM-DD or YYYY-MM or YYYY) leniently."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    logger.debug("Could not parse FHIR date %r", value)
    return None


def _safe_datetime(value: Optional[str]) -> Optional[datetime]:
    """Parse a FHIR ``dateTime`` / ``instant`` leniently → aware UTC.

    Accepts the broad FHIR spec variants: full dateTime, date-only, or
    partial year.  Returns ``None`` if nothing usable can be extracted.
    """
    if not value:
        return None
    try:
        # ``fromisoformat`` accepts trailing offset in 3.11+; normalise Z.
        normalised = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalised)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass
    d = _safe_date(value)
    if d is not None:
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return None


def _coding_first(codeable: Optional[dict[str, Any]]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Return ``(code, code_system, display)`` from a CodeableConcept.

    Picks the first ``coding`` entry; falls back to the concept-level
    ``text`` for the display.  Missing pieces become ``None`` rather than
    raising so the caller can decide how to handle them.
    """
    if not codeable:
        return (None, None, None)
    codings = codeable.get("coding") or []
    code: Optional[str] = None
    system: Optional[str] = None
    display: Optional[str] = None
    for c in codings:
        if not isinstance(c, dict):
            continue
        code = code or c.get("code")
        system = system or _short_code_system(c.get("system"))
        display = display or c.get("display")
        if code and display:
            break
    display = display or codeable.get("text")
    return (code, system, display)


def _extract_name(resource: dict[str, Any]) -> tuple[str, str]:
    """Return ``(first_name, last_name)`` from a FHIR Patient resource."""
    names = resource.get("name") or []
    for n in names:
        if not isinstance(n, dict):
            continue
        given = n.get("given") or []
        first = given[0] if given else ""
        last = n.get("family") or ""
        if first or last:
            return (first or "Unknown", last or "Unknown")
    return ("Unknown", "Unknown")


def _extract_sex(resource: dict[str, Any]) -> SexEnum:
    raw = (resource.get("gender") or "").lower()
    try:
        return SexEnum(raw)
    except ValueError:
        return SexEnum.UNKNOWN


# US Core race / ethnicity live in the extension array.
_RACE_EXT = "http://hl7.org/fhir/us/core/StructureDefinition/us-core-race"
_ETHNICITY_EXT = "http://hl7.org/fhir/us/core/StructureDefinition/us-core-ethnicity"


def _extract_us_core_text(resource: dict[str, Any], url: str) -> Optional[str]:
    """Pull the ``text`` sub-extension of a US Core race/ethnicity block."""
    for ext in resource.get("extension") or []:
        if not isinstance(ext, dict) or ext.get("url") != url:
            continue
        for sub in ext.get("extension") or []:
            if isinstance(sub, dict) and sub.get("url") == "text":
                return sub.get("valueString")
    return None


def _extract_language(resource: dict[str, Any]) -> str:
    """Return ISO 639-1 language code; defaults to ``en``."""
    for comm in resource.get("communication") or []:
        if not isinstance(comm, dict):
            continue
        code, _system, _display = _coding_first(comm.get("language"))
        if code:
            # FHIR ``en-US`` → 2-char base.
            return code.split("-")[0].lower()
    return "en"


# ---------------------------------------------------------------------------
# Resource-specific extractors
# ---------------------------------------------------------------------------

def _parse_patient(resource: dict[str, Any]) -> PatientDraft:
    first, last = _extract_name(resource)
    dob = _safe_date(resource.get("birthDate")) or date(1900, 1, 1)
    status = (
        PatientStatusEnum.DECEASED
        if resource.get("deceasedBoolean") or resource.get("deceasedDateTime")
        else PatientStatusEnum.ACTIVE
    )
    identifiers = resource.get("identifier") or []
    external_id: Optional[str] = None
    if isinstance(identifiers, list) and identifiers:
        first_id = identifiers[0]
        if isinstance(first_id, dict):
            external_id = first_id.get("value")
    # Fall back to resource id when no identifier present.
    external_id = external_id or resource.get("id")

    return PatientDraft(
        external_id=external_id,
        first_name=first,
        last_name=last,
        date_of_birth=dob,
        sex=_extract_sex(resource),
        race=_extract_us_core_text(resource, _RACE_EXT),
        ethnicity=_extract_us_core_text(resource, _ETHNICITY_EXT),
        preferred_language=_extract_language(resource),
        status=status,
        fhir_data=resource,
    )


def _event_status_from(raw: Optional[str], default: EventStatusEnum) -> EventStatusEnum:
    if not raw:
        return default
    mapping = {
        "active": EventStatusEnum.ACTIVE,
        "recurrence": EventStatusEnum.ACTIVE,
        "relapse": EventStatusEnum.ACTIVE,
        "in-progress": EventStatusEnum.ACTIVE,
        "on-hold": EventStatusEnum.INACTIVE,
        "stopped": EventStatusEnum.INACTIVE,
        "completed": EventStatusEnum.RESOLVED,
        "resolved": EventStatusEnum.RESOLVED,
        "remission": EventStatusEnum.RESOLVED,
        "inactive": EventStatusEnum.INACTIVE,
        "entered-in-error": EventStatusEnum.ENTERED_IN_ERROR,
    }
    return mapping.get(raw.lower(), default)


def _parse_condition(resource: dict[str, Any]) -> Optional[MedicalEventDraft]:
    code, system, display = _coding_first(resource.get("code"))
    when = (
        _safe_datetime(resource.get("onsetDateTime"))
        or _safe_datetime(resource.get("recordedDate"))
    )
    if when is None:
        return None
    clinical = _coding_first(resource.get("clinicalStatus"))[0]
    return MedicalEventDraft(
        event_type=EventTypeEnum.DIAGNOSIS,
        event_date=when,
        end_date=_safe_datetime(resource.get("abatementDateTime")),
        code=code,
        code_system=system,
        display_name=display or "Unknown condition",
        status=_event_status_from(clinical, EventStatusEnum.ACTIVE),
        source_document=f"Condition/{resource.get('id', '')}".rstrip("/"),
    )


def _parse_medication(resource: dict[str, Any]) -> Optional[MedicalEventDraft]:
    # MedicationRequest stores the concept under medicationCodeableConcept;
    # MedicationStatement does the same.  Fall back to medication.reference
    # display when present.
    coding = (
        resource.get("medicationCodeableConcept")
        or (resource.get("medicationReference") or {}).get("display")
    )
    code = system = display = None
    if isinstance(coding, dict):
        code, system, display = _coding_first(coding)
    elif isinstance(coding, str):
        display = coding

    when = (
        _safe_datetime(resource.get("authoredOn"))
        or _safe_datetime((resource.get("effectivePeriod") or {}).get("start"))
        or _safe_datetime(resource.get("effectiveDateTime"))
        or _safe_datetime(resource.get("dateAsserted"))
    )
    if when is None:
        return None
    return MedicalEventDraft(
        event_type=EventTypeEnum.MEDICATION,
        event_date=when,
        end_date=_safe_datetime((resource.get("effectivePeriod") or {}).get("end")),
        code=code,
        code_system=system,
        display_name=display or "Unknown medication",
        status=_event_status_from(resource.get("status"), EventStatusEnum.ACTIVE),
        source_document=f"{resource.get('resourceType')}/{resource.get('id', '')}".rstrip("/"),
    )


# LOINC category for vital signs.
_VITAL_SIGN_CATEGORIES = {"vital-signs", "vital_signs"}


def _is_vital_sign(resource: dict[str, Any]) -> bool:
    for cat in resource.get("category") or []:
        if not isinstance(cat, dict):
            continue
        for c in cat.get("coding") or []:
            if isinstance(c, dict) and (c.get("code") or "").lower() in _VITAL_SIGN_CATEGORIES:
                return True
    return False


def _parse_observation(resource: dict[str, Any]) -> Optional[MedicalEventDraft]:
    code, system, display = _coding_first(resource.get("code"))
    when = (
        _safe_datetime(resource.get("effectiveDateTime"))
        or _safe_datetime((resource.get("effectivePeriod") or {}).get("start"))
        or _safe_datetime(resource.get("issued"))
    )
    if when is None:
        return None
    value_str: Optional[str] = None
    unit: Optional[str] = None
    quantity = resource.get("valueQuantity")
    if isinstance(quantity, dict) and quantity.get("value") is not None:
        value_str = str(quantity.get("value"))
        unit = quantity.get("unit") or quantity.get("code")
    elif resource.get("valueString"):
        value_str = resource["valueString"]
    elif resource.get("valueCodeableConcept"):
        _c, _s, vdisplay = _coding_first(resource["valueCodeableConcept"])
        value_str = vdisplay

    return MedicalEventDraft(
        event_type=EventTypeEnum.VITAL_SIGN if _is_vital_sign(resource) else EventTypeEnum.LAB_RESULT,
        event_date=when,
        code=code,
        code_system=system,
        display_name=display or "Observation",
        value=value_str,
        unit=unit,
        status=_event_status_from(resource.get("status"), EventStatusEnum.ACTIVE),
        source_document=f"Observation/{resource.get('id', '')}".rstrip("/"),
    )


def _parse_procedure(resource: dict[str, Any]) -> Optional[MedicalEventDraft]:
    code, system, display = _coding_first(resource.get("code"))
    when = (
        _safe_datetime(resource.get("performedDateTime"))
        or _safe_datetime((resource.get("performedPeriod") or {}).get("start"))
    )
    if when is None:
        return None
    return MedicalEventDraft(
        event_type=EventTypeEnum.PROCEDURE,
        event_date=when,
        end_date=_safe_datetime((resource.get("performedPeriod") or {}).get("end")),
        code=code,
        code_system=system,
        display_name=display or "Procedure",
        status=_event_status_from(resource.get("status"), EventStatusEnum.RESOLVED),
        source_document=f"Procedure/{resource.get('id', '')}".rstrip("/"),
    )


def _parse_allergy(resource: dict[str, Any]) -> Optional[MedicalEventDraft]:
    code, system, display = _coding_first(resource.get("code"))
    when = (
        _safe_datetime(resource.get("recordedDate"))
        or _safe_datetime(resource.get("onsetDateTime"))
    )
    if when is None:
        when = datetime.now(timezone.utc)
    return MedicalEventDraft(
        event_type=EventTypeEnum.ALLERGY,
        event_date=when,
        code=code,
        code_system=system,
        display_name=display or "Allergy",
        status=_event_status_from(
            _coding_first(resource.get("clinicalStatus"))[0],
            EventStatusEnum.ACTIVE,
        ),
        source_document=f"AllergyIntolerance/{resource.get('id', '')}".rstrip("/"),
    )


_RESOURCE_DISPATCH: dict[str, Any] = {
    "Condition": _parse_condition,
    "MedicationStatement": _parse_medication,
    "MedicationRequest": _parse_medication,
    "MedicationAdministration": _parse_medication,
    "Observation": _parse_observation,
    "Procedure": _parse_procedure,
    "AllergyIntolerance": _parse_allergy,
}


# ---------------------------------------------------------------------------
# Public parse / ingest
# ---------------------------------------------------------------------------

def parse_bundle(bundle: dict[str, Any]) -> ParsedBundle:
    """Parse a FHIR Bundle (or a bare Patient resource) into drafts.

    Raises:
        ValueError: if the bundle contains no Patient resource.
    """
    warnings: list[str] = []

    # Allow callers to pass either a Bundle or a bare Patient resource.
    if bundle.get("resourceType") == "Patient":
        return ParsedBundle(patient=_parse_patient(bundle), events=[], warnings=warnings)

    if bundle.get("resourceType") != "Bundle":
        raise ValueError(
            "Expected resourceType=Bundle or Patient, got "
            f"{bundle.get('resourceType')!r}"
        )

    entries = bundle.get("entry") or []
    patient_resource: Optional[dict[str, Any]] = None
    event_drafts: list[MedicalEventDraft] = []

    for entry in entries:
        resource = (entry or {}).get("resource")
        if not isinstance(resource, dict):
            continue
        rtype = resource.get("resourceType")
        if rtype == "Patient":
            if patient_resource is not None:
                warnings.append(
                    "Multiple Patient resources in Bundle; using the first."
                )
                continue
            patient_resource = resource
            continue
        parser = _RESOURCE_DISPATCH.get(rtype)
        if parser is None:
            continue  # Silently skip unsupported resource types.
        try:
            draft = parser(resource)
        except Exception as exc:  # noqa: BLE001 — keep ingest resilient
            warnings.append(f"Failed to parse {rtype}/{resource.get('id')}: {exc}")
            continue
        if draft is not None:
            event_drafts.append(draft)

    if patient_resource is None:
        raise ValueError("Bundle contains no Patient resource.")

    patient_draft = _parse_patient(patient_resource)
    # Sort events chronologically — the timeline engine downstream depends
    # on this and it makes debugging much easier.
    event_drafts.sort(key=lambda e: e.event_date)
    return ParsedBundle(patient=patient_draft, events=event_drafts, warnings=warnings)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

@dataclass
class IngestStats:
    """Counts returned to the caller after :func:`ingest_bundle`."""

    patient_id: uuid.UUID
    created_patient: bool
    events_created: int = 0
    events_updated: int = 0
    events_skipped: int = 0
    new_version: Optional[int] = None
    warnings: list[str] = field(default_factory=list)


def _serialize_patient_snapshot(patient: Patient, events: list[MedicalEvent]) -> dict[str, Any]:
    """Build a JSON snapshot used by :class:`PatientVersion`."""
    return {
        "patient": {
            "id": str(patient.id),
            "external_id": patient.external_id,
            "first_name": patient.first_name,
            "last_name": patient.last_name,
            "date_of_birth": patient.date_of_birth.isoformat()
            if patient.date_of_birth
            else None,
            "sex": patient.sex,
            "race": patient.race,
            "ethnicity": patient.ethnicity,
            "preferred_language": patient.preferred_language,
            "status": patient.status,
        },
        "event_count": len(events),
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }


def ingest_bundle(
    db: Session,
    bundle: dict[str, Any],
    *,
    create_snapshot: bool = True,
    changed_by: Optional[uuid.UUID] = None,
) -> IngestStats:
    """Parse ``bundle`` and persist Patient + MedicalEvent rows.

    The function is **idempotent** with respect to medical events — re-
    ingesting an unchanged bundle leaves the database untouched.  Demographic
    updates always overwrite the patient row but a :class:`PatientVersion`
    snapshot is captured first so the prior state is recoverable.
    """
    parsed = parse_bundle(bundle)
    stats = IngestStats(patient_id=uuid.uuid4(), created_patient=False, warnings=list(parsed.warnings))

    # ── Upsert patient ────────────────────────────────────────────────
    patient: Optional[Patient] = None
    if parsed.patient.external_id:
        stmt = select(Patient).where(Patient.external_id == parsed.patient.external_id)
        patient = db.execute(stmt).scalar_one_or_none()

    if patient is None:
        patient = Patient(
            external_id=parsed.patient.external_id,
            first_name=parsed.patient.first_name,
            last_name=parsed.patient.last_name,
            date_of_birth=parsed.patient.date_of_birth,
            sex=parsed.patient.sex.value,
            race=parsed.patient.race,
            ethnicity=parsed.patient.ethnicity,
            preferred_language=parsed.patient.preferred_language,
            status=parsed.patient.status.value,
            fhir_data=parsed.patient.fhir_data,
            current_version=1,
        )
        db.add(patient)
        db.flush()  # populate patient.id
        stats.created_patient = True
        if create_snapshot:
            db.add(
                PatientVersion(
                    patient_id=patient.id,
                    version_number=1,
                    snapshot_data=_serialize_patient_snapshot(patient, []),
                    change_summary="Initial ingestion.",
                    changed_by=changed_by,
                )
            )
            stats.new_version = 1
    else:
        # Detect material demographic / FHIR changes before mutating.
        previous_snapshot_events = list(patient.medical_events)
        changed_fields: list[str] = []
        for field_name, new_value in (
            ("first_name", parsed.patient.first_name),
            ("last_name", parsed.patient.last_name),
            ("date_of_birth", parsed.patient.date_of_birth),
            ("sex", parsed.patient.sex.value),
            ("race", parsed.patient.race),
            ("ethnicity", parsed.patient.ethnicity),
            ("preferred_language", parsed.patient.preferred_language),
            ("status", parsed.patient.status.value),
        ):
            if getattr(patient, field_name) != new_value:
                changed_fields.append(field_name)
                setattr(patient, field_name, new_value)
        if parsed.patient.fhir_data is not None:
            patient.fhir_data = parsed.patient.fhir_data

        if changed_fields and create_snapshot:
            patient.current_version = (patient.current_version or 1) + 1
            db.add(
                PatientVersion(
                    patient_id=patient.id,
                    version_number=patient.current_version,
                    snapshot_data=_serialize_patient_snapshot(patient, previous_snapshot_events),
                    change_summary=f"Demographic fields changed: {', '.join(changed_fields)}",
                    changed_by=changed_by,
                )
            )
            stats.new_version = patient.current_version

    stats.patient_id = patient.id

    # ── Index existing events for idempotent upsert ───────────────────
    existing: dict[tuple[str, str, str, str], MedicalEvent] = {
        (
            evt.event_type,
            _fingerprint_dt(evt.event_date),
            (evt.code or "").lower(),
            (evt.display_name or "").lower(),
        ): evt
        for evt in patient.medical_events
    }

    new_event_rows: list[MedicalEvent] = []
    for draft in parsed.events:
        fp = draft.fingerprint()
        existing_evt = existing.get(fp)
        if existing_evt is None:
            new_event_rows.append(
                MedicalEvent(
                    patient_id=patient.id,
                    event_type=draft.event_type.value,
                    event_date=_to_utc(draft.event_date),
                    end_date=_to_utc(draft.end_date),
                    code=draft.code,
                    code_system=draft.code_system,
                    display_name=draft.display_name,
                    value=draft.value,
                    unit=draft.unit,
                    status=draft.status.value,
                    source_text=draft.source_text,
                    source_document=draft.source_document,
                    event_metadata=draft.event_metadata,
                )
            )
            stats.events_created += 1
            continue

        # Update status / value if they materially changed.
        mutated = False
        if existing_evt.status != draft.status.value:
            existing_evt.status = draft.status.value
            mutated = True
        if draft.value is not None and existing_evt.value != draft.value:
            existing_evt.value = draft.value
            mutated = True
        if mutated:
            stats.events_updated += 1
        else:
            stats.events_skipped += 1

    if new_event_rows:
        db.add_all(new_event_rows)

    db.commit()
    return stats

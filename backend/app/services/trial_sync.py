"""ClinicalTrials.gov v2 sync service.

Fetches trials matching the configured condition list, normalises the
v2 JSON response into our ``ClinicalTrial`` / ``TrialSite`` schema, and
upserts them inside a single transaction per condition.  Trial **status
transitions** (e.g. ``RECRUITING`` → ``COMPLETED``) are detected and
reported in the returned :class:`SyncStats` so downstream code (Phase 6
re-match worker) can react.

Parsing eligibility text into structured :class:`TrialCriterion` rows is
a separate concern handled by :mod:`app.services.criteria_parser` — this
module only stores the raw eligibility block.

API reference
-------------
https://clinicaltrials.gov/data-api/api  — no auth, no rate limit but
we add a polite sleep between pages anyway.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.trial import (
    ClinicalTrial,
    SiteStatusEnum,
    TrialSite,
)
from app.services.trial_category import derive_category

logger = logging.getLogger(__name__)

CTGOV_API_BASE = "https://clinicaltrials.gov/api/v2/studies"

# Polite default — CT.gov has no rate limit but we keep page-to-page
# latency small without being abusive.
_INTER_PAGE_SLEEP = 0.25


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

@dataclass
class SyncStats:
    """Aggregate result of a sync run."""

    trials_fetched: int = 0
    trials_created: int = 0
    trials_updated: int = 0
    trials_status_changed: int = 0
    sites_created: int = 0
    duration_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# HTTP fetch
# ---------------------------------------------------------------------------

def _build_query_params(
    condition: Optional[str],
    *,
    page_size: int,
    page_token: Optional[str],
    statuses: list[str],
) -> dict[str, Any]:
    """Construct the v2 query-string parameters for a single page.

    When ``condition`` is ``None`` we omit ``query.cond`` entirely and
    fetch every study matching ``statuses`` — the "give me the whole
    catalog" mode used by the admin "Sync everything" button.
    """
    params: dict[str, Any] = {
        "filter.overallStatus": ",".join(statuses),
        "pageSize": page_size,
        "format": "json",
        # Restrict the response to the fields we actually use — cuts
        # payload size 5-10x for large condition queries.
        "fields": ",".join([
            "NCTId",
            "BriefTitle",
            "OfficialTitle",
            "BriefSummary",
            "OverallStatus",
            "Phase",
            "StudyType",
            "Condition",
            "InterventionType",
            "InterventionName",
            "LeadSponsorName",
            "EnrollmentCount",
            "StartDate",
            "PrimaryCompletionDate",
            "CompletionDate",
            "EligibilityCriteria",
            "LocationFacility",
            "LocationCity",
            "LocationState",
            "LocationCountry",
            "LocationZip",
            "LocationStatus",
            "LocationGeoPoint",
            "CentralContactName",
            "CentralContactEMail",
            "CentralContactPhone",
        ]),
    }
    if condition is not None:
        params["query.cond"] = condition
    if page_token:
        params["pageToken"] = page_token
    return params


def fetch_trials_for_condition(
    condition: Optional[str],
    *,
    statuses: Optional[list[str]] = None,
    page_size: int = 50,
    max_trials: Optional[int] = None,
    client: Optional[httpx.Client] = None,
) -> Iterable[dict[str, Any]]:
    """Yield raw CT.gov v2 study objects matching ``condition``.

    The generator walks the ``nextPageToken`` chain until it runs out
    of results or ``max_trials`` is reached.  Each yielded value is the
    individual ``study`` dict — the caller doesn't need to know about
    pagination.

    Passing ``condition=None`` removes the condition filter entirely
    and returns every study that matches ``statuses``.  Use this for
    the admin "Sync everything" button.

    ``statuses`` defaults to ``["RECRUITING", "NOT_YET_RECRUITING"]``.
    """
    if statuses is None:
        statuses = ["RECRUITING", "NOT_YET_RECRUITING"]

    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=30.0)

    yielded = 0
    page_token: Optional[str] = None
    try:
        while True:
            params = _build_query_params(
                condition,
                page_size=page_size,
                page_token=page_token,
                statuses=statuses,
            )
            response = client.get(CTGOV_API_BASE, params=params)
            response.raise_for_status()
            payload = response.json()

            studies = payload.get("studies", []) or []
            for study in studies:
                yield study
                yielded += 1
                if max_trials is not None and yielded >= max_trials:
                    return

            page_token = payload.get("nextPageToken")
            if not page_token:
                return
            time.sleep(_INTER_PAGE_SLEEP)
    finally:
        if owns_client:
            client.close()


# ---------------------------------------------------------------------------
# CT.gov v2 → internal schema mapping
# ---------------------------------------------------------------------------

def _safe_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _ctgov_status_to_site_status(raw: Optional[str]) -> SiteStatusEnum:
    if not raw:
        return SiteStatusEnum.RECRUITING
    mapping = {
        "RECRUITING": SiteStatusEnum.RECRUITING,
        "NOT_YET_RECRUITING": SiteStatusEnum.NOT_YET_RECRUITING,
        "ACTIVE_NOT_RECRUITING": SiteStatusEnum.SUSPENDED,
        "ENROLLING_BY_INVITATION": SiteStatusEnum.RECRUITING,
        "COMPLETED": SiteStatusEnum.COMPLETED,
        "SUSPENDED": SiteStatusEnum.SUSPENDED,
        "TERMINATED": SiteStatusEnum.WITHDRAWN,
        "WITHDRAWN": SiteStatusEnum.WITHDRAWN,
        "AVAILABLE": SiteStatusEnum.RECRUITING,
    }
    return mapping.get(raw.upper(), SiteStatusEnum.RECRUITING)


@dataclass
class NormalisedTrial:
    """Trial fields extracted from a v2 study payload."""

    nct_id: str
    title: str
    brief_summary: Optional[str]
    phase: Optional[str]
    overall_status: str
    study_type: Optional[str]
    conditions: list[str]
    interventions: list[dict[str, Any]]
    sponsor: Optional[str]
    enrollment_count: Optional[int]
    start_date: Optional[date]
    completion_date: Optional[date]
    raw_eligibility_text: Optional[str]
    sites: list[dict[str, Any]]
    source_url: str


def _get(d: Any, *path: str, default: Any = None) -> Any:
    """Safe nested ``dict.get`` chain — never raises on missing keys."""
    cur: Any = d
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def normalise_study(study: dict[str, Any]) -> Optional[NormalisedTrial]:
    """Convert a CT.gov v2 study payload into :class:`NormalisedTrial`.

    Returns ``None`` when the payload lacks an NCT ID — there's nothing
    we can do with such a record.
    """
    protocol = study.get("protocolSection") or {}
    nct_id = _get(protocol, "identificationModule", "nctId")
    if not nct_id:
        return None

    title = (
        _get(protocol, "identificationModule", "briefTitle")
        or _get(protocol, "identificationModule", "officialTitle")
        or "Untitled trial"
    )

    overall_status = _get(protocol, "statusModule", "overallStatus") or "UNKNOWN"

    conditions = _get(protocol, "conditionsModule", "conditions") or []
    if not isinstance(conditions, list):
        conditions = []

    interventions_raw = (
        _get(protocol, "armsInterventionsModule", "interventions") or []
    )
    interventions: list[dict[str, Any]] = []
    for iv in interventions_raw:
        if not isinstance(iv, dict):
            continue
        interventions.append(
            {
                "type": iv.get("type"),
                "name": iv.get("name"),
                "description": iv.get("description"),
            }
        )

    sponsor = _get(protocol, "sponsorCollaboratorsModule", "leadSponsor", "name")
    enrollment_count = _get(protocol, "designModule", "enrollmentInfo", "count")
    if isinstance(enrollment_count, str):
        try:
            enrollment_count = int(enrollment_count)
        except ValueError:
            enrollment_count = None

    start_date = _safe_date(_get(protocol, "statusModule", "startDateStruct", "date"))
    completion_date = _safe_date(
        _get(protocol, "statusModule", "completionDateStruct", "date")
        or _get(protocol, "statusModule", "primaryCompletionDateStruct", "date")
    )

    eligibility_text = _get(protocol, "eligibilityModule", "eligibilityCriteria")

    sites: list[dict[str, Any]] = []
    locations = _get(protocol, "contactsLocationsModule", "locations") or []
    for loc in locations:
        if not isinstance(loc, dict):
            continue
        geo = loc.get("geoPoint") or {}
        sites.append(
            {
                "facility_name": loc.get("facility") or "Unknown facility",
                "city": loc.get("city"),
                "state": loc.get("state"),
                "country": loc.get("country"),
                "zip_code": loc.get("zip"),
                "latitude": geo.get("lat"),
                "longitude": geo.get("lon"),
                "site_status": _ctgov_status_to_site_status(loc.get("status")).value,
            }
        )

    return NormalisedTrial(
        nct_id=nct_id,
        title=title,
        brief_summary=_get(protocol, "descriptionModule", "briefSummary"),
        phase=", ".join(_get(protocol, "designModule", "phases") or []) or None,
        overall_status=overall_status,
        study_type=_get(protocol, "designModule", "studyType"),
        conditions=conditions,
        interventions=interventions,
        sponsor=sponsor,
        enrollment_count=enrollment_count,
        start_date=start_date,
        completion_date=completion_date,
        raw_eligibility_text=eligibility_text,
        sites=sites,
        source_url=f"https://clinicaltrials.gov/study/{nct_id}",
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _upsert_trial(db: Session, n: NormalisedTrial, stats: SyncStats) -> ClinicalTrial:
    """Insert or update a single trial + replace its sites.

    Sites are replaced wholesale on every sync because CT.gov treats
    them as a flat list and we don't want stale rows accumulating.
    Eligibility-criteria parsing is intentionally *not* triggered here
    — the criteria parser is a separate Celery job to keep this sync
    fast and idempotent.
    """
    now = datetime.now(timezone.utc)

    existing = db.execute(
        select(ClinicalTrial).where(ClinicalTrial.nct_id == n.nct_id)
    ).scalar_one_or_none()

    # Derive the top-level specialty bucket once per upsert — used by
    # the catalog dropdown filter and the future per-category counters.
    category = derive_category(n.conditions)

    if existing is None:
        trial = ClinicalTrial(
            nct_id=n.nct_id,
            title=n.title,
            brief_summary=n.brief_summary,
            phase=n.phase,
            overall_status=n.overall_status,
            study_type=n.study_type,
            conditions=n.conditions or None,
            interventions=n.interventions or None,
            sponsor=n.sponsor,
            category=category,
            enrollment_count=n.enrollment_count,
            start_date=n.start_date,
            completion_date=n.completion_date,
            last_synced_at=now,
            raw_eligibility_text=n.raw_eligibility_text,
            source_url=n.source_url,
            is_manually_added=False,
        )
        db.add(trial)
        db.flush()
        stats.trials_created += 1
    else:
        previous_status = existing.overall_status
        existing.title = n.title
        existing.brief_summary = n.brief_summary
        existing.phase = n.phase
        existing.overall_status = n.overall_status
        existing.study_type = n.study_type
        existing.conditions = n.conditions or None
        existing.interventions = n.interventions or None
        existing.sponsor = n.sponsor
        existing.category = category
        existing.enrollment_count = n.enrollment_count
        existing.start_date = n.start_date
        existing.completion_date = n.completion_date
        existing.last_synced_at = now
        existing.raw_eligibility_text = n.raw_eligibility_text
        existing.source_url = n.source_url
        if previous_status != n.overall_status:
            stats.trials_status_changed += 1
        stats.trials_updated += 1
        trial = existing
        # Clear existing sites so we can replace them.
        for site in list(trial.sites):
            db.delete(site)
        db.flush()

    # Add fresh sites.
    for site_dict in n.sites:
        db.add(
            TrialSite(
                trial_id=trial.id,
                facility_name=site_dict["facility_name"],
                city=site_dict.get("city"),
                state=site_dict.get("state"),
                country=site_dict.get("country"),
                zip_code=site_dict.get("zip_code"),
                latitude=site_dict.get("latitude"),
                longitude=site_dict.get("longitude"),
                site_status=site_dict.get("site_status") or SiteStatusEnum.RECRUITING.value,
            )
        )
        stats.sites_created += 1

    return trial


# ---------------------------------------------------------------------------
# Public sync entrypoint
# ---------------------------------------------------------------------------

def sync_trials(
    db: Session,
    *,
    conditions: Optional[list[str]] = None,
    statuses: Optional[list[str]] = None,
    max_trials_per_condition: Optional[int] = None,
    fetch_all: bool = False,
) -> SyncStats:
    """Sync recruiting trials for the given (or configured) conditions.

    The function commits **per condition**, so a transient error fetching
    one condition still leaves the previously-fetched ones persisted.
    Network errors are captured into :attr:`SyncStats.errors` rather than
    aborting the whole run.

    Pass ``fetch_all=True`` to skip the condition filter entirely and
    pull *every* trial matching ``statuses`` — the catalog-wide sync
    triggered by the admin "Sync everything" button.  In that mode the
    ``max_trials_per_condition`` value acts as an overall cap.
    """
    started = time.monotonic()
    settings = get_settings()

    if fetch_all:
        # ``[None]`` is the sentinel that drops ``query.cond`` from the
        # HTTP request — we still want the per-iteration commit + error
        # isolation that the loop provides.
        effective: list[Optional[str]] = [None]
    else:
        effective = list(conditions or settings.trial_sync_conditions or [])
        if not effective:
            raise ValueError(
                "No conditions configured.  Set TRIAL_SYNC_CONDITIONS in .env, "
                "pass conditions=[...] explicitly, or use fetch_all=True."
            )

    stats = SyncStats()
    with httpx.Client(timeout=30.0) as client:
        for condition in effective:
            label = condition if condition is not None else "all-recruiting"
            logger.info("Syncing CT.gov trials for: %s", label)
            try:
                for raw in fetch_trials_for_condition(
                    condition,
                    statuses=statuses,
                    max_trials=max_trials_per_condition,
                    client=client,
                ):
                    stats.trials_fetched += 1
                    normalised = normalise_study(raw)
                    if normalised is None:
                        continue
                    _upsert_trial(db, normalised, stats)
                db.commit()
            except httpx.HTTPError as exc:
                db.rollback()
                stats.errors.append(f"{label}: HTTP error — {exc}")
                logger.exception("HTTP error while syncing %s", label)
            except Exception as exc:  # noqa: BLE001 — keep one condition's failure local
                db.rollback()
                stats.errors.append(f"{label}: {exc}")
                logger.exception("Sync failed for %s", label)

    stats.duration_seconds = round(time.monotonic() - started, 2)
    return stats

"""LLM client abstraction for the AI Clinical Trial Matching system.

This module provides a single :class:`LLMClient` interface backed by one
of several provider implementations (Groq today; OpenAI and a local
Ollama-style runner are stubbed for future work).  Higher-level services
(``criteria_parser``, ``eligibility_reasoner``, ``plain_language``) talk
to this abstraction so that swapping providers is a one-line config
change rather than a refactor.

Design choices
--------------
* **Sync API.**  Groq's official SDK is synchronous; FastAPI runs sync
  endpoint code in a threadpool, and Celery tasks are sync, so an async
  wrapper would buy nothing and complicate retry logic.
* **Token-bucket rate limit.**  Groq's free tier caps us at 30 requests
  per minute.  A simple in-process token bucket prevents the worker
  pool from blasting through that limit and hitting 429s.
* **Exponential back-off with jitter** on retryable errors (429 + 5xx).
* **JSON-mode helper.**  Many downstream callers want a JSON object back
  — :meth:`LLMClient.complete_json` parses the response and falls back
  to extracting the first ``{...}`` block when models append commentary.

The module deliberately has *no* dependency on FastAPI or SQLAlchemy so
it stays trivially testable in isolation.
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Optional

from app.config import LLMProvider, get_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class LLMError(RuntimeError):
    """Base class for LLM-client errors."""


class LLMRateLimitError(LLMError):
    """Raised when the provider has signalled a rate-limit hit.

    Two flavours of 429 produce this:

    * **Short** waits (per-minute RPM / TPM exhaustion).  We surface
      this only after burning the configured retry budget.
    * **Long** waits (per-day TPD exhaustion — typical of Groq's free
      tier).  We surface this *immediately*, without retrying, because
      the provider has told us the cool-down is many minutes.

    ``retry_after_seconds`` carries the parsed cool-down from the
    provider response (or ``None`` when we couldn't parse one) so the
    caller / UI can show a useful message.
    """

    def __init__(self, message: str, *, retry_after_seconds: Optional[float] = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class LLMConfigurationError(LLMError):
    """Raised when the configured provider is missing required settings."""


class LLMResponseParseError(LLMError):
    """Raised when ``complete_json`` cannot recover a JSON object."""


# ---------------------------------------------------------------------------
# Message + result dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class LLMMessage:
    """Chat-style message sent to the model."""

    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass(frozen=True, slots=True)
class LLMResult:
    """Result of a single completion call."""

    text: str
    model: str
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    finish_reason: Optional[str] = None
    raw: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class _TokenBucket:
    """Thread-safe sliding-window rate limiter.

    Tracks request timestamps in a deque and blocks ``acquire()`` until
    a slot opens up within ``period`` seconds.  Simpler than a true
    leaky-bucket and sufficient for Groq's RPM quota.
    """

    def __init__(self, max_requests: int, period_seconds: float) -> None:
        self._max = max_requests
        self._period = period_seconds
        self._stamps: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block until a request slot is available, then claim one."""
        while True:
            with self._lock:
                now = time.monotonic()
                # Drop timestamps that have aged out of the window.
                while self._stamps and now - self._stamps[0] >= self._period:
                    self._stamps.popleft()
                if len(self._stamps) < self._max:
                    self._stamps.append(now)
                    return
                wait = self._period - (now - self._stamps[0])
            # Sleep outside the lock so other callers can advance.
            if wait > 0:
                time.sleep(wait)


# ---------------------------------------------------------------------------
# Provider backends
# ---------------------------------------------------------------------------

class _GroqBackend:
    """Adapter around the official ``groq`` Python SDK.

    Imported lazily so that test environments without the package can
    still import :mod:`app.services.llm_client` to exercise the parsing
    helpers.
    """

    def __init__(self, api_key: str, model: str) -> None:
        if not api_key:
            raise LLMConfigurationError(
                "GROQ_API_KEY is not set.  Add it to your .env file."
            )
        try:
            from groq import Groq  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - covered by env
            raise LLMConfigurationError(
                "The 'groq' package is required for the Groq backend. "
                "Run: pip install groq"
            ) from exc

        self._client = Groq(api_key=api_key)
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    def chat(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float,
        max_tokens: int,
        response_format: Optional[dict[str, str]] = None,
    ) -> LLMResult:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format

        resp = self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        usage = getattr(resp, "usage", None)
        return LLMResult(
            text=choice.message.content or "",
            model=resp.model,
            prompt_tokens=getattr(usage, "prompt_tokens", None) if usage else None,
            completion_tokens=getattr(usage, "completion_tokens", None) if usage else None,
            finish_reason=choice.finish_reason,
            raw=None,  # Avoid carrying the full SDK object; cheaper to log.
        )

    @staticmethod
    def is_rate_limit_error(exc: Exception) -> bool:
        """Heuristically detect Groq SDK rate-limit errors."""
        name = exc.__class__.__name__.lower()
        if "ratelimit" in name:
            return True
        status = getattr(exc, "status_code", None)
        if status == 429:
            return True
        return "429" in str(exc) or "rate limit" in str(exc).lower()

    @staticmethod
    def is_retryable_error(exc: Exception) -> bool:
        """Network blips and 5xx are retryable; client errors are not."""
        if _GroqBackend.is_rate_limit_error(exc):
            return True
        status = getattr(exc, "status_code", None)
        if isinstance(status, int) and 500 <= status < 600:
            return True
        # Connection issues from httpx surface as APIConnectionError etc.
        return "connection" in exc.__class__.__name__.lower()

    @staticmethod
    def retry_after_seconds(exc: Exception) -> Optional[float]:
        """Best-effort parse of Groq's "try again in X" hint.

        Groq's rate-limit responses surface their cool-down in two
        places:

        1. A ``retry-after`` HTTP response header (seconds, integer).
        2. A human-readable phrase embedded in the error message, e.g.
           ``"Please try again in 13m26.976s"`` for daily-token caps
           or ``"Please try again in 21.4s"`` for per-minute caps.

        Returns the cool-down in seconds, or ``None`` when no usable
        hint is present.  Callers use this to:
          - short-circuit the retry loop when the wait exceeds our
            per-call budget (TPD-style multi-minute waits);
          - replace the exponential-backoff guess with the *actual*
            wait the provider asked for.
        """
        # Header path — present on httpx responses surfaced by the SDK.
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        if headers:
            raw = headers.get("retry-after") or headers.get("Retry-After")
            if raw:
                try:
                    return float(str(raw).strip())
                except ValueError:
                    pass

        # Message path — parse phrases like "try again in 13m26.976s"
        # or "try again in 21.4s".  We deliberately keep the regex
        # narrow: only capture the immediate duration that follows
        # "try again in".  Anything else risks matching unrelated
        # numbers in the error body.
        text = str(exc)
        match = re.search(
            r"try\s+again\s+in\s+"
            r"(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+(?:\.\d+)?)s)?",
            text,
            re.IGNORECASE,
        )
        if match and any(match.groups()):
            hours   = float(match.group(1) or 0)
            minutes = float(match.group(2) or 0)
            seconds = float(match.group(3) or 0)
            total = hours * 3600 + minutes * 60 + seconds
            if total > 0:
                return total
        return None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

# Conservative defaults for Groq's free tier: 30 RPM headroom.
_FREE_TIER_RPM = 28

# When the provider asks us to wait longer than this many seconds, we
# stop retrying and surface the 429 immediately.  Anything beyond a
# minute is almost certainly a daily-token-cap (TPD) bucket that won't
# refill within the request lifetime, so retrying just blocks the
# handler and consumes more failed attempts.
_LONG_RATE_LIMIT_WAIT_SECONDS = 30.0


class LLMClient:
    """High-level LLM client used by the rest of the codebase.

    Wraps a provider backend with rate limiting, retry-with-back-off,
    and a JSON-parsing helper.  Instantiate once per process via
    :func:`get_llm_client`.
    """

    def __init__(
        self,
        provider: LLMProvider,
        model: str,
        api_key: str,
        *,
        rate_limit_rpm: int = _FREE_TIER_RPM,
        max_retries: int = 4,
    ) -> None:
        self._provider = provider
        if provider == LLMProvider.GROQ:
            self._backend = _GroqBackend(api_key=api_key, model=model)
        elif provider == LLMProvider.OPENAI:
            raise LLMConfigurationError(
                "OpenAI backend is not implemented yet. "
                "Set LLM_PROVIDER=groq."
            )
        elif provider == LLMProvider.LOCAL:
            raise LLMConfigurationError(
                "Local Ollama backend is not implemented yet. "
                "Set LLM_PROVIDER=groq."
            )
        else:  # pragma: no cover - defensive
            raise LLMConfigurationError(f"Unknown provider: {provider!r}")

        self._limiter = _TokenBucket(max_requests=rate_limit_rpm, period_seconds=60.0)
        self._max_retries = max_retries

    # ── Public API ────────────────────────────────────────────────────

    @property
    def model(self) -> str:
        """Identifier of the underlying model (for audit / reproducibility)."""
        return self._backend.model

    def complete(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        operation: Optional[str] = None,
    ) -> LLMResult:
        """Single-turn completion.  ``system`` is an optional system prompt.

        ``operation`` is a short tag identifying which service made the
        call ("criteria_parser", "eligibility_reasoner", …) — it lands
        in the :class:`~app.models.llm_usage.LLMUsage` row so the admin
        dashboard can break out usage by feature.
        """
        messages: list[LLMMessage] = []
        if system is not None:
            messages.append(LLMMessage(role="system", content=system))
        messages.append(LLMMessage(role="user", content=prompt))
        return self._chat_with_retry(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=None,
            operation=operation,
        )

    def complete_json(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        operation: Optional[str] = None,
    ) -> dict[str, Any]:
        """Request a JSON-object response and return it as a ``dict``.

        Uses the provider's ``response_format={"type": "json_object"}``
        when available; falls back to regex-extracting the first
        ``{...}`` span when the model still wraps the JSON in prose.
        """
        messages: list[LLMMessage] = []
        if system is not None:
            messages.append(LLMMessage(role="system", content=system))
        messages.append(LLMMessage(role="user", content=prompt))

        result = self._chat_with_retry(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            operation=operation,
        )
        return _parse_json_payload(result.text)

    # ── Internal ──────────────────────────────────────────────────────

    def _record_usage(
        self,
        *,
        result: Optional[LLMResult],
        latency_ms: int,
        success: bool,
        error_class: Optional[str],
        operation: Optional[str],
    ) -> None:
        """Persist one :class:`LLMUsage` row.  Best-effort — never raises.

        Imports happen inside the function so a missing DB doesn't
        break the LLM path during early bootstrap / unit tests.
        """
        try:
            from app.database import _get_session_factory
            from app.models.llm_usage import LLMUsage

            tokens_prompt     = result.prompt_tokens     if result is not None else None
            tokens_completion = result.completion_tokens if result is not None else None
            total = None
            if tokens_prompt is not None or tokens_completion is not None:
                total = (tokens_prompt or 0) + (tokens_completion or 0)

            session_factory = _get_session_factory()
            with session_factory() as db:
                db.add(LLMUsage(
                    model=self._backend.model,
                    operation=operation,
                    prompt_tokens=tokens_prompt,
                    completion_tokens=tokens_completion,
                    total_tokens=total,
                    latency_ms=latency_ms,
                    success=success,
                    error_class=error_class,
                ))
                db.commit()
        except Exception as exc:  # noqa: BLE001 - never let telemetry break LLM
            logger.debug("Failed to record LLM usage: %s", exc)

    def _chat_with_retry(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float,
        max_tokens: int,
        response_format: Optional[dict[str, str]],
        operation: Optional[str] = None,
    ) -> LLMResult:
        attempt = 0
        while True:
            self._limiter.acquire()
            started = time.monotonic()
            try:
                result = self._backend.chat(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                )
                # Success — record telemetry, then return.
                self._record_usage(
                    result=result,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    success=True,
                    error_class=None,
                    operation=operation,
                )
                return result
            except Exception as exc:  # noqa: BLE001 — provider-agnostic retry
                # Record the failed attempt before deciding whether to
                # retry — that way every attempt shows up in the
                # admin's usage telemetry.
                self._record_usage(
                    result=None,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    success=False,
                    error_class=exc.__class__.__name__,
                    operation=operation,
                )

                is_rate_limit = _GroqBackend.is_rate_limit_error(exc)

                # Inspect the provider's own retry-after hint.  When
                # it's a long wait (TPD-style daily cap), bail out
                # immediately — the chance of success within the
                # request lifetime is zero, and burning 4 retries just
                # adds ~17 seconds of pointless blocking.
                hint = _GroqBackend.retry_after_seconds(exc) if is_rate_limit else None
                if is_rate_limit and hint is not None and hint > _LONG_RATE_LIMIT_WAIT_SECONDS:
                    logger.warning(
                        "LLM rate-limited with cool-down of %.0fs (>%.0fs threshold); "
                        "skipping retries.",
                        hint, _LONG_RATE_LIMIT_WAIT_SECONDS,
                    )
                    raise LLMRateLimitError(
                        str(exc), retry_after_seconds=hint,
                    ) from exc

                if attempt >= self._max_retries or not _GroqBackend.is_retryable_error(exc):
                    if is_rate_limit:
                        raise LLMRateLimitError(
                            str(exc), retry_after_seconds=hint,
                        ) from exc
                    raise LLMError(f"LLM call failed: {exc}") from exc

                # Pick the wait: honour the provider's short cool-down
                # if one was given, otherwise fall back to exponential
                # backoff with full jitter (2**attempt ± 50%).
                if hint is not None:
                    # Add a tiny pad so we don't race the bucket reset.
                    wait = hint + 0.25
                else:
                    base = min(2 ** attempt, 30)
                    wait = base * (0.5 + random.random())
                logger.warning(
                    "LLM call failed (attempt %d/%d): %s — retrying in %.1fs",
                    attempt + 1,
                    self._max_retries,
                    exc.__class__.__name__,
                    wait,
                )
                time.sleep(wait)
                attempt += 1


# ---------------------------------------------------------------------------
# JSON extraction helper
# ---------------------------------------------------------------------------

# Greedy object regex — matches the largest ``{...}`` span in the text.
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json_payload(text: str) -> dict[str, Any]:
    """Return the first JSON object embedded in ``text`` as a dict.

    Models occasionally wrap JSON in markdown fences or prose even when
    asked for a clean object.  This helper tries strict parsing first
    and falls back to a regex extract.
    """
    stripped = text.strip()
    if not stripped:
        raise LLMResponseParseError("LLM returned an empty response.")

    # Strip common markdown code fences.
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped, flags=re.DOTALL)
        stripped = stripped.strip()

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        match = _JSON_OBJECT_RE.search(stripped)
        if not match:
            raise LLMResponseParseError(
                f"Could not find a JSON object in LLM response: {text[:200]!r}"
            ) from None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise LLMResponseParseError(
                f"Extracted span was not valid JSON: {exc}"
            ) from exc

    if not isinstance(parsed, dict):
        raise LLMResponseParseError(
            f"Expected a JSON object, got {type(parsed).__name__}."
        )
    return parsed


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_client: Optional[LLMClient] = None
_client_lock = threading.Lock()


def get_llm_client() -> LLMClient:
    """Return the process-wide :class:`LLMClient`, building it on first call.

    Reads :class:`~app.config.Settings` lazily so that importing this
    module never requires a Groq API key — useful for tests that swap
    in a fake client.
    """
    global _client  # noqa: PLW0603
    if _client is None:
        with _client_lock:
            if _client is None:
                settings = get_settings()
                _client = LLMClient(
                    provider=settings.LLM_PROVIDER,
                    model=settings.LLM_MODEL,
                    api_key=settings.GROQ_API_KEY,
                )
    return _client


def reset_llm_client() -> None:
    """Discard the cached client.  Test helpers use this to force a rebuild."""
    global _client  # noqa: PLW0603
    with _client_lock:
        _client = None

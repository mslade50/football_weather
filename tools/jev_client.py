"""Thin, fail-soft wrapper around the TypeSafe Jev typed-judgment API.

Shadow-mode only: nothing here may influence production. Every failure path
(missing key, network, API error) logs a warning and returns ``None`` so the
caller records "no verdict" rather than crashing.

Key: TYPESAFE_API_KEY (env / .env).
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Optional

from typesafe_sdk import RetryPolicy, TypeSafeClient, TypeSafeError

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # python-dotenv optional at runtime
    pass

logger = logging.getLogger(__name__)

DEFAULT_RETRY = RetryPolicy(max_retries=2, backoff_initial=0.5, backoff_max=5.0, timeout=30.0)
DEFAULT_TIMEOUT = 60.0


def _transport() -> Any:
    """Work around `pip_system_certs`: its startup hook swaps ``ssl.SSLContext`` for
    pip's vendored truststore class, and the SDK's bundled httpx2 then recurses
    inside it (RecursionError on Python 3.10). Hand the SDK a context built from
    the original class instead. Returns None when no swap is detected."""
    import ssl
    if ssl.SSLContext.__module__ == "ssl":
        return None
    try:
        import httpx2
        return httpx2.HTTPTransport(verify=ssl.create_default_context())
    except Exception as exc:
        logger.warning(f"could not build un-hooked SSL transport ({type(exc).__name__}): {exc}")
        return None


@dataclass
class Verdict:
    """A plain-dict serialization of one Jev response (or a dry-run echo)."""

    answers: Optional[dict] = None
    model: str = ""
    request_id: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    latency_ms: float = 0.0
    dry_run: bool = False
    state_keys: list[str] = field(default_factory=list)
    question_keys: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "answers": self.answers,
            "model": self.model,
            "request_id": self.request_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": round(self.latency_ms, 1),
            "dry_run": self.dry_run,
            "state_keys": self.state_keys,
            "question_keys": self.question_keys,
        }


def _plain(value: Any) -> Any:
    """Coerce SDK/pydantic values into something json.dumps can handle."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "model_dump"):
        return _plain(value.model_dump())
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    return str(value)


def _serialize(resp: Any) -> dict:
    answers: dict[str, dict] = {}
    for key, ans in (resp.choices or {}).items():
        answers[key] = {"kind": "choice", "choice": _plain(ans.choice),
                        "confidence": _plain(getattr(ans, "confidence", None)),
                        "probabilities": _plain(getattr(ans, "probabilities", None))}
    for key, ans in (resp.scores or {}).items():
        answers[key] = {"kind": "score", "score": _plain(ans.score),
                        "confidence": _plain(getattr(ans, "confidence", None)),
                        "legend": _plain(getattr(ans, "legend", None)),
                        "probabilities": _plain(getattr(ans, "probabilities", None))}
    for key, ans in (resp.nouls or {}).items():
        answers[key] = {"kind": "noul", "noul": _plain(ans.noul)}
    return answers


def judge(
    state: dict,
    questions: dict,
    *,
    dry_run: bool = False,
    model: Optional[str] = None,
    retry: Optional[RetryPolicy] = None,
    timeout: Optional[float] = None,
) -> Optional[Verdict]:
    """Ask Jev `questions` about `state`. Returns None on any failure."""
    state_keys = sorted(state or {})
    question_keys = sorted(questions or {})
    if dry_run:
        return Verdict(answers=None, model=model or "dry-run", dry_run=True,
                       state_keys=state_keys, question_keys=question_keys)

    if not os.environ.get("TYPESAFE_API_KEY"):
        logger.warning("TYPESAFE_API_KEY not set — skipping Jev judgment")
        return None

    started = time.perf_counter()
    try:
        with TypeSafeClient(model=model, retry=retry or DEFAULT_RETRY,
                            timeout=timeout or DEFAULT_TIMEOUT, transport=_transport()) as client:
            resp = client.system_one(state, questions)
        latency_ms = (time.perf_counter() - started) * 1000
        usage = getattr(resp, "usage", None)
        return Verdict(
            answers=_serialize(resp),
            model=getattr(resp, "model", "") or (model or ""),
            request_id=getattr(resp, "request_id", None),
            input_tokens=getattr(usage, "input_tokens", None) if usage else None,
            output_tokens=getattr(usage, "output_tokens", None) if usage else None,
            latency_ms=latency_ms,
            state_keys=state_keys,
            question_keys=question_keys,
        )
    except TypeSafeError as exc:
        logger.warning(f"Jev judgment failed ({type(exc).__name__}): {exc}")
        return None
    except Exception as exc:
        logger.warning(f"Jev judgment failed unexpectedly ({type(exc).__name__}): {exc}")
        return None

"""Typed errors. Every error the client raises is a TikToolsError."""

import re
import time
from typing import Any, Mapping, Optional

PRICING_URL = "https://tik.tools/pricing"


def _body(body: Any) -> dict:
    return body if isinstance(body, dict) else {}


class TikToolsError(Exception):
    """Base error. ``status`` is the HTTP status (0 for network errors), ``body`` the parsed response."""

    def __init__(self, message: str, status: int = 0, body: Any = None, endpoint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.body = body
        self.endpoint = endpoint


class AuthError(TikToolsError):
    """401, or a key whose evaluation ended or was suspended."""

    def __init__(self, message: str, status: int = 401, body: Any = None, endpoint: str = "") -> None:
        super().__init__(message, status, body, endpoint)
        self.upgrade_url = _body(body).get("upgrade_url") or PRICING_URL


class PaymentRequiredError(TikToolsError):
    """402: the firehose subscription is not active."""

    def __init__(self, message: str, status: int = 402, body: Any = None, endpoint: str = "") -> None:
        super().__init__(message, status, body, endpoint)
        self.upgrade_url = _body(body).get("upgrade_url") or PRICING_URL


class PlanRequiredError(TikToolsError):
    """403: the key works but its plan does not include this call. Show an upgrade card."""

    def __init__(self, message: str, status: int = 403, body: Any = None, endpoint: str = "", fallback_tier: Optional[str] = None) -> None:
        super().__init__(message, status, body, endpoint)
        b = _body(body)
        self.required_tier = b.get("required_tier") or fallback_tier
        self.current_tier = b.get("current_tier")
        self.upgrade_url = b.get("upgrade_url") or PRICING_URL


class NotFoundError(TikToolsError):
    pass


class RosterLimitError(TikToolsError):
    """409: 50 rosters per account or 1000 creators per roster."""


class ValidationError(TikToolsError, ValueError):
    """400, or bad arguments caught before the request."""


class RateLimitError(TikToolsError):
    """429. ``retry_after`` is in seconds."""

    def __init__(self, message: str, status: int = 429, body: Any = None, endpoint: str = "", retry_after: float = 60) -> None:
        super().__init__(message, status, body, endpoint)
        self.retry_after = retry_after


class ServerError(TikToolsError):
    pass


class NetworkError(TikToolsError):
    """Timeout, DNS or connection failure."""


class WebhookSignatureError(ValueError):
    """The webhook signature is missing, malformed, stale or does not match."""


def _num(value: Any) -> Optional[float]:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def retry_after_seconds(body: Any, headers: Optional[Mapping[str, str]] = None, now: Optional[float] = None) -> float:
    """Seconds to wait, from retry_after / retry_after_s / reset in the body or Retry-After / X-RateLimit-Reset."""
    b = _body(body)
    h = {k.lower(): v for k, v in (headers or {}).items()}
    direct = _num(b.get("retry_after")) or _num(b.get("retry_after_s"))
    if direct:
        return direct
    header = _num(h.get("retry-after"))
    if header:
        return header
    reset = _num(b.get("reset")) or _num(h.get("x-ratelimit-reset"))
    if reset:
        return max(1.0, float(int(reset - (now if now is not None else time.time()) + 0.999)))
    return 60.0


def error_from_response(status: int, body: Any, endpoint: str, headers: Optional[Mapping[str, str]] = None, min_tier: Optional[str] = None) -> TikToolsError:
    b = _body(body)
    text = body[:200] if isinstance(body, str) else ""
    message = " - ".join(str(x) for x in (b.get("error"), b.get("message")) if x) or text or f"Request failed with HTTP {status}"
    if status == 401:
        return AuthError(message, status, body, endpoint)
    if status == 402:
        return PaymentRequiredError(message, status, body, endpoint)
    if status == 403:
        # Evaluation-ended and suspended keys are 403 too, but they are not a plan gap.
        if re.search(r"evaluation|suspend|banned|inactive|revoked", message, re.I) and not b.get("required_tier"):
            return AuthError(message, status, body, endpoint)
        return PlanRequiredError(message, status, body, endpoint, min_tier)
    if status == 404:
        return NotFoundError(message, status, body, endpoint)
    if status == 409:
        return RosterLimitError(message, status, body, endpoint)
    if status in (400, 422):
        return ValidationError(message, status, body, endpoint)
    if status == 429:
        return RateLimitError(message, status, body, endpoint, retry_after_seconds(body, headers))
    if status >= 500:
        return ServerError(message, status, body, endpoint)
    return TikToolsError(message, status, body, endpoint)

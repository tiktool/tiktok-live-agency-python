"""Webhook verification. No API key needed.

    from tiktok_live_agency.webhooks import verify_webhook
    event = verify_webhook(request.body, request.headers["X-Tiktool-Signature"], SECRET)
"""

import hashlib
import hmac
import json
import time
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

from .errors import WebhookSignatureError
from .types import WebhookEvent

SIGNATURE_HEADER = "X-Tiktool-Signature"
EVENT_ID_HEADER = "X-Tiktool-Event-Id"
SECRET_PREFIX = "whsec_"
DEFAULT_TOLERANCE_SECONDS = 300


def parse_signature_header(header: str) -> Tuple[int, List[str]]:
    """Splits "t=<unix>,v1=<hex>" in any order, tolerating spaces and repeated v1 entries."""
    timestamp: Optional[int] = None
    signatures: List[str] = []
    for part in str(header or "").split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key, value = key.strip(), value.strip()
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError:
                timestamp = None
        elif key == "v1" and value:
            signatures.append(value.lower())
    if timestamp is None or not signatures:
        raise WebhookSignatureError("Malformed X-Tiktool-Signature header (expected t=<unix>,v1=<hex>)")
    return timestamp, signatures


def normalize_secret(secret: str) -> str:
    """The server signs with the whsec_-prefixed secret even when you supplied it without the prefix."""
    s = str(secret or "").strip()
    if not s:
        raise WebhookSignatureError("Missing webhook secret")
    return s if s.startswith(SECRET_PREFIX) else SECRET_PREFIX + s


def _header_from(headers_or_signature: Union[str, Mapping[str, Any]]) -> Optional[str]:
    if isinstance(headers_or_signature, str):
        return headers_or_signature
    for k, v in headers_or_signature.items():
        if str(k).lower() == SIGNATURE_HEADER.lower():
            return v[0] if isinstance(v, (list, tuple)) else v
    return None


def _sign(secret: str, timestamp: int, raw: bytes) -> str:
    return hmac.new(normalize_secret(secret).encode(), str(timestamp).encode() + b"." + raw, hashlib.sha256).hexdigest()


def verify_webhook(
    raw_body: Union[bytes, str],
    signature_header: Union[str, Mapping[str, Any]],
    secret: str,
    tolerance: int = DEFAULT_TOLERANCE_SECONDS,
    now: Optional[int] = None,
) -> WebhookEvent:
    """Verifies a delivery and returns the typed event. Pass the RAW body bytes, not parsed JSON.

    Flask: request.get_data(). FastAPI: await request.body(). Django: request.body.
    Uses a constant-time comparison and rejects timestamps more than ``tolerance`` seconds away.
    """
    if not isinstance(raw_body, (bytes, bytearray, str)):
        raise WebhookSignatureError("raw_body must be the raw request body (bytes or str), not parsed JSON")
    header = _header_from(signature_header)
    if not header:
        raise WebhookSignatureError("Missing X-Tiktool-Signature header")
    timestamp, signatures = parse_signature_header(header)
    current = int(time.time()) if now is None else now
    if tolerance > 0 and abs(current - timestamp) > tolerance:
        raise WebhookSignatureError(f"Webhook timestamp is outside the {tolerance}s tolerance")
    raw = raw_body.encode() if isinstance(raw_body, str) else bytes(raw_body)
    expected = _sign(secret, timestamp, raw)
    if not any(len(s) == len(expected) and hmac.compare_digest(s, expected) for s in signatures):
        raise WebhookSignatureError("Webhook signature does not match")
    try:
        payload = json.loads(raw.decode())
    except (ValueError, UnicodeDecodeError):
        raise WebhookSignatureError("Webhook body is not valid JSON") from None
    return WebhookEvent.from_payload(payload)


def sign_webhook_payload(raw_body: Union[bytes, str], secret: str, timestamp: Optional[int] = None) -> str:
    """The header value the server would send. Useful for testing your own endpoint."""
    ts = int(time.time()) if timestamp is None else timestamp
    raw = raw_body.encode() if isinstance(raw_body, str) else bytes(raw_body)
    return f"t={ts},v1={_sign(secret, ts, raw)}"


def _escape(text: Any) -> str:
    out = str(text if text is not None else "")
    for ch in "\\_*~`|>":
        out = out.replace(ch, "\\" + ch)
    return out


def _count(n: Any) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n if n is not None else "")


def _duration(seconds: Any) -> str:
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return ""
    if s <= 0:
        return ""
    h, m = s // 3600, (s % 3600) // 60
    return f"{h}h {m}m" if h else f"{m}m"


def to_discord_message(event: Union[WebhookEvent, Dict[str, Any]]) -> Dict[str, Any]:
    """Formats a webhook event as a Discord webhook body. POST it to your Discord webhook URL."""
    p: Dict[str, Any] = event.payload if isinstance(event, WebhookEvent) else dict(event)
    creator = (p.get("creator") or {}).get("unique_id", "")
    who = f"@{_escape(creator)}"
    kind = p.get("event")
    live = p.get("live") or {}
    if kind == "live.start":
        content = f"{who} is LIVE: {live.get('live_url') or f'https://www.tiktok.com/@{creator}/live'}"
    elif kind == "live.end":
        d = _duration(live.get("duration_seconds"))
        content = f"{who} ended their LIVE" + (f" after {d}" if d else "")
    elif kind == "live.peak_viewers":
        peak = p.get("peak") or {}
        content = f"{who} passed {peak.get('label') or _count(peak.get('threshold'))} viewers ({_count(peak.get('current_count'))} watching)"
    elif kind == "live.gift.high_value":
        gift = p.get("gift") or {}
        gifter = (gift.get("gifter") or {}).get("unique_id", "someone")
        content = f"{who} received {_escape(gift.get('name', 'a gift'))} ({_count(gift.get('diamonds'))} diamonds) from @{_escape(gifter)}"
    elif kind == "creator.follower_milestone":
        ms = p.get("milestone") or {}
        content = f"{who} reached {ms.get('label') or _count(ms.get('threshold'))} followers"
    elif kind == "creator.eligible":
        e = p.get("eligible") or {}
        content = (f"New recruit lead: @{_escape(e.get('username', ''))} (league {_escape(e.get('current_league', '?'))}, "
                   f"{_escape(e.get('region', ''))}, score {_count(e.get('current_snapshot_score'))})")
    else:
        content = f"{who}: {_escape(kind or 'event')}"
    if p.get("is_test"):
        content = f"[test] {content}"
    return {"content": content, "allowed_mentions": {"parse": []}}

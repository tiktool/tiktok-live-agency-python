"""Unofficial TikTok LIVE agency SDK by tik.tools. Not affiliated with or endorsed by TikTok or ByteDance.

    from tiktok_live_agency import AgencyClient
    tt = AgencyClient()  # reads TIKTOOLS_API_KEY
    print(tt.who_is_live(["creator_one", "creator_two"])["live"])
"""

from .aio import AsyncAgencyClient
from .client import DEFAULT_BASE_URL, AgencyClient, __version__, is_free_to_join
from .errors import (
    AuthError, NetworkError, NotFoundError, PaymentRequiredError, PlanRequiredError, RateLimitError, RosterLimitError,
    ServerError, TikToolsError, ValidationError, WebhookSignatureError,
)
from .live import LiveTracker, classify_rows, normalize_handle
from .types import LiveEvent, WebhookEvent
from .webhooks import sign_webhook_payload, to_discord_message, verify_webhook

__all__ = [
    "AgencyClient", "AsyncAgencyClient", "DEFAULT_BASE_URL", "__version__", "is_free_to_join",
    "TikToolsError", "AuthError", "PaymentRequiredError", "PlanRequiredError", "NotFoundError", "RosterLimitError",
    "ValidationError", "RateLimitError", "ServerError", "NetworkError", "WebhookSignatureError",
    "LiveTracker", "classify_rows", "normalize_handle", "LiveEvent", "WebhookEvent",
    "verify_webhook", "to_discord_message", "sign_webhook_payload",
]

"""Big-gift firehose: real-time alerts for gifts of 1,000+ diamonds. Agency plan with an active subscription.

mode "global" (default) delivers every gift of 1,000+ diamonds, mode "region" the gifts to creators in one region
(needs region), mode "league" the gifts to creators in one league division (needs league; region is optional and
narrows it to one region). Each alert carries the receiving creator's division in classType when known.

Needs the optional extra: pip install "tiktok-live-agency[realtime]"

    async for alert in stream_big_gifts(mode="region", region="UK+"):
        print(alert["gifter"]["username"], alert["gift"]["totalDiamonds"])
"""

import asyncio
import json
import os
from typing import Any, AsyncIterator, Optional
from urllib.parse import urlencode

from .errors import AuthError, PaymentRequiredError, PlanRequiredError, TikToolsError
from .types import GifterAlert

ENDPOINT = "/firehose/gifters"
MIN_BACKOFF = 1.0
MAX_BACKOFF = 30.0


def firehose_url(base_url: str, api_key: str, mode: Optional[str] = None, region: Optional[str] = None,
                 league: Optional[str] = None, min_diamonds: Optional[int] = None) -> str:
    ws_base = "ws" + base_url[4:] if base_url.lower().startswith("http") else base_url
    query = {"apiKey": api_key}
    for k, v in (("mode", mode), ("region", region), ("league", league), ("min_diamonds", min_diamonds)):
        if v:
            query[k] = str(v)
    return f"{ws_base.rstrip('/')}{ENDPOINT}?{urlencode(query)}"


def _handshake_error(status: int) -> TikToolsError:
    if status == 401:
        return AuthError("Firehose rejected the API key", 401, None, ENDPOINT)
    if status == 402:
        return PaymentRequiredError("Firehose needs an active Agency subscription", 402, None, ENDPOINT)
    if status == 403:
        return PlanRequiredError("Firehose is an Agency plan feature", 403, None, ENDPOINT, "agency_global")
    return TikToolsError(f"Firehose handshake failed with HTTP {status}", status, None, ENDPOINT)


def _status_of(exc: BaseException) -> Optional[int]:
    # websockets >= 14 raises InvalidStatus(response); older versions InvalidStatusCode(status_code).
    response = getattr(exc, "response", None)
    if response is not None and hasattr(response, "status_code"):
        return int(response.status_code)
    code = getattr(exc, "status_code", None)
    return int(code) if code is not None else None


async def stream_big_gifts(
    mode: Optional[str] = None,
    region: Optional[str] = None,
    league: Optional[str] = None,
    min_diamonds: Optional[int] = None,
    api_key: Optional[str] = None,
    base_url: str = "https://api.tik.tools",
    max_failed_connects: int = 6,
) -> AsyncIterator[GifterAlert]:
    """Yields gifter alerts forever, reconnecting with backoff (1s to 30s).

    mode="region" needs region; mode="league" needs league, and region is optional there.

    Raises PlanRequiredError / PaymentRequiredError / AuthError when the server refuses the key.
    """
    try:
        import websockets  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover
        raise TikToolsError('The firehose needs websockets: pip install "tiktok-live-agency[realtime]"') from None

    key = api_key or os.environ.get("TIKTOOLS_API_KEY")
    if not key:
        raise AuthError("Set TIKTOOLS_API_KEY or pass api_key", 401, None, ENDPOINT)
    url = firehose_url(base_url, key, mode, region, league, min_diamonds)
    backoff = MIN_BACKOFF
    failed = 0
    while True:
        ready = False
        try:
            async with websockets.connect(url, max_size=2 ** 20) as ws:
                async for raw in ws:
                    try:
                        msg: Any = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(msg, dict):
                        continue
                    if msg.get("type") == "ready":
                        ready, failed, backoff = True, 0, MIN_BACKOFF
                    elif msg.get("type") == "gifter_alert":
                        yield msg  # type: ignore[misc]
        except (OSError, asyncio.TimeoutError) as exc:
            if not ready:
                failed += 1
            last: BaseException = exc
        except Exception as exc:  # websockets.* errors share no stable base across versions
            status = _status_of(exc)
            if status in (401, 402, 403):
                raise _handshake_error(status) from None
            if not ready:
                failed += 1
            last = exc
        else:
            last = TikToolsError("Firehose connection closed")
        if failed >= max_failed_connects:
            raise TikToolsError(f"Firehose could not connect after {failed} attempts: {last}", 0, None, ENDPOINT)
        await asyncio.sleep(backoff)
        backoff = min(MAX_BACKOFF, backoff * 2)

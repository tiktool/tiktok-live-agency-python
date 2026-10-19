"""Async client. Same methods as AgencyClient, awaited. Requests run in a worker thread, so the event loop never blocks.

    from tiktok_live_agency import AsyncAgencyClient
    tt = AsyncAgencyClient()
    live = await tt.who_is_live(["creator_one", "creator_two"])
"""

import asyncio
import functools
from typing import Any, AsyncIterator, Callable, Dict, Iterable, List, Optional

from .client import DEFAULT_BASE_URL, AgencyClient
from .live import LiveTracker, MIN_WATCH_INTERVAL_SECONDS, classify_rows, jitter, unique_handles
from .errors import PlanRequiredError, RateLimitError, TikToolsError
from .types import GifterAlert, LiveEvent, NewCreator, RecruitCandidate

# Plain request/response methods mirrored one to one. Generators (iter_*, watch_live, stream_big_gifts) are written below.
_MIRRORED = (
    "request", "get_account", "list_regions", "find_creators_to_recruit", "new_creators", "league_divisions",
    "league_members", "dropped_out_creators", "league_dropouts", "gaming_dropouts", "board_history", "leaderboard",
    "global_leaderboard", "club_rankings", "popular_live", "gaming_rankings", "live_counts", "top_gifters",
    "get_creator", "get_creator_profile", "find_renamed_creator", "resolve_user_ids", "get_creator_summary",
    "get_daily_diamonds", "get_creator_gifters", "get_creator_gifts", "get_creator_battles", "get_creator_chat",
    "get_creator_activity", "get_creator_goals", "list_rosters", "create_roster", "get_roster", "delete_roster",
    "add_to_roster", "remove_from_roster", "roster_diamonds", "is_live", "is_live_detailed", "who_is_live",
    "get_eligibility", "get_eligibility_accounts", "list_webhooks", "create_webhook", "update_webhook",
    "delete_webhook", "test_webhook",
)


class AsyncAgencyClient:
    """Async twin of AgencyClient. Keep the key on the server (env TIKTOOLS_API_KEY)."""

    def __init__(self, api_key: Optional[str] = None, base_url: str = DEFAULT_BASE_URL, max_retries: int = 2,
                 timeout: float = 20, logger: Optional[Callable[[str], None]] = None) -> None:
        self.sync = AgencyClient(api_key=api_key, base_url=base_url, max_retries=max_retries, timeout=timeout, logger=logger)

    @property
    def last_rate_limit(self) -> Optional[Dict[str, float]]:
        return self.sync.last_rate_limit

    async def _paginate(self, fetch_page: Callable[[int], Any], max_pages: Optional[int]) -> AsyncIterator[Any]:
        page = 1
        while True:
            res = await asyncio.to_thread(fetch_page, page)
            for row in res.get("results", []):
                yield row
            if page >= (res.get("total_pages") or 1) or (max_pages and page >= max_pages):
                return
            page += 1

    async def iter_creators_to_recruit(self, region: str, account_region: Optional[str] = None, min_score: Optional[int] = None,
                                       limit: int = 100, max_pages: Optional[int] = None) -> AsyncIterator[RecruitCandidate]:
        async for row in self._paginate(lambda p: self.sync.find_creators_to_recruit(region, account_region, min_score, p, limit), max_pages):
            yield row

    async def iter_new_creators(self, region: str, since: Optional[str] = None, limit: int = 100,
                                max_pages: Optional[int] = None) -> AsyncIterator[NewCreator]:
        async for row in self._paginate(lambda p: self.sync.new_creators(region, since, p, limit), max_pages):
            yield row

    async def watch_live(self, handles: Iterable[str], interval: float = 60, max_polls: Optional[int] = None,
                         on_error: Optional[Callable[[Exception], None]] = None) -> AsyncIterator[LiveEvent]:
        """Async version of AgencyClient.watch_live: ``async for e in tt.watch_live(handles): ...``"""
        all_handles = unique_handles(handles)
        wait_base = max(MIN_WATCH_INTERVAL_SECONDS, interval)
        tracker = LiveTracker()
        bulk = True
        polls = 0
        while max_polls is None or polls < max_polls:
            polls += 1
            wait = jitter(wait_base)
            try:
                result = None
                if bulk:
                    try:
                        result = await asyncio.to_thread(self.sync.who_is_live, all_handles)
                    except PlanRequiredError:
                        bulk = False
                if result is None:
                    rows: List[Dict[str, Any]] = []
                    for h in all_handles:
                        try:
                            s = await asyncio.to_thread(self.sync.is_live_detailed, h)
                            rows.append({"unique_id": h, "is_live": None if s.get("live_status") == "unknown" else s.get("is_live"), "room_id": s.get("room_id")})
                        except RateLimitError:
                            raise
                        except TikToolsError:
                            rows.append({"unique_id": h, "is_live": None, "check_failed": True})
                    result = classify_rows(rows)  # type: ignore[arg-type]
                for ev in tracker.update(result):
                    yield ev
            except RateLimitError as err:
                wait = max(wait, err.retry_after)
                if on_error:
                    on_error(err)
            except TikToolsError as err:
                if on_error:
                    on_error(err)
            if max_polls is None or polls < max_polls:
                await asyncio.sleep(wait)

    def stream_big_gifts(self, mode: Optional[str] = None, region: Optional[str] = None, league: Optional[str] = None,
                         min_diamonds: Optional[int] = None, max_failed_connects: int = 6) -> AsyncIterator[GifterAlert]:
        return self.sync.stream_big_gifts(mode, region, league, min_diamonds, max_failed_connects)


def _mirror(name: str) -> Callable[..., Any]:
    target = getattr(AgencyClient, name)

    @functools.wraps(target)
    async def method(self: AsyncAgencyClient, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(getattr(self.sync, name), *args, **kwargs)

    return method


for _name in _MIRRORED:
    setattr(AsyncAgencyClient, _name, _mirror(_name))

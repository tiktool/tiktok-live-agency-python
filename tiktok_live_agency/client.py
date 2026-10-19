"""Synchronous client. Works in scripts, notebooks, Streamlit and Replit with no extra dependencies.

    from tiktok_live_agency import AgencyClient
    tt = AgencyClient()  # reads TIKTOOLS_API_KEY
    print(tt.is_live("some_creator"))
"""

import json
import logging
import os
import random
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, AsyncIterator, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Union

from .errors import (
    NetworkError, NotFoundError, PlanRequiredError, RateLimitError, TikToolsError, ValidationError, error_from_response,
)
from .live import LiveTracker, MIN_WATCH_INTERVAL_SECONDS, classify_rows, jitter, normalize_handle, unique_handles
from .types import (
    AccountInfo, CreatorCore, CreatorSummary, DailyDiamonds, GifterAlert, LiveCheckRow, LiveEvent, NewCreator,
    NewCreatorPage, RecruitCandidate, RecruitPage, RegionInfo, Roster, RosterDiamonds, WhoIsLiveResult,
)

__version__ = "0.1.0"
DEFAULT_BASE_URL = "https://api.tik.tools"

RETRYABLE_POSTS = {"/webcast/bulk_live_check", "/webcast/resolve_user_ids"}
RETRYABLE_STATUS = {429, 502, 503, 504}
MAX_RETRY_WAIT_SECONDS = 60
# Hourly route caps reset within the hour, so a page walk never waits longer than that.
MAX_PAGE_WAIT_SECONDS = 3600
ACCOUNT_CACHE_SECONDS = 300
ROSTER_CHUNK = 200
ELIGIBILITY_CHUNK = 300
FALLBACK_BULK_LIMIT = 10
UNLIMITED_BULK_CHUNK = 200
FANOUT_WARN_AT = 50


def is_free_to_join(row: Optional[Mapping[str, Any]]) -> bool:
    """True when a recruit row's last recorded Backstage verdict is eligible. Not a live invite check."""
    return bool(row) and row.get("backstage") == "eligible"  # type: ignore[union-attr]


def _chunks(items: Sequence[Any], size: int) -> Iterator[List[Any]]:
    for i in range(0, len(items), size):
        yield list(items[i:i + size])


def _stats_query(kind: str, handle: str, days: Optional[int], start: Optional[str], end: Optional[str], limit: Optional[int]) -> Dict[str, Any]:
    creator = normalize_handle(handle)
    if not creator:
        raise ValidationError("A creator handle is required", 400, None, "/webcast/agency/events")
    if bool(start) != bool(end):
        raise ValidationError("Pass both start and end, or neither", 400, None, "/webcast/agency/events")
    return {"creator": creator, "type": kind, "days": None if start else days, "from": start, "to": end, "limit": limit}


def _webhook_body(url: Optional[str] = None, events: Optional[List[str]] = None, creators: Optional[List[str]] = None,
                  rosters: Optional[List[str]] = None, secret: Optional[str] = None, bearer_token: Optional[str] = None,
                  extra_headers: Optional[Dict[str, str]] = None, name: Optional[str] = None, description: Optional[str] = None,
                  active: Optional[bool] = None) -> Dict[str, Any]:
    body: Dict[str, Any] = {}
    if url is not None:
        body["target_url"] = url
    if events is not None:
        body["event_types"] = list(events)
    if creators is not None:
        body["creators"] = unique_handles(creators)
    if rosters is not None:
        body["lists"] = list(rosters)
    if secret is not None:
        body["secret"] = secret
    if bearer_token is not None:
        body["bearer_token"] = bearer_token
    if extra_headers is not None:
        body["extra_headers"] = extra_headers
    if name is not None:
        body["name"] = name
    if description is not None:
        body["description"] = description
    if active is not None:
        body["active"] = active
    return body


class AgencyClient:
    """tik.tools agency API client. Keep the key on the server (env TIKTOOLS_API_KEY)."""

    def __init__(self, api_key: Optional[str] = None, base_url: str = DEFAULT_BASE_URL, max_retries: int = 2,
                 timeout: float = 20, logger: Optional[Callable[[str], None]] = None) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get("TIKTOOLS_API_KEY")
        self.base_url = base_url.rstrip("/")
        self.max_retries = max(0, max_retries)
        self.timeout = timeout
        self.last_rate_limit: Optional[Dict[str, float]] = None
        self._log = logger or logging.getLogger("tiktok_live_agency").warning
        self._account_cache: Optional[tuple] = None
        self._urlopen = urllib.request.urlopen
        self._sleep = time.sleep

    # -- HTTP

    def request(self, method: str, path: str, query: Optional[Mapping[str, Any]] = None, body: Any = None,
                retry: Optional[bool] = None, min_tier: Optional[str] = None) -> Any:
        """Calls any endpoint, including ones not wrapped yet. Returns the parsed JSON body."""
        verb = method.upper()
        path = path if path.startswith("/") else "/" + path
        url = self.base_url + path
        params = {}
        for k, v in (query or {}).items():
            if v is None or v == "":
                continue
            if isinstance(v, (list, tuple)):
                v = ",".join(str(x) for x in v)
            elif isinstance(v, bool):
                v = "1" if v else "0"
            params[k] = str(v)
        if params:
            url += "?" + urllib.parse.urlencode(params)
        should_retry = retry if retry is not None else (verb == "GET" or path in RETRYABLE_POSTS)
        headers = {"accept": "application/json", "user-agent": f"tiktok-live-agency-python/{__version__}"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        data = None
        if body is not None:
            headers["content-type"] = "application/json"
            data = json.dumps(body).encode()

        attempt = 0
        while True:
            req = urllib.request.Request(url, data=data, headers=headers, method=verb)
            try:
                with self._urlopen(req, timeout=self.timeout) as resp:
                    status, raw, resp_headers = resp.status, resp.read(), dict(resp.headers.items())
            except urllib.error.HTTPError as e:
                status, raw, resp_headers = e.code, e.read(), dict(e.headers.items()) if e.headers else {}
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
                if should_retry and attempt < self.max_retries:
                    self._sleep(self._backoff(attempt))
                    attempt += 1
                    continue
                raise NetworkError(f"Network error: {getattr(e, 'reason', e)}", 0, None, path) from None

            parsed: Any = None
            if raw:
                text = raw.decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(text)
                except ValueError:
                    parsed = text
            self._capture_rate_limit(resp_headers, parsed)

            if status >= 400:
                err = error_from_response(status, parsed, path, resp_headers, min_tier)
                if should_retry and attempt < self.max_retries and status in RETRYABLE_STATUS:
                    wait = err.retry_after if isinstance(err, RateLimitError) else self._backoff(attempt)
                    if wait <= MAX_RETRY_WAIT_SECONDS:
                        self._sleep(wait + random.random() * 0.25)
                        attempt += 1
                        continue
                if not self.api_key and status == 401:
                    err.message = f"{err.message}. Set TIKTOOLS_API_KEY or pass api_key."
                    err.args = (err.message,)
                raise err
            if isinstance(parsed, dict) and isinstance(parsed.get("status_code"), int) and parsed["status_code"] < 0:
                raise TikToolsError(str(parsed.get("error") or "Request failed"), status, parsed, path)
            return parsed

    @staticmethod
    def _backoff(attempt: int) -> float:
        return 0.5 * (2 ** attempt) + random.random() * 0.25

    def _capture_rate_limit(self, headers: Mapping[str, str], body: Any) -> None:
        h = {k.lower(): v for k, v in headers.items()}
        if "x-ratelimit-limit" in h:
            try:
                self.last_rate_limit = {
                    "limit": float(h["x-ratelimit-limit"]),
                    "remaining": float(h.get("x-ratelimit-remaining", 0)),
                    "reset_at": float(h.get("x-ratelimit-reset", 0)),
                }
                return
            except ValueError:
                pass
        rl = body.get("rate_limit") if isinstance(body, dict) else None
        if isinstance(rl, dict) and isinstance(rl.get("limit"), (int, float)):
            self.last_rate_limit = {"limit": rl["limit"], "remaining": rl.get("remaining", 0), "reset_at": rl.get("reset", 0)}

    def _get(self, path: str, query: Optional[Mapping[str, Any]] = None, min_tier: Optional[str] = None) -> Any:
        return self.request("GET", path, query=query, min_tier=min_tier)

    @staticmethod
    def _unwrap(body: Any) -> Any:
        return body["data"] if isinstance(body, dict) and "data" in body else body

    # -- Account

    def get_account(self, refresh: bool = False) -> AccountInfo:
        """Plan, per-minute limit, bulk check size and evaluation end. Cached for 5 minutes."""
        if not refresh and self._account_cache and time.time() - self._account_cache[0] < ACCOUNT_CACHE_SECONDS:
            return self._account_cache[1]
        value = self._unwrap(self._get("/webcast/rate_limits", min_tier="sandbox"))
        self._account_cache = (time.time(), value)
        return value

    def list_regions(self) -> List[RegionInfo]:
        """Every ranked region: code, label and live count."""
        res = self._get("/webcast/leaderboard/global", {"top": 1}, "sandbox")
        return [{"region": r.get("region"), "region_label": r.get("region_label"), "live_count": r.get("live_count")} for r in res.get("regions", [])]

    # -- Find creators to recruit

    def find_creators_to_recruit(self, region: str, account_region: Optional[str] = None, min_score: Optional[int] = None,
                                 page: int = 1, limit: int = 50) -> RecruitPage:
        """One page of recruit candidates. "UK" maps to "UK+" on the server."""
        return self._get("/webcast/eligible_creators", {"region": region, "account_region": account_region,
                                                        "min_score": min_score, "page": page, "limit": limit}, "agency_global")

    def iter_creators_to_recruit(self, region: str, account_region: Optional[str] = None, min_score: Optional[int] = None,
                                 limit: int = 100, max_pages: Optional[int] = None) -> Iterator[RecruitCandidate]:
        """Yields every recruit candidate, page by page."""
        return self._paginate(lambda p: self.find_creators_to_recruit(region, account_region, min_score, p, limit), max_pages)

    def new_creators(self, region: str, since: Optional[str] = None, page: int = 1, limit: int = 50) -> NewCreatorPage:
        """Creators who recently entered a region's recruit pool, newest first."""
        query = {"region": region, "since": since, "page": page, "limit": limit}
        try:
            return self._get("/webcast/creator_digest", query, "agency_global")
        except NotFoundError as err:
            # Board regions for UK, US, FR ... are clusters with a trailing "+"; retry once like eligible_creators does.
            alt = f"{region.upper()}+"
            avail = (err.body or {}).get("available_regions") or [] if isinstance(err.body, dict) else []
            if not region.endswith("+") and alt in avail:
                return self._get("/webcast/creator_digest", {**query, "region": alt}, "agency_global")
            raise

    def iter_new_creators(self, region: str, since: Optional[str] = None, limit: int = 100,
                          max_pages: Optional[int] = None) -> Iterator[NewCreator]:
        return self._paginate(lambda p: self.new_creators(region, since, p, limit), max_pages)

    def _paginate(self, fetch_page: Callable[[int], Any], max_pages: Optional[int]) -> Iterator[Any]:
        page = 1
        while True:
            res = fetch_page(page)
            for row in res.get("results", []):
                yield row
            if page >= (res.get("total_pages") or 1) or (max_pages and page >= max_pages):
                return
            rl = res.get("rate_limit") or {}
            if rl.get("remaining", 1) <= 0 and rl.get("reset"):
                wait = rl["reset"] - time.time()
                if wait > 0:
                    self._sleep(min(wait, MAX_PAGE_WAIT_SECONDS))
            page += 1

    def league_divisions(self, region: str) -> Dict[str, Any]:
        """League divisions in a region with member counts."""
        return self._get("/webcast/leaderboard/league_divisions", {"region": region}, "sandbox")

    def league_members(self, region: str, league: Union[str, List[str]], account_region: Optional[str] = None) -> Dict[str, Any]:
        """Creators in league divisions: "A1", ["A1", "A2"] or "all". Names hidden below Ultra."""
        if not league:
            raise ValidationError('league is required (for example "A1" or "all"); use league_divisions() to list them', 400, None, "/webcast/leaderboard/league_divisions")
        return self._get("/webcast/leaderboard/league_divisions", {"region": region, "league": league, "account_region": account_region}, "sandbox")

    def dropped_out_creators(self, region: str) -> Dict[str, Any]:
        return self._get("/webcast/leaderboard/region_movers", {"region": region}, "sandbox")

    def league_dropouts(self, region: str) -> Dict[str, Any]:
        return self._get("/webcast/leaderboard/league_movers", {"region": region}, "sandbox")

    def gaming_dropouts(self, region: str) -> Dict[str, Any]:
        """Gaming creators who fell off the gaming boards. Beta."""
        return self._get("/webcast/leaderboard/gaming_movers", {"region": region}, "sandbox")

    def board_history(self, region: str, board: str = "leaderboard", window: str = "6h", mode: str = "movers",
                      class_type: Optional[Union[int, str]] = None, limit: Optional[int] = None, at: Optional[str] = None) -> Dict[str, Any]:
        """Board movers or a past snapshot. board="league" needs class_type."""
        if board == "league" and class_type in (None, ""):
            raise ValidationError('board "league" needs class_type (from league_divisions)', 400, None, "/webcast/board_history")
        return self._get("/webcast/board_history", {"region": region, "board": board, "window": window, "mode": mode,
                                                    "class_type": class_type, "limit": limit, "at": at}, "sandbox")

    # -- Rankings and market

    def leaderboard(self, region: str = "US+", type: Optional[str] = None, class_type: Optional[Union[int, str]] = None) -> Dict[str, Any]:
        """A region's board. daily on any key; weekly, gaming, shopping need Agency; league needs Ultra."""
        return self._get("/webcast/leaderboard", {"region": region, "type": type, "class": class_type}, "sandbox")

    def global_leaderboard(self, top: int = 10) -> Dict[str, Any]:
        return self._get("/webcast/leaderboard/global", {"top": top}, "sandbox")

    def club_rankings(self, region: str) -> Dict[str, Any]:
        return self._get("/webcast/leaderboard/teams", {"region": region}, "sandbox")

    def popular_live(self, region: str) -> Dict[str, Any]:
        return self._get("/webcast/leaderboard/popular", {"region": region}, "sandbox")

    def gaming_rankings(self, region: str) -> Dict[str, Any]:
        return self._get("/webcast/leaderboard/gaming", {"region": region}, "sandbox")

    def live_counts(self) -> Dict[str, Any]:
        return self._get("/webcast/live-counts", None, "sandbox")

    def top_gifters(self, region: str, period: Optional[str] = None, limit: Optional[int] = None) -> Dict[str, Any]:
        """Biggest gifters in a region (or "GLOBAL"). Pro and up; names unmasked on Agency."""
        return self._get("/webcast/gifters/leaderboard", {"region": region, "period": period, "limit": limit}, "pro")

    # -- Vet a creator

    def get_creator(self, handle: str) -> CreatorCore:
        return self._get("/webcast/creator-core", {"u": normalize_handle(handle)}, "sandbox")

    def get_creator_profile(self, handle: str) -> Dict[str, Any]:
        return self._unwrap(self._get("/webcast/user_profile", {"unique_id": normalize_handle(handle)}, "pro"))

    def find_renamed_creator(self, handle: str) -> Dict[str, Any]:
        return self._unwrap(self._get("/webcast/lookup_username", {"username": normalize_handle(handle)}, "sandbox"))

    def resolve_user_ids(self, ids: Iterable[Union[str, int]]) -> Dict[str, Dict[str, Any]]:
        """Map of user id to {userId, username, nickname, avatarUrl}."""
        return self._unwrap(self.request("POST", "/webcast/resolve_user_ids", body={"user_ids": [str(i) for i in ids]}, min_tier="basic"))

    # -- Creator stats (Agency)
    # days: 1-90 (default 7). Or start/end as ISO UTC (end exclusive), span <= 90 days. limit: 1-500.

    def _stats(self, kind: str, handle: str, days: Optional[int], start: Optional[str], end: Optional[str], limit: Optional[int]) -> Any:
        return self._get("/webcast/agency/events", _stats_query(kind, handle, days, start, end, limit), "agency_global")

    def get_creator_summary(self, handle: str, days: Optional[int] = None, start: Optional[str] = None, end: Optional[str] = None) -> CreatorSummary:
        return self._stats("summary", handle, days, start, end, None)

    def get_daily_diamonds(self, handle: str, days: Optional[int] = None, start: Optional[str] = None, end: Optional[str] = None) -> DailyDiamonds:
        """Diamonds per UTC day from captured gifts with a complete flag per day. A lower bound, not the Backstage payout figure."""
        return self._stats("daily", handle, days, start, end, None)

    def get_creator_gifters(self, handle: str, days: Optional[int] = None, start: Optional[str] = None, end: Optional[str] = None, limit: Optional[int] = None) -> Dict[str, Any]:
        return self._stats("gifters", handle, days, start, end, limit)

    def get_creator_gifts(self, handle: str, days: Optional[int] = None, start: Optional[str] = None, end: Optional[str] = None, limit: Optional[int] = None) -> Dict[str, Any]:
        return self._stats("gifts", handle, days, start, end, limit)

    def get_creator_battles(self, handle: str, days: Optional[int] = None, start: Optional[str] = None, end: Optional[str] = None, limit: Optional[int] = None) -> Dict[str, Any]:
        return self._stats("battles", handle, days, start, end, limit)

    def get_creator_chat(self, handle: str, days: Optional[int] = None, start: Optional[str] = None, end: Optional[str] = None, limit: Optional[int] = None) -> Dict[str, Any]:
        """Chat history. Only returns messages for creators on one of your rosters."""
        return self._stats("chat", handle, days, start, end, limit)

    def get_creator_activity(self, handle: str, days: Optional[int] = None, start: Optional[str] = None, end: Optional[str] = None, limit: Optional[int] = None) -> Dict[str, Any]:
        return self._stats("activity", handle, days, start, end, limit)

    def get_creator_goals(self, handle: str, days: Optional[int] = None, start: Optional[str] = None, end: Optional[str] = None, limit: Optional[int] = None) -> Dict[str, Any]:
        return self._stats("goals", handle, days, start, end, limit)

    # -- Rosters (Agency)

    def list_rosters(self) -> Dict[str, Any]:
        return self._get("/webcast/agency/lists", None, "agency_global")

    def create_roster(self, name: str) -> Dict[str, Any]:
        clean = str(name or "").strip()
        if not clean:
            raise ValidationError("Roster name is required", 400, None, "/webcast/agency/lists")
        return self.request("POST", "/webcast/agency/lists", body={"name": clean[:60]}, min_tier="agency_global")

    def get_roster(self, roster_id: str, live_only: bool = False) -> Roster:
        return self._get("/webcast/agency/list", {"id": roster_id, "live_only": 1 if live_only else None}, "agency_global")

    def delete_roster(self, roster_id: str) -> Dict[str, Any]:
        return self.request("DELETE", "/webcast/agency/list", query={"id": roster_id}, min_tier="agency_global")

    def add_to_roster(self, roster_id: str, handles: Iterable[str]) -> Dict[str, Any]:
        """Adds creators. Gift and chat capture for them starts from this moment."""
        return self._change_roster(roster_id, handles, "add")

    def remove_from_roster(self, roster_id: str, handles: Iterable[str]) -> Dict[str, Any]:
        return self._change_roster(roster_id, handles, "remove")

    def _change_roster(self, roster_id: str, handles: Iterable[str], action: str) -> Dict[str, Any]:
        all_handles = unique_handles(handles)
        if not all_handles:
            raise ValidationError("At least one creator handle is required", 400, None, "/webcast/agency/list/members")
        last: Dict[str, Any] = {}
        changed = 0
        for part in _chunks(all_handles, ROSTER_CHUNK):
            last = self.request("POST", "/webcast/agency/list/members", body={"id": roster_id, "uniqueIds": part, "action": action}, min_tier="agency_global")
            changed += int(last.get("changed") or 0)
        return {**last, "changed": changed}

    def roster_diamonds(self, roster_id_or_handles: Union[str, Iterable[str]], days: int = 7, concurrency: int = 4) -> RosterDiamonds:
        """Diamonds per creator and in total. One request per creator; there is no roster aggregate endpoint yet."""
        if isinstance(roster_id_or_handles, str):
            handles = [normalize_handle(c["uniqueId"]) for c in self.get_roster(roster_id_or_handles).get("creators", [])]
        else:
            handles = unique_handles(roster_id_or_handles)
        if len(handles) > FANOUT_WARN_AT:
            self._log(f"roster_diamonds: {len(handles)} creators = {len(handles)} requests. Cache the result (st.cache_data) instead of calling it on every rerun.")
        out: RosterDiamonds = {"days": days, "totalDiamonds": 0, "creators": [], "failed": []}

        def one(h: str) -> Any:
            try:
                return h, self.get_daily_diamonds(h, days=days), None
            except PlanRequiredError:
                raise
            except TikToolsError as err:
                return h, None, err

        with ThreadPoolExecutor(max_workers=max(1, min(concurrency, len(handles) or 1))) as pool:
            for h, d, err in pool.map(one, handles):
                if err is not None:
                    out["failed"].append({"uniqueId": h, "error": str(err)})
                else:
                    out["creators"].append({"uniqueId": h, "totalDiamonds": d.get("totalDiamonds") or 0, "completeDays": d.get("completeDays") or 0,
                                            "incompleteDays": d.get("incompleteDays") or 0, "buckets": d.get("buckets") or []})
        out["creators"].sort(key=lambda c: c["totalDiamonds"], reverse=True)
        out["totalDiamonds"] = sum(c["totalDiamonds"] for c in out["creators"])
        return out

    # -- Live status

    def is_live_detailed(self, handle: str) -> Dict[str, Any]:
        return self._unwrap(self._get("/webcast/live_status", {"unique_id": normalize_handle(handle)}, "demo"))

    def is_live(self, handle: str) -> Optional[bool]:
        """True, False, or None when it could not be determined. Works with no key."""
        s = self.is_live_detailed(handle)
        if s.get("live_status") == "unknown" or s.get("is_live") not in (True, False):
            return None
        return bool(s["is_live"])

    def who_is_live(self, handles: Iterable[str], confirm_negatives: Optional[bool] = None, with_titles: Optional[bool] = None,
                    chunk_size: Optional[int] = None) -> WhoIsLiveResult:
        """Splits creators into live, offline and unknown. Chunks to your plan's bulk size. Basic and up."""
        all_handles = unique_handles(handles)
        result: WhoIsLiveResult = {"live": [], "offline": [], "unknown": [], "rows": []}
        if not all_handles:
            return result
        size = chunk_size
        if not size:
            try:
                limit = int(self.get_account().get("bulk_check_limit") or 0)
            except TikToolsError:
                limit = 0
            size = UNLIMITED_BULK_CHUNK if limit == -1 else limit if limit > 0 else FALLBACK_BULK_LIMIT
        for part in _chunks(all_handles, size):
            body: Dict[str, Any] = {"unique_ids": part}
            if confirm_negatives is not None:
                body["confirm_negatives"] = confirm_negatives
            if with_titles is not None:
                body["with_titles"] = with_titles
            try:
                res = self.request("POST", "/webcast/bulk_live_check", body=body, min_tier="basic")
            except PlanRequiredError as err:
                err.message = f"{err.message} On Sandbox, check creators one at a time with is_live(handle) or use watch_live(), which does that for you."
                err.args = (err.message,)
                raise
            rows = res.get("data") if isinstance(res.get("data"), list) else []
            part_result = classify_rows(rows, bool(res.get("partial_failure")))
            for k in ("live", "offline", "unknown", "rows"):
                result[k].extend(part_result[k])  # type: ignore[literal-required]
        return result

    def watch_live(self, handles: Iterable[str], interval: float = 60, max_polls: Optional[int] = None,
                   on_error: Optional[Callable[[Exception], None]] = None) -> Iterator[LiveEvent]:
        """Polls creators and yields LiveEvent(type="live"|"end"). Runs until you break (or max_polls).

        First poll only seeds state; a failed check never ends a stream; an end needs two offline polls in a row.
        Uses bulk checks on Basic and up, is_live per creator on Sandbox.

            for e in tt.watch_live(["creator_one", "creator_two"]):
                print(e.type, e.creator)
        """
        all_handles = unique_handles(handles)
        wait_base = max(MIN_WATCH_INTERVAL_SECONDS, interval)
        tracker = LiveTracker()
        state = {"bulk": True}

        def poll_once() -> WhoIsLiveResult:
            if state["bulk"]:
                try:
                    return self.who_is_live(all_handles)
                except PlanRequiredError:
                    state["bulk"] = False
            rows: List[LiveCheckRow] = []
            for h in all_handles:
                try:
                    s = self.is_live_detailed(h)
                    rows.append({"unique_id": h, "is_live": None if s.get("live_status") == "unknown" else s.get("is_live"), "room_id": s.get("room_id")})
                except RateLimitError:
                    raise
                except TikToolsError:
                    rows.append({"unique_id": h, "is_live": None, "check_failed": True})
            return classify_rows(rows)

        polls = 0
        while max_polls is None or polls < max_polls:
            polls += 1
            wait = jitter(wait_base)
            try:
                for ev in tracker.update(poll_once()):
                    yield ev
            except RateLimitError as err:
                wait = max(wait, err.retry_after)
                if on_error:
                    on_error(err)
            except TikToolsError as err:
                if on_error:
                    on_error(err)
                else:
                    self._log(f"watch_live: {err}")
            if max_polls is None or polls < max_polls:
                self._sleep(wait)

    # -- Big gifts firehose (Agency)

    def stream_big_gifts(self, mode: Optional[str] = None, region: Optional[str] = None, league: Optional[str] = None,
                         min_diamonds: Optional[int] = None, max_failed_connects: int = 6) -> AsyncIterator[GifterAlert]:
        """Async iterator of 1,000+ diamond gift alerts. Needs pip install "tiktok-live-agency[realtime]".

        mode="region" needs region; mode="league" needs league (region optional). Alerts carry the creator's division in classType.

            async for alert in tt.stream_big_gifts(mode="region", region="UK+"): ...
        """
        from .firehose import stream_big_gifts
        return stream_big_gifts(mode, region, league, min_diamonds, api_key=self.api_key, base_url=self.base_url,
                                max_failed_connects=max_failed_connects)

    # -- Eligibility (stored Backstage verdicts, Agency)

    def get_eligibility(self, handles: Iterable[str]) -> Dict[str, Any]:
        """Last verdicts your Backstage extension recorded. Empty when none; does not run a new check."""
        verdicts: Dict[str, Any] = {}
        for part in _chunks(unique_handles(handles), ELIGIBILITY_CHUNK):
            res = self._get("/eligibility/results", {"creators": ",".join(part)}, "agency_global")
            verdicts.update(res.get("verdicts") or {})
        return {"verdicts": verdicts}

    def get_eligibility_accounts(self) -> Dict[str, Any]:
        return self._get("/eligibility/limits", None, "agency_global")

    # -- Webhooks (Basic and up)

    def list_webhooks(self) -> Dict[str, Any]:
        return self._get("/webcast/webhooks", None, "basic")

    def create_webhook(self, url: str, events: List[str], creators: Optional[List[str]] = None, rosters: Optional[List[str]] = None,
                       secret: Optional[str] = None, bearer_token: Optional[str] = None, extra_headers: Optional[Dict[str, str]] = None,
                       name: Optional[str] = None, description: Optional[str] = None) -> Dict[str, Any]:
        """Creates a webhook. The returned secret is shown once; pass your own to keep it stable."""
        if not url:
            raise ValidationError("url is required", 400, None, "/webcast/webhooks")
        if not events:
            raise ValidationError('events is required, for example ["live.start"]', 400, None, "/webcast/webhooks")
        body = _webhook_body(url, events, creators, rosters, secret, bearer_token, extra_headers, name, description)
        return self.request("POST", "/webcast/webhooks", body=body, min_tier="basic")

    def update_webhook(self, webhook_id: str, url: Optional[str] = None, events: Optional[List[str]] = None,
                       creators: Optional[List[str]] = None, rosters: Optional[List[str]] = None, bearer_token: Optional[str] = None,
                       extra_headers: Optional[Dict[str, str]] = None, name: Optional[str] = None, description: Optional[str] = None,
                       active: Optional[bool] = None) -> Dict[str, Any]:
        body = _webhook_body(url, events, creators, rosters, None, bearer_token, extra_headers, name, description, active)
        return self.request("PATCH", f"/webcast/webhooks/{urllib.parse.quote(webhook_id, safe='')}", body=body, min_tier="basic")

    def delete_webhook(self, webhook_id: str) -> Dict[str, Any]:
        return self.request("DELETE", f"/webcast/webhooks/{urllib.parse.quote(webhook_id, safe='')}", min_tier="basic")

    def test_webhook(self, webhook_id: str) -> Dict[str, Any]:
        return self.request("POST", f"/webcast/webhooks/{urllib.parse.quote(webhook_id, safe='')}/test", body={}, min_tier="basic")

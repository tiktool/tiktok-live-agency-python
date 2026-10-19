"""Response types. Field names are the API's own, so docs, raw JSON and these types agree.

Dict responses are TypedDicts (total=False): they drop straight into pandas or st.dataframe.
LiveEvent and WebhookEvent are dataclasses for attribute access in scripts.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TypedDict


class AccountInfo(TypedDict, total=False):
    tier: str
    api: Dict[str, int]
    websocket: Dict[str, int]
    bulk_check_limit: int
    bulk_check_note: str
    evaluation_ends_at: str
    evaluation_days_left: int
    evaluation_note: str


class RegionInfo(TypedDict, total=False):
    region: str
    region_label: str
    live_count: int


class RecruitCandidate(TypedDict, total=False):
    username: str
    current_league: Optional[str]
    current_snapshot_score: int
    region: str
    live_region: str
    account_region: Optional[str]
    account_region_source: Optional[str]
    backstage: str


class RecruitPage(TypedDict, total=False):
    status_code: int
    page: int
    page_size: int
    total: int
    total_pages: int
    returned: int
    backstage_note: str
    region_matched: str
    available_regions: List[str]
    results: List[RecruitCandidate]
    rate_limit: Dict[str, int]


class NewCreator(TypedDict, total=False):
    username: str
    display_name: str
    usd_7d: float
    usd_30d: float
    first_seen: str
    last_active: str
    region: str
    backstage: str


class NewCreatorPage(TypedDict, total=False):
    status_code: int
    region: str
    since: Optional[str]
    page: int
    total: int
    total_pages: int
    results: List[NewCreator]


class LeagueEntry(TypedDict, total=False):
    rank: int
    unique_id: str
    nickname: str
    score: int
    is_live: bool
    league: str
    account_region: Optional[str]
    _masked: bool


class BoardEntry(TypedDict, total=False):
    rank: int
    unique_id: str
    nickname: str
    avatar: str
    score: int
    is_live: bool
    room_id: Optional[str]
    _masked: bool


class Mover(TypedDict, total=False):
    peak_rank: int
    unique_id: str
    nickname: str
    peak_score: int
    appearances: int
    live_now: bool
    diamonds_to_reenter: int


class GifterEntry(TypedDict, total=False):
    rank: int
    gifter_id: str
    unique_id: str
    nickname: str
    diamonds: int
    gifts: int
    creators_gifted: int


class CreatorCore(TypedDict, total=False):
    found: bool
    uniqueId: str
    nickname: str
    region: str
    liveRegion: str
    currentRank: Optional[int]
    earnings: Optional[int]
    isLive: bool
    viewerCount: Optional[int]


class DiamondBucket(TypedDict, total=False):
    date: str
    diamonds: int
    events: int
    uniqueGifters: int
    captured: bool
    complete: bool


class DailyDiamonds(TypedDict, total=False):
    creator: str
    timezone: str
    totalDiamonds: int
    completeDays: int
    incompleteDays: int
    buckets: List[DiamondBucket]


class CreatorSummary(TypedDict, total=False):
    creator: str
    gifts: Dict[str, int]
    battles: Dict[str, int]
    chat: Dict[str, int]
    activity: Dict[str, int]


class RosterMember(TypedDict, total=False):
    uniqueId: str
    isLive: bool
    region: Optional[str]
    nickname: Optional[str]
    score: Optional[int]
    roomId: Optional[str]
    followers: Optional[int]
    diamonds: Optional[int]


class Roster(TypedDict, total=False):
    id: str
    name: str
    memberCount: int
    liveCount: int
    creators: List[RosterMember]


class LiveCheckRow(TypedDict, total=False):
    unique_id: str
    is_live: Optional[bool]
    live_status: str
    room_id: Optional[str]
    user_count: Optional[int]
    title: Optional[str]
    check_failed: bool


class WhoIsLiveResult(TypedDict):
    live: List[LiveCheckRow]
    offline: List[LiveCheckRow]
    unknown: List[LiveCheckRow]
    rows: List[LiveCheckRow]


class RosterDiamondsCreator(TypedDict):
    uniqueId: str
    totalDiamonds: int
    completeDays: int
    incompleteDays: int
    buckets: List[DiamondBucket]


class RosterDiamonds(TypedDict):
    days: int
    totalDiamonds: int
    creators: List[RosterDiamondsCreator]
    failed: List[Dict[str, str]]


class GifterAlert(TypedDict, total=False):
    type: str
    ts: str
    gifter: Dict[str, Any]
    creator: Dict[str, Any]
    gift: Dict[str, Any]
    region: Optional[str]  # filled from the creator's ranking region when the gift has none; None when unknown
    classType: Any  # receiving creator's league division (A1 to D5) when on a league board; None when unknown


@dataclass
class LiveEvent:
    type: str  # "live" or "end"
    creator: str
    room_id: Optional[str]
    title: Optional[str]
    at: str


@dataclass
class WebhookEvent:
    """A verified webhook delivery. ``payload`` is the full JSON; ``event["live"]`` reads from it."""

    event: str
    event_id: str
    occurred_at: str
    creator: Dict[str, Any]
    is_test: bool = False
    payload: Dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        return self.payload[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "WebhookEvent":
        return cls(
            event=str(payload.get("event", "")),
            event_id=str(payload.get("event_id", "")),
            occurred_at=str(payload.get("occurred_at", "")),
            creator=payload.get("creator") or {},
            is_test=bool(payload.get("is_test", False)),
            payload=payload,
        )

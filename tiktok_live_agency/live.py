"""Live-status classification and the go-live state machine used by watch_live."""

import random
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

from .types import LiveCheckRow, LiveEvent, WhoIsLiveResult

MIN_WATCH_INTERVAL_SECONDS = 30
# Offline polls in a row before an end event fires, so one failed check never reads as a stream ending.
END_AFTER_OFFLINE_POLLS = 2


def normalize_handle(handle: str) -> str:
    return str(handle or "").strip().lstrip("@").lower()


def unique_handles(handles: Iterable[str]) -> List[str]:
    seen: Dict[str, None] = {}
    for h in handles:
        n = normalize_handle(h)
        if n:
            seen[n] = None
    return list(seen)


def classify_rows(rows: List[LiveCheckRow], partial_failure: bool = False) -> WhoIsLiveResult:
    """A row is unknown when the check failed or the API could not say, never offline."""
    out: WhoIsLiveResult = {"live": [], "offline": [], "unknown": [], "rows": rows}
    for row in rows:
        is_live = row.get("is_live")
        if row.get("check_failed") or is_live is None or row.get("live_status") == "unknown":
            out["unknown"].append(row)
        elif is_live is True:
            out["live"].append(row)
        elif partial_failure and row.get("live_status") != "offline":
            out["unknown"].append(row)
        else:
            out["offline"].append(row)
    return out


class LiveTracker:
    """First update seeds state; unknown never changes state; end needs two offline checks in a row."""

    def __init__(self) -> None:
        self._state: Dict[str, dict] = {}
        self._seeded = False

    def update(self, result: WhoIsLiveResult, now: Optional[datetime] = None) -> List[LiveEvent]:
        at = (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")
        first = not self._seeded
        self._seeded = True
        events: List[LiveEvent] = []
        for row in result["live"]:
            cid = normalize_handle(row.get("unique_id", ""))
            prev = self._state.get(cid)
            if not first and (prev is None or not prev["live"]):
                events.append(LiveEvent("live", cid, row.get("room_id"), row.get("title"), at))
            self._state[cid] = {"live": True, "streak": 0, "room_id": row.get("room_id")}
        for row in result["offline"]:
            cid = normalize_handle(row.get("unique_id", ""))
            prev = self._state.get(cid)
            if prev is None:
                self._state[cid] = {"live": False, "streak": 0, "room_id": None}
                continue
            if not prev["live"]:
                continue
            streak = prev["streak"] + 1
            if streak >= END_AFTER_OFFLINE_POLLS:
                events.append(LiveEvent("end", cid, prev["room_id"], None, at))
                self._state[cid] = {"live": False, "streak": 0, "room_id": None}
            else:
                self._state[cid] = {**prev, "streak": streak}
        return events

    def is_live(self, handle: str) -> bool:
        s = self._state.get(normalize_handle(handle))
        return bool(s and s["live"])


def jitter(seconds: float, ratio: float = 0.1) -> float:
    delta = seconds * ratio
    return seconds - delta + random.random() * delta * 2

"""Live end-to-end check of every method in spec/methods.json against https://api.tik.tools (Python client).

    TIKTOOLS_API_KEY=<key> python e2e/e2e.py

The key comes from the environment only and is never printed. The run adapts to the key's plan: a method
the plan includes must return the documented shape, a method above it must raise PlanRequiredError (the
firehose must refuse the handshake). Writes use a temporary roster and webhook that are deleted again.
The method list comes from spec/methods.json in the TypeScript package: a sibling checkout of
tiktok-live-agency, TIKTOOLS_SPEC_PATH, or the copy published on npm.
"""

import asyncio
import json
import os
import sys
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tiktok_live_agency import (  # noqa: E402
    AgencyClient, NotFoundError, PlanRequiredError, TikToolsError, is_free_to_join, sign_webhook_payload, to_discord_message, verify_webhook,
)

KEY = os.environ.get("TIKTOOLS_API_KEY")
if not KEY:
    sys.exit("Set TIKTOOLS_API_KEY")
BASE = os.environ.get("TIKTOOLS_BASE_URL", "https://api.tik.tools")
REGION = os.environ.get("E2E_REGION", "UK+")
SPEC_PATH = os.environ.get("TIKTOOLS_SPEC_PATH") or os.path.join(os.path.dirname(__file__), "..", "..", "tiktok-live-agency", "spec", "methods.json")
SPEC_URL = "https://cdn.jsdelivr.net/npm/tiktok-live-agency/spec/methods.json"


def load_spec() -> Dict[str, Any]:
    if os.path.exists(SPEC_PATH):
        with open(SPEC_PATH, encoding="utf-8") as f:
            return json.load(f)
    with urllib.request.urlopen(SPEC_URL, timeout=20) as res:
        return json.load(res)


SPEC = {m["py"]: m for m in load_spec()["methods"]}
RANK = {"demo": -1, "sandbox": 0, "basic": 1, "pro": 2, "ultra": 3, "agency_global": 4, "admin": 5}

tt = AgencyClient(api_key=KEY, base_url=BASE, max_retries=6, logger=lambda m: None)
results: List[Dict[str, Any]] = []
covered = set()
account = tt.get_account()
tier = account.get("tier", "sandbox")
rank = RANK.get(tier, 0)
print(f"plan: {tier} (bulk {account.get('bulk_check_limit')}) base {BASE}\n")


def record(name: str, ok: bool, note: str) -> None:
    covered.add(name)
    results.append({"name": name, "ok": ok, "note": note})
    print(f"{'PASS' if ok else 'FAIL'}  {name:26} {note}")


def pace() -> None:
    rl = tt.last_rate_limit
    if rl and rl.get("remaining", 1) <= 1 and rl.get("reset_at", 0) > time.time():
        time.sleep(rl["reset_at"] - time.time() + 0.5)


def run(name: str, fn: Callable[[], Any], check: Optional[Callable[[Any], bool]] = None, not_found_ok: bool = False) -> Any:
    gated = rank < RANK[SPEC[name]["minTier"]]
    pace()
    try:
        value = fn()
    except PlanRequiredError as err:
        record(name, gated, f"gated: needs {err.required_tier}" if gated else f"unexpected PlanRequiredError: {err}")
        return None
    except NotFoundError as err:
        if not not_found_ok:
            record(name, False, f"NotFoundError: {err}"[:220])
            return None
        # Lower plans may still delete (to clean up after a downgrade); with nothing created there is nothing to find.
        record(name, True, "nothing to delete (404), as expected without a created item")
        return None
    except Exception as err:  # noqa: BLE001 - report every failure kind
        record(name, False, f"{type(err).__name__}: {err}"[:220])
        return None
    if gated:
        record(name, False, f"expected PlanRequiredError on {tier}, got data")
        return value
    try:
        ok = check(value) if check else True
    except Exception as err:  # noqa: BLE001
        ok = False
        record(name, False, f"shape check raised {type(err).__name__}: {err}")
        return value
    record(name, bool(ok), "data" if ok else f"unexpected shape: {json.dumps(value, default=str)[:200]}")
    return value


record("get_account", isinstance(account.get("api", {}).get("limit"), int), "data")
run("list_regions", tt.list_regions, lambda v: len(v) > 10 and "region" in v[0])

board = run("leaderboard", lambda: tt.leaderboard(REGION), lambda v: len(v["entries"]) > 0)
handles = [e["unique_id"] for e in (board or {}).get("entries", []) if not e.get("_masked") and "*" not in e.get("unique_id", "")]
if len(handles) < 3:
    sys.exit("need 3 unmasked handles from the daily board")
h1, h2, h3 = handles[:3]

run("global_leaderboard", lambda: tt.global_leaderboard(3), lambda v: len(v["regions"]) > 10)
run("club_rankings", lambda: tt.club_rankings(REGION), lambda v: isinstance(v["entries"], list))
run("popular_live", lambda: tt.popular_live(REGION), lambda v: isinstance(v["entries"], list))
run("gaming_rankings", lambda: tt.gaming_rankings(REGION), lambda v: isinstance(v.get("entries", v.get("games")), list))
run("live_counts", tt.live_counts, lambda v: isinstance(v["global_live"], int))
run("top_gifters", lambda: tt.top_gifters(REGION, period="weekly", limit=10), lambda v: isinstance(v["entries"], list))

div = run("league_divisions", lambda: tt.league_divisions(REGION), lambda v: len(v["divisions"]) > 0)
league = ((div or {}).get("divisions") or [{"label": "A1"}])[0]["label"]
run("league_members", lambda: tt.league_members(REGION, league), lambda v: len(v["entries"]) > 0)
run("dropped_out_creators", lambda: tt.dropped_out_creators(REGION), lambda v: isinstance(v["movers"], list))
run("league_dropouts", lambda: tt.league_dropouts(REGION), lambda v: isinstance(v["divisions"], list))
run("gaming_dropouts", lambda: tt.gaming_dropouts(REGION), lambda v: isinstance(v, dict))
run("board_history", lambda: tt.board_history(REGION, window="6h", limit=10), lambda v: v["mode"] == "movers" and isinstance(v["movers"], list))
page = run("find_creators_to_recruit", lambda: tt.find_creators_to_recruit("UK", limit=5), lambda v: isinstance(v["results"], list) and "total_pages" in v)
run("iter_creators_to_recruit", lambda: list(tt.iter_creators_to_recruit("UK", limit=3, max_pages=2)), lambda v: 0 < len(v) <= 6)
record("is_free_to_join", is_free_to_join({"backstage": "eligible"}) and not is_free_to_join({"backstage": "unchecked"}), "local")
run("new_creators", lambda: tt.new_creators("UK", limit=5), lambda v: isinstance(v["results"], list))
run("iter_new_creators", lambda: list(tt.iter_new_creators("UK+", limit=2, max_pages=2)), lambda v: 0 < len(v) <= 4)

run("get_creator", lambda: tt.get_creator(h1), lambda v: v["found"] is True)
run("get_creator_profile", lambda: tt.get_creator_profile(h1), lambda v: isinstance(v["profile"]["uniqueId"], str))
run("find_renamed_creator", lambda: tt.find_renamed_creator(h1), lambda v: isinstance(v["status"], str))
run("resolve_user_ids", lambda: tt.resolve_user_ids(["6812345678901234567"]), lambda v: "6812345678901234567" in v)

run("is_live", lambda: tt.is_live(h1), lambda v: v in (True, False, None))
run("is_live_detailed", lambda: tt.is_live_detailed(h2), lambda v: "is_live" in v)
run("who_is_live", lambda: tt.who_is_live([h1, h2, h3]), lambda v: len(v["rows"]) == 3 and len(v["live"]) + len(v["offline"]) + len(v["unknown"]) == 3)
run("watch_live", lambda: list(tt.watch_live([h1, h2], interval=30, max_polls=1)), lambda v: isinstance(v, list) and v == [])

for name, fn in [
    ("get_creator_summary", lambda: tt.get_creator_summary(h1, days=7)),
    ("get_daily_diamonds", lambda: tt.get_daily_diamonds(h1, days=7)),
    ("get_creator_gifters", lambda: tt.get_creator_gifters(h1, days=7, limit=5)),
    ("get_creator_gifts", lambda: tt.get_creator_gifts(h1, days=7, limit=5)),
    ("get_creator_battles", lambda: tt.get_creator_battles(h1, days=7, limit=5)),
    ("get_creator_chat", lambda: tt.get_creator_chat(h1, days=1, limit=5)),
    ("get_creator_activity", lambda: tt.get_creator_activity(h1, days=7)),
    ("get_creator_goals", lambda: tt.get_creator_goals(h1, days=7, limit=5)),
]:
    run(name, fn, lambda v: isinstance(v, dict) and v.get("creator") == h1.lower())

roster_id = None
try:
    run("list_rosters", tt.list_rosters, lambda v: isinstance(v["lists"], list))
    created = run("create_roster", lambda: tt.create_roster(f"sdk-e2e-py-{int(time.time())}"), lambda v: v["ok"] is True)
    roster_id = (created or {}).get("id")
    rid = roster_id or "sdk-e2e-missing"
    run("add_to_roster", lambda: tt.add_to_roster(rid, [h1, h2]), lambda v: v["changed"] == 2)
    run("get_roster", lambda: tt.get_roster(rid), lambda v: len(v["creators"]) == 2)
    run("roster_diamonds", lambda: tt.roster_diamonds(rid, days=7), lambda v: len(v["creators"]) == 2 and not v["failed"])
    run("remove_from_roster", lambda: tt.remove_from_roster(rid, [h2]), lambda v: v["action"] == "remove")
finally:
    run("delete_roster", lambda: tt.delete_roster(roster_id or "sdk-e2e-missing"), lambda v: v["ok"] is True)

run("get_eligibility", lambda: tt.get_eligibility([h1, h2]), lambda v: isinstance(v["verdicts"], dict))
run("get_eligibility_accounts", tt.get_eligibility_accounts, lambda v: isinstance(v["accounts"], list))

webhook_id = None
own_secret = f"sdk_e2e_py_{int(time.time())}_secret"
try:
    run("list_webhooks", tt.list_webhooks, lambda v: isinstance(v["webhooks"], list))
    created = run("create_webhook", lambda: tt.create_webhook(url="https://example.com/tiktools-sdk-e2e-py", events=["live.start"],
                                                              creators=[h1], secret=own_secret, name="sdk e2e py (temporary)"),
                  lambda v: v["secret"] == f"whsec_{own_secret}")
    webhook_id = ((created or {}).get("webhook") or {}).get("id")
    wid = webhook_id or "00000000-0000-0000-0000-000000000000"
    run("update_webhook", lambda: tt.update_webhook(wid, events=["live.start", "live.end"], active=False), lambda v: "live.end" in v["webhook"]["event_types"])
    tested = run("test_webhook", lambda: tt.test_webhook(wid), lambda v: isinstance(v["sent"]["signature"], str))
    sent = (tested or {}).get("sent") or {}
    if sent.get("payload"):
        raw = sent["payload"] if isinstance(sent["payload"], str) else json.dumps(sent["payload"], separators=(",", ":"), ensure_ascii=False)
        try:
            ev = verify_webhook(raw, sent["signature"], own_secret)
            record("verify_webhook", ev.event == "live.start", "verified a real server signature")
        except Exception as err:  # noqa: BLE001
            record("verify_webhook", False, f"server signature did not verify: {err}")
    else:
        raw = json.dumps({"event_id": "e", "event": "live.start", "creator": {"unique_id": h1}})
        record("verify_webhook", verify_webhook(raw, sign_webhook_payload(raw, own_secret), own_secret).event == "live.start", "local signature")
    record("to_discord_message", "is LIVE" in to_discord_message({"event": "live.start", "creator": {"unique_id": h1}, "live": {}})["content"], "local")
finally:
    run("delete_webhook", lambda: tt.delete_webhook(webhook_id or "00000000-0000-0000-0000-000000000000"), lambda v: v["ok"] is True, not_found_ok=not webhook_id)


async def firehose_probe() -> str:
    async def first() -> str:
        async for _ in tt.stream_big_gifts(mode="global", max_failed_connects=1):
            return "alert received"
        return "stream ended"
    return await asyncio.wait_for(first(), 60)


if rank >= RANK["agency_global"]:
    try:
        record("stream_big_gifts", True, asyncio.run(firehose_probe()))
    except Exception as err:  # noqa: BLE001
        record("stream_big_gifts", False, f"{type(err).__name__}: {err}")
else:
    try:
        asyncio.run(firehose_probe())
        record("stream_big_gifts", False, "expected the handshake to be refused")
    except (PlanRequiredError, TikToolsError) as err:
        record("stream_big_gifts", isinstance(err, PlanRequiredError) or err.status in (402, 403), f"gated: {type(err).__name__}")

missing = [n for n in SPEC if n not in covered]
for n in missing:
    record(n, False, "not exercised by the E2E")
failed = [r for r in results if not r["ok"]]
gated_count = sum(1 for r in results if r["ok"] and r["note"].startswith("gated"))
print(f"\n{len(results) - len(failed)}/{len(results)} passed on plan {tier} ({gated_count} gated as documented)")
if failed:
    print("\nFailures:")
    for r in failed:
        print(f"  {r['name']}: {r['note']}")
    sys.exit(1)

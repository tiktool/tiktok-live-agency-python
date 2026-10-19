import os
import unittest
import urllib.error

from tiktok_live_agency import (
    AgencyClient, AuthError, NetworkError, NotFoundError, PlanRequiredError, RateLimitError, RosterLimitError,
    ServerError, TikToolsError, ValidationError, is_free_to_join,
)

from fakes import make_client


class RequestTests(unittest.TestCase):
    def test_sends_key_and_user_agent_and_joins_lists(self) -> None:
        tt, op = make_client([{"body": {"status_code": 0, "entries": []}}])
        tt.league_members("UK+", ["A1", "A2"])
        c = op.calls[0]
        self.assertEqual(c.path, "/webcast/leaderboard/league_divisions")
        self.assertEqual(c.query["league"], "A1,A2")
        self.assertNotIn("account_region", c.query)
        self.assertEqual(c.headers["x-api-key"], "tk_test")
        self.assertTrue(c.headers["user-agent"].startswith("tiktok-live-agency-python/"))

    def test_maps_statuses_to_errors(self) -> None:
        cases = [
            (401, {"error": "Invalid API key"}, AuthError),
            (403, {"error": "Evaluation period ended"}, AuthError),
            (403, {"error": "x", "required_tier": "pro", "current_tier": "sandbox"}, PlanRequiredError),
            (404, {"error": "list not found"}, NotFoundError),
            (409, {"error": "list limit reached (50)"}, RosterLimitError),
            (400, {"error": "bad"}, ValidationError),
        ]
        for status, body, kind in cases:
            tt, _ = make_client([{"status": status, "body": body}])
            with self.assertRaises(kind) as ctx:
                tt.list_rosters()
            self.assertIsInstance(ctx.exception, TikToolsError)
            self.assertEqual(ctx.exception.status, status)

    def test_plan_error_falls_back_to_method_tier(self) -> None:
        tt, _ = make_client([{"status": 403, "body": {"status_code": -1, "error": "Event history is an Agency-tier feature."}}])
        with self.assertRaises(PlanRequiredError) as ctx:
            tt.get_creator_summary("x")
        self.assertEqual(ctx.exception.required_tier, "agency_global")
        self.assertIn("pricing", ctx.exception.upgrade_url)

    def test_retries_429_and_503_on_reads(self) -> None:
        tt, op = make_client([
            {"status": 429, "body": {"error": "slow", "retry_after": 1}},
            {"status": 503, "body": {"error": "busy"}},
            {"body": {"status_code": 0, "global_live": 9, "regions": {}}},
        ])
        self.assertEqual(tt.live_counts()["global_live"], 9)
        self.assertEqual(len(op.calls), 3)

    def test_no_wait_on_long_retry_after_and_no_retry_on_writes(self) -> None:
        tt, op = make_client([{"status": 429, "body": {"error": "daily quota", "retry_after": 3600}}])
        with self.assertRaises(RateLimitError) as ctx:
            tt.live_counts()
        self.assertEqual(ctx.exception.retry_after, 3600)
        self.assertEqual(len(op.calls), 1)
        tt, op = make_client([{"status": 503, "body": {"error": "busy"}}])
        with self.assertRaises(ServerError):
            tt.create_roster("Scouting")
        self.assertEqual(len(op.calls), 1)

    def test_network_errors_retry_then_raise(self) -> None:
        err = urllib.error.URLError("down")
        tt, op = make_client([err, err, err])
        with self.assertRaises(NetworkError):
            tt.live_counts()
        self.assertEqual(len(op.calls), 3)

    def test_negative_status_code_on_200_raises(self) -> None:
        tt, _ = make_client([{"body": {"status_code": -1, "error": "upstream failed"}}])
        with self.assertRaises(TikToolsError):
            tt.live_counts()

    def test_rate_limit_capture(self) -> None:
        tt, _ = make_client([
            {"body": {"status_code": 0}, "headers": {"X-RateLimit-Limit": "120", "X-RateLimit-Remaining": "7", "X-RateLimit-Reset": "1792427500"}},
            {"body": {"status_code": 0, "results": [], "total_pages": 0, "rate_limit": {"limit": 60000, "remaining": 1, "reset": 5}}},
        ])
        tt.live_counts()
        self.assertEqual(tt.last_rate_limit, {"limit": 120.0, "remaining": 7.0, "reset_at": 1792427500.0})
        tt.find_creators_to_recruit("UK")
        self.assertEqual(tt.last_rate_limit["limit"], 60000)

    def test_reads_key_from_env(self) -> None:
        os.environ["TIKTOOLS_API_KEY"] = "tk_env"
        try:
            self.assertEqual(AgencyClient().api_key, "tk_env")
        finally:
            del os.environ["TIKTOOLS_API_KEY"]


class MethodTests(unittest.TestCase):
    def test_get_account_unwraps_and_caches(self) -> None:
        tt, op = make_client([{"body": {"status_code": 0, "data": {"tier": "pro", "bulk_check_limit": 50}}}])
        self.assertEqual(tt.get_account()["tier"], "pro")
        self.assertEqual(tt.get_account()["tier"], "pro")
        self.assertEqual(len(op.calls), 1)

    def test_who_is_live_chunks_and_never_counts_failed_as_offline(self) -> None:
        def rows(call):
            ids = call.body["unique_ids"]
            return {"body": {"status_code": 0, "data": [
                {"unique_id": h, "is_live": [True, False, None][i % 3], "check_failed": i % 3 == 2} for i, h in enumerate(ids)]}}
        tt, op = make_client([{"body": {"status_code": 0, "data": {"tier": "basic", "bulk_check_limit": 3}}}, rows, rows])
        res = tt.who_is_live(["@A", "b", "c", "d", "a"])
        self.assertEqual(op.calls[1].body, {"unique_ids": ["a", "b", "c"]})
        self.assertEqual(op.calls[2].body, {"unique_ids": ["d"]})
        self.assertEqual([r["unique_id"] for r in res["live"]], ["a", "d"])
        self.assertEqual([r["unique_id"] for r in res["offline"]], ["b"])
        self.assertEqual([r["unique_id"] for r in res["unknown"]], ["c"])

    def test_who_is_live_on_sandbox_points_to_is_live(self) -> None:
        tt, _ = make_client([{"status": 403, "body": {"error": "Basic-tier endpoint", "required_tier": "basic"}}])
        with self.assertRaises(PlanRequiredError) as ctx:
            tt.who_is_live(["a"], chunk_size=10)
        self.assertIn("is_live", str(ctx.exception))

    def test_is_live_returns_none_when_unknown(self) -> None:
        tt, op = make_client([
            {"body": {"status_code": 0, "data": {"unique_id": "a", "is_live": True, "live_status": "live"}}},
            {"body": {"status_code": 0, "data": {"unique_id": "a", "is_live": None, "live_status": "unknown"}}},
            {"body": {"status_code": 0, "data": {"unique_id": "a", "is_live": False}}},
        ])
        self.assertIs(tt.is_live("@A"), True)
        self.assertIsNone(tt.is_live("a"))
        self.assertIs(tt.is_live("a"), False)
        self.assertEqual(op.calls[0].query["unique_id"], "a")

    def test_add_to_roster_chunks_by_200(self) -> None:
        reply = lambda c: {"body": {"ok": True, "id": "L1", "action": "add", "changed": len(c.body["uniqueIds"]), "memberCount": 1}}
        tt, op = make_client([reply, reply, reply])
        res = tt.add_to_roster("L1", [f"c{i}" for i in range(450)])
        self.assertEqual(len(op.calls), 3)
        self.assertEqual(res["changed"], 450)

    def test_create_webhook_maps_fields(self) -> None:
        tt, op = make_client([{"body": {"ok": True, "webhook": {"id": "w"}, "secret": "whsec_x"}}])
        tt.create_webhook(url="https://x.dev/h", events=["live.start"], rosters=["L1"], creators=["@Bob"], bearer_token="t")
        self.assertEqual(op.calls[0].body, {"target_url": "https://x.dev/h", "event_types": ["live.start"], "creators": ["bob"], "lists": ["L1"], "bearer_token": "t"})
        with self.assertRaises(ValidationError):
            tt.create_webhook(url="https://x.dev", events=[])

    def test_update_webhook_sends_active(self) -> None:
        tt, op = make_client([{"body": {"ok": True, "webhook": {}}}])
        tt.update_webhook("abc", active=False)
        self.assertEqual((op.calls[0].method, op.calls[0].path, op.calls[0].body), ("PATCH", "/webcast/webhooks/abc", {"active": False}))

    def test_board_history_league_needs_class_type(self) -> None:
        tt, _ = make_client([])
        with self.assertRaises(ValueError):
            tt.board_history("UK+", board="league")

    def test_stats_validate_window(self) -> None:
        tt, _ = make_client([])
        with self.assertRaises(ValidationError):
            tt.get_daily_diamonds("x", start="2026-10-01T00:00:00Z")

    def test_new_creators_retries_plus_cluster(self) -> None:
        tt, op = make_client([
            {"status": 404, "body": {"status_code": 1, "error": "No recruit pool", "available_regions": ["UK+"]}},
            {"body": {"status_code": 0, "region": "UK+", "results": [], "total_pages": 0}},
        ])
        self.assertEqual(tt.new_creators("UK")["region"], "UK+")
        self.assertEqual(op.calls[1].query["region"], "UK+")

    def test_iter_creators_walks_pages(self) -> None:
        page = lambda n: {"body": {"status_code": 0, "total_pages": 2, "results": [{"username": f"p{n}", "backstage": "eligible" if n == 1 else "unchecked"}]}}
        tt, op = make_client([page(1), page(2)])
        rows = list(tt.iter_creators_to_recruit("UK"))
        self.assertEqual([r["username"] for r in rows], ["p1", "p2"])
        self.assertEqual(op.calls[0].query["limit"], "100")
        self.assertTrue(is_free_to_join(rows[0]))
        self.assertFalse(is_free_to_join(rows[1]))

    def test_roster_diamonds_totals_and_records_failures(self) -> None:
        def daily(c):
            if c.query["creator"] == "bad":
                return {"status": 500, "body": {"error": "boom"}}
            return {"body": {"type": "daily", "totalDiamonds": 10 if c.query["creator"] == "a" else 30, "completeDays": 7, "incompleteDays": 0, "buckets": []}}
        tt, _ = make_client([daily, daily, daily], max_retries=0)
        res = tt.roster_diamonds(["a", "b", "bad"], days=7, concurrency=1)
        self.assertEqual(res["totalDiamonds"], 40)
        self.assertEqual([c["uniqueId"] for c in res["creators"]], ["b", "a"])
        self.assertEqual(res["failed"][0]["uniqueId"], "bad")

    def test_resolve_user_ids_returns_map(self) -> None:
        tt, op = make_client([{"body": {"status_code": 0, "data": {"1": {"userId": "1", "username": "x"}}}}])
        self.assertEqual(tt.resolve_user_ids([1])["1"]["username"], "x")
        self.assertEqual(op.calls[0].body, {"user_ids": ["1"]})


if __name__ == "__main__":
    unittest.main()

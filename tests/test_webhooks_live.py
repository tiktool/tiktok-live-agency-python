import asyncio
import hashlib
import hmac
import json
import unittest

from tiktok_live_agency import (
    AsyncAgencyClient, LiveTracker, WebhookSignatureError, classify_rows, sign_webhook_payload, to_discord_message,
    verify_webhook,
)
from tiktok_live_agency.firehose import _handshake_error, firehose_url
from tiktok_live_agency.errors import PlanRequiredError, PaymentRequiredError, AuthError

from fakes import FakeOpener, make_client

SECRET = "whsec_0123456789abcdef0123456789abcdef"
NOW = 1792400000
PAYLOAD = json.dumps({
    "event_id": "evt_1", "event": "live.start", "occurred_at": "2026-10-19T10:00:00Z",
    "creator": {"unique_id": "some_creator"},
    "live": {"room_id": "7", "started_at": "2026-10-19T10:00:00Z", "live_url": "https://www.tiktok.com/@some_creator/live"},
})


def server_sign(body: str, ts: int, key: str = SECRET) -> str:
    # Same algorithm as the server implementation.
    return f"t={ts},v1={hmac.new(key.encode(), f'{ts}.{body}'.encode(), hashlib.sha256).hexdigest()}"


class WebhookTests(unittest.TestCase):
    def test_accepts_server_signature_bytes_str_and_headers(self) -> None:
        sig = server_sign(PAYLOAD, NOW)
        ev = verify_webhook(PAYLOAD.encode(), sig, SECRET, now=NOW)
        self.assertEqual((ev.event, ev.event_id, ev.creator["unique_id"]), ("live.start", "evt_1", "some_creator"))
        self.assertEqual(ev["live"]["room_id"], "7")
        verify_webhook(PAYLOAD, {"X-Tiktool-Signature": sig}, SECRET, now=NOW)
        verify_webhook(PAYLOAD, {"x-tiktool-signature": [sig]}, SECRET, now=NOW)

    def test_any_order_spaces_and_multiple_v1(self) -> None:
        good = server_sign(PAYLOAD, NOW).split(",")[1]
        verify_webhook(PAYLOAD, f" v1={'0' * 64} , {good} , t={NOW} ", SECRET, now=NOW)

    def test_adds_whsec_prefix_like_the_server(self) -> None:
        supplied = "my_own_secret_value_1234"
        verify_webhook(PAYLOAD, server_sign(PAYLOAD, NOW, "whsec_" + supplied), supplied, now=NOW)

    def test_rejections(self) -> None:
        sig = server_sign(PAYLOAD, NOW)
        for args in [
            (PAYLOAD.replace("some_creator", "x"), sig, SECRET, NOW),
            (PAYLOAD, sig, "whsec_wrong_secret_value", NOW),
            (PAYLOAD, sig, SECRET, NOW + 301),
            (PAYLOAD, "v1=abc", SECRET, NOW),
            (PAYLOAD, {}, SECRET, NOW),
            (json.loads(PAYLOAD), sig, SECRET, NOW),
        ]:
            with self.assertRaises(WebhookSignatureError):
                verify_webhook(args[0], args[1], args[2], now=args[3])

    def test_sign_matches_server(self) -> None:
        self.assertEqual(sign_webhook_payload(PAYLOAD, SECRET, NOW), server_sign(PAYLOAD, NOW))

    def test_discord_formatting(self) -> None:
        ev = json.loads(PAYLOAD)
        self.assertEqual(to_discord_message(ev)["content"], "@some\\_creator is LIVE: https://www.tiktok.com/@some_creator/live")
        self.assertIn("after 1h 23m", to_discord_message({**ev, "event": "live.end", "live": {"duration_seconds": 4980}})["content"])
        gift = {**ev, "event": "live.gift.high_value", "gift": {"name": "Lion", "diamonds": 29999, "gifter": {"unique_id": "whale"}}}
        self.assertIn("Lion (29,999 diamonds) from @whale", to_discord_message(gift)["content"])
        self.assertTrue(to_discord_message(verify_webhook(PAYLOAD, server_sign(PAYLOAD, NOW), SECRET, now=NOW))["content"].startswith("@some"))


def poll(live=(), offline=(), unknown=()):
    return classify_rows([{"unique_id": h, "is_live": True, "room_id": f"r_{h}"} for h in live]
                         + [{"unique_id": h, "is_live": False} for h in offline]
                         + [{"unique_id": h, "is_live": None, "check_failed": True} for h in unknown])


class LiveTrackerTests(unittest.TestCase):
    def test_seed_live_unknown_and_end(self) -> None:
        t = LiveTracker()
        self.assertEqual(t.update(poll(offline=["a"])), [])
        ev = t.update(poll(live=["a"]))
        self.assertEqual((ev[0].type, ev[0].creator, ev[0].room_id), ("live", "a", "r_a"))
        self.assertEqual(t.update(poll(unknown=["a"])), [])
        self.assertTrue(t.is_live("a"))
        self.assertEqual(t.update(poll(offline=["a"])), [])
        self.assertEqual(t.update(poll(offline=["a"]))[0].type, "end")

    def test_first_poll_live_does_not_fire(self) -> None:
        self.assertEqual(LiveTracker().update(poll(live=["a"])), [])

    def test_watch_live_falls_back_on_sandbox(self) -> None:
        tt, op = make_client([
            {"body": {"status_code": 0, "data": {"tier": "sandbox", "bulk_check_limit": 0}}},
            {"status": 403, "body": {"error": "Basic-tier endpoint", "required_tier": "basic"}},
            {"body": {"status_code": 0, "data": {"unique_id": "a", "is_live": False, "live_status": "offline"}}},
            {"body": {"status_code": 0, "data": {"unique_id": "a", "is_live": True, "live_status": "live", "room_id": "9"}}},
        ])
        events = list(tt.watch_live(["a"], interval=30, max_polls=2))
        self.assertEqual([(e.type, e.creator, e.room_id) for e in events], [("live", "a", "9")])
        self.assertEqual(op.calls[3].path, "/webcast/live_status")


class AsyncTests(unittest.TestCase):
    def test_async_client_mirrors_sync(self) -> None:
        tt = AsyncAgencyClient(api_key="tk")
        op = FakeOpener([{"body": {"status_code": 0, "global_live": 3, "regions": {}}},
                         {"body": {"status_code": 0, "total_pages": 1, "results": [{"username": "x"}]}}])
        tt.sync._urlopen = op  # type: ignore[assignment]

        async def go():
            counts = await tt.live_counts()
            rows = [r async for r in tt.iter_creators_to_recruit("UK")]
            return counts, rows
        counts, rows = asyncio.run(go())
        self.assertEqual(counts["global_live"], 3)
        self.assertEqual(rows, [{"username": "x"}])


class FirehoseTests(unittest.TestCase):
    def test_url_and_handshake_errors(self) -> None:
        u = firehose_url("https://api.tik.tools", "tk", mode="region", region="UK+", min_diamonds=5000)
        self.assertTrue(u.startswith("wss://api.tik.tools/firehose/gifters?"))
        self.assertIn("region=UK%2B", u)
        league = firehose_url("https://api.tik.tools", "tk", mode="league", league="A1")
        self.assertIn("mode=league", league)
        self.assertIn("league=A1", league)
        self.assertNotIn("region=", league)
        self.assertIsInstance(_handshake_error(403), PlanRequiredError)
        self.assertIsInstance(_handshake_error(402), PaymentRequiredError)
        self.assertIsInstance(_handshake_error(401), AuthError)

    def test_streams_from_a_local_server(self) -> None:
        try:
            import websockets
            from websockets.asyncio.server import serve  # type: ignore[import-not-found]
        except ImportError:
            self.skipTest("websockets >= 13 not installed")
        from tiktok_live_agency.firehose import stream_big_gifts

        async def handler(ws):
            await ws.send(json.dumps({"type": "ready", "filter": {}}))
            await ws.send(json.dumps({"type": "gifter_alert", "gift": {"name": "Lion", "totalDiamonds": 29999}}))
            await ws.wait_closed()

        async def go():
            async with serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                async for alert in stream_big_gifts(api_key="tk", base_url=f"http://127.0.0.1:{port}"):
                    return alert
        alert = asyncio.run(asyncio.wait_for(go(), 10))
        self.assertEqual(alert["gift"]["totalDiamonds"], 29999)


if __name__ == "__main__":
    unittest.main()

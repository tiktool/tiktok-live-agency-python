import io
import json
import urllib.error
from email.message import Message
from typing import Any, Callable, List, Union
from urllib.parse import parse_qs, urlparse

from tiktok_live_agency import AgencyClient


def _headers(d: dict) -> Message:
    m = Message()
    for k, v in d.items():
        m[k] = v
    return m


class FakeResponse:
    def __init__(self, status: int, body: bytes, headers: dict) -> None:
        self.status = status
        self._body = body
        self.headers = _headers(headers)

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *a: Any) -> None:
        return None


class Call:
    def __init__(self, req: Any) -> None:
        u = urlparse(req.full_url)
        self.method = req.get_method()
        self.path = u.path
        self.query = {k: v[0] for k, v in parse_qs(u.query).items()}
        self.headers = {k.lower(): v for k, v in req.header_items()}
        self.body = json.loads(req.data) if req.data else None


Reply = Union[dict, Exception, Callable[[Call], dict]]


class FakeOpener:
    """Scripted urlopen: each call takes the next reply ({status, body, headers}) and records the request."""

    def __init__(self, replies: List[Reply]) -> None:
        self.replies = list(replies)
        self.calls: List[Call] = []

    def __call__(self, req: Any, timeout: float = 0) -> FakeResponse:
        call = Call(req)
        self.calls.append(call)
        if not self.replies:
            raise AssertionError(f"unexpected request {call.method} {call.path}")
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        if callable(r):
            r = r(call)
        status = r.get("status", 200)
        body = r.get("body", {})
        raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        headers = {"content-type": "application/json", **r.get("headers", {})}
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "error", _headers(headers), io.BytesIO(raw))
        return FakeResponse(status, raw, headers)


def make_client(replies: List[Reply], **kwargs: Any) -> "tuple[AgencyClient, FakeOpener]":
    tt = AgencyClient(api_key="tk_test", logger=lambda m: None, **kwargs)
    opener = FakeOpener(replies)
    tt._urlopen = opener  # type: ignore[assignment]
    tt._sleep = lambda s: None  # type: ignore[assignment]
    return tt, opener

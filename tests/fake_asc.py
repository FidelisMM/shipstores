"""A fake App Store Connect at the httpx level, shared by the API tests.

Tests go through the real apple.request (URL building, retry, error format);
only the network and the key are swapped. No credential is read.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

import httpx

from shipstores import apple, server
from shipstores.config import AppleConfig

Handler = Callable[[str, str, dict[str, Any], dict[str, Any] | None], tuple[int, dict[str, Any]]]


class FakeASC:
    """Routes (method, path) to handlers and records every call."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Handler] = {}
        self.calls: list[tuple[str, str, dict[str, Any], dict[str, Any] | None]] = []

    def on(self, method: str, path: str, handler: Handler) -> None:
        self.routes[(method, path)] = handler

    def __call__(self, method: str, url: str, *, params=None, json=None, **_: Any) -> httpx.Response:
        path = url.removeprefix(apple.BASE_URL)
        self.calls.append((method, path, params or {}, json))
        handler = self.routes.get((method, path))
        if handler is None:
            status, body = 404, {"errors": [{"status": "404", "detail": f"no route {method} {path}"}]}
        else:
            status, body = handler(method, path, params or {}, json)
        return httpx.Response(status, json=body, request=httpx.Request(method, url))

    def bodies(self, method: str, path: str) -> list[dict[str, Any] | None]:
        return [body for m, p, _, body in self.calls if m == method and p == path]


class ASCTestCase(unittest.TestCase):
    """Gives each test a fresh FakeASC in `self.asc`, wired into apple.request."""

    def setUp(self) -> None:
        super().setUp()
        self.asc = FakeASC()
        cfg = AppleConfig(key_id="TESTKEY", issuer_id="issuer", private_key_path=Path("/nonexistent.p8"), team_id="TEAM")
        for p in (
            patch.object(apple, "load_apple", return_value=cfg),
            patch.object(server, "load_apple", return_value=cfg),
            patch.object(apple, "_token", return_value="fake-token"),
            patch.object(apple.httpx, "request", self.asc),
        ):
            p.start()
            self.addCleanup(p.stop)

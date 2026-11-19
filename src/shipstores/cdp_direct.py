"""Run console scripts directly on the dedicated Chrome over CDP, without browser-harness.

The dedicated Chrome belongs to this server, not to the user, so when it is up the
console scripts run here: the same helpers browser-harness offers (new_tab,
wait_for_load, js, page_info, cdp, wait, ...), with the same semantics, over a
WebSocket straight to the profile's debugging port. This removes browser-harness
from the main path and keeps the page a script left open available to the next
call (play_content_open, then _answer, then _save).
"""

from __future__ import annotations

import contextlib
import io
import itertools
import json
import sys
import time
import traceback
import types

import httpx
from websockets.sync.client import connect

# How long to wait for Page.navigate to confirm before switching to a fresh tab
# (Apple's console usually confirms within a few seconds).
NAVIGATE_TIMEOUT = 45


class CdpError(RuntimeError):
    pass


class _Session:
    def __init__(self, cdp_url: str):
        ws_url = httpx.get(f"{cdp_url}/json/version", timeout=5).json()["webSocketDebuggerUrl"]
        self.ws = connect(ws_url, max_size=None, open_timeout=10)
        self.ids = itertools.count(1)
        self.session_id: str | None = None
        self.tabs: list[str] = []

    def send(self, method: str, session_id: str | None = None, timeout: float = 120, **params):
        msg_id = next(self.ids)
        msg = {"id": msg_id, "method": method, "params": params}
        sid = session_id if session_id is not None else (
            None if method.startswith(("Target.", "Browser.")) else self.session_id
        )
        if sid:
            msg["sessionId"] = sid
        self.ws.send(json.dumps(msg))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                resp = json.loads(self.ws.recv(timeout=max(0.1, deadline - time.time())))
            except TimeoutError:
                break
            if resp.get("id") == msg_id:
                if "error" in resp:
                    raise CdpError(f"{method}: {resp['error'].get('message')}")
                return resp.get("result", {})
        raise CdpError(f"{method}: no response within {timeout}s")

    def evaluate(self, expression: str):
        r = self.send(
            "Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True
        )
        if "exceptionDetails" in r:
            det = r["exceptionDetails"]
            text = det.get("exception", {}).get("description") or det.get("text")
            if "Illegal return" in str(text):
                return self.evaluate(f"(async()=>{{{expression}}})()")
            raise CdpError(f"js: {text}")
        return r.get("result", {}).get("value")

    def pages(self) -> list[str]:
        """Page targets, real pages last (the blank tab Chrome opens on startup must not
        win over the page where the previous call stopped)."""
        targets = [t for t in self.send("Target.getTargets")["targetInfos"] if t["type"] == "page"]
        blank = lambda t: t["url"].startswith(("chrome://", "about:"))  # noqa: E731
        return [t["targetId"] for t in sorted(targets, key=lambda t: not blank(t))]

    def attach(self, target_id: str) -> None:
        self.session_id = self.send("Target.attachToTarget", targetId=target_id, flatten=True)["sessionId"]
        self.send("Page.enable")

    def close(self):
        # The last tab stays open: multi-call flows (play_content_open, then _answer,
        # then _save) depend on finding the page where they stopped. Other tabs this
        # call opened are closed.
        for tid in self.tabs[:-1]:
            with contextlib.suppress(Exception):
                self.send("Target.closeTarget", targetId=tid, timeout=5)
        with contextlib.suppress(Exception):
            self.ws.close()


def run(script: str, cdp_url: str) -> str:
    """Run `script` (the same text that would go to browser-harness) and return its stdout."""
    try:
        s = _Session(cdp_url)
    except Exception as exc:  # noqa: BLE001 — nothing ran yet; report it as a CDP failure
        raise CdpError(f"could not connect to the dedicated Chrome at {cdp_url}: {exc}") from exc

    def cdp(method, session_id=None, **params):
        return s.send(method, session_id, **params)

    def new_tab(url="about:blank"):
        leftovers = s.pages()
        tid = s.send("Target.createTarget", url="about:blank", background=True)["targetId"]
        s.tabs.append(tid)
        s.attach(tid)
        # Close what earlier calls left behind, after opening the new one (never zero tabs).
        for old in leftovers:
            with contextlib.suppress(Exception):
                s.send("Target.closeTarget", targetId=old, timeout=5)
        if url != "about:blank":
            navigate(url)
        return tid

    def navigate(url):
        # Page.navigate only answers once navigation commits. A tab can hang with no
        # answer for minutes while the same call works on a fresh tab, and a hung tab
        # does not recover: open another one and try once more.
        for attempt in (1, 2):
            try:
                r = s.send("Page.navigate", url=url, timeout=NAVIGATE_TIMEOUT)
            except CdpError:
                if attempt == 2:
                    raise CdpError(
                        f"Page.navigate to {url} got no response twice. The dedicated Chrome "
                        "may be stuck: run store_browser_session and try again."
                    ) from None
                stuck = s.tabs.pop()
                tid = s.send("Target.createTarget", url="about:blank", background=True)["targetId"]
                s.tabs.append(tid)
                s.attach(tid)
                with contextlib.suppress(Exception):
                    s.send("Target.closeTarget", targetId=stuck, timeout=5)
                continue
            if r.get("errorText"):
                raise CdpError(f"Page.navigate to {url}: {r['errorText']}")
            return

    def js(expression, target_id=None):
        return s.evaluate(expression)

    def wait_for_load(timeout=15.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with contextlib.suppress(CdpError):
                if s.evaluate("document.readyState") == "complete":
                    return True
            time.sleep(0.3)
        return False

    def page_info():
        return json.loads(s.evaluate(
            "JSON.stringify({url:location.href,title:document.title,w:innerWidth,h:innerHeight,"
            "sx:scrollX,sy:scrollY,pw:document.documentElement.scrollWidth,"
            "ph:document.documentElement.scrollHeight})"
        ))

    def current_tab():
        if not s.session_id:
            existing = s.pages()
            if existing:
                # Continue on the tab the previous call left open.
                s.tabs.append(existing[-1])
                s.attach(existing[-1])
            else:
                new_tab()
        return {"targetId": s.tabs[-1], "url": s.evaluate("location.href")}

    def goto_url(url):
        current_tab()
        navigate(url)

    def activate_tab(tab=None):
        tid = tab.get("targetId") if isinstance(tab, dict) else (tab or current_tab()["targetId"])
        s.send("Target.activateTarget", targetId=tid)

    def click_at_xy(x, y, button="left", clicks=1):
        for kind in ("mousePressed", "mouseReleased"):
            s.send("Input.dispatchMouseEvent", type=kind, x=x, y=y, button=button, clickCount=clicks)

    def wait(seconds=1.0):
        time.sleep(seconds)

    helpers_fns = {
        "cdp": cdp, "new_tab": new_tab, "js": js, "wait_for_load": wait_for_load,
        "page_info": page_info, "wait": wait, "current_tab": current_tab,
        "goto_url": goto_url, "activate_tab": activate_tab, "click_at_xy": click_at_xy,
    }
    # Preludes do `from browser_harness.helpers import ...`: serve the same functions.
    helpers = types.ModuleType("browser_harness.helpers")
    helpers.__dict__.update(helpers_fns)
    package = types.ModuleType("browser_harness")
    package.helpers = helpers
    before = {k: sys.modules.get(k) for k in ("browser_harness", "browser_harness.helpers")}
    sys.modules.update({"browser_harness": package, "browser_harness.helpers": helpers})

    output = io.StringIO()
    env = {**helpers_fns, "__name__": "__cdp_direct__"}
    try:
        with contextlib.redirect_stdout(output):
            exec(compile(script, "<console>", "exec"), env)
    except SystemExit as stop:
        # Scripts end with `raise SystemExit('ERROR: ...')`, a habit from running as a
        # browser-harness subprocess. Here they run inside the server: without this, the
        # SystemExit would take the whole MCP server down and the message would be lost.
        # Treat it like exiting with a non-zero code.
        if stop.code not in (None, 0):
            output.write(f"\n{stop.code}\n")
            raise CdpError(output.getvalue()[-3000:]) from None
    except Exception:
        output.write("\n" + traceback.format_exc())
        raise CdpError(output.getvalue()[-3000:])
    finally:
        for k, v in before.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        s.close()
    return output.getvalue()

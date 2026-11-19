"""Browser automation for the store consoles.

Neither store exposes app creation or the "App content" declarations through its
API. This module drives Chrome (via browser-harness) to fill in those forms.

## Why we run our own Chrome

The user's Chrome works, but at two costs: it takes over the screen (Play Console
only processes clicks when the tab is in the foreground — in the background
dispatchMouseEvent raises TimeoutError and the event never reaches the page) and
it fights over tabs with whoever is working in it.

In headless mode there is no "background tab": the renderer always considers the
page visible, so clicks go through without stealing focus. That is why, when the
dedicated profile exists, this module starts its own headless Chrome and runs the
console scripts on it directly over CDP (cdp_direct.py, the same helpers
browser-harness offers). browser-harness is only needed without the profile, or as
the fallback when the profile lost its session.

The profile must be logged in to both consoles. To create or renew the login:

    python -m shipstores.browser login

This opens the profile's Chrome WITH a window so a person can authenticate
(including 2FA). Close the window when done; the sessions are stored in the profile.

Without the profile, everything keeps working as before, in the user's Chrome.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import httpx

ASC_NEW_APP_URL = "https://appstoreconnect.apple.com/apps"
PLAY_NEW_APP_URL = "https://play.google.com/console/developers"

# Apple's console is an SPA that renders nothing in headless mode: Chrome loads
# the header and stops, and what we read is an empty page. This cost four
# investigations that assumed an expired session or a slow page. For these hosts
# Chrome starts WITH a window.
HOSTS_QUE_EXIGEM_JANELA = ("appstoreconnect.apple.com",)

PROFILE_DIR = Path(
    os.environ.get("SHIPSTORES_CHROME_PROFILE", Path.home() / ".config" / "shipstores" / "chrome-profile")
).expanduser()
CDP_PORT = int(os.environ.get("SHIPSTORES_CDP_PORT", "9333"))
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"

CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome",
    "chromium",
)


class BrowserError(RuntimeError):
    pass


class ScriptFailed(BrowserError):
    """The console script itself ran and failed; the message carries its output."""


def _chrome_binary() -> str | None:
    for c in CHROME_CANDIDATES:
        if os.path.isabs(c):
            if os.path.exists(c):
                return c
        elif shutil.which(c):
            return shutil.which(c)
    return None


def _cdp_alive() -> bool:
    try:
        httpx.get(f"{CDP_URL}/json/version", timeout=2).raise_for_status()
        return True
    except Exception:  # noqa: BLE001 — any failure means "not up"
        return False


def _running_headless() -> bool:
    ps = subprocess.run(["ps", "-axo", "command="], capture_output=True, text=True).stdout
    return any(
        f"--user-data-dir={PROFILE_DIR}" in line and "--headless" in line and "--type=" not in line
        for line in ps.splitlines()
    )


def _login_in_progress() -> bool:
    """Is any tab of the dedicated Chrome on a login screen (someone authenticating)?"""
    try:
        tabs = httpx.get(f"{CDP_URL}/json/list", timeout=3).json()
    except Exception:  # noqa: BLE001
        return False
    return any(
        t.get("type") == "page" and any(f"://{h}/" in t.get("url", "") for h in _LOGIN_HOSTS)
        for t in tabs
    )


def return_to_headless() -> bool:
    """Close the login window cleanly; the next call starts Chrome headless again.

    Uses Browser.close over CDP, not pkill, so Chrome flushes the profile before
    exiting. Returns True if there was a window and it was closed.
    """
    if not _cdp_alive() or _running_headless():
        return False
    # Apple's console login is a session cookie: without saving it first, Browser.close
    # drops it and the next call asks for a login again.
    save_cookies()
    _close_chrome()
    for _ in range(20):
        time.sleep(0.5)
        if not _cdp_alive():
            return True
    raise BrowserError("The dedicated Chrome did not close within 10s of Browser.close.")


def _close_chrome() -> None:
    _browser_cdp("Browser.close", wait_for_reply=False)


# ------------------------------------------------------------------ saved cookies
#
# Chrome drops session cookies when it closes, and that is where Apple keeps the
# console login. Since this module closes and reopens Chrome (a window to log in,
# headless for everything else), the login used to last only until the next call.
# The consoles' cookies are now kept in a file inside the profile (readable only by
# the owner) and handed back to Chrome every time it starts.

COOKIES_FILE = PROFILE_DIR / "console-session.json"
_CONSOLE_DOMAINS = ("apple.com", "google.com")


def _browser_cdp(method: str, params: dict | None = None, wait_for_reply: bool = True) -> dict:
    """CDP call on the browser target (Storage.*, Browser.close)."""
    from websockets.sync.client import connect

    ws_url = httpx.get(f"{CDP_URL}/json/version", timeout=5).json()["webSocketDebuggerUrl"]
    with connect(ws_url, open_timeout=10, max_size=None) as ws:
        ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        if not wait_for_reply:
            return {}
        while True:
            msg = json.loads(ws.recv(timeout=15))
            if msg.get("id") == 1:
                if "error" in msg:
                    raise BrowserError(f"CDP {method}: {msg['error']}")
                return msg.get("result", {})


def _console_cookies(cookies: list[dict]) -> list[dict]:
    return [c for c in cookies if any(c.get("domain", "").endswith(d) for d in _CONSOLE_DOMAINS)]


def _cookie_param(c: dict) -> dict:
    """A cookie read with Storage.getCookies, in Storage.setCookies form; session cookies stay session cookies."""
    p = {k: c[k] for k in ("name", "value", "domain", "path", "httpOnly", "secure", "sameSite") if k in c}
    if not c.get("session") and c.get("expires", -1) > 0:
        p["expires"] = c["expires"]
    return p


def save_cookies() -> None:
    """Copy the consoles' cookies from the dedicated Chrome to the profile. Failures are ignored."""
    try:
        cookies = _console_cookies(_browser_cdp("Storage.getCookies").get("cookies", []))
        if not cookies:
            return
        tmp = COOKIES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cookies))
        tmp.chmod(0o600)
        tmp.replace(COOKIES_FILE)
    except Exception:  # noqa: BLE001 — without a copy we are back to the old behavior
        pass


def restore_cookies() -> None:
    """Hand the saved cookies back to a freshly started Chrome (it discards expired ones)."""
    try:
        if not COOKIES_FILE.is_file():
            return
        cookies = [_cookie_param(c) for c in json.loads(COOKIES_FILE.read_text())]
        if cookies:
            _browser_cdp("Storage.setCookies", {"cookies": cookies})
    except Exception:  # noqa: BLE001
        pass


def _spawn(headless: bool, urls: tuple[str, ...] = ()) -> None:
    binary = _chrome_binary()
    if not binary:
        raise BrowserError("Chrome not found to launch the dedicated profile.")
    args = [
        binary,
        f"--user-data-dir={PROFILE_DIR}",
        f"--remote-debugging-port={CDP_PORT}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if headless:
        # headless=new keeps real Chrome behavior (including saved logins)
        args += ["--headless=new", "--window-size=1440,2000"]
    args += list(urls)
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def exige_janela(url: str) -> bool:
    """Is the URL on a host that only works with a visible window?"""
    return any(host in url for host in HOSTS_QUE_EXIGEM_JANELA)


def ensure_headless(visivel: bool = False) -> str | None:
    """Ensure a headless Chrome with the dedicated profile is running. Returns the CDP URL.

    Returns None when the profile does not exist — the caller then falls back to
    the user's Chrome, which is the legacy behavior.
    """
    if os.environ.get("SHIPSTORES_HEADLESS") == "0":
        return None
    if not PROFILE_DIR.exists():
        return None
    if _cdp_alive():
        if visivel or _running_headless() or _login_in_progress():
            return CDP_URL
        # Still has the window of a login that already finished: that window steals
        # focus from whoever is working on the machine. Close it cleanly (cookies saved)
        # and start headless.
        return_to_headless()
    _spawn(headless=True)
    for _ in range(20):
        time.sleep(1)
        if _cdp_alive():
            restore_cookies()
            return CDP_URL
    raise BrowserError(
        f"The Chrome for profile {PROFILE_DIR} did not answer on CDP within 20s. "
        "Run `python -m shipstores.browser login` to check the profile."
    )


def login() -> None:
    """Open the dedicated profile WITH a window to log in to both consoles."""
    if _cdp_alive():
        raise BrowserError(
            f"A Chrome is already using port {CDP_PORT}. Close it before logging in "
            "(the same profile cannot be opened twice)."
        )
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    _spawn(headless=False, urls=(PLAY_NEW_APP_URL, ASC_NEW_APP_URL))
    print(f"Chrome opened with profile {PROFILE_DIR}.")
    print("Log in to Play Console and App Store Connect, then close the window.")


# ------------------------------------------------------------------ sessions
#
# Apple's session expires far more often than Google's. Without a check, the
# symptom is confusing: the automation "works" but reads the login screen, and
# the result comes back empty or meaningless. So we check before operating and,
# when the session has expired, swap headless for a real window — there is no
# way to complete 2FA without a person.

CONSOLES = {
    "play": {
        "url": "https://play.google.com/console/developers",
        # when logged out, Google redirects to accounts.google.com
        "login_hosts": ("accounts.google.com",),
    },
    "apple": {
        "url": "https://appstoreconnect.apple.com/apps",
        # when logged out, Apple redirects to idmsa or, when the session expired, to
        # the console's own /login with authResult=FAILED
        "login_hosts": ("idmsa.apple.com", "signin.apple.com", "appstoreconnect.apple.com/login"),
    },
}


def _session_script(console: str) -> str:
    """Script that opens the console and prints the final URL and, on Apple, whether the
    internal API accepts the session.

    The URL alone is not enough on Apple: the SPA takes a while to redirect to /login,
    and 4 s after opening the page it still looks logged in. /iris answers 401 right away.
    """
    cfg = CONSOLES[console]
    script = (
        "import time\n"
        f"new_tab({cfg['url']!r})\n"
        "wait_for_load()\n"
        "time.sleep(4)\n"
    )
    if console == "apple":
        script += (
            "js(\"window.__ss='running';fetch('/iris/v1/userPreferences',{credentials:'include'})"
            ".then(r=>window.__ss=String(r.status)).catch(e=>window.__ss='0');'ok'\")\n"
            "for _ in range(20):\n"
            "    s = js('String(window.__ss)')\n"
            "    if s != 'running': break\n"
            "    time.sleep(0.5)\n"
            f"if s != '200': print({LOGGED_OUT_MARK!r} + ' apple')\n"
        )
    return script + "print('URL=' + str(page_info().get('url','')))\n"


def _logged_in(console: str, output: str) -> bool:
    final = next((ln[4:].strip() for ln in output.splitlines() if ln.startswith("URL=")), "")
    return bool(final) and _logged_out_console(output) is None and not any(
        h in final for h in CONSOLES[console]["login_hosts"]
    )


def session_status(console: str | None = None) -> dict[str, bool]:
    """Report, for each console, whether the dedicated profile is still logged in."""
    alvos = [console] if console else list(CONSOLES)
    status = {nome: _logged_in(nome, run_harness(_session_script(nome), auto_login=False)) for nome in alvos}
    if any(status.values()):
        save_cookies()
    return status


def session_status_user_browser(console: str) -> bool:
    """Does the console open in the browser browser-harness drives by default (the user's)?"""
    output = _via_user_browser(_session_script(console), 180)
    return output is not None and _logged_in(console, output)


def relogin(consoles: list[str] | None = None) -> str:
    """Close the headless Chrome and reopen it WITH a window on the consoles that need a login."""
    alvos = consoles or list(CONSOLES)
    urls = tuple(CONSOLES[c]["url"] for c in alvos if c in CONSOLES)
    if _cdp_alive() and not _running_headless():
        # There is a window already: just open the login tabs in it. Killing and
        # reopening drops the session cookies of whoever just logged in.
        for url in urls:
            httpx.put(f"{CDP_URL}/json/new?{url}", timeout=5)
        return (
            f"Login tabs opened in the dedicated Chrome on: {', '.join(alvos)}. "
            "Log in (including 2FA; on Apple, choose to trust this browser) and report back. "
            "Then store_browser_session() checks the sessions and closes the window (back to headless)."
        )
    if _cdp_alive():
        save_cookies()
    subprocess.run(
        ["pkill", "-f", str(PROFILE_DIR)], capture_output=True, check=False
    )
    time.sleep(3)
    _spawn(headless=False, urls=urls)
    return (
        f"Chrome opened with a window on: {', '.join(alvos)}. "
        "Log in (including 2FA) and report back — the session is stored in the profile "
        "and subsequent operations go back to running headless."
    )


def ensure_session(console: str, auto: bool = True) -> bool:
    """Ensure a live session on a console. With auto, opens the login window if it expired."""
    if not PROFILE_DIR.exists():
        return True  # without a dedicated profile, the user's Chrome is in charge
    if session_status(console)[console]:
        return True
    if not auto:
        return False
    raise BrowserError(
        f"The '{console}' console session expired in the dedicated profile.\n"
        + relogin([console])
    )


def run_harness(script: str, timeout: int = 180, auto_login: bool = True) -> str:
    """Run a browser-harness style Python script and return its stdout.

    With the dedicated profile, the script runs on its Chrome directly over CDP;
    without it, in browser-harness (the user's browser). `auto_login=False` avoids
    recursion when the caller is the session check itself (session_status uses
    run_harness to find out where the page ended up).
    """
    cdp = ensure_headless()
    if not cdp:
        return _harness(_harness_binary(), script, timeout)
    from . import cdp_direct

    try:
        output = cdp_direct.run(script, cdp)
    except cdp_direct.CdpError as exc:
        raise ScriptFailed(f"Console script failed on the dedicated Chrome:\n{exc}") from exc
    if not auto_login:
        return output
    if _logged_out_console(output) is None:
        save_cookies()  # good session: keep the copy fresh for the next Chrome
        return output
    # The dedicated profile lost its session, but the browser browser-harness drives by
    # default (the user's) is usually logged in. Try there before asking for a 2FA login.
    fallback = _via_user_browser(script, timeout)
    if fallback is not None:
        return fallback
    _falhar_se_deslogou(output)
    return output


def _harness_binary() -> str:
    binary = shutil.which("browser-harness")
    if not binary:
        raise BrowserError(
            "browser-harness not found on PATH. "
            "Install it, create the dedicated profile (python -m shipstores.browser login) "
            "or do this step manually in the console."
        )
    return binary


def _harness(binary: str, script: str, timeout: int) -> str:
    # Never point browser-harness at the dedicated Chrome: that one is driven over CDP.
    env = dict(os.environ)
    env.pop("BU_CDP_URL", None)
    result = subprocess.run(
        [binary], input=script, capture_output=True, text=True, timeout=timeout, env=env
    )
    output = f"{result.stdout}\n{result.stderr}".strip()
    if result.returncode != 0:
        raise ScriptFailed(f"browser-harness failed (exit {result.returncode}):\n{output}")
    return output


def _via_user_browser(script: str, timeout: int) -> str | None:
    """Run the script in browser-harness's default browser; None if it is logged out too."""
    binary = shutil.which("browser-harness")
    if not binary:
        return None
    try:
        output = _harness(binary, script, timeout)
    except (BrowserError, subprocess.TimeoutExpired):
        return None
    return output if _logged_out_console(output) is None else None


# Hosts that only show up when the console bounced navigation to the login screen.
# We check the result instead of verifying the session before every call: a
# pre-check would cost an extra navigation every time, and the symptom we want to
# avoid (reading the login screen thinking it is the console) shows up here.
_LOGIN_HOSTS = {
    "accounts.google.com": "play",
    "idmsa.apple.com": "apple",
    "signin.apple.com": "apple",
    # expired session: Apple stays on the same host and redirects to /login?authResult=FAILED
    "appstoreconnect.apple.com/login": "apple",
}

# Line scripts print when a fetch to the internal API returns 401/403: the page loaded,
# but without a session. Without it the result looked like "no messages".
LOGGED_OUT_MARK = "__LOGGED_OUT__"


def _logged_out_console(output: str) -> str | None:
    """Console whose login screen shows up in the script output, or None."""
    # Only a page URL on a line of its own counts (what scripts print with
    # page_info()["url"]). Looking for the host anywhere in the text fired on footer
    # links and iframes and reopened Chrome with a window for nothing.
    for host, console in _LOGIN_HOSTS.items():
        if re.search(rf"^\s*(URL=)?https://{re.escape(host)}[/?]", output, re.MULTILINE):
            return console
    mark = re.search(rf"{LOGGED_OUT_MARK} (\w+)", output)
    if mark and mark.group(1) in CONSOLES:
        return mark.group(1)
    return None


def _falhar_se_deslogou(output: str) -> None:
    console = _logged_out_console(output)
    if console:
        raise BrowserError(
            f"The '{console}' console session expired (in the dedicated profile and in the "
            "browser browser-harness drives by default), so what was read is the login "
            "screen, not the console.\n" + relogin([console])
        )


def open_page(url: str) -> str:
    """Open a URL in a new tab and return the page state."""
    return run_harness(
        f'new_tab({url!r})\n'
        "wait_for_load()\n"
        "info = page_info()\n"
        # The URL on its own line is what the logged-out detection looks at.
        "print('URL=' + str(info.get('url', '')))\n"
        "print(info)\n"
    )


def read_page_text(url: str, wait_seconds: int = 4) -> str:
    """Navigate to the URL and return the visible text — useful for verifying results."""
    return run_harness(
        "import time\n"
        f'new_tab({url!r})\n'
        "wait_for_load()\n"
        f"time.sleep({wait_seconds})\n"
        'print(js("document.body.innerText")[:4000])\n'
    )


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "login":
        login()
    else:
        print(f"dedicated profile: {PROFILE_DIR} ({'exists' if PROFILE_DIR.exists() else 'missing'})")
        print(f"CDP at {CDP_URL}: {'up' if _cdp_alive() else 'down'}")

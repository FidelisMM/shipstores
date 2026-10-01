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
dedicated profile exists, this module starts its own headless Chrome and points
browser-harness at it via BU_CDP_URL.

The profile must be logged in to both consoles. To create or renew the login:

    python -m shipstores.browser login

This opens the profile's Chrome WITH a window so a person can authenticate
(including 2FA). Close the window when done; the sessions are stored in the profile.

Without the profile, everything keeps working as before, in the user's Chrome.
"""

from __future__ import annotations

import os
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
        return CDP_URL
    _spawn(headless=True)
    for _ in range(20):
        time.sleep(1)
        if _cdp_alive():
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
        # when logged out, Apple redirects to idmsa
        "login_hosts": ("idmsa.apple.com", "signin.apple.com"),
    },
}


def _final_url(url: str) -> str:
    """Navigate and return the URL where the page ended up (follows login redirects)."""
    out = run_harness(
        "import time\n"
        f"new_tab({url!r})\n"
        "wait_for_load()\n"
        "time.sleep(4)\n"
        "print('URL=' + str(page_info().get('url','')))\n",
        auto_login=False,
    )
    for line in out.splitlines():
        if line.startswith("URL="):
            return line[4:].strip()
    return ""


def session_status(console: str | None = None) -> dict[str, bool]:
    """Report, for each console, whether the dedicated profile is still logged in."""
    alvos = [console] if console else list(CONSOLES)
    estado: dict[str, bool] = {}
    for nome in alvos:
        cfg = CONSOLES[nome]
        final = _final_url(cfg["url"])
        estado[nome] = bool(final) and not any(h in final for h in cfg["login_hosts"])
    return estado


def relogin(consoles: list[str] | None = None) -> str:
    """Close the headless Chrome and reopen it WITH a window on the consoles that need a login."""
    alvos = consoles or list(CONSOLES)
    urls = tuple(CONSOLES[c]["url"] for c in alvos if c in CONSOLES)
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
    """Run a Python script inside browser-harness and return its stdout.

    `auto_login=False` avoids recursion when the caller is the session check itself
    (session_status uses run_harness to find out where the page ended up).
    """
    binary = shutil.which("browser-harness")
    if not binary:
        raise BrowserError(
            "browser-harness not found on PATH. "
            "Install it or create the app manually in the console."
        )
    env = dict(os.environ)
    cdp = ensure_headless()
    if cdp:
        env["BU_CDP_URL"] = cdp
    result = subprocess.run(
        [binary], input=script, capture_output=True, text=True, timeout=timeout, env=env
    )
    output = f"{result.stdout}\n{result.stderr}".strip()
    if result.returncode != 0:
        raise BrowserError(f"browser-harness failed (exit {result.returncode}):\n{output}")
    if auto_login and cdp:
        _falhar_se_deslogou(output)
    return output


# Hosts that only show up when the console bounced navigation to the login screen.
# We check the result instead of verifying the session before every call: a
# pre-check would cost an extra navigation every time, and the symptom we want to
# avoid (reading the login screen thinking it is the console) shows up here.
_LOGIN_HOSTS = {
    "accounts.google.com": "play",
    "idmsa.apple.com": "apple",
    "signin.apple.com": "apple",
}


def _falhar_se_deslogou(output: str) -> None:
    for host, console in _LOGIN_HOSTS.items():
        if host in output:
            raise BrowserError(
                f"The '{console}' console session expired — the page ended up on {host}, "
                "so what was read is the login screen, not the console.\n"
                + relogin([console])
            )


def open_page(url: str) -> str:
    """Open a URL in a new tab and return the page state."""
    return run_harness(
        f'new_tab({url!r})\n'
        "wait_for_load()\n"
        "print(page_info())\n"
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

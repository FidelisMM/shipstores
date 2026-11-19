"""EAS CLI (Expo Application Services) client.

EAS produces the binary; the native tools in apple.py/play.py publish it. This
module bridges the two. It only applies to Expo projects — native projects
(Gradle, Xcode) keep using the direct path.

Every command runs with --non-interactive: the MCP server has no TTY to answer
prompts, and without that flag the CLI hangs waiting for input.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import httpx

# EAS builds take 10 to 40 minutes. No tool waits for that — the build is
# started with --no-wait and its progress is polled later.
BUILD_START_TIMEOUT = 300
SUBMIT_TIMEOUT = 1800

# Commands that reject --non-interactive ("Nonexistent flag") and would always fail
NO_NON_INTERACTIVE = {"build:view"}

# eas build error once the account used up the Free plan's monthly builds:
# "This account has used its iOS builds from the Free plan this month"
FREE_PLAN_EXHAUSTED = "from the free plan"

# Local builds run on this machine; without these tools eas fails halfway through the build
LOCAL_BUILD_TOOLS = {"ios": ("xcodebuild", "fastlane", "pod")}
NPM_CI_TIMEOUT = 900
# Last line of the local build log, with eas's exit code
LOCAL_DONE_MARKER = "[eas_build_local] exit="


class EasError(RuntimeError):
    pass


def _project_dir(project_path: str) -> Path:
    path = Path(project_path).expanduser()
    if not (path / "eas.json").exists():
        raise EasError(
            f"{path} has no eas.json — it is not an EAS project. "
            "For native projects use apple_upload_build / play_upload_bundle."
        )
    return path


def run(
    args: list[str], project_path: str, *, timeout: int = 120, parse_json: bool = True
) -> Any:
    """Run eas-cli inside the project and return its JSON output."""
    binary = shutil.which("eas")
    if not binary:
        raise EasError("eas-cli not found on PATH. Install it with: npm i -g eas-cli")

    cwd = _project_dir(project_path)
    flags = [] if args[0] in NO_NON_INTERACTIVE else ["--non-interactive"]
    result = subprocess.run(
        [binary, *args, *flags],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        # The real cause is usually on stdout; stderr only carries the version warning
        noise = ("eas-cli@", "npm install -g eas-cli", "Proceeding with outdated", "To upgrade, run")
        lines = [l for l in f"{result.stdout}\n{result.stderr}".splitlines() if l.strip() and not any(n in l for n in noise)]
        raise EasError(
            f"eas {' '.join(args)} failed (exit {result.returncode}):\n" + "\n".join(lines)[-1500:]
        )
    if not parse_json:
        return result.stdout.strip()

    # The CLI mixes version warnings into stdout; the JSON starts at the first { or [.
    text = result.stdout
    start = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=-1)
    if start == -1:
        raise EasError(f"eas {' '.join(args)} did not return JSON:\n{text[-800:]}")
    try:
        return json.loads(text[start:])
    except json.JSONDecodeError as exc:
        raise EasError(f"Invalid JSON from eas-cli: {exc}\n{text[start:][:800]}") from exc


def summarize(build: dict[str, Any]) -> dict[str, Any]:
    """Reduce an EAS build object to the fields that matter for publishing."""
    artifacts = build.get("artifacts") or {}
    return {
        "build_id": build.get("id"),
        "platform": build.get("platform"),
        "status": build.get("status"),
        "app_version": build.get("appVersion"),
        "build_number": build.get("appBuildVersion"),
        "profile": build.get("buildProfile"),
        "created_at": build.get("createdAt"),
        "artifact_url": artifacts.get("applicationArchiveUrl"),
        "logs_url": build.get("buildUrl"),
    }


def download_artifact(url: str, destination: Path) -> Path:
    """Stream the EAS binary down to a local path."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", url, follow_redirects=True, timeout=1800) as response:
        if response.status_code >= 400:
            raise EasError(f"artifact download -> HTTP {response.status_code}")
        with destination.open("wb") as handle:
            for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                handle.write(chunk)
    return destination


def ensure_node_modules(path: Path) -> bool:
    """Run `npm ci` when the project has no installed dependencies.

    eas build reads the config with `npx expo config`, which without node_modules fails
    with "Failed to resolve plugin for module expo-router". Returns True if it installed.
    """
    if (path / "node_modules" / "expo").is_dir():
        return False
    if not (path / "package-lock.json").exists():
        raise EasError(
            f"{path} has no node_modules and no package-lock.json: install the dependencies "
            "with the project's package manager before a local build."
        )
    npm = shutil.which("npm")
    if not npm:
        raise EasError("npm not found on PATH.")
    result = subprocess.run(
        [npm, "ci"], cwd=path, capture_output=True, text=True, timeout=NPM_CI_TIMEOUT
    )
    if result.returncode != 0:
        raise EasError(f"npm ci failed (exit {result.returncode}):\n{(result.stdout + result.stderr)[-1500:]}")
    return True


def start_local_build(
    project_path: str, platform: str, profile: str, output: str | None = None
) -> dict[str, Any]:
    """Start `eas build --local` in the background and return the log and binary paths.

    The process outlives the call (its own session); the log ends with
    LOCAL_DONE_MARKER followed by eas's exit code.
    """
    binary = shutil.which("eas")
    if not binary:
        raise EasError("eas-cli not found on PATH. Install it with: npm i -g eas-cli")
    missing = [tool for tool in LOCAL_BUILD_TOOLS.get(platform, ()) if not shutil.which(tool)]
    if missing:
        raise EasError(
            f"A local {platform} build needs {', '.join(missing)} on PATH "
            "(Xcode, fastlane and CocoaPods, macOS only)."
        )
    cwd = _project_dir(project_path)
    installed = ensure_node_modules(cwd)

    extension = ".ipa" if platform == "ios" else ".aab"
    artifact = (
        Path(output).expanduser()
        if output
        else Path(tempfile.mkdtemp(prefix="sp-eas-local-")) / f"{profile}{extension}"
    )
    artifact.parent.mkdir(parents=True, exist_ok=True)
    log_path = artifact.with_suffix(".log")
    command = [
        binary, "build", "--platform", platform, "--profile", profile,
        "--local", "--non-interactive", "--output", str(artifact),
    ]
    with log_path.open("w") as log:
        process = subprocess.Popen(
            ["/bin/sh", "-c", f'"$@"; echo "{LOCAL_DONE_MARKER}$?"', "sh", *command],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return {
        "pid": process.pid,
        "log_path": str(log_path),
        "artifact_path": str(artifact),
        "npm_ci": installed,
    }

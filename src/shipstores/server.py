"""MCP server for publishing apps to the App Store and Google Play."""

from __future__ import annotations

import hashlib
import json
import math
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import apple, apple_console, apple_review, browser, data_safety, eas, play, play_console, signing
from .config import (
    TOOLSETS,
    load_apple,
    load_play,
    load_toolsets,
    play_console_app_ids,
    play_developer_id,
)

mcp = FastMCP(
    "shipstores",
    instructions=(
        "Publishes iOS and Android apps. Neither store allows CREATING a new app "
        "via API: use apple_create_app_form / play_create_app_form, which open the "
        "console form in the browser. Everything else (bundle IDs, binary upload, "
        "versions, metadata, releases, submission) is done via API.\n\n"
        "Where the binary comes from depends on the project: if it has eas.json, the "
        "build comes from EAS (eas_* tools); native projects (Gradle, Xcode) produce "
        "the .ipa/.aab locally and go straight to apple_upload_build / "
        "play_upload_bundle.\n\n"
        "iOS subscriptions (IAP) are fully API-driven: apple_create_subscription_group, "
        "apple_create_subscription, apple_list_price_points, "
        "apple_set_subscription_price, apple_set_subscription_availability and "
        "apple_create_intro_offer. A product only becomes READY_TO_SUBMIT after it "
        "has both a price AND availability; check with apple_list_subscriptions. "
        "apple_submit_for_review takes every READY_TO_SUBMIT subscription and its group "
        "in the same submission as the version (an app's first subscription only passes "
        "that way).\n\n"
        "Outside Apple's API, browser only: the App Store Server Notifications URL "
        "and the Paid Apps agreements.\n\n"
        "Store listing screenshots are API-driven on both stores: "
        "apple_upload_screenshots and play_upload_screenshots upload a whole folder, "
        "in alphabetical order of file name. On Play the change takes effect "
        "immediately and is independent of releases; on the App Store a screenshot "
        "belongs to a version, so a READY_FOR_SALE version cannot be changed — "
        "create the next one with apple_create_version and point to it.\n\n"
        "Run store_doctor first whenever something fails due to credentials."
    ),
)


_CORE_TOOLS = {"store_doctor", "store_audit_identity", "store_browser_session"}

# Tools whose docstring says "External action" change something in a store (or
# reach a person at Apple/Google). Clients that read MCP annotations can gate
# them without relying on the description text.
_EXTERNAL_ACTION = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)


def _register_tool(function: Callable[..., Any]) -> Any:
    name = function.__name__
    if name in _CORE_TOOLS:
        toolset = "core"
    else:
        toolset = name.partition("_")[0]
        if toolset not in TOOLSETS - {"core"}:
            raise ValueError(f"Tool {name!r} does not belong to a known toolset.")
    external = "External action" in (function.__doc__ or "")
    return mcp.tool(tags={toolset}, annotations=_EXTERNAL_ACTION if external else None)(function)


# ---------------------------------------------------------------- diagnostics


@_register_tool
def store_doctor() -> dict[str, Any]:
    """Check the credentials for both stores with real API calls.

    Use this before investigating any authentication error. Returns, for each
    store, whether the credential authenticates and what is missing.
    """
    report: dict[str, Any] = {}

    apple_cfg = load_apple()
    apple_status: dict[str, Any] = {
        "key_id": apple_cfg.key_id,
        "issuer_id": apple_cfg.issuer_id,
        "team_id": apple_cfg.team_id,
        "private_key_path": str(apple_cfg.private_key_path),
        "private_key_exists": apple_cfg.private_key_path.exists(),
    }
    try:
        data = apple.request("GET", "/v1/apps", params={"limit": 200}, cfg=apple_cfg)
        apple_status["authenticated"] = True
        apple_status["app_count"] = len(data.get("data", []))
        # The API does not expose the Team ID directly; the seedId of any bundle ID is it.
        # A brand-new account with no bundle ID yet has no way to discover it.
        bundles = apple.request("GET", "/v1/bundleIds", params={"limit": 1, "fields[bundleIds]": "seedId"}, cfg=apple_cfg)
        seed = next((x["attributes"].get("seedId") for x in bundles.get("data", [])), None)
        apple_status["detected_team_id"] = seed or "register a bundle ID to discover it"
        if seed and seed != apple_cfg.team_id:
            apple_status["warning"] = f"Configured APPLE_TEAM_ID ({apple_cfg.team_id}) differs from the key's team ({seed})."
    except Exception as exc:  # noqa: BLE001 — diagnostics must report, not raise
        apple_status["authenticated"] = False
        apple_status["error"] = str(exc)[:500]
    report["apple"] = apple_status

    play_cfg = load_play()
    play_status: dict[str, Any] = {
        "service_account_path": str(play_cfg.service_account_path),
        "service_account_exists": play_cfg.service_account_path.exists(),
    }
    try:
        play._headers(play_cfg)  # noqa: SLF001 — token check, no public endpoint
        play_status["authenticated"] = True
        play_status["note"] = (
            "Token OK. Per-app access depends on the service account being "
            "invited in Play Console; use play_track_status to confirm."
        )
    except Exception as exc:  # noqa: BLE001
        play_status["authenticated"] = False
        play_status["error"] = str(exc)[:500]
    report["play"] = play_status

    eas_status: dict[str, Any] = {}
    try:
        import shutil

        binary = shutil.which("eas")
        eas_status["cli_path"] = binary
        if binary:
            import subprocess

            who = subprocess.run(
                [binary, "whoami"], capture_output=True, text=True, timeout=60
            )
            eas_status["authenticated"] = who.returncode == 0
            eas_status["account"] = who.stdout.strip().splitlines()[0] if who.stdout.strip() else None
        else:
            eas_status["authenticated"] = False
            eas_status["error"] = "eas-cli not installed (npm i -g eas-cli)"
    except Exception as exc:  # noqa: BLE001
        eas_status["authenticated"] = False
        eas_status["error"] = str(exc)[:500]
    report["eas"] = eas_status

    return report


@_register_tool
def store_browser_session(relogin: bool = False, console: str | None = None) -> dict[str, Any]:
    """Login state of the dedicated browser on both consoles.

    Console forms (App content, Data safety, app creation) run in a headless
    Chrome with its own profile, so they do not fight the user for the screen.
    Apple's session expires often; when it does, the automation ends up reading
    the login screen and returns meaningless results — so check here before
    investigating a form that "did not save".

    With relogin=True, opens Chrome WITH a window so a person can authenticate
    (2FA cannot be automated). Pass `console` ('play' or 'apple') to open only
    the one that expired. Without relogin and with both sessions alive, a login
    window left open is closed (Browser.close, not pkill, with the console cookies
    saved first) and everything goes back to running headless.
    """
    if not browser.PROFILE_DIR.exists():
        return {
            "dedicated_profile": False,
            "note": (
                "No dedicated profile — automations use the user's Chrome. "
                "Run `python -m shipstores.browser login` to create the profile."
            ),
        }
    estado = browser.session_status(console)
    out: dict[str, Any] = {
        "dedicated_profile": True,
        "path": str(browser.PROFILE_DIR),
        "sessions": estado,
    }
    caidas = [k for k, v in estado.items() if not v]
    if caidas:
        # Automations fall back to the browser browser-harness drives by default when the
        # dedicated profile loses its session; this shows whether that path works.
        out["sessions_in_user_browser"] = {k: browser.session_status_user_browser(k) for k in caidas}
    if not caidas and browser.return_to_headless():
        # Logged in and the login window is still open: close it so it stops taking the screen.
        out["window"] = "closed; the next operations run headless"
    if caidas and relogin:
        out["action"] = browser.relogin(caidas)
    elif caidas:
        out["suggested_action"] = (
            f"Session expired on: {', '.join(caidas)}. "
            "Call again with relogin=True to open the login window."
        )
    return out


# --------------------------------------------------------------------- apple


@_register_tool
def apple_list_apps() -> list[dict[str, Any]]:
    """List the apps in the App Store Connect account with id, bundleId and name.

    The returned `app_id` is the App ID (ascAppId) used by every other Apple tool.
    """
    data = apple.request("GET", "/v1/apps", params={"limit": 200})
    return [
        {
            "app_id": item["id"],
            "bundle_id": item["attributes"]["bundleId"],
            "name": item["attributes"]["name"],
            "sku": item["attributes"].get("sku"),
            "primary_locale": item["attributes"].get("primaryLocale"),
        }
        for item in data.get("data", [])
    ]


@_register_tool
def apple_register_bundle_id(identifier: str, name: str, platform: str = "IOS") -> dict[str, Any]:
    """Register a new Bundle ID in the Apple Developer portal.

    This is the first step for a new app and IS supported by the API. After it,
    the app record itself must be created through the form (apple_create_app_form).

    platform: IOS, MAC_OS or UNIVERSAL.
    """
    data = apple.request(
        "POST",
        "/v1/bundleIds",
        json={
            "data": {
                "type": "bundleIds",
                "attributes": {"identifier": identifier, "name": name, "platform": platform},
            }
        },
    )
    return {"bundle_id_resource_id": data["data"]["id"], "identifier": identifier}


@_register_tool
def apple_create_app_form(bundle_id: str, suggested_name: str) -> dict[str, Any]:
    """Open the App Store Connect new-app form in the browser.

    Apple's API does not expose app creation (there is no POST /v1/apps), so this
    step is assisted: the tool opens the page in the already logged-in Chrome and
    returns the values you need to fill in. Register the Bundle ID first with
    apple_register_bundle_id.
    """
    page = browser.open_page(browser.ASC_NEW_APP_URL)
    return {
        "opened": browser.ASC_NEW_APP_URL,
        "page": page,
        "fill_in": {
            "Platform": "iOS",
            "Name": suggested_name,
            "Primary language": "Portuguese (Brazil)",
            "Bundle ID": bundle_id,
            "SKU": bundle_id,
        },
        "next_step": "After creating it, run apple_list_apps to get the app_id.",
    }


@_register_tool
def apple_upload_build(ipa_path: str, platform: str = "ios") -> dict[str, Any]:
    """Upload an .ipa to App Store Connect through the buildUploads API (no altool).

    Version, build number and bundle id come from the .ipa's own Info.plist; the app
    is found by bundle id. Blocks until Apple accepts the upload (COMPLETE) or rejects
    it (FAILED, with Apple's errors in `errors`). Processing takes a few more minutes
    after that — wait for it with apple_wait_build. platform: ios, osx or appletvos.
    Works on any OS: no Xcode needed.
    """
    path = Path(ipa_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    result = apple.upload_binary(path, platform, load_apple())
    result["uploaded"] = str(path)
    if result["state"] == "COMPLETE":
        result["next_step"] = (
            f"Wait for processing with apple_wait_build(app_id={result['app_id']}, "
            f"build_number={result['build_number']})."
        )
    elif result["state"] == "FAILED":
        result["warning"] = "Apple rejected the upload; see errors."
    else:
        result["warning"] = (
            f"Upload sent, but still {result['state']}. Check with apple_wait_build."
        )
    return result


@_register_tool
def apple_list_builds(app_id: str, limit: int = 10) -> list[dict[str, Any]]:
    """List an app's builds and the processing state of each.

    processingState PROCESSING means the build cannot be attached to a version
    yet; VALID means it is ready to use.
    """
    data = apple.request(
        "GET",
        "/v1/builds",
        params={"filter[app]": app_id, "limit": limit, "sort": "-uploadedDate"},
    )
    return [
        {
            "build_id": item["id"],
            "version": item["attributes"].get("version"),
            "processing_state": item["attributes"].get("processingState"),
            "uploaded_date": item["attributes"].get("uploadedDate"),
            "expired": item["attributes"].get("expired"),
        }
        for item in data.get("data", [])
    ]


# Interval between apple_wait_build polls. Processing takes 5 to 30 minutes.
BUILD_POLL_SECONDS = 30


@_register_tool
def apple_wait_build(app_id: str, build_number: str, timeout_min: float = 30) -> dict[str, Any]:
    """Wait for a build to leave PROCESSING and return its build_id.

    Blocks until build `build_number` (the CFBundleVersion, e.g. "42") reaches
    VALID, FAILED or INVALID, polling every 30 s. Right after the upload the build
    is not in the API yet; that counts as waiting, not as an error. With VALID, the
    build_id can go straight to apple_attach_build.
    """
    tries = max(1, math.ceil(timeout_min * 60 / BUILD_POLL_SECONDS))
    state = "NOT_LISTED_YET"
    for n in range(tries):
        if n:
            time.sleep(BUILD_POLL_SECONDS)
        data = apple.request(
            "GET",
            "/v1/builds",
            params={
                "filter[app]": app_id,
                "filter[version]": build_number,
                "sort": "-uploadedDate",
                "limit": 1,
            },
        ).get("data", [])
        if not data:
            continue
        build = data[0]
        state = build["attributes"].get("processingState")
        if state in ("VALID", "FAILED", "INVALID"):
            result = {
                "build_id": build["id"],
                "build_number": build_number,
                "processing_state": state,
                "uploaded_date": build["attributes"].get("uploadedDate"),
            }
            if state != "VALID":
                result["warning"] = (
                    "Apple rejected the binary. The reason arrives by email to the account; "
                    "upload a new build with a different build_number."
                )
            return result
    raise apple.AppleError(
        f"Build {build_number} of app {app_id} still {state} after {timeout_min} min. "
        "Run apple_wait_build again to keep waiting."
    )


@_register_tool
def apple_create_version(app_id: str, version_string: str, platform: str = "IOS") -> dict[str, Any]:
    """Create a new App Store version for an existing app.

    Fails if an editable version already exists — in that case reuse the existing
    one (apple_list_versions) instead of creating another.
    """
    data = apple.request(
        "POST",
        "/v1/appStoreVersions",
        json={
            "data": {
                "type": "appStoreVersions",
                "attributes": {"platform": platform, "versionString": version_string},
                "relationships": {"app": {"data": {"type": "apps", "id": app_id}}},
            }
        },
    )
    return {"version_id": data["data"]["id"], "version_string": version_string}


@_register_tool
def apple_list_versions(app_id: str, limit: int = 10) -> list[dict[str, Any]]:
    """List an app's App Store versions, with the review state of each."""
    data = apple.request(
        "GET", f"/v1/apps/{app_id}/appStoreVersions", params={"limit": limit}
    )
    return [
        {
            "version_id": item["id"],
            "version_string": item["attributes"].get("versionString"),
            "state": item["attributes"].get("appStoreState")
            or item["attributes"].get("appVersionState"),
            "platform": item["attributes"].get("platform"),
        }
        for item in data.get("data", [])
    ]


@_register_tool
def apple_update_listing(
    version_id: str,
    locale: str = "pt-BR",
    whats_new: str | None = None,
    description: str | None = None,
    keywords: str | None = None,
    promotional_text: str | None = None,
) -> dict[str, Any]:
    """Update the store listing texts of a version, for one locale.

    Only the fields passed are changed. `whats_new` is the version's "What's New"
    text; it cannot be used on an app's first version.
    """
    localizations = apple.request(
        "GET", f"/v1/appStoreVersions/{version_id}/appStoreVersionLocalizations"
    )
    match = next(
        (loc for loc in localizations.get("data", []) if loc["attributes"]["locale"] == locale),
        None,
    )
    if match is None:
        available = [loc["attributes"]["locale"] for loc in localizations.get("data", [])]
        raise ValueError(f"Locale {locale} does not exist on this version. Available: {available}")

    attributes = {
        key: value
        for key, value in {
            "whatsNew": whats_new,
            "description": description,
            "keywords": keywords,
            "promotionalText": promotional_text,
        }.items()
        if value is not None
    }
    if not attributes:
        raise ValueError("No fields to update.")

    apple.request(
        "PATCH",
        f"/v1/appStoreVersionLocalizations/{match['id']}",
        json={
            "data": {
                "type": "appStoreVersionLocalizations",
                "id": match["id"],
                "attributes": attributes,
            }
        },
    )
    return {"localization_id": match["id"], "locale": locale, "updated": list(attributes)}


@_register_tool
def apple_attach_build(version_id: str, build_id: str) -> dict[str, Any]:
    """Attach a processed build to an App Store version.

    The build must have processingState VALID (see apple_list_builds).
    """
    apple.request(
        "PATCH",
        f"/v1/appStoreVersions/{version_id}/relationships/build",
        json={"data": {"type": "builds", "id": build_id}},
    )
    return {"version_id": version_id, "build_id": build_id, "attached": True}


@_register_tool
def apple_submit_for_review(version_id: str, include_subscriptions: bool = True) -> dict[str, Any]:
    """Submit a version for Apple review. External action, hard to reverse.

    Confirm with the user before calling. Requires the version to already have a
    build attached and complete metadata.

    With include_subscriptions (default), every subscription in READY_TO_SUBMIT and
    its group go in the same submission. An app's first subscription only passes this
    way: in-app purchases always travel with a version, and without the group the
    submission gets stuck. Same as "Add for Review" on the version, on each plan and on
    the group in the console, picking the same draft. Reuses the app's open draft
    submission, if there is one. Before submitting it counts the draft's items and
    stops, without submitting, if any is missing.
    """
    # The 'app' relationship does not accept a direct GET (403 FORBIDDEN_ERROR); the
    # app_id comes from the include on the version itself.
    version = apple.request("GET", f"/v1/appStoreVersions/{version_id}", params={"include": "app"})
    app_id = version["data"]["relationships"]["app"]["data"]["id"]

    submission_id = _draft_submission(app_id)
    # Without the include, Apple returns the items without the relationship, the version
    # looks absent from the draft and the next POST gets 409 "already added".
    items = apple.request(
        "GET", f"/v1/reviewSubmissions/{submission_id}/items", params={"include": "appStoreVersion"}
    )["data"]
    versions_in_draft = {
        (((i.get("relationships") or {}).get("appStoreVersion") or {}).get("data") or {}).get("id")
        for i in items
    }
    if version_id not in versions_in_draft:
        apple.request(
            "POST",
            "/v1/reviewSubmissionItems",
            json={
                "data": {
                    "type": "reviewSubmissionItems",
                    "relationships": {
                        "reviewSubmission": {
                            "data": {"type": "reviewSubmissions", "id": submission_id}
                        },
                        "appStoreVersion": {
                            "data": {"type": "appStoreVersions", "id": version_id}
                        },
                    },
                }
            },
        )

    # The version must be in the draft first: it is what unlocks the first subscription.
    extras = (_submit_subscriptions(app_id, submission_id) if include_subscriptions
              else {"subscriptions": [], "groups": [], "warnings": []})

    expected = 1 + len(extras["subscriptions"]) + len(extras["groups"])
    items = apple.request("GET", f"/v1/reviewSubmissions/{submission_id}/items")["data"]
    if include_subscriptions and len(items) < expected:
        # Submitting like this would send the version without its plans, and Apple
        # rejects that under 2.1.
        raise apple.AppleError(
            f"Draft {submission_id} has {len(items)} items, expected {expected} "
            f"(version + {len(extras['subscriptions'])} subscriptions + {len(extras['groups'])} groups). "
            "Nothing was submitted. Check App Store Connect > Distribution > Draft Submissions."
        )

    result = apple.request(
        "PATCH",
        f"/v1/reviewSubmissions/{submission_id}",
        json={
            "data": {
                "type": "reviewSubmissions",
                "id": submission_id,
                "attributes": {"submitted": True},
            }
        },
    )
    out = {
        "review_submission_id": submission_id,
        "version_id": version_id,
        "submitted": True,
        "state": result.get("data", {}).get("attributes", {}).get("state"),
        "items": len(items),
        "subscriptions": extras["subscriptions"],
        "groups": extras["groups"],
    }
    if extras["warnings"]:
        out["warnings"] = extras["warnings"]
    return out


def _draft_submission(app_id: str) -> str:
    """The app's open iOS draft submission (READY_FOR_REVIEW, not submitted yet), or a new one."""
    subs = apple.request("GET", f"/v1/apps/{app_id}/reviewSubmissions", params={"limit": 10})
    for sub in subs.get("data", []):
        attrs = sub["attributes"]
        if attrs.get("state") == "READY_FOR_REVIEW" and attrs.get("platform", "IOS") == "IOS":
            return sub["id"]
    created = apple.request(
        "POST",
        "/v1/reviewSubmissions",
        json={
            "data": {
                "type": "reviewSubmissions",
                "attributes": {"platform": "IOS"},
                "relationships": {"app": {"data": {"type": "apps", "id": app_id}}},
            }
        },
    )
    return created["data"]["id"]


def _no_pending_version(exc: apple.AppleError) -> bool:
    return "no pending version" in str(exc)


def _submit_subscriptions(app_id: str, submission_id: str) -> dict[str, list[str]]:
    """Put each READY_TO_SUBMIT subscription and its group in the open draft.

    A subscription failure stops everything (without it the submission goes out
    incomplete). A group failure becomes a warning: a group that already went through
    review may refuse a new item. After a canceled submission, the API refuses with
    "no pending version" what the console accepts; in that case the item goes in
    through the console's "Add for Review" button.
    """
    subscriptions: list[str] = []
    groups: list[str] = []
    warnings: list[str] = []
    for group in apple.request("GET", f"/v1/apps/{app_id}/subscriptionGroups").get("data", []):
        subs = apple.request("GET", f"/v1/subscriptionGroups/{group['id']}/subscriptions").get("data", [])
        ready = [s for s in subs if s["attributes"].get("state") == "READY_TO_SUBMIT"]
        for sub in ready:
            try:
                apple.request(
                    "POST",
                    "/v1/subscriptionSubmissions",
                    json={"data": {"type": "subscriptionSubmissions", "relationships": {
                        "subscription": {"data": {"type": "subscriptions", "id": sub["id"]}}}}},
                )
            except apple.AppleError as exc:
                if not _no_pending_version(exc):
                    raise
                apple_console.add_to_review_draft(app_id, "subscriptions", sub["id"])
                warnings.append(f"subscription {sub['id']} went in through the console (API: no pending version)")
            subscriptions.append(sub["id"])
        if not ready:
            continue
        try:
            apple.request(
                "POST",
                "/v1/subscriptionGroupSubmissions",
                json={"data": {"type": "subscriptionGroupSubmissions", "relationships": {
                    "subscriptionGroup": {"data": {"type": "subscriptionGroups", "id": group["id"]}}}}},
            )
            groups.append(group["id"])
        except apple.AppleError as exc:
            failure: Exception = exc
            if _no_pending_version(exc):
                try:
                    apple_console.add_to_review_draft(app_id, "subscription-groups", group["id"])
                    groups.append(group["id"])
                    warnings.append(f"group {group['id']} went in through the console (API: no pending version)")
                    continue
                except browser.BrowserError as console_exc:
                    failure = console_exc
            warnings.append(f"group {group['id']} did not join the submission: {str(failure)[:300]}")
    return {"subscriptions": subscriptions, "groups": groups, "warnings": warnings}


def _active_submission(app_id: str, states: tuple[str, ...]) -> str:
    """Id of the app's most recent review submission in one of the given states."""
    subs = apple.request("GET", f"/v1/apps/{app_id}/reviewSubmissions", params={"limit": 10})
    for sub in subs.get("data", []):
        if sub["attributes"].get("state") in states:
            return sub["id"]
    raise apple.AppleError(f"No review submission for app {app_id} in {', '.join(states)}.")


def _editable_app_info(app_id: str) -> str:
    """The editable appInfo (the one not on sale; if there is only one, that one)."""
    infos = apple.request("GET", f"/v1/apps/{app_id}/appInfos")["data"]
    for info in infos:
        # appStoreState is deprecated; newer accounts only carry `state`
        state = info["attributes"].get("appStoreState") or info["attributes"].get("state")
        if state not in ("READY_FOR_SALE", "READY_FOR_DISTRIBUTION", "REMOVED_FROM_SALE"):
            return info["id"]
    return infos[0]["id"]


def _editable_version(app_id: str) -> str:
    editable = ("PREPARE_FOR_SUBMISSION", "DEVELOPER_REJECTED", "REJECTED", "METADATA_REJECTED", "INVALID_BINARY")
    for v in apple.request("GET", f"/v1/apps/{app_id}/appStoreVersions", params={"limit": 10})["data"]:
        if v["attributes"].get("appStoreState") in editable:
            return v["id"]
    raise apple.AppleError("No editable version: create one with apple_create_version.")


@_register_tool
def apple_set_app_info(
    app_id: str,
    locale: str = "pt-BR",
    subtitle: str | None = None,
    privacy_policy_url: str | None = None,
    support_url: str | None = None,
    marketing_url: str | None = None,
    copyright: str | None = None,
    primary_category: str | None = None,
    secondary_category: str | None = None,
    uses_third_party_content: bool | None = None,
) -> dict[str, Any]:
    """Fill in the listing fields that apple_update_listing does not cover. External action.

    Subtitle and privacy policy URL live on the appInfo; support URL, marketing URL
    and copyright live on the editable version; categories use Apple IDs (e.g.
    MEDICAL, HEALTH_AND_FITNESS, LIFESTYLE, EDUCATION, PRODUCTIVITY, BOOKS).
    Only the fields passed are changed.
    """
    done: list[str] = []
    info_id = _editable_app_info(app_id)
    if subtitle is not None or privacy_policy_url is not None:
        locs = apple.request("GET", f"/v1/appInfos/{info_id}/appInfoLocalizations")["data"]
        loc = next((l for l in locs if l["attributes"]["locale"] == locale), None)
        if loc is None:
            raise apple.AppleError(f"appInfo has no localization {locale}.")
        attrs = {k: v for k, v in {"subtitle": subtitle, "privacyPolicyUrl": privacy_policy_url}.items() if v is not None}
        apple.request("PATCH", f"/v1/appInfoLocalizations/{loc['id']}",
                      json={"data": {"type": "appInfoLocalizations", "id": loc["id"], "attributes": attrs}})
        done += list(attrs)
    if primary_category or secondary_category:
        rel = {}
        if primary_category:
            rel["primaryCategory"] = {"data": {"type": "appCategories", "id": primary_category.upper()}}
        if secondary_category:
            rel["secondaryCategory"] = {"data": {"type": "appCategories", "id": secondary_category.upper()}}
        apple.request("PATCH", f"/v1/appInfos/{info_id}",
                      json={"data": {"type": "appInfos", "id": info_id, "relationships": rel}})
        done += list(rel)
    if support_url is not None or marketing_url is not None or copyright is not None:
        version_id = _editable_version(app_id)
        if copyright is not None:
            apple.request("PATCH", f"/v1/appStoreVersions/{version_id}",
                          json={"data": {"type": "appStoreVersions", "id": version_id, "attributes": {"copyright": copyright}}})
            done.append("copyright")
        if support_url is not None or marketing_url is not None:
            vlocs = apple.request("GET", f"/v1/appStoreVersions/{version_id}/appStoreVersionLocalizations")["data"]
            vloc = next((l for l in vlocs if l["attributes"]["locale"] == locale), None)
            if vloc is None:
                raise apple.AppleError(f"Version has no localization {locale}.")
            attrs = {k: v for k, v in {"supportUrl": support_url, "marketingUrl": marketing_url}.items() if v is not None}
            apple.request("PATCH", f"/v1/appStoreVersionLocalizations/{vloc['id']}",
                          json={"data": {"type": "appStoreVersionLocalizations", "id": vloc["id"], "attributes": attrs}})
            done += list(attrs)
    if uses_third_party_content is not None:
        value = "USES_THIRD_PARTY_CONTENT" if uses_third_party_content else "DOES_NOT_USE_THIRD_PARTY_CONTENT"
        apple.request("PATCH", f"/v1/apps/{app_id}",
                      json={"data": {"type": "apps", "id": app_id, "attributes": {"contentRightsDeclaration": value}}})
        done.append("contentRightsDeclaration")
    return {"app_id": app_id, "updated": done}


# Apple's limit for the app name on the App Store
APP_NAME_MAX = 30


def _name_taken(exc: apple.AppleError) -> bool:
    """Apple answers 409 when another app on the store already uses the name."""
    text = str(exc).lower()
    return "duplicate" in text or "already being used" in text or "already in use" in text


@_register_tool
def apple_set_app_name(
    app_id: str,
    name: str,
    locale: str | None = None,
    alternatives: list[str] | None = None,
) -> dict[str, Any]:
    """Change the app's name on the App Store (appInfoLocalizations.name). External action.

    Changes the editable appInfo (the one for the version being prepared, not the one
    on sale). Without locale, changes it in every language of the listing. The name
    must be unique on the App Store and at most 30 characters: if Apple refuses it as
    already in use, each item of `alternatives` is tried in order, stopping at the
    first one accepted. apple_set_app_info covers subtitle, URLs and categories, but
    not the name.
    """
    info_id = _editable_app_info(app_id)
    locs = apple.request("GET", f"/v1/appInfos/{info_id}/appInfoLocalizations")["data"]
    if locale is not None:
        locs = [loc for loc in locs if loc["attributes"]["locale"] == locale]
        if not locs:
            raise apple.AppleError(f"The appInfo has no {locale} localization.")

    refused: list[dict[str, str]] = []
    for candidate in [name, *(alternatives or [])]:
        if len(candidate) > APP_NAME_MAX:
            refused.append({"name": candidate, "reason": f"longer than {APP_NAME_MAX} characters"})
            continue
        try:
            for loc in locs:
                apple.request("PATCH", f"/v1/appInfoLocalizations/{loc['id']}",
                              json={"data": {"type": "appInfoLocalizations", "id": loc["id"],
                                             "attributes": {"name": candidate}}})
        except apple.AppleError as exc:
            if not _name_taken(exc):
                raise
            refused.append({"name": candidate, "reason": "already used by another App Store app"})
            continue
        return {
            "app_id": app_id,
            "name": candidate,
            "locales": [loc["attributes"]["locale"] for loc in locs],
            "refused": refused,
        }
    raise apple.AppleError(
        "Apple accepted none of the names: "
        + "; ".join(f"'{r['name']}' ({r['reason']})" for r in refused)
        + ". Pass other options in alternatives."
    )


# Neutral age rating answers; overrides change only what the app actually has
AGE_RATING_DEFAULTS: dict[str, Any] = {
    "advertising": False, "alcoholTobaccoOrDrugUseOrReferences": "NONE", "contests": "NONE",
    "gambling": False, "gamblingSimulated": "NONE", "gunsOrOtherWeapons": "NONE",
    "healthOrWellnessTopics": False, "lootBox": False, "medicalOrTreatmentInformation": "NONE",
    "messagingAndChat": False, "parentalControls": False, "profanityOrCrudeHumor": "NONE",
    "ageAssurance": False, "sexualContentGraphicAndNudity": "NONE", "sexualContentOrNudity": "NONE",
    "socialMedia": False, "horrorOrFearThemes": "NONE", "matureOrSuggestiveThemes": "NONE",
    "unrestrictedWebAccess": False, "userGeneratedContent": False, "violenceCartoonOrFantasy": "NONE",
    "violenceRealisticProlongedGraphicOrSadistic": "NONE", "violenceRealistic": "NONE",
}


@_register_tool
def apple_set_age_rating(app_id: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Answer the age rating questionnaire. External action.

    Starts from all "no/NONE" and applies `overrides` with what the app actually has.
    Frequency values: NONE, INFREQUENT_OR_MILD, FREQUENT_OR_INTENSE.
    E.g. a health app: {"healthOrWellnessTopics": true,
    "medicalOrTreatmentInformation": "INFREQUENT_OR_MILD"}.
    """
    answers = {**AGE_RATING_DEFAULTS, **(overrides or {})}
    unknown = set(answers) - set(AGE_RATING_DEFAULTS)
    if unknown:
        raise ValueError(f"Unknown fields: {sorted(unknown)}. Valid: {sorted(AGE_RATING_DEFAULTS)}")
    info_id = _editable_app_info(app_id)
    decl = apple.request("GET", f"/v1/appInfos/{info_id}/ageRatingDeclaration")["data"]["id"]
    apple.request("PATCH", f"/v1/ageRatingDeclarations/{decl}",
                  json={"data": {"type": "ageRatingDeclarations", "id": decl, "attributes": answers}})
    return {"app_id": app_id, "answers": {k: v for k, v in answers.items() if v not in (False, "NONE")}}


@_register_tool
def apple_set_free_price(app_id: str, base_territory: str = "BRA") -> dict[str, Any]:
    """Make the app free (price 0 in the base territory). External action."""
    points = apple.request("GET", f"/v1/apps/{app_id}/appPricePoints",
                           params={"filter[territory]": base_territory, "limit": 10})["data"]
    free = next((p["id"] for p in points if float(p["attributes"]["customerPrice"]) == 0), None)
    if free is None:
        raise apple.AppleError(f"No free price point in {base_territory}.")
    apple.request("POST", "/v1/appPriceSchedules", json={
        "data": {"type": "appPriceSchedules", "relationships": {
            "app": {"data": {"type": "apps", "id": app_id}},
            "baseTerritory": {"data": {"type": "territories", "id": base_territory}},
            "manualPrices": {"data": [{"type": "appPrices", "id": "${p0}"}]}}},
        "included": [{"type": "appPrices", "id": "${p0}", "attributes": {"startDate": None},
                      "relationships": {"appPricePoint": {"data": {"type": "appPricePoints", "id": free}}}}],
    })
    return {"app_id": app_id, "price": "free", "base_territory": base_territory}


@_register_tool
def apple_set_availability(
    app_id: str, territories: list[str], available_in_new_territories: bool = False
) -> dict[str, Any]:
    """Choose the countries where the app is available (3-letter ISO, e.g. ["BRA"]). External action.

    Uses the console's internal API (the public one answers 409), in the dedicated
    Chrome session. Including the European Union requires the trader declaration
    (DSA) on the account.
    """
    return {"app_id": app_id, **apple_console.set_availability(app_id, territories, available_in_new_territories)}


@_register_tool
def apple_set_app_privacy(app_id: str, usages: list[dict[str, Any]]) -> dict[str, Any]:
    """Replace and publish the "App Privacy" declaration (privacy nutrition label). External action.

    usages: one item per collected data type:
      {"category": "NAME", "purposes": ["APP_FUNCTIONALITY"], "linked": true, "tracking": false}
    Empty list = "we do not collect data". Accepted categories and purposes are in
    apple_console.DATA_CATEGORIES / DATA_PURPOSES. Uses the console's internal API.
    """
    return {"app_id": app_id, **apple_console.set_privacy(app_id, usages)}


@_register_tool
def apple_testflight_invite(
    app_id: str, email: str, first_name: str = "", last_name: str = "", group_name: str = "Internal"
) -> dict[str, Any]:
    """Invite someone to the app's internal TestFlight (a group that receives every build). External action.

    Internal testing skips review, but the person must be a user on the team in
    App Store Connect (check under Users and Access). If no internal group named
    `group_name` exists, one is created.
    """
    users = apple.request("GET", "/v1/users", params={"limit": 200})["data"]
    if not any(u["attributes"]["username"].lower() == email.lower() for u in users):
        raise apple.AppleError(f"{email} is not a user on the team; invite them under Users and Access first (internal testing requires it).")
    groups = apple.request("GET", f"/v1/apps/{app_id}/betaGroups")["data"]
    group = next((g for g in groups if g["attributes"]["name"] == group_name and g["attributes"].get("isInternalGroup")), None)
    if group is None:
        group = apple.request("POST", "/v1/betaGroups", json={"data": {"type": "betaGroups",
            "attributes": {"name": group_name, "isInternalGroup": True, "hasAccessToAllBuilds": True},
            "relationships": {"app": {"data": {"type": "apps", "id": app_id}}}}})["data"]
    tester = apple.request("POST", "/v1/betaTesters", json={"data": {"type": "betaTesters",
        "attributes": {"email": email, "firstName": first_name, "lastName": last_name},
        "relationships": {"betaGroups": {"data": [{"type": "betaGroups", "id": group["id"]}]}}}})["data"]
    apple.request("POST", "/v1/betaTesterInvitations", json={"data": {"type": "betaTesterInvitations",
        "relationships": {"app": {"data": {"type": "apps", "id": app_id}},
                          "betaTester": {"data": {"type": "betaTesters", "id": tester["id"]}}}}})
    builds = [b["attributes"]["version"] for b in apple.request("GET", f"/v1/betaGroups/{group['id']}/builds")["data"]]
    return {"group": group_name, "group_id": group["id"], "invited": email, "visible_builds": builds}


# Canceling takes from seconds to a few minutes (about 1 min in practice).
CANCEL_WAIT_TRIES = 30
CANCEL_WAIT_SECONDS = 10


@_register_tool
def apple_cancel_submission(app_id: str) -> dict[str, Any]:
    """Pull a submission in review, or a rejected one, out of the queue. External action.

    Use it to swap the build without waiting for the review, or on a rejected
    submission (UNRESOLVED_ISSUES) when the fix is in an item it locks: while the
    submission exists, a subscription's image and metadata reject edits ("version is
    not editable"). Waits for CANCELING to finish, since only then are the items
    unlocked. The version can be resubmitted with apple_submit_for_review.
    """
    sub_id = _active_submission(
        app_id, ("WAITING_FOR_REVIEW", "IN_REVIEW", "READY_FOR_REVIEW", "UNRESOLVED_ISSUES")
    )
    result = apple.request(
        "PATCH",
        f"/v1/reviewSubmissions/{sub_id}",
        json={"data": {"type": "reviewSubmissions", "id": sub_id, "attributes": {"canceled": True}}},
    )
    state = result["data"]["attributes"].get("state")
    for _ in range(CANCEL_WAIT_TRIES):
        if state != "CANCELING":
            break
        time.sleep(CANCEL_WAIT_SECONDS)
        state = apple.request("GET", f"/v1/reviewSubmissions/{sub_id}")["data"]["attributes"].get("state")
    return {"review_submission_id": sub_id, "state": state}


@_register_tool
def apple_resubmit_for_review(app_id: str) -> dict[str, Any]:
    """Resubmit a rejected version after replying to App Review or fixing it. External action.

    Same as "Update Review" on the version page followed by "Resubmit to App
    Review": marks the rejected items as resolved and submits the same review
    submission again. Replying in the Resolution Center alone keeps the version
    Rejected. Attach a new build first (apple_attach_build) if the fix needed one.
    """
    sub_id = _active_submission(app_id, ("UNRESOLVED_ISSUES",))
    items = apple.request("GET", f"/v1/reviewSubmissions/{sub_id}/items")["data"]
    resolved = []
    for item in items:
        if item["attributes"].get("state") == "REJECTED":
            apple.request("PATCH", f"/v1/reviewSubmissionItems/{item['id']}",
                          json={"data": {"type": "reviewSubmissionItems", "id": item["id"], "attributes": {"resolved": True}}})
            resolved.append(item["id"])
    result = apple.request("PATCH", f"/v1/reviewSubmissions/{sub_id}",
                           json={"data": {"type": "reviewSubmissions", "id": sub_id, "attributes": {"submitted": True}}})
    return {"review_submission_id": sub_id, "resolved_items": len(resolved),
            "state": result["data"]["attributes"].get("state")}


@_register_tool
def apple_review_messages(app_id: str) -> dict[str, Any]:
    """Read the review messages (Resolution Center) and the rejection reasons.

    The public API does not expose this: it reads through the console's internal
    API, in the dedicated Chrome session (check store_browser_session if it fails
    due to login).
    """
    messages = apple_review.read_messages(app_id)
    return {"app_id": app_id, "messages": messages, "total": len(messages)}


# Preview tokens already used to send a reply, so a repeated confirm cannot send twice.
_SENT_REPLY_TOKENS: set[str] = set()


def _reply_token(app_id: str, sub_id: str, text: str, files: list[Path]) -> str:
    """Hash of everything that would be sent: the submission, the exact text and each attachment's bytes."""
    digest = hashlib.sha256()
    for part in (app_id, sub_id, text):
        digest.update(part.encode())
        digest.update(b"\0")
    for path in files:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()[:16]


@_register_tool
def apple_reply_review(
    app_id: str, text: str, attachments: list[str] | None = None, confirm_token: str = ""
) -> dict[str, Any]:
    """Reply to App Review on a rejected submission, with attachments. External action.

    Two steps, because the reply goes to a person at Apple and cannot be edited
    or taken back:
    1. Call without confirm_token. Nothing is sent: it validates the text and
       attachments, converts videos, finds the rejected submission and returns a
       preview plus a `confirm_token` (a hash of exactly what would be sent).
    2. Show the preview to the user. Only after an explicit yes, call again with
       the same arguments and that confirm_token. If the text, an attachment or
       the submission changed since the preview, the token no longer matches and
       nothing is sent. A token can only send once.

    For a "Guideline 2.1 - Information Needed" request: send the answers in
    `text` (up to 4000 characters) and the video recorded on the iPhone in
    `attachments`. HEVC video is converted to H.264 at 1920px before upload. Copy
    the same information into the review notes with apple_set_review_details, as
    Apple asks. Replying is not enough: the version stays Rejected until you
    resubmit it ("Update Review" on the version page, then resubmit).
    """
    if not text.strip():
        raise ValueError("The reply is empty.")
    if len(text) > 4000:
        raise ValueError(f"The reply has {len(text)} characters; the console limit is 4000.")
    originals = []
    for item in attachments or []:
        path = Path(item).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"Attachment not found: {path}")
        originals.append(path)
    sub_id = _active_submission(app_id, ("UNRESOLVED_ISSUES",))
    token = _reply_token(app_id, sub_id, text, originals)
    if not confirm_token:
        files = [apple_review.prepare_attachment(path) for path in originals]
        return {
            "sent": False,
            "preview": {
                "review_submission_id": sub_id,
                "text": text,
                "characters": len(text),
                "attachments": [str(f) for f in files],
            },
            "confirm_token": token,
            "next_step": (
                "Nothing was sent. Show this preview to the user; only after an explicit yes, "
                "call apple_reply_review again with the same arguments and this confirm_token."
            ),
        }
    if confirm_token in _SENT_REPLY_TOKENS:
        raise ValueError("This reply was already sent with this confirm_token; nothing was sent again.")
    if confirm_token != token:
        raise ValueError(
            "confirm_token does not match: the text, an attachment or the submission changed since "
            "the preview. Nothing was sent. Call again without confirm_token for a new preview."
        )
    files = [apple_review.prepare_attachment(path) for path in originals]
    _SENT_REPLY_TOKENS.add(token)
    try:
        apple_review.reply(app_id, sub_id, text, files)
    except apple_review.ReplyNotSent:
        # Nothing reached Apple, so the same token may retry.
        _SENT_REPLY_TOKENS.discard(token)
        raise
    except Exception as exc:
        # Unknown outcome: keep the token burned so a retry cannot double-post.
        raise RuntimeError(
            f"{exc}\nThe reply may have reached Apple. Check with apple_review_messages "
            "before sending anything again."
        ) from exc
    return {
        "review_submission_id": sub_id,
        "sent": True,
        "attachments": [str(f) for f in files],
        "next_step": "Now resubmit with apple_resubmit_for_review: replying alone keeps the version Rejected.",
    }


@_register_tool
def apple_review_details(version_id: str) -> dict[str, Any]:
    """Read a version's App Review information (contact, demo account, notes).

    The demo account password is masked — the goal here is to check what Apple
    will see, not to extract credentials.
    """
    resposta = apple.request("GET", f"/v1/appStoreVersions/{version_id}/appStoreReviewDetail")
    dado = resposta.get("data")
    if not dado:
        return {"exists": False}

    atributos = dict(dado["attributes"])
    if atributos.get("demoAccountPassword"):
        atributos["demoAccountPassword"] = "<set>"
    return {"exists": True, "review_detail_id": dado["id"], **atributos}


@_register_tool
def apple_set_review_details(
    version_id: str,
    demo_account_name: str | None = None,
    demo_account_password: str | None = None,
    notes: str | None = None,
    contact_first_name: str | None = None,
    contact_last_name: str | None = None,
    contact_phone: str | None = None,
    contact_email: str | None = None,
) -> dict[str, Any]:
    """Fill in the information App Review reads before testing. External action.

    Only the fields passed are changed. When the app has content behind a login,
    the demo account must be able to reach what Apple wants to evaluate: in a
    subscription app, an account that already has everything unlocked hides the
    purchase screen, and review rejects the app for not being able to test it.

    Passing `demo_account_name` turns on `demoAccountRequired` automatically.
    """
    atributos = {
        chave: valor
        for chave, valor in {
            "demoAccountName": demo_account_name,
            "demoAccountPassword": demo_account_password,
            "notes": notes,
            "contactFirstName": contact_first_name,
            "contactLastName": contact_last_name,
            "contactPhone": contact_phone,
            "contactEmail": contact_email,
        }.items()
        if valor is not None
    }
    if not atributos:
        raise ValueError("No fields to update.")
    if demo_account_name is not None:
        atributos["demoAccountRequired"] = True

    existente = apple.request(
        "GET", f"/v1/appStoreVersions/{version_id}/appStoreReviewDetail"
    ).get("data")

    if existente:
        apple.request(
            "PATCH",
            f"/v1/appStoreReviewDetails/{existente['id']}",
            json={
                "data": {
                    "type": "appStoreReviewDetails",
                    "id": existente["id"],
                    "attributes": atributos,
                }
            },
        )
        return {"review_detail_id": existente["id"], "updated": sorted(atributos)}

    criado = apple.request(
        "POST",
        "/v1/appStoreReviewDetails",
        json={
            "data": {
                "type": "appStoreReviewDetails",
                "attributes": atributos,
                "relationships": {
                    "appStoreVersion": {
                        "data": {"type": "appStoreVersions", "id": version_id}
                    }
                },
            }
        },
    )
    return {"review_detail_id": criado["data"]["id"], "created": sorted(atributos)}


@_register_tool
def apple_attach_review_file(version_id: str, file_path: str) -> dict[str, Any]:
    """Attach a file (the iPhone video, a PDF) to the version's App Review information. External action.

    The preferred path for "2.1 Information Needed": it is pure API and does not
    depend on the App Store Connect web session, which expires often and needs a 2FA
    login. Sequence: answers in the notes with apple_set_review_details, the video
    here, then apple_resubmit_for_review. Use apple_reply_review (Resolution Center,
    through the browser) only if Apple insists on an answer there. HEVC video is
    converted to H.264 at 1920px before uploading.
    """
    path = Path(file_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Attachment not found: {path}")
    detail = apple.request(
        "GET", f"/v1/appStoreVersions/{version_id}/appStoreReviewDetail"
    ).get("data")
    if not detail:
        raise ValueError("The version has no App Review information yet: run apple_set_review_details first.")
    upload = apple_review.prepare_attachment(path)
    attachment_id = apple.upload_asset(
        "appStoreReviewAttachments",
        upload,
        {"appStoreReviewDetail": {"data": {"type": "appStoreReviewDetails", "id": detail["id"]}}},
    )
    return {"attachment_id": attachment_id, "file": upload.name, "review_detail_id": detail["id"]}


@_register_tool
def apple_set_version_string(version_id: str, version_string: str) -> dict[str, Any]:
    """Rename an editable version. External action.

    Useful to reuse a REJECTED version: while it occupies the slot, Apple refuses
    to create another one ("You cannot create a new version of the App in the
    current state"). Renaming it to the new build's version number avoids wiping
    the already filled-in listing.
    """
    resposta = apple.request(
        "PATCH",
        f"/v1/appStoreVersions/{version_id}",
        json={
            "data": {
                "type": "appStoreVersions",
                "id": version_id,
                "attributes": {"versionString": version_string},
            }
        },
    )
    return {
        "version_id": version_id,
        "version_string": resposta["data"]["attributes"]["versionString"],
        "state": resposta["data"]["attributes"]["appStoreState"],
    }


# ------------------------------------------------------- subscriptions (IAP)


@_register_tool
def apple_list_subscriptions(app_id: str) -> list[dict[str, Any]]:
    """List the app's subscription groups, with the products in each.

    `state` tells whether the product can go to review yet: MISSING_METADATA
    means a price, availability or localization is missing. Only READY_TO_SUBMIT
    can be included in a submission.
    """
    grupos = apple.request("GET", f"/v1/apps/{app_id}/subscriptionGroups")
    saida = []
    for grupo in grupos.get("data", []):
        assinaturas = apple.request(
            "GET", f"/v1/subscriptionGroups/{grupo['id']}/subscriptions"
        )
        saida.append(
            {
                "group_id": grupo["id"],
                "reference_name": grupo["attributes"].get("referenceName"),
                "subscriptions": [
                    {
                        "id": sub["id"],
                        "product_id": sub["attributes"].get("productId"),
                        "name": sub["attributes"].get("name"),
                        "period": sub["attributes"].get("subscriptionPeriod"),
                        "state": sub["attributes"].get("state"),
                    }
                    for sub in assinaturas.get("data", [])
                ],
            }
        )
    return saida


@_register_tool
def apple_create_subscription_group(
    app_id: str,
    reference_name: str,
    display_name: str,
    locale: str = "pt-BR",
) -> dict[str, Any]:
    """Create a subscription group and its localization. External action.

    `reference_name` is internal (only you see it); `display_name` is shown to
    customers on the App Store. Every subscription product lives inside a group,
    and free-trial eligibility is counted PER GROUP: someone who already had a
    trial does not get another one, even when switching products within the group.
    """
    grupo = apple.request(
        "POST",
        "/v1/subscriptionGroups",
        json={
            "data": {
                "type": "subscriptionGroups",
                "attributes": {"referenceName": reference_name},
                "relationships": {"app": {"data": {"type": "apps", "id": app_id}}},
            }
        },
    )
    group_id = grupo["data"]["id"]
    apple.request(
        "POST",
        "/v1/subscriptionGroupLocalizations",
        json={
            "data": {
                "type": "subscriptionGroupLocalizations",
                "attributes": {"locale": locale, "name": display_name},
                "relationships": {
                    "subscriptionGroup": {
                        "data": {"type": "subscriptionGroups", "id": group_id}
                    }
                },
            }
        },
    )
    return {"group_id": group_id, "reference_name": reference_name, "locale": locale}


@_register_tool
def apple_create_subscription(
    group_id: str,
    product_id: str,
    name: str,
    display_name: str,
    description: str,
    subscription_period: str = "ONE_MONTH",
    locale: str = "pt-BR",
) -> dict[str, Any]:
    """Create a subscription product inside a group. External action.

    `product_id` is IMMUTABLE and cannot be reused later — check it against what
    the app sends to the store before calling. `name` is the internal reference
    name; `display_name` and `description` are what customers read on the App Store.

    subscription_period: ONE_WEEK, ONE_MONTH, TWO_MONTHS, THREE_MONTHS,
    SIX_MONTHS or ONE_YEAR. The App Store has no installment plans.

    The product starts in MISSING_METADATA: it still needs a price
    (apple_set_subscription_price) and availability
    (apple_set_subscription_availability).
    """
    assinatura = apple.request(
        "POST",
        "/v1/subscriptions",
        json={
            "data": {
                "type": "subscriptions",
                "attributes": {
                    "name": name,
                    "productId": product_id,
                    "subscriptionPeriod": subscription_period,
                },
                "relationships": {
                    "group": {"data": {"type": "subscriptionGroups", "id": group_id}}
                },
            }
        },
    )
    subscription_id = assinatura["data"]["id"]
    apple.request(
        "POST",
        "/v1/subscriptionLocalizations",
        json={
            "data": {
                "type": "subscriptionLocalizations",
                "attributes": {
                    "locale": locale,
                    "name": display_name,
                    "description": description,
                },
                "relationships": {
                    "subscription": {"data": {"type": "subscriptions", "id": subscription_id}}
                },
            }
        },
    )
    return {
        "subscription_id": subscription_id,
        "product_id": product_id,
        "period": subscription_period,
        "state": assinatura["data"]["attributes"].get("state"),
    }


@_register_tool
def apple_list_price_points(
    subscription_id: str,
    territory: str = "BRA",
    around: float | None = None,
) -> list[dict[str, Any]]:
    """List the price points available for a subscription in a territory.

    Apple does not accept arbitrary prices: you pick a point from its price table.
    Pass `around` to see only the points near the price you want (e.g. 79.90).
    """
    # A territory's table can have hundreds of points and Apple returns at most 200
    # per page, in ascending order. Without paging, higher prices simply do not
    # show up (in Brazil, for example, the first page stops around R$ 50).
    itens: list[dict[str, Any]] = []
    offset: str | None = None
    while True:
        params: dict[str, Any] = {"filter[territory]": territory, "limit": 200}
        if offset:
            params["cursor"] = offset
        pagina = apple.request(
            "GET", f"/v1/subscriptions/{subscription_id}/pricePoints", params=params
        )
        itens.extend(
            {
                "price_point_id": ponto["id"],
                "customer_price": float(ponto["attributes"]["customerPrice"]),
                "proceeds": float(ponto["attributes"]["proceeds"]),
            }
            for ponto in pagina.get("data", [])
        )
        offset = pagina.get("meta", {}).get("paging", {}).get("nextCursor")
        if not offset:
            break
    if around is not None:
        itens.sort(key=lambda item: abs(item["customer_price"] - around))
        return itens[:8]
    return sorted(itens, key=lambda item: item["customer_price"])


@_register_tool
def apple_set_subscription_price(
    subscription_id: str, price_point_id: str, preserve_current_price: bool | None = None
) -> dict[str, Any]:
    """Set the subscription price from a price point. External action.

    Get the `price_point_id` from apple_list_price_points, and list it AFTER
    setting availability: price points are issued per territory.

    `preserve_current_price` keeps existing subscribers on their current price when
    the price goes up, and only applies to a price CHANGE. Sending this attribute
    on the initial price makes Apple answer 409 with "An error occurred while
    processing the pricing information" — that is why the default is None, which
    omits the field.
    """
    attributes: dict[str, Any] = {}
    if preserve_current_price is not None:
        attributes["preserveCurrentPrice"] = preserve_current_price

    preco = apple.request(
        "POST",
        "/v1/subscriptionPrices",
        json={
            "data": {
                "type": "subscriptionPrices",
                "attributes": attributes,
                "relationships": {
                    "subscription": {"data": {"type": "subscriptions", "id": subscription_id}},
                    "subscriptionPricePoint": {
                        "data": {"type": "subscriptionPricePoints", "id": price_point_id}
                    },
                },
            }
        },
    )
    return {"price_id": preco["data"]["id"], "subscription_id": subscription_id}


@_register_tool
def apple_set_subscription_availability(
    subscription_id: str,
    territories: list[str] | None = None,
    available_in_new_territories: bool = True,
) -> dict[str, Any]:
    """Set the countries where the subscription is sold. External action.

    Without this the product stays in MISSING_METADATA and cannot go to review.
    The default is Brazil only (BRA).
    """
    territories = territories or ["BRA"]
    disponibilidade = apple.request(
        "POST",
        "/v1/subscriptionAvailabilities",
        json={
            "data": {
                "type": "subscriptionAvailabilities",
                "attributes": {"availableInNewTerritories": available_in_new_territories},
                "relationships": {
                    "subscription": {"data": {"type": "subscriptions", "id": subscription_id}},
                    "availableTerritories": {
                        "data": [
                            {"type": "territories", "id": codigo} for codigo in territories
                        ]
                    },
                },
            }
        },
    )
    return {
        "availability_id": disponibilidade["data"]["id"],
        "territories": territories,
    }


@_register_tool
def apple_create_intro_offer(
    subscription_id: str,
    duration: str = "TWO_WEEKS",
    offer_mode: str = "FREE_TRIAL",
    number_of_periods: int = 1,
    territory: str = "BRA",
) -> dict[str, Any]:
    """Create the subscription's introductory offer (free trial). External action.

    duration: THREE_DAYS, ONE_WEEK, TWO_WEEKS, ONE_MONTH, TWO_MONTHS,
    THREE_MONTHS, SIX_MONTHS or ONE_YEAR. Apple has no "14 days" — TWO_WEEKS
    is the exact equivalent.

    offer_mode: FREE_TRIAL (free), PAY_AS_YOU_GO or PAY_UP_FRONT. The last two
    charge a lower price and require a price point, which this tool does not
    cover.

    `territory` is REQUIRED, one offer per country: Apple refuses to create the
    offer without it. To apply in several countries, call once per territory.
    """
    relationships: dict[str, Any] = {
        "subscription": {"data": {"type": "subscriptions", "id": subscription_id}},
        "territory": {"data": {"type": "territories", "id": territory}},
    }

    oferta = apple.request(
        "POST",
        "/v1/subscriptionIntroductoryOffers",
        json={
            "data": {
                "type": "subscriptionIntroductoryOffers",
                "attributes": {
                    "duration": duration,
                    "numberOfPeriods": number_of_periods,
                    "offerMode": offer_mode,
                },
                "relationships": relationships,
            }
        },
    )
    return {
        "offer_id": oferta["data"]["id"],
        "duration": duration,
        "offer_mode": offer_mode,
    }


@_register_tool
def apple_upload_subscription_screenshot(
    subscription_id: str, image_path: str
) -> dict[str, Any]:
    """Upload a subscription's App Review screenshot. External action.

    Every subscription needs a screenshot showing where the purchase happens in
    the app, otherwise it stays stuck in MISSING_METADATA and cannot be part of
    any submission — even with price, localization, availability and offer all
    set. Use the screen where the subscription is offered, not the home screen.

    App Store Connect uploads happen in three steps: reserve the asset, send the
    bytes to the URL it returns, and commit with the checksum.
    """
    caminho = Path(image_path).expanduser()
    if not caminho.exists():
        raise ValueError(f"File not found: {caminho}")
    conteudo = caminho.read_bytes()

    screenshot_id = apple.upload_asset(
        "subscriptionAppStoreReviewScreenshots",
        caminho,
        {"subscription": {"data": {"type": "subscriptions", "id": subscription_id}}},
    )
    return {"screenshot_id": screenshot_id, "file": caminho.name, "bytes": len(conteudo)}


SCREENSHOT_EXTENSOES = (".png", ".jpg", ".jpeg")
MAX_SCREENSHOTS_POR_SET = 10  # App Store limit per screenshot set


@_register_tool
def apple_upload_screenshots(
    version_id: str,
    folder: str,
    display_type: str = "APP_IPHONE_67",
    locale: str = "pt-BR",
    replace: bool = True,
) -> dict[str, Any]:
    """Upload the App Store listing screenshots, in order. External action.

    Uploads every image in the folder, in alphabetical order of file name, and pins
    that order in the store — without this the order falls back to creation order,
    which Apple does not guarantee. Name the files with a numeric prefix (01-, 02-).

    A screenshot belongs to a VERSION: a READY_FOR_SALE version cannot be changed.
    Create the next one with apple_create_version and point to it — the new
    version inherits the previous one's screenshots, and `replace` swaps them for
    these.

    display_type: APP_IPHONE_67, APP_IPHONE_65, APP_IPHONE_61,
    APP_IPAD_PRO_3GEN_129, among others.
    """
    pasta = Path(folder).expanduser()
    if not pasta.is_dir():
        raise ValueError(f"Folder not found: {pasta}")
    imagens = sorted(
        f for f in pasta.iterdir() if f.suffix.lower() in SCREENSHOT_EXTENSOES
    )
    if not imagens:
        raise ValueError(f"No images in {pasta}")

    localizations = apple.request(
        "GET", f"/v1/appStoreVersions/{version_id}/appStoreVersionLocalizations"
    )["data"]
    alvo = next((l for l in localizations if l["attributes"]["locale"] == locale), None)
    if alvo is None:
        disponiveis = ", ".join(l["attributes"]["locale"] for l in localizations)
        raise ValueError(f"Locale {locale} does not exist on this version. Available: {disponiveis}")

    conjuntos = apple.request(
        "GET", f"/v1/appStoreVersionLocalizations/{alvo['id']}/appScreenshotSets"
    )["data"]
    conjunto = next(
        (c for c in conjuntos if c["attributes"]["screenshotDisplayType"] == display_type),
        None,
    )
    if conjunto is None:
        conjunto = apple.request(
            "POST",
            "/v1/appScreenshotSets",
            json={
                "data": {
                    "type": "appScreenshotSets",
                    "attributes": {"screenshotDisplayType": display_type},
                    "relationships": {
                        "appStoreVersionLocalization": {
                            "data": {
                                "type": "appStoreVersionLocalizations",
                                "id": alvo["id"],
                            }
                        }
                    },
                }
            },
        )["data"]
    set_id = conjunto["id"]

    antigos = (
        apple.request("GET", f"/v1/appScreenshotSets/{set_id}/appScreenshots")["data"]
        if replace
        else []
    )

    # ASC has no transactional edit like Play: if the upload fails halfway through
    # a replace, whatever was deleted does not come back. So we upload first and
    # delete afterwards — unless the total would exceed the set limit, in which
    # case there is no choice but to make room first.
    apagar_antes = len(antigos) + len(imagens) > MAX_SCREENSHOTS_POR_SET

    def apagar() -> list[str]:
        nomes = []
        for antigo in antigos:
            apple.request("DELETE", f"/v1/appScreenshots/{antigo['id']}")
            nomes.append(antigo["attributes"].get("fileName"))
        return nomes

    removidos = apagar() if apagar_antes else []

    enviados = []
    for imagem in imagens:
        asset_id = apple.upload_asset(
            "appScreenshots",
            imagem,
            {"appScreenshotSet": {"data": {"type": "appScreenshotSets", "id": set_id}}},
        )
        enviados.append({"id": asset_id, "file": imagem.name})

    if not apagar_antes:
        removidos = apagar()

    apple.set_order(
        "appScreenshotSets", set_id, "appScreenshots", [e["id"] for e in enviados]
    )

    return {
        "set_id": set_id,
        "display_type": display_type,
        "locale": locale,
        "removed": removidos,
        "order": [e["file"] for e in enviados],
    }


# ---------------------------------------------------------------------- play


@_register_tool
def play_create_app_form(package_name: str, suggested_name: str) -> dict[str, Any]:
    """Open the Play Console app list in the browser to create a new app.

    The Google Play Developer API cannot create a new packageName — the initial
    registration must go through the console. This tool opens the page and returns
    what to fill in.
    """
    page = browser.open_page(browser.PLAY_NEW_APP_URL)
    return {
        "opened": browser.PLAY_NEW_APP_URL,
        "page": page,
        "fill_in": {
            "App name": suggested_name,
            "Default language": "Portuguese (Brazil)",
            "App or game": "App",
            "Free or paid": "confirm with the user",
        },
        "warning": (
            f"The package {package_name} is defined by the binary, not by the form. "
            "It only shows up in the Console after the first AAB upload."
        ),
    }


@_register_tool
def play_track_status(package_name: str) -> list[dict[str, Any]]:
    """List the app's Play tracks and the active releases on each.

    Read-only — opens and discards an edit without committing anything. Also useful
    to confirm that the service account has access to the app.
    """
    tracks = play.read_tracks(package_name)
    return [
        {
            "track": track["track"],
            "releases": [
                {
                    "name": release.get("name"),
                    "status": release.get("status"),
                    "version_codes": release.get("versionCodes", []),
                    "user_fraction": release.get("userFraction"),
                }
                for release in track.get("releases", [])
            ],
        }
        for track in tracks
    ]


@_register_tool
def play_upload_bundle(
    package_name: str,
    aab_path: str,
    track: str = "internal",
    status: str = "completed",
    release_name: str | None = None,
    release_notes_pt_br: str | None = None,
    release_notes: dict[str, str] | None = None,
) -> dict[str, Any]:
    """External action: upload an .aab and release it on a Play track in one edit.

    On commit, the release becomes visible to the track's testers
    (or to the public, if track=production). Confirm the track with the user.

    track: internal, alpha, beta or production.
    status: completed, draft, inProgress or halted.
    release_notes: locale-to-text mapping (e.g. {"en-US": "Bug fixes", "es-419": "Correcciones"}).
        Each note can contain up to 500 Unicode characters.
    release_notes_pt_br: legacy Brazilian Portuguese note, used if the map has no pt-BR entry.
    """
    path = Path(aab_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    notes = dict(release_notes) if release_notes is not None else {}
    if release_notes_pt_br:
        notes.setdefault("pt-BR", release_notes_pt_br)
    for locale, note in notes.items():
        if len(note) > 500:
            raise ValueError(f"Release notes for {locale!r} exceed 500 characters.")

    result: dict[str, Any] = {}
    with play.edit_session(package_name) as (edit_id, headers):
        bundle = play.upload_bundle(package_name, path, edit_id, headers)
        version_code = int(bundle["versionCode"])
        play.assign_track(
            package_name,
            track,
            [version_code],
            edit_id,
            headers,
            status=status,
            release_name=release_name,
            release_notes=notes or None,
        )
        result = {"version_code": version_code, "track": track, "status": status}

    result["committed"] = True
    return result


@_register_tool
def play_upload_screenshots(
    package_name: str,
    folder: str,
    image_type: str = "phoneScreenshots",
    language: str = "pt-BR",
    replace: bool = True,
) -> dict[str, Any]:
    """Upload the Play Store listing screenshots, in order. External action.

    The commit publishes to the listing immediately — it does not depend on a
    release or on review. Store order is upload order, so images go in alphabetical
    order of file name: name them with a numeric prefix (01-, 02-).

    image_type: phoneScreenshots, sevenInchScreenshots, tenInchScreenshots,
    tvScreenshots or wearScreenshots. Each size is a separate set — call once
    per folder.
    """
    if image_type not in play.IMAGE_TYPES:
        raise ValueError(f"Invalid image_type. Use one of: {', '.join(play.IMAGE_TYPES)}")
    pasta = Path(folder).expanduser()
    if not pasta.is_dir():
        raise ValueError(f"Folder not found: {pasta}")
    imagens = sorted(
        f for f in pasta.iterdir() if f.suffix.lower() in SCREENSHOT_EXTENSOES
    )
    if not imagens:
        raise ValueError(f"No images in {pasta}")

    with play.edit_session(package_name) as (edit_id, headers):
        if replace:
            play.delete_images(package_name, language, image_type, edit_id, headers)
        for imagem in imagens:
            play.upload_image(
                package_name, language, image_type, imagem, edit_id, headers
            )

    return {
        "package_name": package_name,
        "image_type": image_type,
        "language": language,
        "replaced": replace,
        "order": [i.name for i in imagens],
    }


@_register_tool
def play_list_screenshots(
    package_name: str, image_type: str = "phoneScreenshots", language: str = "pt-BR"
) -> dict[str, Any]:
    """List the published screenshots of one listing image type. Read-only.

    Returns the sha256 of each image, in the order they appear in the store — you
    can compare them with local files to confirm what is published.
    """
    imagens = play.list_images(package_name, language, image_type)
    return {
        "image_type": image_type,
        "language": language,
        "total": len(imagens),
        "images": [
            {"position": i, "id": im.get("id"), "sha256": im.get("sha256"), "url": im.get("url")}
            for i, im in enumerate(imagens, 1)
        ],
    }


@_register_tool
def play_promote_release(
    package_name: str,
    version_code: int,
    track: str,
    status: str = "completed",
    user_fraction: float | None = None,
    release_notes_pt_br: str | None = None,
) -> dict[str, Any]:
    """Promote an already uploaded versionCode to another track, without a new upload.

    External action: promoting to production publishes the app. Confirm with the user.
    For a staged rollout use status=inProgress with user_fraction (e.g. 0.1 = 10%).
    """
    with play.edit_session(package_name) as (edit_id, headers):
        play.assign_track(
            package_name,
            track,
            [version_code],
            edit_id,
            headers,
            status=status,
            user_fraction=user_fraction,
            release_notes={"pt-BR": release_notes_pt_br} if release_notes_pt_br else None,
        )
    return {
        "package_name": package_name,
        "version_code": version_code,
        "track": track,
        "status": status,
        "user_fraction": user_fraction,
        "committed": True,
    }


@_register_tool
def play_update_listing(
    package_name: str,
    language: str = "pt-BR",
    title: str | None = None,
    short_description: str | None = None,
    full_description: str | None = None,
) -> dict[str, Any]:
    """Update the Play Store listing texts, for one language.

    External action: the commit publishes the texts to the store. Google's limits —
    title 30 characters, short description 80, full description 4000.
    """

    fields = {
        key: value
        for key, value in {
            "title": title,
            "shortDescription": short_description,
            "fullDescription": full_description,
        }.items()
        if value is not None
    }
    if not fields:
        raise ValueError("No fields to update.")

    with play.edit_session(package_name) as (edit_id, headers):
        url = (
            f"{play.BASE_URL}/applications/{package_name}"
            f"/edits/{edit_id}/listings/{language}"
        )
        # The API replaces the whole listing on PUT, so we read the current state
        # and overlay only the requested fields.
        current = httpx.get(url, headers=headers, timeout=60)
        existing = current.json() if current.status_code == 200 else {}
        payload = {**existing, **fields, "language": language}
        play._check(  # noqa: SLF001
            httpx.put(url, headers=headers, json=payload, timeout=60),
            f"edits.listings.update({language})",
        )
    return {"package_name": package_name, "language": language, "updated": list(fields)}


@_register_tool
def play_signing_sha1(package_name: str, download_dir: str) -> dict[str, Any]:
    """Download Play's app signing certificates and return the SHA-1 of each.

    Use when "Sign in with Google" works on a local build but fails with the store
    APK — symptom: the account picker opens, the user picks an account and lands
    back on the login screen, with no error. With Play App Signing the distributed
    APK is re-signed by Google, and the SHA-1 that reaches the device is the one
    from `deployment_cert.der`.

    The SHA-1 shown as text on the "App signing" page is the UPLOAD key's and does
    not work. This is the value to register as an Android OAuth client
    (package + SHA-1) in the Google Cloud Console — there is no API for that, UI only.
    """
    dev_id, app_id = _console_ids(package_name)
    destino = Path(download_dir).expanduser()
    destino.mkdir(parents=True, exist_ok=True)
    antes = set(destino.iterdir())

    play_console.run(
        dev_id,
        app_id,
        f'cdp("Browser.setDownloadBehavior", behavior="allowAndName", '
        f'downloadPath={str(destino)!r}, eventsEnabled=True)\n'
        "time.sleep(1)\n"
        f'goto_url("{play_console.CONSOLE}/developers/{dev_id}/app/{app_id}/keymanagement")\n'
        "wait_for_load()\ntime.sleep(9)\n"
        # "Baixar certificados" ("Download certificates") is console UI text, matched literally (pt-BR)
        'print(real_click("""Array.from(document.querySelectorAll(\'a,button\'))'
        '.find(e=>/Baixar certificados/.test(e.innerText||\'\'))""", pause=10))\n',
    )

    novos = [p for p in destino.iterdir() if p not in antes and p.is_file()]
    if not novos:
        raise RuntimeError("The download produced no file — check that the console opened logged in.")
    baixado = max(novos, key=lambda p: p.stat().st_mtime)
    zip_path = destino / "play-signing-certs.zip"
    baixado.rename(zip_path)

    impressoes = signing.fingerprints_from_zip(zip_path, destino / "certs")
    return {
        "package_name": package_name,
        "sha1_for_oauth_client": impressoes[signing.DEPLOYMENT_CERT],
        "all": impressoes,
        "where_to_register": (
            "Google Cloud Console -> APIs & Services -> Credentials -> Create "
            "credentials -> OAuth client ID -> Android. There is no API to create an "
            "OAuth client. Do not delete the existing debug client."
        ),
    }


@_register_tool
def play_contact_details(package_name: str) -> dict[str, Any]:
    """Read the public contact details of the app's Play listing.

    These fields (contactEmail, contactWebsite, contactPhone) ARE covered by the API —
    do not automate the console form for them.
    """
    return play.read_details(package_name)


@_register_tool
def play_set_contact_details(
    package_name: str,
    contact_email: str | None = None,
    contact_website: str | None = None,
    contact_phone: str | None = None,
) -> dict[str, Any]:
    """Update the public contact details of the app's Play listing and commit.

    External action: these details are shown to users on Google Play. Double-check
    that the e-mail and website belong to the publishing entity (see
    store_audit_identity).
    """
    fields = {
        key: value
        for key, value in {
            "contactEmail": contact_email,
            "contactWebsite": contact_website,
            "contactPhone": contact_phone,
        }.items()
        if value is not None
    }
    if not fields:
        raise ValueError("No fields to update.")
    return {"package_name": package_name, "updated": play.patch_details(package_name, fields)}


@_register_tool
def store_audit_identity(pattern: str = r"example\.com|example corp") -> dict[str, Any]:
    """Scan both stores for text that should not be there.

    Built to catch identity leaks — the wrong company's e-mail or name in a public
    listing, review notes or contact details. `pattern` is a case-insensitive regex;
    pass the domains/names that must not appear (the default is only a placeholder).
    Returns what matched and where.
    """
    import re

    regex = re.compile(pattern, re.I)
    achados: list[dict[str, str]] = []

    for item in apple.request("GET", "/v1/apps", params={"limit": 200}).get("data", []):
        app_id, nome = item["id"], item["attributes"]["name"]
        versions = apple.request(
            "GET", f"/v1/apps/{app_id}/appStoreVersions", params={"limit": 3}
        ).get("data", [])
        for version in versions:
            for path, rotulo in (
                (f"/v1/appStoreVersions/{version['id']}/appStoreReviewDetail", "review"),
                (f"/v1/appStoreVersions/{version['id']}/appStoreVersionLocalizations", "listing"),
            ):
                try:
                    data = apple.request("GET", path).get("data")
                except Exception:  # noqa: BLE001 — the resource may not exist on this version
                    continue
                for entry in data if isinstance(data, list) else [data]:
                    for campo, valor in (entry or {}).get("attributes", {}).items():
                        if isinstance(valor, str) and regex.search(valor):
                            achados.append(
                                {"store": "apple", "app": nome, "where": f"{rotulo}.{campo}",
                                 "value": valor[:120]}
                            )

    for package_name in play_console_app_ids():
        try:
            detalhes = play.read_details(package_name)
        except Exception:  # noqa: BLE001 — the app may not be accessible
            continue
        for campo, valor in detalhes.items():
            if isinstance(valor, str) and regex.search(valor):
                achados.append(
                    {"store": "play", "app": package_name, "where": f"details.{campo}",
                     "value": valor[:120]}
                )

    return {"pattern": pattern, "count": len(achados), "matches": achados}


# ------------------------------------------------------- play console (forms)


def _console_ids(package_name: str) -> tuple[str, str]:
    known = play_console_app_ids()
    app_id = known.get(package_name)
    if not app_id:
        raise ValueError(
            f"Unknown Play Console app id for {package_name}. Known: {list(known)}. "
            "Copy the number after /app/ in the console URL into [play.console_app_ids] in config.toml."
        )
    return play_developer_id(), app_id


@_register_tool
def play_submission_status(package_name: str) -> dict[str, Any]:
    """Report whether there are changes waiting to be sent, in review, or nothing pending.

    Reads the "Publishing overview" page. "Changes in review" means the changes were
    already sent to Google; "Send N changes for review" means they are still waiting
    to be sent.
    """
    dev_id, app_id = _console_ids(package_name)
    out = play_console.run(
        dev_id,
        app_id,
        f'goto_url("{play_console.CONSOLE}/developers/{dev_id}/app/{app_id}/publishing")\n'
        "wait_for_load()\ntime.sleep(10)\n"
        "t = body()\n"
        "import re\n"
        # Console UI text, matched literally (pt-BR): "Send N changes for review",
        # "Changes in review", "Running checks"
        "m = re.search(r'Enviar (\\d+) mudanças para revisão', t)\n"
        "print('PENDING=' + (m.group(1) if m else '0'))\n"
        "print('IN_REVIEW=' + str('Alterações em análise' in t))\n"
        "print('CHECKING=' + str('Executando verificações' in t))\n",
    )
    dados = dict(
        linha.split("=", 1)
        for linha in out.strip().splitlines()
        if "=" in linha and linha.split("=")[0] in ("PENDING", "IN_REVIEW", "CHECKING")
    )
    return {
        "package_name": package_name,
        "pending_changes": int(dados.get("PENDING", 0)),
        "in_review": dados.get("IN_REVIEW") == "True",
        "running_checks": dados.get("CHECKING") == "True",
    }


@_register_tool
def play_submit_for_review(package_name: str) -> dict[str, Any]:
    """Send the app's pending changes to Google for review.

    External action. On a new app this does NOT publish: "Send for review" and
    "Publish" are separate steps, and the second one remains yours. On an already
    published app with managed publishing turned off, approval publishes
    automatically — confirm before using it in that case.

    Before calling, the release must be confirmed (Production -> Releases ->
    Edit release -> Next -> Save). Use play_submission_status to check.
    """
    dev_id, app_id = _console_ids(package_name)
    out = play_console.run(
        dev_id,
        app_id,
        f'goto_url("{play_console.CONSOLE}/developers/{dev_id}/app/{app_id}/publishing")\n'
        "wait_for_load()\ntime.sleep(10)\n"
        # the button sits under a transparent modal; real_click probes several points.
        # "Enviar N mudanças para revisão" ("Send N changes for review") is console
        # UI text, matched literally (pt-BR)
        'print(real_click("""Array.from(document.querySelectorAll(\'button\'))'
        '.find(b=>/Enviar \\\\d+ mudanças para revisão/.test(b.innerText||\'\'))""", pause=7))\n'
        # confirmation dialog
        'print(real_click("""(() => {const d=document.querySelector(\'[role=dialog],.cdk-overlay-pane\');'
        'return d ? Array.from(d.querySelectorAll(\'button\'))'
        '.find(b=>/Enviar mudanças para revisão/.test(b.innerText||\'\')) : null;})()""", pause=12))\n'
        "t = body()\n"
        "print('IN_REVIEW=' + str('Alterações em análise' in t))\n",
        timeout=420,
    )
    enviado = "IN_REVIEW=True" in out
    return {
        "package_name": package_name,
        "sent": enviado,
        "result": out.strip()[-500:],
        "note": (
            "Review takes up to 7 days (longer the first time, when the account is reviewed too). "
            "The app only goes live once you run 'Publish app on Google Play'."
        ),
    }


@_register_tool
def play_content_status(package_name: str) -> dict[str, Any]:
    """Tell which "App content" declarations are pending, or that everything is up to date.

    Reads the App content page (/app-content/overview), not the release checklist on
    the dashboard. Its "Needs attention (N)" tab lists each pending declaration with a
    "Start declaration" or "Edit declaration" button; when nothing is missing, the page
    says it is all up to date and the pending block disappears.

    Returns `up_to_date`, `pending` (the N on the tab) and `pending_declarations` (each
    one's name as the console shows it). A draft app on Play only leaves draft once all
    of them are complete, and none of them has an API endpoint. Console labels are
    matched in pt-BR for now.
    """
    dev_id, app_id = _console_ids(package_name)
    index = f"{play_console.CONSOLE}/developers/{dev_id}/app/{app_id}/app-content/overview"
    out = play_console.run(
        dev_id,
        app_id,
        f"goto_url({index!r})\n"
        "wait_for_load()\ntime.sleep(10)\n"
        "t = body()\n"
        "tab, names = None, []\n"
        "for n in cdp('Accessibility.getFullAXTree').get('nodes', []):\n"
        "    if n.get('ignored'):\n"
        "        continue\n"
        "    role, name = (n.get('role') or {}).get('value'), _ax_name(n)\n"
        # "Precisam de atenção (N)" ("Needs attention (N)") is console UI text (pt-BR)
        "    if role == 'tab' and re.match(r'^Precisa', name):\n"
        "        tab = name\n"
        # Each pending one has a button "Iniciar/Editar a declaração de <name>"
        # ("Start/Edit the <name> declaration")
        "    m = re.match(r'^(?:Iniciar|Editar|Atualizar|Revisar) a declaração de (.+)$', name)\n"
        "    if role == 'button' and m and m.group(1).strip() not in names:\n"
        "        names.append(m.group(1).strip())\n"
        "n_tab = re.search(r'\\((\\d+)\\)', tab or '')\n"
        # "Tudo em dia" ("All up to date") is console UI text (pt-BR)
        "print(json.dumps({'url': page_info()['url'], 'tab': tab,\n"
        "    'n_tab': int(n_tab.group(1)) if n_tab else None,\n"
        "    'up_to_date': 'Tudo em dia' in t, 'names': names}, ensure_ascii=False))\n",
    )
    data = play_console.parse_json_tail(out)
    if not isinstance(data, dict) or "/app-content/overview" not in data.get("url", ""):
        raise RuntimeError(
            f"Could not read the App content page ({index}). Output:\n{out.strip()[-800:]}"
        )
    names = data["names"]
    pending = data["n_tab"] if data["n_tab"] is not None else len(names)
    up_to_date = data["up_to_date"] and not names
    if not up_to_date and not pending:
        raise RuntimeError(
            "The App content page opened but showed neither 'up to date' nor pending "
            f"declarations (tab: {data['tab']!r}). The console may not have finished "
            "loading (try again), or it is not in pt-BR, the only language matched so far."
        )
    return {
        "package_name": package_name,
        "up_to_date": up_to_date,
        "pending": 0 if up_to_date else pending,
        "pending_declarations": names,
        "index": index,
    }


@_register_tool
def play_content_open(package_name: str, form: str) -> dict[str, Any]:
    """Open an "App content" declaration in the browser and return its text.

    Use it to see the questions and options before answering. URL slugs do not
    follow the form name (e.g. "financial" lives at /finance, "app_access" at
    /testing-credentials) — this mapping is already resolved.

    form: privacy_policy, ads, app_access, government, financial, health,
    data_safety, content_rating.
    """
    dev_id, app_id = _console_ids(package_name)
    text = play_console.open_form(dev_id, app_id, form)
    return {"form": form, "url": play_console.form_url(dev_id, app_id, form), "page": text[-3000:]}


@_register_tool
def play_content_options(package_name: str) -> list[dict[str, Any]]:
    """List the controls (radios/checkboxes) of the open declaration and the state of each.

    Reads from the accessibility tree, not the DOM — Angular Material controls are
    not usable via querySelector. Call play_content_open first.
    """
    dev_id, app_id = _console_ids(package_name)
    out = play_console.run(
        dev_id, app_id, "print(json.dumps(ax_controls(), ensure_ascii=False))\n"
    )
    return play_console.parse_json_tail(out) or []


@_register_tool
def play_content_answer(
    package_name: str, option: str, nth: int = 0, save_after: bool = False
) -> dict[str, Any]:
    """Select an option by its text in the open declaration and confirm it took effect.

    Material's <input> sits ~15px above the visible circle, so a click at the
    center toggles nothing. This tool tries several offsets and only reports
    success when the accessibility tree confirms the state.

    option: the option's label as shown in the console UI (pt-BR, e.g. "Não").
    nth: when the same text appears several times (e.g. several "Não"), which
    occurrence to select, starting at 0.
    """
    dev_id, app_id = _console_ids(package_name)
    body = f"print(pick({option!r}, nth={nth}))\n"
    if save_after:
        # "Salvar" ("Save") is console UI text, matched literally (pt-BR)
        body += 'print(ax_click(r"^Salvar$", pause=7))\nprint(dismiss_dialog())\n'
    out = play_console.run(dev_id, app_id, body)
    return {"option": option, "nth": nth, "result": out.strip()[-600:]}


@_register_tool
def play_content_save(package_name: str, button: str = "Salvar") -> dict[str, Any]:
    """Save the open declaration and dismiss the dialog the console opens afterwards.

    button: the console button label, matched literally against the pt-BR UI:
    "Salvar" (Save), "Avançar" (Next, in multi-step wizards) or
    "Salvar como rascunho" (Save as draft).
    """
    dev_id, app_id = _console_ids(package_name)
    out = play_console.run(
        dev_id,
        app_id,
        f'print(ax_click(r"^{button}$", pause=7))\nprint(dismiss_dialog())\n',
    )
    return {"button": button, "result": out.strip()[-500:]}


@_register_tool
def play_data_safety_export(package_name: str, destination_dir: str) -> dict[str, Any]:
    """Download the Data safety declaration CSV template.

    The declaration is a 5-step wizard with hundreds of checkboxes, but it accepts
    CSV import — much faster and more reliable than clicking screen by screen.
    The download requires Browser.setDownloadBehavior (Page. does not work) and the
    file arrives with a GUID name, which this tool renames to datasafety-template.csv.
    """
    dev_id, app_id = _console_ids(package_name)
    target = Path(destination_dir).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    antes = set(target.iterdir())

    play_console.run(
        dev_id,
        app_id,
        f'cdp("Browser.setDownloadBehavior", behavior="allowAndName", '
        f'downloadPath={str(target)!r}, eventsEnabled=True)\n'
        "time.sleep(1)\n"
        f'goto_url("{play_console.CONSOLE}/developers/{dev_id}/app/{app_id}'
        '/app-content/data-privacy-security")\n'
        "wait_for_load()\ntime.sleep(9)\n"
        # "Exportar para .csv" ("Export to .csv") is console UI text, matched literally (pt-BR)
        'print(ax_click(r"Exportar para .csv", pause=12))\n',
    )

    novos = [p for p in target.iterdir() if p not in antes and p.is_file()]
    if not novos:
        raise RuntimeError("The export produced no file. Check that the console opened logged in.")
    baixado = max(novos, key=lambda p: p.stat().st_mtime)
    final = target / "datasafety-template.csv"
    baixado.rename(final)
    return {"template": str(final), "rows": sum(1 for _ in final.open()) - 1}


@_register_tool
def play_data_safety_fill(
    template_path: str,
    destination_path: str,
    collected_data: dict[str, Any],
    account_deletion_url: str | None = None,
    encrypted_in_transit: bool = True,
    supports_deletion: bool = True,
    account_creation: str | list[str] | None = None,
    account_creation_other: str | None = None,
    external_accounts: bool | None = None,
    external_account_types: list[str] | None = None,
    external_account_description: str | None = None,
) -> dict[str, Any]:
    """Fill in the Data safety CSV and return a summary of what was declared.

    Imports nothing — it generates the file for review. An incorrect declaration here
    can take down a published app, so review the summary with the user before
    play_data_safety_import.

    collected_data maps the Response ID of each collected data type (e.g. "PSL_EMAIL") to
    {"group": "PSL_DATA_TYPES_PERSONAL", "shared": false, "optional": false,
     "purposes": ["PSL_APP_FUNCTIONALITY", "PSL_ACCOUNT_MANAGEMENT"]}.

    Accounts (the questions at the top of the template):
    - account_creation: how users create an account IN the app. A list with
      "password" (username and password), "other_auth" (username and other
      authentication), "password_and_other_auth", "oauth", "other" (needs
      account_creation_other), or "none" (the app does not allow creating accounts;
      alone). Response IDs (PSL_ACM_*) are accepted too. Omitted = ["password",
      "oauth"]. Methods not chosen are written empty. With "none",
      account_deletion_url only goes to the data deletion URL.
    - external_accounts: can users log in with an account created OUTSIDE the app
      (e.g. the company registers the customer)? Omitted = the question is left as
      is. With True, pass external_account_types: "outside_app_id" (SIM card, service
      subscription), "enterprise" or "other" (needs external_account_description).

    Example (accounts created by the provider, no sign-up in the app):
      account_creation="none", external_accounts=True,
      external_account_types=["other"],
      external_account_description="Accounts are created by the provider for customers under contract."
    """
    responses = data_safety.build_responses(
        collected_data,
        encrypted_in_transit=encrypted_in_transit,
        supports_deletion=supports_deletion,
        account_deletion_url=account_deletion_url,
        account_methods=(
            data_safety.DEFAULT_ACCOUNT_METHODS if account_creation is None else account_creation
        ),
        account_creation_other=account_creation_other,
        external_accounts=external_accounts,
        external_account_types=external_account_types or (),
        external_account_description=external_account_description,
    )
    result = data_safety.fill_csv(
        Path(template_path).expanduser(), Path(destination_path).expanduser(), responses
    )
    result["declared"] = data_safety.summarize(Path(destination_path).expanduser())
    return result


# Data safety CSV import, as walked through on a real app. Console labels are pt-BR,
# matched literally (English in parentheses):
#   1. "Importar de arquivo CSV" (Import from CSV file) opens dialog 1 (upload), with
#      "Importar"/"Descartar" (Import/Discard);
#   2. the file goes in through input[type=file] and the dialog says
#      "O upload do arquivo CSV foi concluído" (The CSV file upload is complete);
#   3. "Importar" in dialog 1 opens dialog 2, "Importar de arquivo CSV?" (Import from CSV
#      file?), with "Cancelar"/"Importar". Only its "Importar" applies the CSV.
#      ax_click(index=1) does not reach that button; real_click on the button of the LAST
#      dialog does;
#   4. once applied, the dialogs close and the wizard jumps to step 2. "Salvar" (Save) only
#      exists on step 5, so it is "Avançar" (Next) until there and then "Salvar"; the
#      console answers "A mudança foi salva" (The change was saved).
# Uses __PLACEHOLDER__ because the body is JS full of braces.
_DS_IMPORT_SCRIPT = r"""
URL, CSV, DRY_RUN = __URL__, __CSV__, __DRY_RUN__
res = {'upload_ok': False, 'confirm_dialog': False, 'imported': False,
       'steps': [], 'errors': [], 'saved': False}
# Visible dialogs only: after the second "Importar" the upload dialog stays in the DOM
# with zero height and visibility hidden, and counting it made the import look refused.
VISIBLE = ("Array.from(document.querySelectorAll('[role=dialog]'))"
           ".filter(d=>d.getBoundingClientRect().height>0&&getComputedStyle(d).visibility!=='hidden')")
DIALOGS = "JSON.stringify(" + VISIBLE + ".map(d=>d.innerText))"
LAST_BUTTON = ("(()=>{const ds=" + VISIBLE + ";"
               "const d=ds[ds.length-1]; if(!d) return null;"
               "return Array.from(d.querySelectorAll('button')).find(b=>b.innerText.trim()==='%s')})()")

def dialogs():
    return json.loads(js(DIALOGS) or '[]')

def wait_until(cond, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(1)
    return False

def step():
    m = re.search(r'Etapa (\d) de 5', body())
    return int(m.group(1)) if m else None

def visible_errors():
    return json.loads(js("JSON.stringify(Array.from(document.querySelectorAll('mat-error,[role=alert]'))"
                         ".map(e=>e.innerText.trim()).filter(Boolean).slice(0,10))") or '[]')

def finish(reason=None):
    if reason:
        res['stopped_at'] = reason
    print('RESULT=' + json.dumps(res, ensure_ascii=False))

def import_csv():
    goto_url(URL)
    wait_for_load()
    time.sleep(9)
    res['open'] = ax_click(r'^Importar de arquivo CSV$', pause=4)
    if not wait_until(lambda: any('Para importar as respostas' in d for d in dialogs()), 15):
        return finish('the upload dialog did not open')
    root = cdp('DOM.getDocument')['root']['nodeId']
    target = cdp('DOM.querySelector', nodeId=root, selector='input[type=file]')['nodeId']
    if not target:
        return finish('input[type=file] not found in the upload dialog')
    cdp('DOM.setFileInputFiles', files=[CSV], nodeId=target)
    if not wait_until(lambda: any('upload do arquivo CSV foi concluído' in d for d in dialogs()), 30):
        res['dialogs'] = [d[:600] for d in dialogs()]
        return finish('the CSV upload did not complete (file refused?)')
    res['upload_ok'] = True

    # dialog 1 -> "Importar" opens confirmation dialog 2
    res['import_1'] = real_click(LAST_BUTTON % 'Importar', pause=3)
    if not wait_until(lambda: any(d.startswith('Importar de arquivo CSV?') for d in dialogs()), 15):
        res['dialogs'] = [d[:600] for d in dialogs()]
        return finish('the "Importar de arquivo CSV?" confirmation dialog did not appear')
    res['confirm_dialog'] = True

    if DRY_RUN:
        res['cancel'] = real_click(LAST_BUTTON % 'Cancelar', pause=3)
        res['remaining_dialogs'] = len(dialogs())
        return finish('dry_run: confirmation canceled, nothing applied')

    res['import_2'] = real_click(LAST_BUTTON % 'Importar', pause=3)
    # Applied, both dialogs close (and the wizard may jump to step 2)
    if not wait_until(lambda: not dialogs(), 20):
        res['dialogs'] = [d[:600] for d in dialogs()]
        return finish('the CSV was not applied: the confirmation dialog is still open')
    res['imported'] = True

    current = step()
    res['steps'].append(current)
    while current is not None and current < 5:
        ax_click(r'^Avançar$', pause=5)
        new = step()
        res['steps'].append(new)
        if new is None or new <= current:
            res['errors'] = visible_errors()
            return finish('the wizard did not move past step ' + str(current) + ' (an answer missing from the CSV?)')
        current = new
    if current != 5:
        return finish('could not find step 5 (Preview) to save')
    res['save'] = ax_click(r'^Salvar$', pause=8)
    res['saved'] = wait_until(lambda: 'A mudança foi salva' in body(), 15)
    res['errors'] = visible_errors()
    return finish(None if res['saved'] else 'clicked Save but the console did not confirm')

import_csv()
"""


@_register_tool
def play_data_safety_import(
    package_name: str, csv_path: str, dry_run: bool = False
) -> dict[str, Any]:
    """Import the filled-in CSV into the Data safety declaration and save it.

    External action: replaces the app's entire declaration. Review the summary from
    play_data_safety_fill with the user before calling.

    The console asks for two confirmations: the upload dialog ("Import") and then
    "Import from CSV file?" ("Import" again). Only the second one applies the file.
    Then the wizard is walked with "Next" up to the Preview, where "Save" stores it.
    `imported` is True only when both dialogs closed after the second "Import";
    `saved` is True only when the console answers that the change was saved. If a step
    does not advance (an answer missing), it stops without saving and returns the errors.

    dry_run=True goes as far as the "Import from CSV file?" dialog and clicks "Cancel":
    it checks that the console accepts the file without applying or saving anything.
    Console labels are matched in pt-BR.
    """
    dev_id, app_id = _console_ids(package_name)
    path = Path(csv_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"CSV not found: {path}")
    url = play_console.form_url(dev_id, app_id, "data_safety")
    out = play_console.run(
        dev_id,
        app_id,
        _DS_IMPORT_SCRIPT.replace("__URL__", repr(url))
        .replace("__CSV__", repr(str(path)))
        .replace("__DRY_RUN__", repr(dry_run)),
        timeout=420,
    )
    line = next(
        (ln for ln in reversed(out.strip().splitlines()) if ln.startswith("RESULT=")), None
    )
    if not line:
        raise RuntimeError(f"The import returned no result. Output:\n{out.strip()[-1500:]}")
    result = json.loads(line.removeprefix("RESULT="))
    result["csv"] = str(path)
    result["dry_run"] = dry_run
    if not dry_run:
        result["ok"] = result["imported"] and result["saved"]
    return result


# ----------------------------------------------------------------------- eas


@_register_tool
def eas_build_list(
    project_path: str,
    platform: str | None = None,
    status: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """List an Expo project's EAS builds, with the artifact URL.

    Use this to find out whether a ready binary already exists before spending a new
    build. A present `artifact_url` means it can be published directly.

    platform: ios or android. status: finished, in-queue, in-progress, errored.
    """
    args = ["build:list", "--json", "--limit", str(limit)]
    if platform:
        args += ["--platform", platform]
    if status:
        args += ["--status", status]
    builds = eas.run(args, project_path, timeout=180)
    return [eas.summarize(build) for build in builds]


@_register_tool
def eas_build_start(project_path: str, platform: str, profile: str = "production") -> dict[str, Any]:
    """Start an EAS build and return immediately, without waiting for it to finish.

    Builds take 10 to 40 minutes. This tool does NOT block — track progress with
    eas_build_status using the returned build_id.

    platform: ios, android or all. profile: a profile defined in eas.json.
    If the Free plan's monthly builds run out, the error points to eas_build_local.
    """
    try:
        result = eas.run(
            ["build", "--platform", platform, "--profile", profile, "--json", "--no-wait"],
            project_path,
            timeout=eas.BUILD_START_TIMEOUT,
        )
    except eas.EasError as exc:
        if eas.FREE_PLAN_EXHAUSTED in str(exc).lower():
            raise eas.EasError(
                f"{exc}\n\nThe account used up this month's Free plan builds. Build on this machine with "
                f"eas_build_local(project_path, platform={platform!r}, profile={profile!r})."
            ) from exc
        raise
    builds = result if isinstance(result, list) else [result]
    return {
        "started": [eas.summarize(build) for build in builds],
        "next_step": "Track it with eas_build_status(build_id).",
    }


@_register_tool
def eas_build_local(
    project_path: str, platform: str = "ios", profile: str = "production", output: str | None = None
) -> dict[str, Any]:
    """Build on this machine with `eas build --local`, in the background, without using EAS quota.

    The path for when the account runs out of Free plan builds. iOS needs macOS with
    Xcode, fastlane and CocoaPods; Android needs the Android SDK and a JDK. Installs
    dependencies with `npm ci` when node_modules is missing. Returns right away with
    the log and binary paths; the build takes 15 to 30 min. It is done when the log
    ends with "[eas_build_local] exit=N" (0 = success, binary at artifact_path).
    Without output, writes to a temporary directory.
    """
    started = eas.start_local_build(project_path, platform, profile, output)
    return {
        **started,
        "next_step": (
            f"Follow the end of the log ({started['log_path']}) until the line "
            f"'{eas.LOCAL_DONE_MARKER}N'. With exit=0, upload the binary with apple_upload_build and wait "
            "for processing with apple_wait_build (Android: play_upload_bundle)."
        ),
    }


@_register_tool
def eas_build_status(project_path: str, build_id: str) -> dict[str, Any]:
    """Get the state of an EAS build by id.

    status FINISHED with artifact_url set means it is ready to publish.
    """
    return eas.summarize(eas.run(["build:view", build_id, "--json"], project_path, timeout=120))


@_register_tool
def eas_build_download(project_path: str, build_id: str, destination: str) -> dict[str, Any]:
    """Download the binary of a finished EAS build to a local path.

    This is the link between EAS and the publishing tools: download the artifact and
    pass the path to apple_upload_build or play_upload_bundle.
    """
    build = eas.summarize(eas.run(["build:view", build_id, "--json"], project_path, timeout=120))
    url = build.get("artifact_url")
    if not url:
        raise ValueError(
            f"Build {build_id} has no artifact (status={build.get('status')}). "
            "Only FINISHED builds have a binary."
        )
    path = eas.download_artifact(url, Path(destination).expanduser())
    return {
        "downloaded": str(path),
        "size_bytes": path.stat().st_size,
        "platform": build.get("platform"),
        "build_number": build.get("build_number"),
    }


@_register_tool
def eas_submit(
    project_path: str,
    platform: str,
    profile: str = "production",
    build_id: str | None = None,
) -> dict[str, Any]:
    """Submit an EAS build straight to the store, using the submit profile in eas.json.

    External action: this really publishes. Confirm the profile and platform with the user.

    iOS: downloads the .ipa from EAS and uploads it through the buildUploads API with this server's key
    (eas submit does not accept an API key without a prompt). Android: eas submit with
    the submit profile in eas.json. Without build_id, uses the most recent finished build.
    """
    if platform == "ios":
        # eas submit refuses an App Store Connect key in --non-interactive mode
        # ("API Keys cannot be set up in --non-interactive mode"). Download the .ipa
        # from EAS and upload it through the buildUploads API with this server's account key.
        if build_id:
            build = eas.summarize(eas.run(["build:view", build_id, "--json"], project_path))
        else:
            latest = eas.run(
                ["build:list", "--platform", "ios", "--status", "finished", "--limit", "1", "--json"],
                project_path,
            )
            if not latest:
                raise eas.EasError("No finished iOS build on EAS.")
            build = eas.summarize(latest[0])
        if not build["artifact_url"]:
            raise eas.EasError(f"Build {build['build_id']} has no .ipa (status {build['status']}).")
        ipa = eas.download_artifact(
            build["artifact_url"], Path(tempfile.mkdtemp(prefix="sp-ipa-")) / f"{build['build_id']}.ipa"
        )
        upload = apple.upload_binary(ipa, "ios", load_apple())
        return {
            "platform": platform,
            "build_id": build["build_id"],
            "build_number": build["build_number"],
            "upload": upload,
            "next_step": "Wait for processing (apple_wait_build) and attach it with apple_attach_build.",
        }
    args = ["submit", "--platform", platform, "--profile", profile]
    if build_id:
        args += ["--id", build_id]
    output = eas.run(args, project_path, timeout=eas.SUBMIT_TIMEOUT, parse_json=False)
    return {"platform": platform, "profile": profile, "output": output[-2000:]}


def _configure_toolsets() -> None:
    disabled = TOOLSETS - load_toolsets()
    if disabled:
        mcp.disable(tags=disabled)


_configure_toolsets()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

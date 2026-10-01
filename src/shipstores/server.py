"""MCP server for publishing apps to the App Store and Google Play."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import httpx
from fastmcp import FastMCP

from . import apple, apple_console, apple_review, browser, data_safety, eas, play, play_console, signing
from .config import load_apple, load_play, play_console_app_ids, play_developer_id

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
        "has both a price AND availability; check with apple_list_subscriptions.\n\n"
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


# ---------------------------------------------------------------- diagnostics


@mcp.tool
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
        "altool_key_dir": str(apple_cfg.private_key_path.parent),
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


@mcp.tool
def store_browser_session(relogin: bool = False, console: str | None = None) -> dict[str, Any]:
    """Login state of the dedicated browser on both consoles.

    Console forms (App content, Data safety, app creation) run in a headless
    Chrome with its own profile, so they do not fight the user for the screen.
    Apple's session expires often; when it does, the automation ends up reading
    the login screen and returns meaningless results — so check here before
    investigating a form that "did not save".

    With relogin=True, opens Chrome WITH a window so a person can authenticate
    (2FA cannot be automated). Pass `console` ('play' or 'apple') to open only
    the one that expired.
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
    if caidas and relogin:
        out["action"] = browser.relogin(caidas)
    elif caidas:
        out["suggested_action"] = (
            f"Session expired on: {', '.join(caidas)}. "
            "Call again with relogin=True to open the login window."
        )
    return out


# --------------------------------------------------------------------- apple


@mcp.tool
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


@mcp.tool
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


@mcp.tool
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


@mcp.tool
def apple_upload_build(ipa_path: str, platform: str = "ios") -> dict[str, Any]:
    """Upload an .ipa to App Store Connect via xcrun altool.

    Build processing takes a few minutes after the upload — track it with
    apple_list_builds. platform: ios, osx or appletvos.
    """
    path = Path(ipa_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    output = apple.upload_binary(path, platform, load_apple())
    return {
        "uploaded": str(path),
        "output": output[-2000:],
        "next_step": "Wait for processing and check with apple_list_builds.",
    }


@mcp.tool
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


@mcp.tool
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


@mcp.tool
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


@mcp.tool
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


@mcp.tool
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


@mcp.tool
def apple_submit_for_review(version_id: str) -> dict[str, Any]:
    """Submit a version for Apple review. External action, hard to reverse.

    Confirm with the user before calling. Requires the version to already have a
    build attached and complete metadata.
    """
    # The 'app' relationship does not accept a direct GET (403 FORBIDDEN_ERROR); the
    # app_id comes from the include on the version itself.
    version = apple.request("GET", f"/v1/appStoreVersions/{version_id}", params={"include": "app"})
    app_id = version["data"]["relationships"]["app"]["data"]["id"]

    submission = apple.request(
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
    submission_id = submission["data"]["id"]
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
    apple.request(
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
    return {"review_submission_id": submission_id, "version_id": version_id, "submitted": True}


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
        if info["attributes"].get("appStoreState") != "READY_FOR_SALE":
            return info["id"]
    return infos[0]["id"]


def _editable_version(app_id: str) -> str:
    editable = ("PREPARE_FOR_SUBMISSION", "DEVELOPER_REJECTED", "REJECTED", "METADATA_REJECTED", "INVALID_BINARY")
    for v in apple.request("GET", f"/v1/apps/{app_id}/appStoreVersions", params={"limit": 10})["data"]:
        if v["attributes"].get("appStoreState") in editable:
            return v["id"]
    raise apple.AppleError("No editable version: create one with apple_create_version.")


@mcp.tool
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


@mcp.tool
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


@mcp.tool
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


@mcp.tool
def apple_set_availability(
    app_id: str, territories: list[str], available_in_new_territories: bool = False
) -> dict[str, Any]:
    """Choose the countries where the app is available (3-letter ISO, e.g. ["BRA"]). External action.

    Uses the console's internal API (the public one answers 409), in the dedicated
    Chrome session. Including the European Union requires the trader declaration
    (DSA) on the account.
    """
    return {"app_id": app_id, **apple_console.set_availability(app_id, territories, available_in_new_territories)}


@mcp.tool
def apple_set_app_privacy(app_id: str, usages: list[dict[str, Any]]) -> dict[str, Any]:
    """Replace and publish the "App Privacy" declaration (privacy nutrition label). External action.

    usages: one item per collected data type:
      {"category": "NAME", "purposes": ["APP_FUNCTIONALITY"], "linked": true, "tracking": false}
    Empty list = "we do not collect data". Accepted categories and purposes are in
    apple_console.DATA_CATEGORIES / DATA_PURPOSES. Uses the console's internal API.
    """
    return {"app_id": app_id, **apple_console.set_privacy(app_id, usages)}


@mcp.tool
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


@mcp.tool
def apple_cancel_submission(app_id: str) -> dict[str, Any]:
    """Pull a version submitted for review out of the queue (before Apple decides). External action.

    Use it to swap the build without waiting for the review: the version goes back to
    DEVELOPER_REJECTED and can be resubmitted with apple_submit_for_review.
    """
    sub_id = _active_submission(app_id, ("WAITING_FOR_REVIEW", "IN_REVIEW", "READY_FOR_REVIEW"))
    result = apple.request(
        "PATCH",
        f"/v1/reviewSubmissions/{sub_id}",
        json={"data": {"type": "reviewSubmissions", "id": sub_id, "attributes": {"canceled": True}}},
    )
    return {"review_submission_id": sub_id, "state": result["data"]["attributes"].get("state")}


@mcp.tool
def apple_review_messages(app_id: str) -> dict[str, Any]:
    """Read the review messages (Resolution Center) and the rejection reasons.

    The public API does not expose this: it reads through the console's internal
    API, in the dedicated Chrome session (check store_browser_session if it fails
    due to login).
    """
    messages = apple_review.read_messages(app_id)
    return {"app_id": app_id, "messages": messages, "total": len(messages)}


@mcp.tool
def apple_reply_review(
    app_id: str, text: str, attachments: list[str] | None = None
) -> dict[str, Any]:
    """Reply to App Review on a rejected submission, with attachments. External action.

    For a "Guideline 2.1 - Information Needed" request: send the answers in
    `text` (up to 4000 characters) and the video recorded on the iPhone in
    `attachments`. HEVC video is converted to H.264 at 1920px before upload. Copy
    the same information into the review notes with apple_set_review_details, as
    Apple asks. After the reply the resubmit button stays disabled: Apple itself
    resumes the review.
    """
    if len(text) > 4000:
        raise ValueError(f"The reply has {len(text)} characters; the console limit is 4000.")
    files = []
    for item in attachments or []:
        path = Path(item).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"Attachment not found: {path}")
        files.append(apple_review.prepare_attachment(path))
    sub_id = _active_submission(app_id, ("UNRESOLVED_ISSUES",))
    apple_review.reply(app_id, sub_id, text, files)
    return {
        "review_submission_id": sub_id,
        "sent": True,
        "attachments": [str(f) for f in files],
        "next_step": "Wait for Apple; follow up with apple_review_messages and apple_list_versions.",
    }


@mcp.tool
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


@mcp.tool
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


@mcp.tool
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


@mcp.tool
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


@mcp.tool
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


@mcp.tool
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


@mcp.tool
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


@mcp.tool
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


@mcp.tool
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


@mcp.tool
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


@mcp.tool
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


@mcp.tool
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


@mcp.tool
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


@mcp.tool
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


@mcp.tool
def play_upload_bundle(
    package_name: str,
    aab_path: str,
    track: str = "internal",
    status: str = "completed",
    release_name: str | None = None,
    release_notes_pt_br: str | None = None,
) -> dict[str, Any]:
    """Upload an .aab and release it on a Play track, in a single committed edit.

    External action: on commit, the release becomes visible to the track's testers
    (or to the public, if track=production). Confirm the track with the user.

    track: internal, alpha, beta or production.
    status: completed, draft, inProgress or halted.
    """
    path = Path(aab_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

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
            release_notes={"pt-BR": release_notes_pt_br} if release_notes_pt_br else None,
        )
        result = {"version_code": version_code, "track": track, "status": status}

    result["committed"] = True
    return result


@mcp.tool
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


@mcp.tool
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


@mcp.tool
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


@mcp.tool
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
    import httpx

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


@mcp.tool
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


@mcp.tool
def play_contact_details(package_name: str) -> dict[str, Any]:
    """Read the public contact details of the app's Play listing.

    These fields (contactEmail, contactWebsite, contactPhone) ARE covered by the API —
    do not automate the console form for them.
    """
    return play.read_details(package_name)


@mcp.tool
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


@mcp.tool
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


@mcp.tool
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


@mcp.tool
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


@mcp.tool
def play_content_status(package_name: str) -> dict[str, Any]:
    """Show how many "App content" declarations are still missing before the app can publish.

    A draft app on Play only leaves draft once all of them are complete. None of
    them has an API endpoint — they are console forms.
    """
    dev_id, app_id = _console_ids(package_name)
    out = play_console.run(
        dev_id,
        app_id,
        f'goto_url("{play_console.CONSOLE}/developers/{dev_id}/app/{app_id}/app-dashboard")\n'
        "wait_for_load()\ntime.sleep(9)\n"
        # "Tarefas concluídas" ("Tasks completed") is console UI text, matched literally (pt-BR)
        't = body()\ni = t.find("Tarefas concluídas")\n'
        'print(t[i:i+40].split("\\n")[0] if i > 0 else "?")\n',
    )
    return {
        "package_name": package_name,
        "progress": out.strip().splitlines()[-1] if out.strip() else "?",
        "index": f"{play_console.CONSOLE}/developers/{dev_id}/app/{app_id}/app-content/overview",
    }


@mcp.tool
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


@mcp.tool
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


@mcp.tool
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


@mcp.tool
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


@mcp.tool
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


@mcp.tool
def play_data_safety_fill(
    template_path: str,
    destination_path: str,
    collected_data: dict[str, Any],
    account_deletion_url: str | None = None,
    encrypted_in_transit: bool = True,
    supports_deletion: bool = True,
) -> dict[str, Any]:
    """Fill in the Data safety CSV and return a summary of what was declared.

    Imports nothing — it generates the file for review. An incorrect declaration here
    can take down a published app, so review the summary with the user before
    play_data_safety_import.

    collected_data maps the Response ID of each collected data type (e.g. "PSL_EMAIL") to
    {"group": "PSL_DATA_TYPES_PERSONAL", "shared": false, "optional": false,
     "purposes": ["PSL_APP_FUNCTIONALITY", "PSL_ACCOUNT_MANAGEMENT"]}.
    """
    responses = data_safety.build_responses(
        collected_data,
        encrypted_in_transit=encrypted_in_transit,
        supports_deletion=supports_deletion,
        account_deletion_url=account_deletion_url,
    )
    result = data_safety.fill_csv(
        Path(template_path).expanduser(), Path(destination_path).expanduser(), responses
    )
    result["declared"] = data_safety.summarize(Path(destination_path).expanduser())
    return result


@mcp.tool
def play_data_safety_import(package_name: str, csv_path: str) -> dict[str, Any]:
    """Import the filled-in CSV into the Data safety declaration.

    External action: replaces the app's entire declaration. Review the summary from
    play_data_safety_fill with the user before calling.
    """
    dev_id, app_id = _console_ids(package_name)
    path = Path(csv_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"CSV not found: {path}")
    out = play_console.run(
        dev_id,
        app_id,
        f'goto_url("{play_console.CONSOLE}/developers/{dev_id}/app/{app_id}'
        '/app-content/data-privacy-security")\n'
        "wait_for_load()\ntime.sleep(9)\n"
        # Button labels are console UI text, matched literally (pt-BR):
        # "Importar de arquivo CSV" (Import from CSV file), "Importar" (Import),
        # "Fazer upload" (Upload), "Continuar" (Continue), "Salvar" (Save)
        'print(ax_click(r"Importar de arquivo CSV", pause=6))\n'
        # The native file picker does not open over CDP, but the page's
        # input[type=file] accepts the file directly via DOM.setFileInputFiles.
        'root = cdp("DOM.getDocument")["root"]["nodeId"]\n'
        'target = cdp("DOM.querySelector", nodeId=root, selector="input[type=file]")["nodeId"]\n'
        'if not target:\n'
        '    raise RuntimeError("input[type=file] not found on the Data safety page")\n'
        f'cdp("DOM.setFileInputFiles", files=[{str(path)!r}], nodeId=target)\n'
        'time.sleep(8)\n'
        'print(ax_click(r"^(Importar|Fazer upload|Continuar)$", pause=8))\n'
        'print(ax_click(r"^Salvar$", pause=8))\n'
        'print(dismiss_dialog())\n',
    )
    return {
        "csv": str(path),
        "result": out.strip()[-800:],
        "warning": (
            "Confirm with play_data_safety_export that the values were applied — in "
            "some states the console accepts the click without applying the file."
        ),
    }


# ----------------------------------------------------------------------- eas


@mcp.tool
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


@mcp.tool
def eas_build_start(project_path: str, platform: str, profile: str = "production") -> dict[str, Any]:
    """Start an EAS build and return immediately, without waiting for it to finish.

    Builds take 10 to 40 minutes. This tool does NOT block — track progress with
    eas_build_status using the returned build_id.

    platform: ios, android or all. profile: a profile defined in eas.json.
    """
    result = eas.run(
        ["build", "--platform", platform, "--profile", profile, "--json", "--no-wait"],
        project_path,
        timeout=eas.BUILD_START_TIMEOUT,
    )
    builds = result if isinstance(result, list) else [result]
    return {
        "started": [eas.summarize(build) for build in builds],
        "next_step": "Track it with eas_build_status(build_id).",
    }


@mcp.tool
def eas_build_status(project_path: str, build_id: str) -> dict[str, Any]:
    """Get the state of an EAS build by id.

    status FINISHED with artifact_url set means it is ready to publish.
    """
    return eas.summarize(eas.run(["build:view", build_id, "--json"], project_path, timeout=120))


@mcp.tool
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


@mcp.tool
def eas_submit(
    project_path: str,
    platform: str,
    profile: str = "production",
    build_id: str | None = None,
) -> dict[str, Any]:
    """Submit an EAS build straight to the store, using the submit profile in eas.json.

    External action: this really publishes. Confirm the profile and platform with the user.

    iOS: downloads the .ipa from EAS and uploads it via altool with this server's key
    (eas submit does not accept an API key without a prompt). Android: eas submit with
    the submit profile in eas.json. Without build_id, uses the most recent finished build.
    """
    if platform == "ios":
        # eas submit refuses an App Store Connect key in --non-interactive mode
        # ("API Keys cannot be set up in --non-interactive mode"). Download the .ipa
        # from EAS and upload it via altool with this server's account key.
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
        output = apple.upload_binary(ipa, "ios", load_apple())
        return {
            "platform": platform,
            "build_id": build["build_id"],
            "build_number": build["build_number"],
            "output": output[-1500:],
            "next_step": "Wait for processing (apple_list_builds) and attach it with apple_attach_build.",
        }
    args = ["submit", "--platform", platform, "--profile", profile]
    if build_id:
        args += ["--id", build_id]
    output = eas.run(args, project_path, timeout=eas.SUBMIT_TIMEOUT, parse_json=False)
    return {"platform": platform, "profile": profile, "output": output[-2000:]}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

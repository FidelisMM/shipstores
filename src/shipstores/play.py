"""Google Play Developer API client (androidpublisher v3).

Every write to Play goes through an "edit": open an edit, apply the changes,
and the commit publishes everything at once. An uncommitted edit expires on
its own, so aborting on error is safe but not required.
"""

from __future__ import annotations

import contextlib
import time
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

import google.auth.transport.requests
import httpx
from google.oauth2 import service_account

from .config import PlayConfig, load_play

BASE_URL = "https://androidpublisher.googleapis.com/androidpublisher/v3"
UPLOAD_URL = "https://androidpublisher.googleapis.com/upload/androidpublisher/v3"
SCOPES = ["https://www.googleapis.com/auth/androidpublisher"]


class PlayError(RuntimeError):
    pass

T = TypeVar("T")

RETRY_TENTATIVAS = 5


def with_retry(fn: Callable[[], T], what: str, tentativas: int = RETRY_TENTATIVAS) -> T:
    """Retry a network call with exponential backoff.

    Screenshot uploads send large files and the connection drops; the edit only
    commits at the end, so retrying here is safe.
    """
    for n in range(1, tentativas + 1):
        try:
            return fn()
        except (httpx.TransportError, PlayError) as exc:
            recuperavel = isinstance(exc, httpx.TransportError) or " HTTP 5" in str(exc)
            if n == tentativas or not recuperavel:
                raise
            time.sleep(2**n)
    raise PlayError(f"{what}: unreachable")


def _headers(cfg: PlayConfig) -> dict[str, str]:
    creds = service_account.Credentials.from_service_account_file(
        str(cfg.service_account_path), scopes=SCOPES
    )
    creds.refresh(google.auth.transport.requests.Request())
    return {"Authorization": f"Bearer {creds.token}"}


def _check(resp: httpx.Response, what: str) -> dict[str, Any]:
    if resp.status_code >= 400:
        raise PlayError(f"Play {what} -> HTTP {resp.status_code}: {resp.text[:800]}")
    return resp.json() if resp.content else {}


@contextlib.contextmanager
def edit_session(package_name: str, cfg: PlayConfig | None = None) -> Iterator[tuple[str, dict[str, str]]]:
    """Open an edit and discard it if anything fails before the commit."""
    cfg = cfg or load_play()
    headers = _headers(cfg)
    app_url = f"{BASE_URL}/applications/{package_name}"
    edit_id = _check(
        httpx.post(f"{app_url}/edits", headers=headers, json={}, timeout=60),
        f"edits.insert({package_name})",
    )["id"]
    committed = False
    try:
        yield edit_id, headers
        _check(
            httpx.post(f"{app_url}/edits/{edit_id}:commit", headers=headers, timeout=120),
            "edits.commit",
        )
        committed = True
    finally:
        if not committed:
            with contextlib.suppress(Exception):
                httpx.delete(f"{app_url}/edits/{edit_id}", headers=headers, timeout=30)


def read_tracks(package_name: str, cfg: PlayConfig | None = None) -> list[dict[str, Any]]:
    """Read the tracks without committing anything — the edit is always discarded."""
    cfg = cfg or load_play()
    headers = _headers(cfg)
    app_url = f"{BASE_URL}/applications/{package_name}"
    edit_id = _check(
        httpx.post(f"{app_url}/edits", headers=headers, json={}, timeout=60),
        f"edits.insert({package_name})",
    )["id"]
    try:
        return _check(
            httpx.get(f"{app_url}/edits/{edit_id}/tracks", headers=headers, timeout=60),
            "edits.tracks.list",
        ).get("tracks", [])
    finally:
        with contextlib.suppress(Exception):
            httpx.delete(f"{app_url}/edits/{edit_id}", headers=headers, timeout=30)


def read_details(package_name: str, cfg: PlayConfig | None = None) -> dict[str, Any]:
    """Read the store listing contact details (contactEmail, contactWebsite, contactPhone).

    Read-only — opens and discards an edit.
    """
    cfg = cfg or load_play()
    headers = _headers(cfg)
    app_url = f"{BASE_URL}/applications/{package_name}"
    edit_id = _check(
        httpx.post(f"{app_url}/edits", headers=headers, json={}, timeout=60),
        f"edits.insert({package_name})",
    )["id"]
    try:
        return _check(
            httpx.get(f"{app_url}/edits/{edit_id}/details", headers=headers, timeout=60),
            "edits.details.get",
        )
    finally:
        with contextlib.suppress(Exception):
            httpx.delete(f"{app_url}/edits/{edit_id}", headers=headers, timeout=30)


def patch_details(
    package_name: str, fields: dict[str, str], cfg: PlayConfig | None = None
) -> dict[str, Any]:
    """Update the store listing contact details and commit.

    Exists because these fields ARE covered by the API — there is no reason to
    automate the console form to change them.
    """
    cfg = cfg or load_play()
    with edit_session(package_name, cfg) as (edit_id, headers):
        return _check(
            httpx.patch(
                f"{BASE_URL}/applications/{package_name}/edits/{edit_id}/details",
                headers=headers,
                json=fields,
                timeout=60,
            ),
            "edits.details.patch",
        )


def upload_bundle(
    package_name: str, aab_path: Path, edit_id: str, headers: dict[str, str]
) -> dict[str, Any]:
    """Upload an .aab into the open edit. Returns the Bundle resource (with versionCode)."""
    url = f"{UPLOAD_URL}/applications/{package_name}/edits/{edit_id}/bundles"
    with aab_path.open("rb") as handle:
        resp = httpx.post(
            url,
            params={"uploadType": "media"},
            headers={**headers, "Content-Type": "application/octet-stream"},
            content=handle,
            timeout=1800,
        )
    return _check(resp, "edits.bundles.upload")


def assign_track(
    package_name: str,
    track: str,
    version_codes: list[int],
    edit_id: str,
    headers: dict[str, str],
    *,
    status: str = "completed",
    release_name: str | None = None,
    release_notes: dict[str, str] | None = None,
    user_fraction: float | None = None,
) -> dict[str, Any]:
    """Put versionCodes on a track. `user_fraction` only applies with status inProgress."""
    release: dict[str, Any] = {"versionCodes": [str(v) for v in version_codes], "status": status}
    if release_name:
        release["name"] = release_name
    if user_fraction is not None:
        release["userFraction"] = user_fraction
    if release_notes:
        release["releaseNotes"] = [
            {"language": lang, "text": text} for lang, text in release_notes.items()
        ]
    return _check(
        httpx.put(
            f"{BASE_URL}/applications/{package_name}/edits/{edit_id}/tracks/{track}",
            headers=headers,
            json={"track": track, "releases": [release]},
            timeout=60,
        ),
        f"edits.tracks.update({track})",
    )


IMAGE_TYPES = (
    "phoneScreenshots",
    "sevenInchScreenshots",
    "tenInchScreenshots",
    "tvScreenshots",
    "wearScreenshots",
)


def list_images(
    package_name: str, language: str, image_type: str, cfg: PlayConfig | None = None
) -> list[dict[str, Any]]:
    """Read the listing images of one type. Read-only — the edit is discarded."""
    cfg = cfg or load_play()
    headers = _headers(cfg)
    app_url = f"{BASE_URL}/applications/{package_name}"
    edit_id = _check(
        httpx.post(f"{app_url}/edits", headers=headers, json={}, timeout=60),
        f"edits.insert({package_name})",
    )["id"]
    try:
        return _check(
            httpx.get(
                f"{app_url}/edits/{edit_id}/listings/{language}/{image_type}",
                headers=headers,
                timeout=60,
            ),
            "edits.images.list",
        ).get("images", [])
    finally:
        with contextlib.suppress(Exception):
            httpx.delete(f"{app_url}/edits/{edit_id}", headers=headers, timeout=30)


def upload_image(
    package_name: str,
    language: str,
    image_type: str,
    image_path: Path,
    edit_id: str,
    headers: dict[str, str],
) -> dict[str, Any]:
    """Upload an image into the open edit. Listing order is upload order."""
    suffix = image_path.suffix.lower()
    mime = "image/jpeg" if suffix in (".jpg", ".jpeg") else "image/png"
    url = f"{UPLOAD_URL}/applications/{package_name}/edits/{edit_id}/listings/{language}/{image_type}"

    def enviar() -> dict[str, Any]:
        resp = httpx.post(
            url,
            params={"uploadType": "media"},
            headers={**headers, "Content-Type": mime},
            content=image_path.read_bytes(),
            timeout=600,
        )
        return _check(resp, f"edits.images.upload({image_path.name})")

    return with_retry(enviar, f"upload {image_path.name}")


def delete_images(
    package_name: str,
    language: str,
    image_type: str,
    edit_id: str,
    headers: dict[str, str],
) -> None:
    """Delete all images of one type inside the open edit."""
    _check(
        httpx.delete(
            f"{BASE_URL}/applications/{package_name}/edits/{edit_id}"
            f"/listings/{language}/{image_type}",
            headers=headers,
            timeout=60,
        ),
        "edits.images.deleteall",
    )

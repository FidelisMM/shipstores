"""App Store Connect API client (JWT ES256)."""

from __future__ import annotations

import hashlib
import plistlib
import time
import zipfile
from typing import Callable, TypeVar
from pathlib import Path
from typing import Any

import httpx
import jwt

from .config import AppleConfig, load_apple

BASE_URL = "https://api.appstoreconnect.apple.com"
TOKEN_TTL_SECONDS = 900  # Apple's maximum lifetime for API tokens


class AppleError(RuntimeError):
    pass

T = TypeVar("T")

RETRY_TENTATIVAS = 5


def with_retry(fn: Callable[[], T], what: str, tentativas: int = RETRY_TENTATIVAS) -> T:
    """Retry a network call with exponential backoff.

    Screenshot uploads send ~1 MB files to Apple's CDN, and a connection dropped
    mid-transfer is common. Without this, a single network failure leaves the
    screenshot set half-uploaded.
    """
    for n in range(1, tentativas + 1):
        try:
            return fn()
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            if n == tentativas:
                raise AppleError(f"{what} failed after {tentativas} attempts: {exc}") from exc
            time.sleep(2**n)
    raise AppleError(f"{what}: unreachable")


def _token(cfg: AppleConfig) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": cfg.issuer_id,
            "iat": now,
            "exp": now + TOKEN_TTL_SECONDS,
            "aud": "appstoreconnect-v1",
        },
        cfg.private_key,
        algorithm="ES256",
        headers={"kid": cfg.key_id, "typ": "JWT"},
    )


def request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
    cfg: AppleConfig | None = None,
) -> dict[str, Any]:
    cfg = cfg or load_apple()

    def chamar() -> httpx.Response:
        resp = httpx.request(
            method,
            f"{BASE_URL}{path}",
            headers={"Authorization": f"Bearer {_token(cfg)}"},
            params=params,
            json=json,
            timeout=120,
        )
        # 5xx means instability on Apple's side: worth retrying. 4xx is a usage error.
        if resp.status_code >= 500:
            resp.raise_for_status()
        return resp

    resp = with_retry(chamar, f"ASC {method} {path}")
    if resp.status_code >= 400:
        detail = resp.text[:800]
        raise AppleError(f"ASC {method} {path} -> HTTP {resp.status_code}: {detail}")
    return resp.json() if resp.content else {}


def upload_asset(
    resource: str,
    file_path: Path,
    relationships: dict[str, Any],
    *,
    cfg: AppleConfig | None = None,
) -> str:
    """Upload a file to App Store Connect and return the id of the created resource.

    App Store Connect uploads happen in three steps: reserve the asset, send the
    bytes to the URLs it returns, and commit with the checksum. Works for any
    resource that follows this contract (version screenshots, subscription
    review screenshots). Every step is retried — ~1 MB files sent to Apple's
    CDN fail often.
    """
    cfg = cfg or load_apple()
    conteudo = file_path.read_bytes()
    nome = file_path.name

    def reservar() -> dict[str, Any]:
        return request(
            "POST",
            f"/v1/{resource}",
            json={
                "data": {
                    "type": resource,
                    "attributes": {"fileName": nome, "fileSize": len(conteudo)},
                    "relationships": relationships,
                }
            },
            cfg=cfg,
        )

    reserva = reservar()
    asset_id = reserva["data"]["id"]

    for operacao in reserva["data"]["attributes"]["uploadOperations"]:
        trecho = conteudo[operacao["offset"] : operacao["offset"] + operacao["length"]]

        def enviar(op: dict[str, Any] = operacao, bloco: bytes = trecho) -> None:
            resposta = httpx.request(
                op["method"],
                op["url"],
                headers={h["name"]: h["value"] for h in op.get("requestHeaders", [])},
                content=bloco,
                timeout=600,
            )
            resposta.raise_for_status()

        with_retry(enviar, f"upload {nome}")

    def confirmar() -> None:
        try:
            request(
                "PATCH",
                f"/v1/{resource}/{asset_id}",
                json={
                    "data": {
                        "type": resource,
                        "id": asset_id,
                        "attributes": {
                            "uploaded": True,
                            "sourceFileChecksum": hashlib.md5(conteudo).hexdigest(),
                        },
                    }
                },
                cfg=cfg,
            )
        except AppleError as exc:
            # This PATCH is not idempotent: if the response was lost and the retry
            # resent it, ASC answers 409 ("can't be re-committed"). When the
            # asset is already COMPLETE, the upload succeeded — not an error.
            if "HTTP 409" not in str(exc):
                raise
            estado = request("GET", f"/v1/{resource}/{asset_id}", cfg=cfg)
            entrega = estado["data"]["attributes"].get("assetDeliveryState") or {}
            if entrega.get("state") != "COMPLETE":
                raise

    confirmar()
    return asset_id


def set_order(resource: str, set_id: str, child: str, ids: list[str]) -> None:
    """Pin the display order of a set (screenshots, previews).

    Without this the order falls back to creation order, which Apple does not guarantee.
    """
    cfg = load_apple()

    def chamar() -> httpx.Response:
        resp = httpx.patch(
            f"{BASE_URL}/v1/{resource}/{set_id}/relationships/{child}",
            headers={"Authorization": f"Bearer {_token(cfg)}"},
            json={"data": [{"type": child, "id": i} for i in ids]},
            timeout=120,
        )
        if resp.status_code >= 500:
            resp.raise_for_status()
        return resp

    resp = with_retry(chamar, f"reorder {resource}")
    if resp.status_code >= 400:
        raise AppleError(f"reordering failed: HTTP {resp.status_code} {resp.text[:300]}")


# Tool platform (altool names, kept for compatibility) -> buildUploads API platform.
UPLOAD_PLATFORMS = {"ios": "IOS", "osx": "MAC_OS", "appletvos": "TV_OS"}

UPLOAD_POLL_SECONDS = 15
UPLOAD_POLL_TRIES = 60  # 15 min for Apple to answer COMPLETE or FAILED
UPLOAD_CHUNK_BYTES = 1024 * 1024


def upload_platform(platform: str) -> str:
    try:
        return UPLOAD_PLATFORMS[platform.lower()]
    except KeyError:
        raise AppleError(
            f"Invalid platform: {platform!r}. Use {', '.join(UPLOAD_PLATFORMS)}."
        ) from None


def read_ipa_info(ipa_path: Path) -> dict[str, str]:
    """Read bundle id, version and build number from the .ipa's Payload/*.app/Info.plist.

    The version sent to buildUploads must match the binary's, or Apple rejects it
    with ITMS-90345 — so it comes from the file itself.
    """
    with zipfile.ZipFile(ipa_path) as ipa:
        names = [
            n for n in ipa.namelist()
            if n.startswith("Payload/") and n.endswith(".app/Info.plist") and n.count("/") == 2
        ]
        if not names:
            raise AppleError(f"{ipa_path.name} has no Payload/*.app/Info.plist — it does not look like an .ipa.")
        info = plistlib.loads(ipa.read(names[0]))
    missing = [
        k for k in ("CFBundleIdentifier", "CFBundleShortVersionString", "CFBundleVersion") if not info.get(k)
    ]
    if missing:
        raise AppleError(f"Info.plist in {ipa_path.name} has no {', '.join(missing)}.")
    return {
        "bundle_id": str(info["CFBundleIdentifier"]),
        "version": str(info["CFBundleShortVersionString"]),
        "build_number": str(info["CFBundleVersion"]),
    }


def find_app_id(bundle_id: str, cfg: AppleConfig) -> str:
    data = request("GET", "/v1/apps", params={"filter[bundleId]": bundle_id, "limit": 200}, cfg=cfg)
    # The filter is not strictly exact; check each app's bundle id.
    for app in data.get("data", []):
        if app["attributes"].get("bundleId") == bundle_id:
            return app["id"]
    raise AppleError(
        f"No app with bundle id {bundle_id} in this account. Create the app (apple_create_app_form) "
        "or check that the key belongs to the right account."
    )


def _md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as f:
        while chunk := f.read(UPLOAD_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def upload_binary(ipa_path: Path, platform: str, cfg: AppleConfig) -> dict[str, Any]:
    """Upload an .ipa through App Store Connect's buildUploads API and wait for the verdict.

    No altool and no Transporter: altool needs macOS with Xcode (and breaks on every
    command in Xcode 27 with "Defaults.properties couldn't be opened"), while the API
    works anywhere with the same key as every other call. Steps: reserve the upload,
    reserve the file, send the parts, commit with the MD5 and poll the state until
    COMPLETE or FAILED.
    """
    api_platform = upload_platform(platform)
    info = read_ipa_info(ipa_path)
    app_id = find_app_id(info["bundle_id"], cfg)
    size = ipa_path.stat().st_size

    upload = request(
        "POST",
        "/v1/buildUploads",
        json={
            "data": {
                "type": "buildUploads",
                "attributes": {
                    "cfBundleShortVersionString": info["version"],
                    "cfBundleVersion": info["build_number"],
                    "platform": api_platform,
                },
                "relationships": {"app": {"data": {"type": "apps", "id": app_id}}},
            }
        },
        cfg=cfg,
    )
    upload_id = upload["data"]["id"]

    upload_file = request(
        "POST",
        "/v1/buildUploadFiles",
        json={
            "data": {
                "type": "buildUploadFiles",
                "attributes": {
                    "fileName": ipa_path.name,
                    "fileSize": size,
                    "uti": "com.apple.ipa",
                    "assetType": "ASSET",
                },
                "relationships": {"buildUpload": {"data": {"type": "buildUploads", "id": upload_id}}},
            }
        },
        cfg=cfg,
    )
    file_id = upload_file["data"]["id"]
    operations = upload_file["data"]["attributes"]["uploadOperations"]

    # One part in memory at a time, never the whole .ipa.
    with ipa_path.open("rb") as f:
        for n, operation in enumerate(operations, 1):

            def send(op: dict[str, Any] = operation) -> None:
                f.seek(op["offset"])
                response = httpx.request(
                    op["method"],
                    op["url"],
                    headers={h["name"]: h["value"] for h in op.get("requestHeaders", [])},
                    content=f.read(op["length"]),
                    timeout=600,
                )
                response.raise_for_status()

            with_retry(send, f"upload {ipa_path.name} part {n}/{len(operations)}")

    try:
        request(
            "PATCH",
            f"/v1/buildUploadFiles/{file_id}",
            json={
                "data": {
                    "type": "buildUploadFiles",
                    "id": file_id,
                    "attributes": {
                        "uploaded": True,
                        "sourceFileChecksums": {"file": {"hash": _md5(ipa_path), "algorithm": "MD5"}},
                    },
                }
            },
            cfg=cfg,
        )
    except AppleError as exc:
        # This PATCH is not idempotent: a 409 after a retry means the earlier commit
        # went through. The buildUpload state below tells whether it worked.
        if "HTTP 409" not in str(exc):
            raise

    state: dict[str, Any] = {}
    for n in range(UPLOAD_POLL_TRIES):
        if n:
            time.sleep(UPLOAD_POLL_SECONDS)
        data = request("GET", f"/v1/buildUploads/{upload_id}", cfg=cfg)
        state = data["data"]["attributes"].get("state") or {}
        if state.get("state") in ("COMPLETE", "FAILED"):
            break

    result: dict[str, Any] = {
        "upload_id": upload_id,
        "state": state.get("state"),
        "app_id": app_id,
        **info,
        "platform": api_platform,
        "size_bytes": size,
        "parts": len(operations),
    }
    for field in ("errors", "warnings"):
        if state.get(field):
            result[field] = state[field]
    return result

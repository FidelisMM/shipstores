"""App Store Connect API client (JWT ES256)."""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
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


def upload_binary(ipa_path: Path, platform: str, cfg: AppleConfig) -> str:
    """Upload an .ipa via `xcrun altool`. Returns the command's combined output.

    altool does not accept a key path as an argument, but it honors
    API_PRIVATE_KEYS_DIR: point it straight at the .p8's folder, no copying or linking.
    """
    env = {**os.environ, "API_PRIVATE_KEYS_DIR": str(cfg.private_key_path.parent)}
    result = subprocess.run(
        [
            "xcrun", "altool", "--upload-app",
            "-f", str(ipa_path),
            "-t", platform,
            "--apiKey", cfg.key_id,
            "--apiIssuer", cfg.issuer_id,
        ],
        capture_output=True,
        text=True,
        timeout=3600,
        env=env,
    )
    output = f"{result.stdout}\n{result.stderr}".strip()
    if result.returncode != 0:
        raise AppleError(f"altool failed (exit {result.returncode}):\n{output}")
    return output

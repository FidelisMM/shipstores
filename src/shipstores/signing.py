"""Play signing keys and the SHA-1 that Google Sign-In requires.

## The problem this module solves

With Play App Signing, Google **re-signs** the APK before delivering it to users.
The SHA-1 that reaches the device is the one from Play's *deployment cert* — not
your local debug key nor your upload key.

Google Sign-In validates the `package name + SHA-1` pair. If Play's SHA-1 is not
registered as an Android OAuth client, sign-in **fails silently**: the account
picker opens, the user picks an account, and the app returns to the login screen
with no error at all. It works on local builds (whose debug SHA-1 is registered)
and only breaks with the store APK — which makes it look like an app bug.

This happened on a real app: the project's only Android OAuth client was
literally named "<App> Android — debug".

## How to get the correct SHA-1

The Play Console "App signing" page shows the SHA-1 of the **upload** key as
text — that one does NOT work. The right value comes from the certificate bundle:

    Play Console -> Protected with Google Play -> App signing
      -> "Download certificates"  (zip with 3 .der files;
         labeled "Baixar certificados" in the pt-BR UI)

    deployment_cert.der        <- THIS ONE. It signs the distributed APK
    hybrid_classical_cert.der
    hybrid_pqc_cert.der

    openssl x509 -inform DER -in deployment_cert.der -noout -fingerprint -sha1

For the download to work over CDP you need `Browser.setDownloadBehavior`
(NOT `Page.`), and the file arrives with a GUID name.

## How to register it

There is no public Google Cloud API to create OAuth clients — only the Console:

    console.cloud.google.com -> the app's project -> APIs & Services -> Credentials
      -> Create credentials -> OAuth client ID -> Android
         Package name: <applicationId>
         SHA-1: <from deployment_cert.der>

In the newer UI the path is "Google Auth Platform -> Clients -> Create client", at
`console.cloud.google.com/auth/clients/create?project=<id>`.

**Never delete the debug client** — it is what makes sign-in work on local builds.

## Access to the Google Cloud project

A project without an organization does not show up in the Workspace account
picker, even with IAM granted; open it via the URL with `?project=<id>` or use the
"All" tab. Also, `roles/owner` cannot be granted via API on a project without an
organization (`SOLO_MUST_INVITE_OWNERS`) — only by invitation in the Console.
`roles/editor` works via API and is already enough to manage credentials.
"""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

# Name of the certificate, inside Play's zip, that signs the distributed APK.
DEPLOYMENT_CERT = "deployment_cert.der"


class SigningError(RuntimeError):
    pass


def sha1_of_der(cert_path: Path) -> str:
    """Extract the SHA-1 fingerprint from a DER certificate."""
    result = subprocess.run(
        ["openssl", "x509", "-inform", "DER", "-in", str(cert_path),
         "-noout", "-fingerprint", "-sha1"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise SigningError(f"openssl failed: {result.stderr[:300]}")
    return result.stdout.strip().split("=", 1)[-1].strip()


def fingerprints_from_zip(zip_path: Path, extract_to: Path) -> dict[str, str]:
    """Unzip Play's certificate bundle and return the SHA-1 of each certificate.

    The dict key is the file name; `deployment_cert.der` is the one that matters
    for registering the OAuth client.
    """
    extract_to.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_to)

    saida: dict[str, str] = {}
    for der in sorted(extract_to.glob("*.der")):
        saida[der.name] = sha1_of_der(der)
    if DEPLOYMENT_CERT not in saida:
        raise SigningError(
            f"{DEPLOYMENT_CERT} is not in the zip (found: {list(saida)}). "
            "Check that the download came from the 'Download certificates' button."
        )
    return saida

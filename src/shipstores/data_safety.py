"""Fill in the Play Data safety declaration via CSV.

The form is a 5-step wizard with hundreds of checkboxes, but the console offers
CSV export and import ("Exportar para .csv" / "Importar de arquivo CSV" in the
pt-BR UI). Building the CSV is orders of magnitude faster and more reliable than
clicking through screen by screen.

File format (5 columns):
    Question ID (machine readable) | Response ID (machine readable) |
    Response value | Answer requirement | Human-friendly question label

How to fill the "Response value" column:
    MULTIPLE_CHOICE  -> "true" on the row of each selected option (leave the others empty)
    SINGLE_CHOICE    -> "true" on the row of the chosen option
    REQUIRED /
    MAYBE_REQUIRED   -> the value itself ("true"/"false" or a text/URL)

The download only works with `Browser.setDownloadBehavior` (NOT `Page.`), and the
file arrives with a GUID name when behavior="allowAndName" is used.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

# Data types the app collects. The key is the CSV Response ID.
# Each value describes how that data is handled.
DataTypeSpec = dict[str, Any]

# "Which of the following account creation methods does your app support?"
# Alias -> Response ID. The raw Response ID is accepted too.
ACCOUNT_CREATION_METHODS = {
    "password": "PSL_ACM_USER_ID_PASSWORD",                       # username and password
    "other_auth": "PSL_ACM_USER_ID_OTHER_AUTH",                   # username and other authentication
    "password_and_other_auth": "PSL_ACM_USER_ID_PASSWORD_OTHER_AUTH",
    "oauth": "PSL_ACM_OAUTH",
    "other": "PSL_ACM_OTHER",                                     # needs account_creation_other
    "none": "PSL_ACM_NONE",                                       # the app does not allow creating accounts
}
DEFAULT_ACCOUNT_METHODS = ("PSL_ACM_USER_ID_PASSWORD", "PSL_ACM_OAUTH")

# "How are these accounts created?" (login with accounts created outside the app)
EXTERNAL_ACCOUNT_TYPES = {
    "outside_app_id": "PSL_LOGIN_WITH_OUTSIDE_APP_ID",            # SIM card, service subscription
    "enterprise": "PSL_LOGIN_THROUGH_EMPLOYMENT_OR_ENTERPRISE_ACCOUNT",
    "other": "PSL_OUTSIDE_APP_ACCOUNT_TYPE_OTHER",                # needs external_account_description
}


def _resolve(values: list[str] | tuple[str, ...], table: dict[str, str], what: str) -> list[str]:
    ids = []
    for value in values:
        key = value.strip()
        resolved = table.get(key.lower(), key)
        if resolved not in table.values():
            raise ValueError(f"Unknown {what}: {value!r}. Use one of {list(table)} or the Response ID.")
        if resolved not in ids:
            ids.append(resolved)
    return ids


def build_responses(
    collected: dict[str, DataTypeSpec],
    *,
    encrypted_in_transit: bool = True,
    supports_deletion: bool = True,
    account_deletion_url: str | None = None,
    account_methods: str | list[str] | tuple[str, ...] = DEFAULT_ACCOUNT_METHODS,
    account_creation_other: str | None = None,
    external_accounts: bool | None = None,
    external_account_types: list[str] | tuple[str, ...] = (),
    external_account_description: str | None = None,
) -> dict[tuple[str, str], str]:
    """Build the (question_id, response_id) -> value map to write into the CSV.

    `collected` maps the data type's Response ID (e.g. "PSL_EMAIL") to a dict with:
        group    -> the group's question id (e.g. "PSL_DATA_TYPES_PERSONAL")
        shared   -> True if the data is also shared with third parties
        optional -> True if the user can use the app without providing the data
        purposes -> list of purposes (PSL_APP_FUNCTIONALITY, PSL_ACCOUNT_MANAGEMENT, ...)

    Accounts:
        account_methods  -> aliases from ACCOUNT_CREATION_METHODS or Response IDs; "none"
                            (the app does not allow creating accounts) does not combine
                            with the others. Methods not chosen are written empty, so a
                            template exported from an app that was already filled in does
                            not keep stale checks. An empty list leaves the question as it
                            is in the template.
        account_creation_other -> description, required with "other".
        external_accounts -> "Can users log in with accounts created outside the app?"
                            None leaves the question as it is in the template.
        external_account_types / external_account_description -> how those accounts are
                            created (aliases from EXTERNAL_ACCOUNT_TYPES); the description
                            is required with "other".
    """
    out: dict[tuple[str, str], str] = {
        ("PSL_DATA_COLLECTION_COLLECTS_PERSONAL_DATA", ""): "true" if collected else "false",
        ("PSL_DATA_COLLECTION_ENCRYPTED_IN_TRANSIT", ""): str(encrypted_in_transit).lower(),
    }

    methods = _resolve(
        [account_methods] if isinstance(account_methods, str) else account_methods,
        ACCOUNT_CREATION_METHODS,
        "account creation method",
    )
    no_account = "PSL_ACM_NONE" in methods
    if no_account and len(methods) > 1:
        raise ValueError('"none" (the app does not allow creating accounts) does not combine with other methods.')
    other_method = "PSL_ACM_OTHER" in methods
    if other_method and not account_creation_other:
        raise ValueError('The "other" method needs account_creation_other with a description.')

    deletion_id = "DATA_DELETION_YES" if supports_deletion else "DATA_DELETION_NO"
    out[("PSL_SUPPORT_DATA_DELETION_BY_USER", deletion_id)] = "true"
    if account_deletion_url:
        # Without account creation in the app the console does not ask for the account
        # deletion link, only for the data deletion one.
        if not no_account:
            out[("PSL_ACCOUNT_DELETION_URL", "")] = account_deletion_url
        out[("PSL_DATA_DELETION_URL", "")] = account_deletion_url

    if methods:
        for method in ACCOUNT_CREATION_METHODS.values():
            out[("PSL_SUPPORTED_ACCOUNT_CREATION_METHODS", method)] = "true" if method in methods else ""
        out[("PSL_ACM_SPECIFY", "")] = account_creation_other if other_method else ""

    types = _resolve(external_account_types, EXTERNAL_ACCOUNT_TYPES, "external account type")
    if external_accounts is None:
        if types or external_account_description:
            raise ValueError("external_account_types/description need external_accounts=True.")
    elif external_accounts:
        if not types:
            raise ValueError(
                "external_accounts=True needs external_account_types (how those accounts are "
                f"created): {list(EXTERNAL_ACCOUNT_TYPES)}."
            )
        other_type = "PSL_OUTSIDE_APP_ACCOUNT_TYPE_OTHER" in types
        if other_type and not external_account_description:
            raise ValueError('The "other" external account type needs external_account_description.')
        out[("PSL_HAS_OUTSIDE_APP_ACCOUNTS", "")] = "true"
        for kind in EXTERNAL_ACCOUNT_TYPES.values():
            out[("PSL_OUTSIDE_APP_ACCOUNT_TYPES", kind)] = "true" if kind in types else ""
        out[("PSL_OUTSIDE_APP_ACCOUNT_TYPE_SPECIFY", "")] = (
            external_account_description if other_type else ""
        )
    else:
        if types or external_account_description:
            raise ValueError("external_accounts=False takes no types or description.")
        out[("PSL_HAS_OUTSIDE_APP_ACCOUNTS", "")] = "false"
        for kind in EXTERNAL_ACCOUNT_TYPES.values():
            out[("PSL_OUTSIDE_APP_ACCOUNT_TYPES", kind)] = ""
        out[("PSL_OUTSIDE_APP_ACCOUNT_TYPE_SPECIFY", "")] = ""

    for data_id, spec in collected.items():
        out[(spec["group"], data_id)] = "true"

        prefix = f"PSL_DATA_USAGE_RESPONSES:{data_id}"
        sharing = "PSL_DATA_USAGE_ONLY_SHARED" if spec.get("shared") else "PSL_DATA_USAGE_ONLY_COLLECTED"
        out[(f"{prefix}:PSL_DATA_USAGE_COLLECTION_AND_SHARING", sharing)] = "true"
        out[(f"{prefix}:PSL_DATA_USAGE_EPHEMERAL", "")] = "false"

        control = (
            "PSL_DATA_USAGE_USER_CONTROL_OPTIONAL"
            if spec.get("optional")
            else "PSL_DATA_USAGE_USER_CONTROL_REQUIRED"
        )
        out[(f"{prefix}:DATA_USAGE_USER_CONTROL", control)] = "true"

        for purpose in spec.get("purposes", ["PSL_APP_FUNCTIONALITY"]):
            out[(f"{prefix}:DATA_USAGE_COLLECTION_PURPOSE", purpose)] = "true"
            if spec.get("shared"):
                out[(f"{prefix}:DATA_USAGE_SHARING_PURPOSE", purpose)] = "true"

    return out


def fill_csv(
    template: Path, destination: Path, responses: dict[tuple[str, str], str]
) -> dict[str, Any]:
    """Write the exported CSV back out, filling in the "Response value" column."""
    with template.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        rows = list(reader)

    written = 0
    for row in rows:
        key = (row["Question ID (machine readable)"], row["Response ID (machine readable)"])
        if key in responses:
            row["Response value"] = responses[key]
            written += 1

    with destination.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    faltando = [k for k in responses if k not in {
        (r["Question ID (machine readable)"], r["Response ID (machine readable)"]) for r in rows
    }]
    return {
        "destination": str(destination),
        "total_rows": len(rows),
        "responses_written": written,
        "keys_not_found": faltando,
    }


def summarize(destination: Path) -> list[dict[str, str]]:
    """List what the CSV declares, for review before importing."""
    with destination.open(newline="", encoding="utf-8") as handle:
        return [
            {
                "question": row["Human-friendly question label"][:110],
                "response_id": row["Response ID (machine readable)"],
                "value": row["Response value"],
            }
            for row in csv.DictReader(handle)
            if row["Response value"].strip()
        ]

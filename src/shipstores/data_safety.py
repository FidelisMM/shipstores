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


def build_responses(
    collected: dict[str, DataTypeSpec],
    *,
    encrypted_in_transit: bool = True,
    supports_deletion: bool = True,
    account_deletion_url: str | None = None,
    account_methods: tuple[str, ...] = ("PSL_ACM_USER_ID_PASSWORD", "PSL_ACM_OAUTH"),
) -> dict[tuple[str, str], str]:
    """Build the (question_id, response_id) -> value map to write into the CSV.

    `collected` maps the data type's Response ID (e.g. "PSL_EMAIL") to a dict with:
        group    -> the group's question id (e.g. "PSL_DATA_TYPES_PERSONAL")
        shared   -> True if the data is also shared with third parties
        optional -> True if the user can use the app without providing the data
        purposes -> list of purposes (PSL_APP_FUNCTIONALITY, PSL_ACCOUNT_MANAGEMENT, ...)
    """
    out: dict[tuple[str, str], str] = {
        ("PSL_DATA_COLLECTION_COLLECTS_PERSONAL_DATA", ""): "true" if collected else "false",
        ("PSL_DATA_COLLECTION_ENCRYPTED_IN_TRANSIT", ""): str(encrypted_in_transit).lower(),
    }

    deletion_id = "DATA_DELETION_YES" if supports_deletion else "DATA_DELETION_NO"
    out[("PSL_SUPPORT_DATA_DELETION_BY_USER", deletion_id)] = "true"
    if account_deletion_url:
        out[("PSL_ACCOUNT_DELETION_URL", "")] = account_deletion_url
        out[("PSL_DATA_DELETION_URL", "")] = account_deletion_url

    for method in account_methods:
        out[("PSL_SUPPORTED_ACCOUNT_CREATION_METHODS", method)] = "true"

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

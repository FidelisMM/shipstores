import csv
import tempfile
import unittest
from pathlib import Path

from shipstores.data_safety import build_responses, fill_csv


class BuildResponsesTests(unittest.TestCase):
    def test_no_data_collected(self) -> None:
        responses = build_responses({})
        self.assertEqual(responses[("PSL_DATA_COLLECTION_COLLECTS_PERSONAL_DATA", "")], "false")

    def test_collection_sharing_and_user_control(self) -> None:
        prefix = "PSL_DATA_USAGE_RESPONSES:PSL_EMAIL"
        purposes = ["PSL_APP_FUNCTIONALITY", "PSL_ACCOUNT_MANAGEMENT"]
        for shared in (False, True):
            for optional in (False, True):
                with self.subTest(shared=shared, optional=optional):
                    responses = build_responses({"PSL_EMAIL": {
                        "group": "PSL_DATA_TYPES_PERSONAL", "shared": shared,
                        "optional": optional, "purposes": purposes,
                    }})
                    self.assertEqual(responses[("PSL_DATA_COLLECTION_COLLECTS_PERSONAL_DATA", "")], "true")
                    self.assertEqual(responses[("PSL_DATA_TYPES_PERSONAL", "PSL_EMAIL")], "true")
                    sharing = "PSL_DATA_USAGE_ONLY_SHARED" if shared else "PSL_DATA_USAGE_ONLY_COLLECTED"
                    opposite = "PSL_DATA_USAGE_ONLY_COLLECTED" if shared else "PSL_DATA_USAGE_ONLY_SHARED"
                    self.assertEqual(responses[(f"{prefix}:PSL_DATA_USAGE_COLLECTION_AND_SHARING", sharing)], "true")
                    self.assertNotIn((f"{prefix}:PSL_DATA_USAGE_COLLECTION_AND_SHARING", opposite), responses)
                    control = "OPTIONAL" if optional else "REQUIRED"
                    self.assertEqual(responses[(f"{prefix}:DATA_USAGE_USER_CONTROL", f"PSL_DATA_USAGE_USER_CONTROL_{control}")], "true")
                    self.assertEqual(responses[(f"{prefix}:PSL_DATA_USAGE_EPHEMERAL", "")], "false")
                    collection_keys = {key for key in responses if key[0] == f"{prefix}:DATA_USAGE_COLLECTION_PURPOSE"}
                    sharing_keys = {key for key in responses if key[0] == f"{prefix}:DATA_USAGE_SHARING_PURPOSE"}
                    self.assertEqual(collection_keys, {(f"{prefix}:DATA_USAGE_COLLECTION_PURPOSE", purpose) for purpose in purposes})
                    self.assertEqual(sharing_keys, {(f"{prefix}:DATA_USAGE_SHARING_PURPOSE", purpose) for purpose in purposes} if shared else set())
                    for purpose in purposes:
                        self.assertEqual(responses[(f"{prefix}:DATA_USAGE_COLLECTION_PURPOSE", purpose)], "true")
                        key = (f"{prefix}:DATA_USAGE_SHARING_PURPOSE", purpose)
                        if shared:
                            self.assertEqual(responses[key], "true")
                        else:
                            self.assertNotIn(key, responses)

    def test_omitted_purposes_default_to_app_functionality(self) -> None:
        responses = build_responses({"PSL_EMAIL": {"group": "PSL_DATA_TYPES_PERSONAL"}})
        key = ("PSL_DATA_USAGE_RESPONSES:PSL_EMAIL:DATA_USAGE_COLLECTION_PURPOSE", "PSL_APP_FUNCTIONALITY")
        self.assertEqual(responses[key], "true")
        self.assertEqual({entry for entry in responses if entry[0] == key[0]}, {key})

    def test_encryption_does_not_depend_on_deletion_support(self) -> None:
        for encrypted in (False, True):
            for deletion in (False, True):
                with self.subTest(encrypted=encrypted, deletion=deletion):
                    responses = build_responses({}, encrypted_in_transit=encrypted, supports_deletion=deletion)
                    self.assertEqual(responses[("PSL_DATA_COLLECTION_ENCRYPTED_IN_TRANSIT", "")], str(encrypted).lower())

    def test_deletion_encryption_and_account_methods(self) -> None:
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                responses = build_responses({}, supports_deletion=enabled, encrypted_in_transit=enabled)
                chosen = "DATA_DELETION_YES" if enabled else "DATA_DELETION_NO"
                other = "DATA_DELETION_NO" if enabled else "DATA_DELETION_YES"
                self.assertEqual(responses[("PSL_SUPPORT_DATA_DELETION_BY_USER", chosen)], "true")
                self.assertNotIn(("PSL_SUPPORT_DATA_DELETION_BY_USER", other), responses)
                self.assertEqual(responses[("PSL_DATA_COLLECTION_ENCRYPTED_IN_TRANSIT", "")], str(enabled).lower())
                self.assertNotIn(("PSL_ACCOUNT_DELETION_URL", ""), responses)
                for method in ("PSL_ACM_USER_ID_PASSWORD", "PSL_ACM_OAUTH"):
                    self.assertEqual(responses[("PSL_SUPPORTED_ACCOUNT_CREATION_METHODS", method)], "true")
        url = "https://example.com/delete-account"
        responses = build_responses({}, account_deletion_url=url, account_methods=("PSL_ACM_OAUTH",))
        self.assertEqual(responses[("PSL_ACCOUNT_DELETION_URL", "")], url)
        self.assertEqual(responses[("PSL_DATA_DELETION_URL", "")], url)
        self.assertEqual(responses[("PSL_SUPPORTED_ACCOUNT_CREATION_METHODS", "PSL_ACM_OAUTH")], "true")
        self.assertNotIn(("PSL_SUPPORTED_ACCOUNT_CREATION_METHODS", "PSL_ACM_USER_ID_PASSWORD"), responses)
        empty = build_responses({}, account_methods=())
        self.assertFalse(any(question == "PSL_SUPPORTED_ACCOUNT_CREATION_METHODS" for question, _ in empty))


class FillCsvTests(unittest.TestCase):
    def test_round_trip_preserves_unmatched_rows_and_reports_missing_keys(self) -> None:
        fields = ["Question ID (machine readable)", "Response ID (machine readable)",
                  "Response value", "Answer requirement", "Human-friendly question label"]
        rows = [
            ["collection", "", "old", "REQUIRED", "Données, collected"],
            ["control", "optional", "", "MULTIPLE_CHOICE", "Optional data"],
            ["untouched", "existing", "keep", "SINGLE_CHOICE", "Unchanged"],
        ]
        with tempfile.TemporaryDirectory() as directory:
            template = Path(directory) / "template.csv"
            destination = Path(directory) / "filled.csv"
            with template.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(fields)
                writer.writerows(rows)
            original = template.read_bytes()
            result = fill_csv(template, destination, {
                ("collection", ""): "false", ("control", "optional"): "true",
                ("missing", "answer"): "value",
            })
            with destination.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                output = list(reader)
                self.assertEqual(reader.fieldnames, fields)
            expected = [dict(zip(fields, row)) for row in rows]
            expected[0]["Response value"] = "false"
            expected[1]["Response value"] = "true"
            self.assertEqual(output, expected)
            self.assertEqual(template.read_bytes(), original)
            self.assertEqual(result, {"destination": str(destination), "total_rows": 3,
                                     "responses_written": 2, "keys_not_found": [("missing", "answer")]})


if __name__ == "__main__":
    unittest.main()

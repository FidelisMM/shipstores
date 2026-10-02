import json
import re
import unittest
from unittest.mock import patch

from shipstores import apple_console


class SetPrivacyTests(unittest.TestCase):
    def plan_for(self, usages):
        response = {"published": True}
        with patch.object(apple_console, "_run_iris", return_value=response) as run:
            self.assertIs(apple_console.set_privacy("test-app", usages), response)
        run.assert_called_once()
        app_id, body = run.call_args.args
        self.assertEqual(app_id, "test-app")
        match = re.search(r"const plan=(.*);", body)
        self.assertIsNotNone(match)
        return json.loads(match.group(1))

    def test_expands_categories_purposes_and_protections(self):
        plan = self.plan_for([
            {"category": "name", "purposes": ["analytics", "app_functionality"],
             "linked": False, "tracking": True},
            {"category": "EMAIL_ADDRESS", "purposes": ["OTHER_PURPOSES"]},
        ])
        self.assertEqual(plan, [
            {"category": "NAME", "purpose": purpose, "protection": protection}
            for purpose in ("ANALYTICS", "APP_FUNCTIONALITY")
            for protection in ("DATA_NOT_LINKED_TO_YOU", "DATA_USED_TO_TRACK_YOU")
        ] + [{"category": "EMAIL_ADDRESS", "purpose": "OTHER_PURPOSES",
              "protection": "DATA_LINKED_TO_YOU"}])

    def test_linked_tracking_expands_both_protections(self):
        self.assertEqual(self.plan_for([
            {"category": "NAME", "purposes": ["ANALYTICS"], "tracking": True},
        ]), [
            {"category": "NAME", "purpose": "ANALYTICS", "protection": protection}
            for protection in ("DATA_LINKED_TO_YOU", "DATA_USED_TO_TRACK_YOU")
        ])

    def test_empty_usages_passes_empty_plan(self):
        self.assertEqual(self.plan_for([]), [])

    def test_invalid_category_reports_options_without_running_iris(self):
        with patch.object(apple_console, "_run_iris") as run:
            with self.assertRaises(ValueError) as error:
                apple_console.set_privacy("test-app", [
                    {"category": "unknown", "purposes": ["ANALYTICS"]},
                ])
        self.assertEqual(str(error.exception),
                         "Invalid category: UNKNOWN. Options: "
                         + ", ".join(apple_console.DATA_CATEGORIES))
        run.assert_not_called()

    def test_invalid_purposes_report_options_without_running_iris(self):
        cases = [
            ({}, "(none)"),
            ({"purposes": []}, "(none)"),
            ({"purposes": None}, "(none)"),
            ({"purposes": ["analytics", "unknown"]}, "['UNKNOWN']"),
        ]
        for fields, invalid in cases:
            with self.subTest(fields=fields):
                with patch.object(apple_console, "_run_iris") as run:
                    with self.assertRaises(ValueError) as error:
                        apple_console.set_privacy("test-app", [
                            {"category": "NAME", "purposes": ["ANALYTICS"]},
                            {"category": "name", **fields},
                        ])
                self.assertEqual(str(error.exception),
                                 f"NAME: invalid purposes {invalid}. Options: "
                                 + ", ".join(apple_console.DATA_PURPOSES))
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

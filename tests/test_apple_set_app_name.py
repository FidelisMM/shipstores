import unittest

from fake_asc import ASCTestCase
from shipstores import apple, server

APP = "1000000002"  # fictional app
TAKEN = {
    "errors": [{
        "status": "409",
        "code": "ENTITY_ERROR.ATTRIBUTE.INVALID.DUPLICATE",
        "title": "The provided entity includes an attribute with a value that has already been used",
        "detail": "The App Name you entered is already being used.",
    }]
}


class SetAppNameTests(ASCTestCase):
    def setUp(self) -> None:
        """An app with one appInfo on sale and another being prepared, in two languages."""
        super().setUp()
        self.asc.on("GET", f"/v1/apps/{APP}/appInfos", lambda *_: (200, {"data": [
            {"id": "info-live", "attributes": {"state": "READY_FOR_DISTRIBUTION"}},
            {"id": "info-edit", "attributes": {"state": "PREPARE_FOR_SUBMISSION"}},
        ]}))
        self.asc.on("GET", "/v1/appInfos/info-edit/appInfoLocalizations", lambda *_: (200, {"data": [
            {"id": "loc-pt", "attributes": {"locale": "pt-BR", "name": "Agenda Exemplo"}},
            {"id": "loc-en", "attributes": {"locale": "en-US", "name": "Example Planner"}},
        ]}))

        def patch_name(_m, _p, _params, body):
            if body["data"]["attributes"]["name"] == "Taken Name":
                return 409, TAKEN
            return 200, body

        for loc in ("loc-pt", "loc-en"):
            self.asc.on("PATCH", f"/v1/appInfoLocalizations/{loc}", patch_name)

    def _patches(self) -> list[tuple[str, str]]:
        return [(path, body["data"]["attributes"]["name"]) for m, path, _, body in self.asc.calls if m == "PATCH"]

    def test_changes_every_language_of_the_editable_appinfo(self) -> None:
        result = server.apple_set_app_name(APP, "New Planner")

        self.assertEqual(result["name"], "New Planner")
        self.assertEqual(result["locales"], ["pt-BR", "en-US"])
        self.assertEqual(self._patches(), [
            ("/v1/appInfoLocalizations/loc-pt", "New Planner"),
            ("/v1/appInfoLocalizations/loc-en", "New Planner"),
        ])

    def test_locale_restricts_to_one_language(self) -> None:
        server.apple_set_app_name(APP, "New Planner", locale="en-US")
        self.assertEqual(self._patches(), [("/v1/appInfoLocalizations/loc-en", "New Planner")])

    def test_unknown_locale_fails_before_changing_anything(self) -> None:
        with self.assertRaisesRegex(apple.AppleError, "no fr-FR localization"):
            server.apple_set_app_name(APP, "New Planner", locale="fr-FR")
        self.assertEqual(self._patches(), [])

    def test_taken_name_tries_alternatives_and_stops_at_the_first_accepted(self) -> None:
        result = server.apple_set_app_name(
            APP, "Taken Name", alternatives=["A Name Far Too Long For The Store", "Free Planner", "Never Tried"]
        )

        self.assertEqual(result["name"], "Free Planner")
        self.assertEqual([r["name"] for r in result["refused"]], ["Taken Name", "A Name Far Too Long For The Store"])
        self.assertIn("already used", result["refused"][0]["reason"])
        self.assertNotIn("Never Tried", [n for _, n in self._patches()])

    def test_all_refused_explains_each_reason(self) -> None:
        with self.assertRaisesRegex(apple.AppleError, "none of the names.*'Taken Name' \\(already used"):
            server.apple_set_app_name(APP, "Taken Name")

    def test_other_errors_do_not_move_on_to_alternatives(self) -> None:
        self.asc.on("PATCH", "/v1/appInfoLocalizations/loc-pt",
                    lambda *_: (403, {"errors": [{"status": "403", "detail": "forbidden"}]}))

        with self.assertRaisesRegex(apple.AppleError, "HTTP 403"):
            server.apple_set_app_name(APP, "New Planner", alternatives=["Free Planner"])
        self.assertEqual(self._patches(), [("/v1/appInfoLocalizations/loc-pt", "New Planner")])

    def test_editable_appinfo_with_the_legacy_state_field(self) -> None:
        self.asc.on("GET", f"/v1/apps/{APP}/appInfos", lambda *_: (200, {"data": [
            {"id": "info-live", "attributes": {"appStoreState": "READY_FOR_SALE"}},
            {"id": "info-edit", "attributes": {"appStoreState": "PREPARE_FOR_SUBMISSION"}},
        ]}))
        self.assertEqual(server._editable_app_info(APP), "info-edit")


if __name__ == "__main__":
    unittest.main()

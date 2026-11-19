import unittest
from unittest.mock import patch

from fake_asc import ASCTestCase
from shipstores import apple, server

APP = "1000000001"  # fictional app
VERSION = "ver-1"
DRAFT = "draft-1"


def _version(*_):
    return 200, {"data": {"id": VERSION, "type": "appStoreVersions",
                          "relationships": {"app": {"data": {"type": "apps", "id": APP}}}}}


def _item(n: int, version_id: str | None = None) -> dict:
    rel = {"appStoreVersion": {"data": {"type": "appStoreVersions", "id": version_id} if version_id else None}}
    return {"id": f"item-{n}", "type": "reviewSubmissionItems", "attributes": {"state": "READY_FOR_REVIEW"},
            "relationships": rel}


def _no_pending_version(*_):
    return 409, {"errors": [{"status": "409", "detail": "Subscription monthly has no pending version for submission"}]}


class SubmitForReviewTests(ASCTestCase):
    def _setup(self, *, draft: bool, states: dict[str, str], items_after: int | None = None) -> None:
        """An app with an open draft (or none) and subscriptions in the given states."""
        asc = self.asc
        asc.on("GET", f"/v1/appStoreVersions/{VERSION}", _version)
        submissions = [{"id": DRAFT, "type": "reviewSubmissions",
                        "attributes": {"platform": "IOS", "state": "READY_FOR_REVIEW"}}] if draft else []
        asc.on("GET", f"/v1/apps/{APP}/reviewSubmissions", lambda *_: (200, {"data": submissions}))
        asc.on("POST", "/v1/reviewSubmissions", lambda *_: (201, {"data": {"id": DRAFT, "type": "reviewSubmissions"}}))
        asc.on("POST", "/v1/reviewSubmissionItems", lambda *_: (201, {"data": {"id": "item-v"}}))
        groups = {"data": [{"id": "grp-1", "type": "subscriptionGroups", "attributes": {"referenceName": "Plans"}}]}
        subs = {"data": [{"id": sid, "type": "subscriptions", "attributes": {"productId": sid, "state": st}}
                         for sid, st in states.items()]}
        asc.on("GET", f"/v1/apps/{APP}/subscriptionGroups", lambda *_: (200, groups))
        asc.on("GET", "/v1/subscriptionGroups/grp-1/subscriptions", lambda *_: (200, subs))
        asc.on("POST", "/v1/subscriptionSubmissions", lambda *_: (201, {"data": {"id": "ss"}}))
        asc.on("POST", "/v1/subscriptionGroupSubmissions", lambda *_: (201, {"data": {"id": "gs"}}))
        ready = sum(1 for st in states.values() if st == "READY_TO_SUBMIT")
        total = items_after if items_after is not None else 1 + ready + (1 if ready else 0)
        asc.on("GET", f"/v1/reviewSubmissions/{DRAFT}/items",
               lambda *_: (200, {"data": [_item(n) for n in range(total)]}))
        asc.on("PATCH", f"/v1/reviewSubmissions/{DRAFT}",
               lambda *_: (200, {"data": {"id": DRAFT, "attributes": {"state": "WAITING_FOR_REVIEW"}}}))

    def _console(self, **kwargs):
        p = patch.object(server.apple_console, "add_to_review_draft", **kwargs)
        mock = p.start()
        self.addCleanup(p.stop)
        return mock

    def _patched(self) -> bool:
        return any(m == "PATCH" for m, *_ in self.asc.calls)

    def test_ready_subscriptions_and_their_group_go_in_the_same_submission(self) -> None:
        self._setup(draft=False, states={"monthly": "READY_TO_SUBMIT", "yearly": "READY_TO_SUBMIT",
                                         "legacy": "APPROVED"})

        result = server.apple_submit_for_review(VERSION)

        subs = [b["data"]["relationships"]["subscription"]["data"]["id"]
                for b in self.asc.bodies("POST", "/v1/subscriptionSubmissions")]
        self.assertEqual(subs, ["monthly", "yearly"])
        groups = [b["data"]["relationships"]["subscriptionGroup"]["data"]["id"]
                  for b in self.asc.bodies("POST", "/v1/subscriptionGroupSubmissions")]
        self.assertEqual(groups, ["grp-1"])
        self.assertTrue(result["submitted"])
        self.assertEqual(result["items"], 4)
        self.assertEqual(result["subscriptions"], ["monthly", "yearly"])
        # The version joins the draft before the subscriptions: without it Apple refuses the first one.
        order = [p for m, p, _, _ in self.asc.calls if m == "POST"]
        self.assertLess(order.index("/v1/reviewSubmissionItems"), order.index("/v1/subscriptionSubmissions"))
        first_patch = next((m, p) for m, p, _, _ in self.asc.calls if m == "PATCH")
        self.assertEqual(first_patch, ("PATCH", f"/v1/reviewSubmissions/{DRAFT}"))

    def test_reuses_a_draft_that_already_has_the_version(self) -> None:
        self._setup(draft=True, states={"monthly": "READY_TO_SUBMIT"})
        items = iter([[_item(0, VERSION)], [_item(n) for n in range(3)]])
        self.asc.on("GET", f"/v1/reviewSubmissions/{DRAFT}/items", lambda *_: (200, {"data": next(items)}))

        result = server.apple_submit_for_review(VERSION)

        self.assertEqual(self.asc.bodies("POST", "/v1/reviewSubmissions"), [])
        self.assertEqual(self.asc.bodies("POST", "/v1/reviewSubmissionItems"), [])
        self.assertEqual(result["review_submission_id"], DRAFT)
        self.assertEqual(result["items"], 3)

    def test_without_ready_subscriptions_only_the_version_goes(self) -> None:
        self._setup(draft=False, states={"legacy": "APPROVED"})

        result = server.apple_submit_for_review(VERSION)

        self.assertEqual(self.asc.bodies("POST", "/v1/subscriptionSubmissions"), [])
        self.assertEqual(self.asc.bodies("POST", "/v1/subscriptionGroupSubmissions"), [])
        self.assertEqual(result["items"], 1)

    def test_include_subscriptions_false_leaves_subscriptions_alone(self) -> None:
        self._setup(draft=False, states={"monthly": "READY_TO_SUBMIT"}, items_after=1)

        server.apple_submit_for_review(VERSION, include_subscriptions=False)

        self.assertFalse(any("/subscription" in p for _, p, _, _ in self.asc.calls))

    def test_does_not_submit_when_a_subscription_missed_the_draft(self) -> None:
        # Apple accepted the POST but the item landed elsewhere: submitting would send the version alone.
        self._setup(draft=False, states={"monthly": "READY_TO_SUBMIT"}, items_after=1)

        with self.assertRaisesRegex(apple.AppleError, "expected 3 "):
            server.apple_submit_for_review(VERSION)

        self.assertFalse(self._patched())

    def test_no_pending_version_goes_in_through_the_console(self) -> None:
        # After a canceled submission the API refuses the subscription and the console accepts it.
        self._setup(draft=False, states={"monthly": "READY_TO_SUBMIT"})
        self.asc.on("POST", "/v1/subscriptionSubmissions", _no_pending_version)
        self.asc.on("POST", "/v1/subscriptionGroupSubmissions", _no_pending_version)
        console = self._console(return_value="ok")

        result = server.apple_submit_for_review(VERSION)

        self.assertEqual([c.args for c in console.call_args_list],
                         [(APP, "subscriptions", "monthly"), (APP, "subscription-groups", "grp-1")])
        self.assertTrue(result["submitted"])
        self.assertEqual((result["subscriptions"], result["groups"]), (["monthly"], ["grp-1"]))
        self.assertEqual(len(result["warnings"]), 2)

    def test_does_not_submit_when_the_console_refuses_the_subscription_too(self) -> None:
        self._setup(draft=False, states={"monthly": "READY_TO_SUBMIT"})
        self.asc.on("POST", "/v1/subscriptionSubmissions", _no_pending_version)
        self._console(side_effect=server.browser.BrowserError("button did not appear"))

        with self.assertRaisesRegex(server.browser.BrowserError, "button"):
            server.apple_submit_for_review(VERSION)

        self.assertFalse(self._patched())

    def test_group_refused_everywhere_becomes_a_warning(self) -> None:
        self._setup(draft=False, states={"monthly": "READY_TO_SUBMIT"}, items_after=2)
        self.asc.on("POST", "/v1/subscriptionGroupSubmissions", _no_pending_version)
        self._console(side_effect=server.browser.BrowserError("menu has no open draft"))

        result = server.apple_submit_for_review(VERSION)

        self.assertEqual(result["groups"], [])
        self.assertIn("menu has no open draft", result["warnings"][0])

    def test_other_subscription_errors_do_not_go_to_the_console(self) -> None:
        self._setup(draft=False, states={"monthly": "READY_TO_SUBMIT"})
        self.asc.on("POST", "/v1/subscriptionSubmissions",
                    lambda *_: (409, {"errors": [{"status": "409", "detail": "missing price"}]}))
        console = self._console()

        with self.assertRaisesRegex(apple.AppleError, "missing price"):
            server.apple_submit_for_review(VERSION)
        console.assert_not_called()

    def test_asks_for_items_with_the_version_included(self) -> None:
        # Without the include Apple omits the relationship, the version looks absent and the POST gets 409.
        self._setup(draft=True, states={})
        self.asc.on("GET", f"/v1/reviewSubmissions/{DRAFT}/items", lambda *_: (200, {"data": [_item(0, VERSION)]}))

        server.apple_submit_for_review(VERSION)

        gets = [params for m, p, params, _ in self.asc.calls if m == "GET" and p.endswith("/items")]
        self.assertEqual(gets[0], {"include": "appStoreVersion"})
        self.assertEqual(self.asc.bodies("POST", "/v1/reviewSubmissionItems"), [])


class CancelSubmissionTests(ASCTestCase):
    def test_cancels_a_rejected_submission_and_waits_for_canceling(self) -> None:
        sleep = patch.object(server.time, "sleep", lambda _s: None)
        sleep.start()
        self.addCleanup(sleep.stop)
        self.asc.on("GET", f"/v1/apps/{APP}/reviewSubmissions", lambda *_: (200, {"data": [
            {"id": "sub-r", "type": "reviewSubmissions", "attributes": {"platform": "IOS", "state": "UNRESOLVED_ISSUES"}}]}))
        self.asc.on("PATCH", "/v1/reviewSubmissions/sub-r",
                    lambda *_: (200, {"data": {"id": "sub-r", "attributes": {"state": "CANCELING"}}}))
        states = iter(["CANCELING", "COMPLETE"])
        self.asc.on("GET", "/v1/reviewSubmissions/sub-r",
                    lambda *_: (200, {"data": {"id": "sub-r", "attributes": {"state": next(states)}}}))

        self.assertEqual(server.apple_cancel_submission(APP), {"review_submission_id": "sub-r", "state": "COMPLETE"})


class AddToReviewDraftTests(unittest.TestCase):
    def test_banner_or_ok_is_success_and_anything_else_raises(self) -> None:
        with patch.object(server.apple_console.browser, "run_harness", return_value="URL=x\n__SP__ok"):
            self.assertEqual(server.apple_console.add_to_review_draft(APP, "subscriptions", "s1"), "ok")
        with patch.object(server.apple_console.browser, "run_harness",
                          return_value="__SP__ERROR Add for Review button stayed disabled for 45 s"):
            with self.assertRaisesRegex(server.browser.BrowserError, "stayed disabled"):
                server.apple_console.add_to_review_draft(APP, "subscription-groups", "g1")

    def test_rejects_unknown_kinds(self) -> None:
        with self.assertRaises(ValueError):
            server.apple_console.add_to_review_draft(APP, "versions", "v1")


if __name__ == "__main__":
    unittest.main()

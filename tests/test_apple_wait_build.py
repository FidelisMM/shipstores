import unittest
from unittest.mock import patch

from fake_asc import ASCTestCase
from shipstores import apple, server

APP = "1000000001"  # fictional app


def _build(state: str, build_id: str = "build-abc") -> dict:
    return {"data": [{"id": build_id, "type": "builds",
                      "attributes": {"version": "7", "processingState": state,
                                     "uploadedDate": "2026-10-11T12:00:00Z"}}]}


class WaitBuildTests(ASCTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sleeps: list[float] = []
        p = patch.object(server.time, "sleep", self.sleeps.append)
        p.start()
        self.addCleanup(p.stop)

    def test_waits_for_it_to_appear_and_process_until_valid(self) -> None:
        responses = iter([{"data": []}, _build("PROCESSING"), _build("VALID")])
        self.asc.on("GET", "/v1/builds", lambda *_: (200, next(responses)))

        result = server.apple_wait_build(APP, "7")

        self.assertEqual(result["build_id"], "build-abc")
        self.assertEqual(result["processing_state"], "VALID")
        self.assertNotIn("warning", result)
        self.assertEqual(len(self.sleeps), 2)
        params = self.asc.calls[0][2]
        self.assertEqual(params["filter[app]"], APP)
        self.assertEqual(params["filter[version]"], "7")

    def test_final_rejected_states_come_back_with_a_warning(self) -> None:
        for state in ("FAILED", "INVALID"):
            with self.subTest(state=state):
                self.asc.on("GET", "/v1/builds", lambda *_, st=state: (200, _build(st)))
                result = server.apple_wait_build(APP, "7")
                self.assertEqual(result["processing_state"], state)
                self.assertIn("warning", result)

    def test_timeout_reports_the_last_state(self) -> None:
        self.asc.on("GET", "/v1/builds", lambda *_: (200, _build("PROCESSING")))

        with self.assertRaisesRegex(apple.AppleError, "still PROCESSING"):
            server.apple_wait_build(APP, "7", timeout_min=1)

        # 1 min / 30 s = 2 polls, with one wait between them
        self.assertEqual(len(self.asc.calls), 2)
        self.assertEqual(self.sleeps, [server.BUILD_POLL_SECONDS])


if __name__ == "__main__":
    unittest.main()

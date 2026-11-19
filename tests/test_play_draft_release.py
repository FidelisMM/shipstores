import unittest
from contextlib import nullcontext
from unittest.mock import patch

from shipstores import play, server


class DraftReleaseTests(unittest.TestCase):
    def invoke(self, error=None, status="completed"):
        with (
            patch.object(server.Path, "exists", return_value=True),
            patch.object(play, "edit_session", return_value=nullcontext(("edit", {}))),
            patch.object(play, "upload_bundle", return_value={"versionCode": "1"}),
            patch.object(play, "assign_track", side_effect=error) as assign,
        ):
            try:
                return server.play_upload_bundle("example.test", "placeholder.aab", status=status)
            finally:
                assign.assert_called_once()
                self.assertEqual(assign.call_args.kwargs["status"], status)

    def test_draft_rejection_has_guidance(self):
        error = play.PlayError("Play -> HTTP 400: Only releases with status draft may be created on draft app.")
        with self.assertRaisesRegex(play.PlayError, 'status="draft"') as caught:
            self.invoke(error)
        self.assertIs(caught.exception.__cause__, error)

    def test_other_errors_preserve_identity(self):
        for error in (play.PlayError("HTTP 403: forbidden"), RuntimeError("Only releases with status draft may be created on draft app.")):
            with self.subTest(error=error), self.assertRaises(type(error)) as caught:
                self.invoke(error)
            self.assertIs(caught.exception, error)

    def test_success_preserves_status(self):
        for status in ("completed", "draft"):
            self.assertEqual(self.invoke(status=status), {"version_code": 1, "track": "internal", "status": status, "committed": True})

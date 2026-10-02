import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shipstores import server


class ReplyReviewConfirmTests(unittest.TestCase):
    def setUp(self) -> None:
        server._SENT_REPLY_TOKENS.clear()
        self.submission = patch.object(server, "_active_submission", return_value="sub-1")
        self.reply = patch.object(server.apple_review, "reply")
        self.submission.start()
        self.reply_mock = self.reply.start()

    def tearDown(self) -> None:
        self.reply.stop()
        self.submission.stop()

    def test_default_returns_preview_and_token_without_sending(self) -> None:
        result = server.apple_reply_review("123", "Here is the demo video.")
        self.reply_mock.assert_not_called()
        self.assertFalse(result["sent"])
        self.assertEqual(result["preview"]["text"], "Here is the demo video.")
        self.assertTrue(result["confirm_token"])

    def test_confirm_with_matching_token_sends(self) -> None:
        token = server.apple_reply_review("123", "Here is the demo video.")["confirm_token"]
        result = server.apple_reply_review("123", "Here is the demo video.", confirm_token=token)
        self.reply_mock.assert_called_once_with("123", "sub-1", "Here is the demo video.", [])
        self.assertTrue(result["sent"])

    def test_changed_text_after_preview_is_rejected(self) -> None:
        token = server.apple_reply_review("123", "Here is the demo video.")["confirm_token"]
        with self.assertRaises(ValueError):
            server.apple_reply_review("123", "Something else entirely.", confirm_token=token)
        self.reply_mock.assert_not_called()

    def test_changed_attachment_after_preview_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            note = Path(tmp) / "notes.pdf"
            note.write_bytes(b"%PDF v1")
            token = server.apple_reply_review("123", "See attached.", [str(note)])["confirm_token"]
            note.write_bytes(b"%PDF v2")
            with self.assertRaises(ValueError):
                server.apple_reply_review("123", "See attached.", [str(note)], confirm_token=token)
        self.reply_mock.assert_not_called()

    def test_changed_submission_after_preview_is_rejected(self) -> None:
        token = server.apple_reply_review("123", "Hi")["confirm_token"]
        with patch.object(server, "_active_submission", return_value="sub-2"):
            with self.assertRaises(ValueError):
                server.apple_reply_review("123", "Hi", confirm_token=token)
        self.reply_mock.assert_not_called()

    def test_two_confirms_in_a_row_send_once(self) -> None:
        token = server.apple_reply_review("123", "Hi")["confirm_token"]
        server.apple_reply_review("123", "Hi", confirm_token=token)
        with self.assertRaises(ValueError):
            server.apple_reply_review("123", "Hi", confirm_token=token)
        self.reply_mock.assert_called_once()

    def test_failure_before_send_allows_retry(self) -> None:
        token = server.apple_reply_review("123", "Hi")["confirm_token"]
        self.reply_mock.side_effect = [RuntimeError("ERROR: the reply box did not open"), "sent"]
        with self.assertRaises(RuntimeError):
            server.apple_reply_review("123", "Hi", confirm_token=token)
        server.apple_reply_review("123", "Hi", confirm_token=token)
        self.assertEqual(self.reply_mock.call_count, 2)

    def test_failure_after_send_keeps_token_burned(self) -> None:
        token = server.apple_reply_review("123", "Hi")["confirm_token"]
        self.reply_mock.side_effect = RuntimeError("ERROR: the message did not appear in the thread after sending")
        with self.assertRaises(RuntimeError):
            server.apple_reply_review("123", "Hi", confirm_token=token)
        with self.assertRaises(ValueError):
            server.apple_reply_review("123", "Hi", confirm_token=token)
        self.assertEqual(self.reply_mock.call_count, 1)

    def test_validation_runs_before_preview(self) -> None:
        with self.assertRaises(ValueError):
            server.apple_reply_review("123", "x" * 4001)
        with self.assertRaises(ValueError):
            server.apple_reply_review("123", "   ")
        with self.assertRaises(FileNotFoundError):
            server.apple_reply_review("123", "See attached.", ["/nonexistent/video.mov"])
        self.reply_mock.assert_not_called()


class ToolAnnotationTests(unittest.TestCase):
    def test_external_action_tools_are_annotated(self) -> None:
        tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
        for name in ("apple_reply_review", "apple_submit_for_review", "play_promote_release", "eas_submit"):
            annotations = tools[name].annotations
            self.assertIsNotNone(annotations, name)
            self.assertTrue(annotations.destructiveHint, name)
            self.assertFalse(annotations.readOnlyHint, name)

    def test_read_tools_are_not_flagged_destructive(self) -> None:
        tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
        self.assertIsNone(tools["apple_review_messages"].annotations)


if __name__ == "__main__":
    unittest.main()

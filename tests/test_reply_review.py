import asyncio
import subprocess
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
        self.reply_mock.side_effect = [server.apple_review.ReplyNotSent("ERROR: the reply box did not open"), "sent"]
        with self.assertRaises(server.apple_review.ReplyNotSent):
            server.apple_reply_review("123", "Hi", confirm_token=token)
        server.apple_reply_review("123", "Hi", confirm_token=token)
        self.assertEqual(self.reply_mock.call_count, 2)

    def test_failure_after_send_keeps_token_burned(self) -> None:
        token = server.apple_reply_review("123", "Hi")["confirm_token"]
        self.reply_mock.side_effect = server.browser.BrowserError("ERROR: the message did not appear in the thread")
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


class ReplyOutcomeTests(unittest.TestCase):
    """apple_review.reply says "not sent" only when the click provably did not happen."""

    def _reply(self, **harness):
        with patch.object(server.apple_review.browser, "run_harness", **harness):
            return server.apple_review.reply("123", "sub-1", "Hi", [])

    def test_script_error_before_click_is_not_sent(self) -> None:
        error = server.browser.BrowserError("browser-harness failed (exit 1):\nERROR: the reply box did not open")
        with self.assertRaises(server.apple_review.ReplyNotSent):
            self._reply(side_effect=error)

    def test_script_error_after_click_may_be_sent(self) -> None:
        error = server.browser.BrowserError(
            "browser-harness failed (exit 1):\n__SP__clicked\nERROR: the message did not appear in the thread"
        )
        with self.assertRaises(server.browser.BrowserError) as ctx:
            self._reply(side_effect=error)
        self.assertNotIsInstance(ctx.exception, server.apple_review.ReplyNotSent)

    def test_timeout_after_click_may_be_sent(self) -> None:
        timeout = subprocess.TimeoutExpired("browser-harness", 600, output="__SP__clicked\n")
        with self.assertRaises(server.browser.BrowserError) as ctx:
            self._reply(side_effect=timeout)
        self.assertNotIsInstance(ctx.exception, server.apple_review.ReplyNotSent)

    def test_timeout_without_output_may_be_sent(self) -> None:
        with self.assertRaises(server.browser.BrowserError) as ctx:
            self._reply(side_effect=subprocess.TimeoutExpired("browser-harness", 600))
        self.assertNotIsInstance(ctx.exception, server.apple_review.ReplyNotSent)

    def test_other_errors_may_be_sent(self) -> None:
        with self.assertRaises(server.browser.BrowserError) as ctx:
            self._reply(side_effect=server.browser.BrowserError("Logged out of the Apple console"))
        self.assertNotIsInstance(ctx.exception, server.apple_review.ReplyNotSent)


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

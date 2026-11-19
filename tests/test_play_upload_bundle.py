import asyncio
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from shipstores import server


class PlayUploadBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.bundle = Path(tmp.name) / "test.aab"
        self.bundle.write_bytes(b"test bundle")
        self.package = "com.example.app"
        self.headers = {"X-Test": "example"}
        self.edit_session = self.enterContext(patch.object(server.play, "edit_session"))
        self.upload = self.enterContext(patch.object(server.play, "upload_bundle"))
        self.assign = self.enterContext(patch.object(server.play, "assign_track"))
        self.steps: list[str] = []
        self.edit_session.side_effect = self._edit_session
        self.upload.side_effect = self._upload_bundle
        self.assign.side_effect = self._assign_track

    @contextmanager
    def _edit_session(self, _package):
        self.steps.append("open edit")
        yield "edit-1", self.headers
        self.steps.append("commit edit")

    def _upload_bundle(self, *_args):
        self.steps.append("upload")
        return {"versionCode": "42"}

    def _assign_track(self, *_args, **_kwargs):
        self.steps.append("assign")

    def assertRelease(self, expected_notes, **kwargs):
        result = server.play_upload_bundle(self.package, str(self.bundle), **kwargs)
        self.edit_session.assert_called_once_with(self.package)
        self.upload.assert_called_once_with(self.package, self.bundle, "edit-1", self.headers)
        self.assign.assert_called_once_with(
            self.package,
            "internal",
            [42],
            "edit-1",
            self.headers,
            status="completed",
            release_name=None,
            release_notes=expected_notes,
        )
        self.assertEqual(self.steps, ["open edit", "upload", "assign", "commit edit"])
        self.assertEqual(
            result, {"version_code": 42, "track": "internal", "status": "completed", "committed": True},
        )

    def test_notes_in_multiple_locales_and_exact_character_limit(self):
        notes = {"en-US": "Crash fixed on startup.", "es-419": "á" * 500}
        self.assertRelease(notes, release_notes=notes)

    def test_merges_legacy_note_without_mutating_callers_dict(self):
        notes = {"en-US": "Crash fixed on startup."}
        self.assertRelease(
            {**notes, "pt-BR": "Falha corrigida."},
            release_notes=notes,
            release_notes_pt_br="Falha corrigida.",
        )
        self.assertEqual(notes, {"en-US": "Crash fixed on startup."})

    def test_explicit_pt_br_note_takes_precedence(self):
        self.assertRelease(
            {"pt-BR": "Texto novo"},
            release_notes={"pt-BR": "Texto novo"},
            release_notes_pt_br="Texto antigo",
        )

    def test_legacy_positional_parameter_still_works(self):
        server.play_upload_bundle(
            self.package, str(self.bundle), "beta", "draft", "Test release", "Falha corrigida.",
        )
        self.assign.assert_called_once_with(
            self.package, "beta", [42], "edit-1", self.headers,
            status="draft", release_name="Test release", release_notes={"pt-BR": "Falha corrigida."},
        )

    def test_no_notes_are_passed_as_none(self):
        self.assertRelease(None)

    def test_overlong_notes_fail_without_opening_an_edit(self):
        for kwargs, locale in [
            ({"release_notes": {"en-US": "OK", "es-419": "á" * 501}}, "es-419"),
            ({"release_notes_pt_br": "á" * 501}, "pt-BR"),
        ]:
            with self.subTest(locale=locale):
                with self.assertRaisesRegex(ValueError, f"{locale}.*500"):
                    server.play_upload_bundle(self.package, str(self.bundle), **kwargs)
        self.edit_session.assert_not_called()
        self.upload.assert_not_called()
        self.assign.assert_not_called()

    def test_mcp_schema_includes_locale_map_and_preserves_action_annotation(self):
        tools = asyncio.run(server.mcp.list_tools())
        tool = next(tool for tool in tools if tool.name == "play_upload_bundle")
        props = tool.parameters["properties"]
        self.assertIn({"type": "object", "additionalProperties": {"type": "string"}},
                      props["release_notes"]["anyOf"])
        self.assertIn("release_notes_pt_br", props)
        self.assertTrue(tool.annotations.destructiveHint)

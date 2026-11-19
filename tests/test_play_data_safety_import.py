"""The Data safety import flow, run against a fake console state machine."""

import contextlib
import io
import json
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from shipstores import play_console, server

UPLOAD = "Para importar as respostas, faça upload do arquivo"
UPLOADED = UPLOAD + "\nO upload do arquivo CSV foi concluído"
CONFIRM = "Importar de arquivo CSV?\nIsso substitui as respostas"


class FakeConsole:
    def __init__(self, stuck_at_step: int | None = None, applies: bool = True) -> None:
        self.dialogs: list[str] = []
        self.step: int | None = None
        self.saved = False
        self.stuck_at_step = stuck_at_step
        self.applies = applies
        self.clock = 0.0

    # browser helpers -------------------------------------------------------
    def js(self, expression: str):
        if "mat-error" in expression:
            return json.dumps(["Required answer"] if self.stuck_at_step else [])
        if "[role=dialog]" in expression:
            return json.dumps(self.dialogs)
        return ""

    def body(self) -> str:
        text = f"Etapa {self.step} de 5" if self.step else "Segurança dos dados"
        return text + ("\nA mudança foi salva" if self.saved else "")

    def cdp(self, method: str, **params):
        if method == "DOM.getDocument":
            return {"root": {"nodeId": 1}}
        if method == "DOM.querySelector":
            return {"nodeId": 5}
        if method == "DOM.setFileInputFiles":
            self.dialogs = [UPLOADED]
        return {}

    def ax_click(self, pattern: str, pause: float = 2, **_) -> str:
        if pattern == r"^Importar de arquivo CSV$":
            self.dialogs = [UPLOAD]
        elif pattern == r"^Avançar$" and self.step != self.stuck_at_step:
            self.step += 1
        elif pattern == r"^Salvar$" and self.step == 5:
            self.saved = True
        return "ax_click: clicked"

    def real_click(self, finder: str, pause: float = 5) -> str:
        label = re.search(r"==='([^']+)'", finder).group(1)
        if label == "Importar" and self.dialogs == [UPLOADED]:
            self.dialogs = [UPLOADED, CONFIRM]
        elif label == "Importar" and self.dialogs[-1:] == [CONFIRM] and self.applies:
            self.dialogs, self.step = [], 2
        elif label == "Cancelar":
            self.dialogs = [UPLOADED]
        return "clicked"

    def sleep(self, seconds: float) -> None:
        self.clock += seconds

    # run the tool ----------------------------------------------------------
    def run(self, dev_id, app_id, body, timeout=300) -> str:
        env = {
            "js": self.js, "body": self.body, "cdp": self.cdp, "ax_click": self.ax_click,
            "real_click": self.real_click, "goto_url": lambda url: None,
            "wait_for_load": lambda: True, "json": json, "re": re, "time": time,
        }
        out = io.StringIO()
        with patch("time.sleep", self.sleep), patch("time.time", lambda: self.clock), \
                contextlib.redirect_stdout(out):
            exec(body, env)
        return out.getvalue()


class DataSafetyImportTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.csv = Path(tmp.name) / "filled.csv"
        self.csv.write_text("Question ID (machine readable)\n")
        p = patch.object(server, "_console_ids", return_value=("dev", "app"))
        p.start()
        self.addCleanup(p.stop)

    def _import(self, console: FakeConsole, dry_run: bool = False) -> dict:
        with patch.object(play_console, "run", console.run):
            return server.play_data_safety_import("com.example.app", str(self.csv), dry_run=dry_run)

    def test_confirms_both_dialogs_walks_the_wizard_and_saves(self) -> None:
        result = self._import(FakeConsole())
        self.assertTrue(result["upload_ok"])
        self.assertTrue(result["confirm_dialog"])
        self.assertTrue(result["imported"])
        self.assertEqual(result["steps"], [2, 3, 4, 5])
        self.assertTrue(result["saved"])
        self.assertTrue(result["ok"])
        self.assertNotIn("stopped_at", result)

    def test_dry_run_cancels_the_confirmation(self) -> None:
        console = FakeConsole()
        result = self._import(console, dry_run=True)
        self.assertTrue(result["confirm_dialog"])
        self.assertFalse(result["imported"])
        self.assertIn("dry_run", result["stopped_at"])
        self.assertNotIn("ok", result)
        self.assertIsNone(console.step)

    def test_confirmation_that_does_not_apply_is_not_imported(self) -> None:
        result = self._import(FakeConsole(applies=False))
        self.assertFalse(result["imported"])
        self.assertFalse(result["ok"])
        self.assertIn("still open", result["stopped_at"])

    def test_stuck_step_stops_without_saving(self) -> None:
        result = self._import(FakeConsole(stuck_at_step=3))
        self.assertTrue(result["imported"])
        self.assertFalse(result["saved"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["errors"], ["Required answer"])
        self.assertIn("step 3", result["stopped_at"])

    def test_missing_csv_fails_before_opening_the_console(self) -> None:
        with patch.object(play_console, "run", side_effect=AssertionError("opened the console")):
            with self.assertRaises(FileNotFoundError):
                server.play_data_safety_import("com.example.app", str(self.csv) + ".missing")


if __name__ == "__main__":
    unittest.main()

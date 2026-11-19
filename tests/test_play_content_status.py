import contextlib
import io
import json
import sys
import types
import unittest
from unittest.mock import patch

from shipstores import play_console, server

URL = "https://play.google.com/console/u/0/developers/dev/app/app/app-content/overview"


def _node(role: str, name: str) -> dict:
    return {"role": {"value": role}, "name": {"value": name}}


def _run_script(body: str, nodes: list[dict], text: str, url: str = URL) -> str:
    """Execute the tool's console script against fake browser helpers."""
    helpers = types.ModuleType("browser_harness.helpers")
    helpers.cdp = lambda method, **_: {"nodes": nodes} if method == "Accessibility.getFullAXTree" else {}
    helpers.activate_tab = helpers.click_at_xy = helpers.current_tab = lambda *a, **k: None
    package = types.ModuleType("browser_harness")
    package.helpers = helpers
    env = {
        "goto_url": lambda u: None,
        "wait_for_load": lambda: True,
        "js": lambda expression: text,
        "page_info": lambda: {"url": url},
    }
    out = io.StringIO()
    with patch.dict(sys.modules, {"browser_harness": package, "browser_harness.helpers": helpers}), \
            patch("time.sleep", lambda _s: None), contextlib.redirect_stdout(out):
        exec(play_console.script(body), env)
    return out.getvalue()


class PlayContentStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        p = patch.object(server, "_console_ids", return_value=("dev", "app"))
        p.start()
        self.addCleanup(p.stop)

    def _status(self, nodes: list[dict], text: str, url: str = URL) -> dict:
        def fake_run(dev_id, app_id, body, timeout=300):
            return _run_script(body, nodes, text, url)

        with patch.object(play_console, "run", fake_run):
            return server.play_content_status("com.example.app")

    def test_lists_pending_declarations_from_the_buttons(self) -> None:
        result = self._status([
            _node("tab", "Precisam de atenção (2)"),
            _node("button", "Iniciar a declaração de Apps governamentais"),
            _node("button", "Editar a declaração de Segurança dos dados"),
            _node("button", "Editar a declaração de Segurança dos dados"),
            _node("button", "Salvar"),
        ], "Conteúdo do app")
        self.assertFalse(result["up_to_date"])
        self.assertEqual(result["pending"], 2)
        self.assertEqual(result["pending_declarations"], ["Apps governamentais", "Segurança dos dados"])

    def test_everything_up_to_date(self) -> None:
        result = self._status([_node("tab", "Declarações concluídas")], "Conteúdo do app\nTudo em dia")
        self.assertTrue(result["up_to_date"])
        self.assertEqual(result["pending"], 0)
        self.assertEqual(result["pending_declarations"], [])

    def test_page_that_shows_neither_is_an_error(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "neither"):
            self._status([], "loading")

    def test_redirect_away_from_the_page_is_an_error(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Could not read the App content page"):
            self._status([], "Sign in", url="https://accounts.google.com/signin")

    def test_output_is_one_json_line(self) -> None:
        def fake_run(dev_id, app_id, body, timeout=300):
            out = _run_script(body, [_node("tab", "Precisam de atenção (1)")], "x")
            json.loads(out.strip().splitlines()[-1])
            return out

        with patch.object(play_console, "run", fake_run):
            self.assertEqual(server.play_content_status("com.example.app")["pending"], 1)


if __name__ == "__main__":
    unittest.main()

"""pick() from the Play Console prelude, run against a fake accessibility tree."""

import sys
import types
import unittest
from unittest.mock import patch

from shipstores import play_console


class FakeConsole:
    """A radio named `name` that is unnamed for the first `blank_reads` tree reads and
    only becomes checked when `checks_on` receives a full mouse click."""

    def __init__(self, name: str, blank_reads: int = 0, checks_on: str = "offset") -> None:
        self.name = name
        self.blank_reads = blank_reads
        self.checks_on = checks_on
        self.checked = False
        self.reads = 0
        self.methods: list[str] = []

    def cdp(self, method, **params):
        self.methods.append(method)
        if method == "Accessibility.getFullAXTree":
            self.reads += 1
            named = self.reads > self.blank_reads
            return {"nodes": [{
                "role": {"value": "radio"},
                "name": {"value": self.name if named else ""},
                "properties": [{"name": "checked", "value": {"value": self.checked}}],
                "backendDOMNodeId": 7,
            }]}
        if method == "DOM.getBoxModel":
            return {"model": {"content": [100, 100, 120, 100, 120, 120, 100, 120]}}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "obj-7"}}
        if method == "Runtime.callFunctionOn":
            return {"result": {"value": [300, 400]}}
        if method == "Input.dispatchMouseEvent" and params["type"] == "mouseReleased":
            component = (params["x"], params["y"]) == (300, 400)
            if (self.checks_on == "component") == component and self.checks_on != "never":
                self.checked = True
        return {}

    def run(self, body: str) -> dict:
        helpers = types.ModuleType("browser_harness.helpers")
        helpers.cdp = self.cdp
        helpers.activate_tab = helpers.click_at_xy = helpers.current_tab = lambda *a, **k: None
        package = types.ModuleType("browser_harness")
        package.helpers = helpers
        env: dict = {"js": lambda expression: ""}
        with patch.dict(sys.modules, {"browser_harness": package, "browser_harness.helpers": helpers}), \
                patch("time.sleep", lambda _s: None):
            exec(play_console.script(body), env)
        return env


class PickTests(unittest.TestCase):
    def test_waits_for_the_tree_to_get_names(self) -> None:
        console = FakeConsole("No", blank_reads=2)
        env = console.run("result = pick('No')")
        self.assertTrue(env["result"].startswith("selected: 'No' (offset"), env["result"])
        self.assertIn("Accessibility.enable", console.methods)

    def test_clicks_the_component_when_no_offset_hits_the_input(self) -> None:
        console = FakeConsole("No", checks_on="component")
        env = console.run("result = pick('No')")
        self.assertEqual(env["result"], "selected: 'No' (component click)")

    def test_reports_failure_when_nothing_checks_it(self) -> None:
        console = FakeConsole("No", checks_on="never")
        env = console.run("result = pick('No')")
        self.assertTrue(env["result"].startswith("FAILED: clicked 'No'"), env["result"])

    def test_missing_option_lists_what_is_available(self) -> None:
        console = FakeConsole("Yes")
        env = console.run("result = pick('No')")
        self.assertIn("available: ['Yes']", env["result"])


if __name__ == "__main__":
    unittest.main()

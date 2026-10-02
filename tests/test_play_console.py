import json
import unittest

from shipstores import play_console


class ParseJsonTailTests(unittest.TestCase):
    def test_last_valid_json_line_wins(self):
        output = '{"first": true}\n[1, 2, 3]\n'

        self.assertEqual(play_console.parse_json_tail(output), [1, 2, 3])

    def test_skips_log_lines_after_json(self):
        output = '{"status": "ok"}\nFinished successfully\n'

        self.assertEqual(play_console.parse_json_tail(output), {"status": "ok"})

    def test_skips_invalid_line_starting_with_opening_brace(self):
        output = '{"status": "ok"}\n{not valid json}\n'

        self.assertEqual(play_console.parse_json_tail(output), {"status": "ok"})

    def test_returns_none_when_output_has_no_json(self):
        self.assertIsNone(play_console.parse_json_tail("log line\nanother log line\n"))

    def test_supports_json_objects_and_arrays(self):
        for value in ({"status": "ok"}, ["one", "two"]):
            with self.subTest(value=value):
                self.assertEqual(play_console.parse_json_tail(json.dumps(value)), value)


class FormUrlTests(unittest.TestCase):
    def test_known_form_key_returns_play_console_url(self):
        self.assertEqual(
            play_console.form_url("dev", "app", "ads"),
            "https://play.google.com/console/u/0/developers/dev/app/app/app-content/ads-declaration",
        )

    def test_unknown_form_key_returns_none(self):
        self.assertIsNone(play_console.form_url("dev", "app", "unknown"))

    def test_form_without_its_own_url_returns_none(self):
        self.assertIsNone(play_console.form_url("dev", "app", "target_audience"))


if __name__ == "__main__":
    unittest.main()

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shipstores.config import ConfigError, TOOLSETS, _file, load_toolsets


class LoadToolsetsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp_dir.name) / "config.toml"
        self.original_toolsets = os.environ.pop("SHIPSTORES_TOOLSETS", None)
        self.config_env = patch.dict(os.environ, {"SHIPSTORES_CONFIG": str(self.config_path)})
        self.config_env.start()
        _file.cache_clear()

    def tearDown(self) -> None:
        self.config_env.stop()
        if self.original_toolsets is not None:
            os.environ["SHIPSTORES_TOOLSETS"] = self.original_toolsets
        _file.cache_clear()
        self.temp_dir.cleanup()

    def test_defaults_to_all_toolsets(self) -> None:
        self.assertEqual(load_toolsets(), TOOLSETS)

    def test_environment_toolsets_override_config_and_keep_core(self) -> None:
        self.config_path.write_text('[server]\ntoolsets = ["play"]\n')
        with patch.dict(os.environ, {"SHIPSTORES_TOOLSETS": "apple,eas"}):
            self.assertEqual(load_toolsets(), frozenset({"apple", "core", "eas"}))

    def test_reads_toolsets_from_config(self) -> None:
        self.config_path.write_text('[server]\ntoolsets = ["play"]\n')
        self.assertEqual(load_toolsets(), frozenset({"core", "play"}))

    def test_rejects_unknown_toolsets(self) -> None:
        with patch.dict(os.environ, {"SHIPSTORES_TOOLSETS": "apple,unknown"}):
            with self.assertRaisesRegex(ConfigError, "Unknown toolset"):
                load_toolsets()

    def test_rejects_non_array_config_toolsets(self) -> None:
        self.config_path.write_text('[server]\ntoolsets = "apple"\n')
        with self.assertRaisesRegex(ConfigError, "must be an array"):
            load_toolsets()

    def test_rejects_non_table_server_config(self) -> None:
        self.config_path.write_text('server = "invalid"\n')
        with self.assertRaisesRegex(ConfigError, "must be a table"):
            load_toolsets()


if __name__ == "__main__":
    unittest.main()

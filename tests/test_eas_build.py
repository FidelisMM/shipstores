"""eas_build_start (Free plan quota) and eas_build_local.

The external binaries (eas, npm, xcodebuild, fastlane, pod) are fake shell scripts
on an isolated PATH: the subprocess really runs, without touching EAS or Xcode.
"""

import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from shipstores import eas, server


def _script(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _wait_done(log_path: Path) -> str:
    for _ in range(200):
        text = log_path.read_text()
        if eas.LOCAL_DONE_MARKER in text:
            return text
        time.sleep(0.05)
    raise AssertionError(f"fake build did not finish:\n{log_path.read_text()}")


class EasBuildTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        env = patch.dict(os.environ, {"PATH": f"{self.bin}:/usr/bin:/bin"})
        env.start()
        self.addCleanup(env.stop)
        # A fictional Expo project, without node_modules
        self.project = self.tmp / "example-app"
        self.project.mkdir()
        (self.project / "eas.json").write_text(json.dumps({"build": {"production": {}}}))
        (self.project / "package.json").write_text(json.dumps({"name": "example-app"}))
        (self.project / "package-lock.json").write_text("{}")

    def mac_toolchain(self) -> None:
        """iOS local toolchain present; the fake eas writes the .ipa to --output."""
        for tool in ("xcodebuild", "fastlane", "pod"):
            _script(self.bin, tool, "exit 0")
        _script(self.bin, "npm", 'echo "npm $@" >> npm.calls; mkdir -p node_modules/expo')
        _script(self.bin, "eas", '''echo "eas $@"
while [ $# -gt 0 ]; do
  if [ "$1" = "--output" ]; then echo ipa > "$2"; fi
  shift
done
echo "Build successful"''')


class CloudBuildTests(EasBuildTestCase):
    def test_free_plan_quota_points_to_the_local_build(self) -> None:
        _script(self.bin, "eas", 'echo "This account has used its iOS builds from the Free plan this month."\nexit 1')
        with self.assertRaisesRegex(eas.EasError, "eas_build_local") as ctx:
            server.eas_build_start(str(self.project), "ios")
        self.assertIn("from the Free plan", str(ctx.exception))

    def test_other_errors_pass_through_untouched(self) -> None:
        _script(self.bin, "eas", 'echo "Invalid credentials"; exit 1')
        with self.assertRaises(eas.EasError) as ctx:
            server.eas_build_start(str(self.project), "ios")
        self.assertNotIn("eas_build_local", str(ctx.exception))


class LocalBuildTests(EasBuildTestCase):
    def test_installs_dependencies_and_runs_in_the_background(self) -> None:
        self.mac_toolchain()
        ipa = self.tmp / "out" / "app.ipa"

        result = server.eas_build_local(str(self.project), "ios", "production", str(ipa))

        self.assertTrue(result["npm_ci"])
        self.assertEqual((self.project / "npm.calls").read_text().strip(), "npm ci")
        self.assertEqual(result["artifact_path"], str(ipa))
        log = _wait_done(Path(result["log_path"]))
        self.assertIn(f"{eas.LOCAL_DONE_MARKER}0", log)
        self.assertIn(f"--platform ios --profile production --local --non-interactive --output {ipa}", log)
        self.assertEqual(ipa.read_text().strip(), "ipa")

    def test_does_not_reinstall_node_modules(self) -> None:
        self.mac_toolchain()
        (self.project / "node_modules" / "expo").mkdir(parents=True)

        result = server.eas_build_local(str(self.project))

        self.assertFalse(result["npm_ci"])
        self.assertFalse((self.project / "npm.calls").exists())
        self.assertTrue(result["artifact_path"].endswith("production.ipa"))
        _wait_done(Path(result["log_path"]))

    def test_failure_exit_code_lands_in_the_log(self) -> None:
        self.mac_toolchain()
        (self.project / "node_modules" / "expo").mkdir(parents=True)
        _script(self.bin, "eas", 'echo "Pod install failed"; exit 3')

        result = server.eas_build_local(str(self.project))

        log = _wait_done(Path(result["log_path"]))
        self.assertIn("Pod install failed", log)
        self.assertIn(f"{eas.LOCAL_DONE_MARKER}3", log)

    def test_ios_needs_the_mac_toolchain(self) -> None:
        _script(self.bin, "eas", "exit 0")
        _script(self.bin, "xcodebuild", "exit 0")
        with self.assertRaisesRegex(eas.EasError, "fastlane, pod"):
            server.eas_build_local(str(self.project))

    def test_without_lockfile_does_not_guess_the_package_manager(self) -> None:
        self.mac_toolchain()
        (self.project / "package-lock.json").unlink()
        with self.assertRaisesRegex(eas.EasError, "no node_modules and no package-lock.json"):
            server.eas_build_local(str(self.project))


if __name__ == "__main__":
    unittest.main()

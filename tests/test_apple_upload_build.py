import plistlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fake_asc import ASCTestCase
from shipstores import apple, server

APP = "1000000001"  # fictional app
BUNDLE = "com.example.app"


def _ipa(folder: Path, info: dict | None = None, name: str = "Example.app") -> Path:
    info = info if info is not None else {
        "CFBundleIdentifier": BUNDLE,
        "CFBundleShortVersionString": "1.2.0",
        "CFBundleVersion": "7",
    }
    path = folder / "Example.ipa"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"Payload/{name}/Info.plist", plistlib.dumps(info, fmt=plistlib.FMT_BINARY))
        # An embedded framework's plist must not be mistaken for the app's
        z.writestr(f"Payload/{name}/Frameworks/X.framework/Info.plist", plistlib.dumps({"CFBundleIdentifier": "x"}))
        z.writestr(f"Payload/{name}/binary", b"\0" * 300)
    return path


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(tmp.name)
        self.addCleanup(tmp.cleanup)


class IpaInfoTests(TempDirTestCase):
    def test_reads_the_app_info_plist(self) -> None:
        self.assertEqual(apple.read_ipa_info(_ipa(self.tmp)), {
            "bundle_id": BUNDLE, "version": "1.2.0", "build_number": "7",
        })

    def test_ipa_without_info_plist(self) -> None:
        path = self.tmp / "bad.ipa"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("other/thing.txt", "x")
        with self.assertRaisesRegex(apple.AppleError, "Payload"):
            apple.read_ipa_info(path)

    def test_info_plist_without_version(self) -> None:
        with self.assertRaisesRegex(apple.AppleError, "CFBundleShortVersionString"):
            apple.read_ipa_info(_ipa(self.tmp, {"CFBundleIdentifier": BUNDLE, "CFBundleVersion": "7"}))

    def test_platform_mapping(self) -> None:
        for given, expected in (("ios", "IOS"), ("osx", "MAC_OS"), ("appletvos", "TV_OS"), ("IOS", "IOS")):
            with self.subTest(given=given):
                self.assertEqual(apple.upload_platform(given), expected)
        with self.assertRaisesRegex(apple.AppleError, "Invalid platform"):
            apple.upload_platform("android")


class UploadBuildTests(ASCTestCase, TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        p = patch.object(apple.time, "sleep", lambda _s: None)
        p.start()
        self.addCleanup(p.stop)

    def _routes(self, states: list[dict]) -> None:
        responses = iter(states)
        self.asc.on("GET", "/v1/apps", lambda *_: (200, {"data": [
            {"id": "other", "attributes": {"bundleId": BUNDLE + ".widget"}},
            {"id": APP, "attributes": {"bundleId": BUNDLE}},
        ]}))
        self.asc.on("POST", "/v1/buildUploads", lambda *_: (201, {"data": {"id": "up-1"}}))
        self.asc.on("POST", "/v1/buildUploadFiles", lambda *_: (201, {"data": {"id": "file-1", "attributes": {
            "uploadOperations": [
                {"method": "PUT", "url": "https://upload.example/p1", "offset": 0, "length": 100, "requestHeaders": []},
                {"method": "PUT", "url": "https://upload.example/p2", "offset": 100, "length": 100, "requestHeaders": []},
            ]}}}))
        for part in ("p1", "p2"):
            self.asc.on("PUT", f"https://upload.example/{part}", lambda *_: (200, {}))
        self.asc.on("PATCH", "/v1/buildUploadFiles/file-1", lambda *_: (200, {"data": {}}))
        self.asc.on("GET", "/v1/buildUploads/up-1",
                    lambda *_: (200, {"data": {"attributes": {"state": next(responses)}}}))

    def test_complete_upload(self) -> None:
        self._routes([{"state": "PROCESSING"}, {"state": "COMPLETE"}])

        result = server.apple_upload_build(str(_ipa(self.tmp)), "ios")

        self.assertEqual(result["upload_id"], "up-1")
        self.assertEqual(result["state"], "COMPLETE")
        self.assertEqual(result["app_id"], APP)
        self.assertEqual(result["parts"], 2)
        self.assertNotIn("errors", result)
        self.assertIn("apple_wait_build", result["next_step"])
        self.assertEqual(self.asc.calls[0][2]["filter[bundleId]"], BUNDLE)
        body = self.asc.bodies("POST", "/v1/buildUploads")[0]
        self.assertEqual(body["data"]["attributes"],
                         {"cfBundleShortVersionString": "1.2.0", "cfBundleVersion": "7", "platform": "IOS"})
        self.assertEqual(body["data"]["relationships"]["app"]["data"]["id"], APP)
        commit = self.asc.bodies("PATCH", "/v1/buildUploadFiles/file-1")[0]
        self.assertEqual(commit["data"]["attributes"]["sourceFileChecksums"]["file"]["algorithm"], "MD5")

    def test_rejected_upload_returns_apple_errors(self) -> None:
        errors = [{"code": "ITMS-90345", "description": "version mismatch"}]
        self._routes([{"state": "FAILED", "errors": errors}])

        result = server.apple_upload_build(str(_ipa(self.tmp)), "ios")

        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(result["errors"], errors)
        self.assertIn("warning", result)

    def test_unknown_bundle_id_fails_before_uploading(self) -> None:
        self.asc.on("GET", "/v1/apps", lambda *_: (200, {"data": []}))
        with self.assertRaisesRegex(apple.AppleError, "No app with bundle id"):
            server.apple_upload_build(str(_ipa(self.tmp)), "ios")
        self.assertEqual(self.asc.bodies("POST", "/v1/buildUploads"), [])


if __name__ == "__main__":
    unittest.main()

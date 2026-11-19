import tempfile
import unittest
from pathlib import Path

from fake_asc import ASCTestCase
from shipstores import server

VERSION = "ver-1"


class AttachReviewFileTests(ASCTestCase):
    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = Path(tmp.name) / "walkthrough.pdf"
        self.file.write_bytes(b"%PDF demo")

    def test_uploads_the_file_to_the_review_detail(self) -> None:
        self.asc.on("GET", f"/v1/appStoreVersions/{VERSION}/appStoreReviewDetail",
                    lambda *_: (200, {"data": {"id": "detail-1", "type": "appStoreReviewDetails"}}))
        self.asc.on("POST", "/v1/appStoreReviewAttachments", lambda *_: (201, {"data": {"id": "att-1", "attributes": {
            "uploadOperations": [{"method": "PUT", "url": "https://upload.example/a", "offset": 0, "length": 9,
                                  "requestHeaders": []}]}}}))
        self.asc.on("PUT", "https://upload.example/a", lambda *_: (200, {}))
        self.asc.on("PATCH", "/v1/appStoreReviewAttachments/att-1", lambda *_: (200, {"data": {}}))

        result = server.apple_attach_review_file(VERSION, str(self.file))

        self.assertEqual(result, {"attachment_id": "att-1", "file": "walkthrough.pdf", "review_detail_id": "detail-1"})
        reserve = self.asc.bodies("POST", "/v1/appStoreReviewAttachments")[0]
        self.assertEqual(reserve["data"]["relationships"]["appStoreReviewDetail"]["data"]["id"], "detail-1")
        self.assertEqual(reserve["data"]["attributes"], {"fileName": "walkthrough.pdf", "fileSize": 9})

    def test_needs_review_details_first(self) -> None:
        self.asc.on("GET", f"/v1/appStoreVersions/{VERSION}/appStoreReviewDetail", lambda *_: (200, {"data": None}))
        with self.assertRaisesRegex(ValueError, "apple_set_review_details"):
            server.apple_attach_review_file(VERSION, str(self.file))

    def test_missing_file_fails_before_any_call(self) -> None:
        with self.assertRaises(FileNotFoundError):
            server.apple_attach_review_file(VERSION, str(self.file) + ".missing")
        self.assertEqual(self.asc.calls, [])


if __name__ == "__main__":
    unittest.main()

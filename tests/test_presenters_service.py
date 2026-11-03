import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.testclient import TestClient
from PIL import Image

from studio.presenters_service import PresenterLibrary, register_presenter_routes, MAX_BYTES


def image_bytes(color="navy"):
    output = io.BytesIO()
    Image.new("RGB", (256, 320), color).save(output, "PNG")
    return output.getvalue()


class PortraitServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.library = PresenterLibrary(Path(self.temporary.name))
        self.identifier = str(uuid4())
        self.options = dict(identifier=self.identifier, name="本人形象", filename="portrait.png", consent_confirmed=True)

    def test_upload_is_durable_but_not_falsely_ready_and_catalog_has_no_paths(self):
        with patch("studio.presenter.configuration_status", return_value={"configured": False, "runtime_verified": False}):
            result = self.library.import_bytes(image_bytes(), **self.options)
            self.assertFalse(result["presenter"]["ready"])
            other = PresenterLibrary(Path(self.temporary.name))
            entry = other.catalog()["presenters"][0]
            self.assertEqual(entry["id"], self.identifier)
            self.assertEqual(other.image(self.identifier)[:8], b"\x89PNG\r\n\x1a\n")
            self.assertNotIn(self.temporary.name, str(entry))

    def test_duplicate_same_id_is_safe_and_changed_body_or_name_conflicts(self):
        payload = image_bytes()
        self.library.import_bytes(payload, **self.options)
        self.assertTrue(self.library.import_bytes(payload, **self.options)["duplicate"])
        for body, options in ((image_bytes("red"), self.options), (payload, {**self.options, "name": "另一个名称"})):
            with self.assertRaises(HTTPException) as raised:
                self.library.import_bytes(body, **options)
            self.assertEqual(raised.exception.status_code, 409)

    def test_missing_authorization_cannot_import_even_on_duplicate(self):
        self.library.import_bytes(image_bytes(), **self.options)
        for consent in (False, "true", 1):
            with self.assertRaises(HTTPException) as raised:
                self.library.import_bytes(image_bytes(), **{**self.options, "consent_confirmed": consent})
            self.assertEqual(raised.exception.status_code, 403)

    def test_invalid_files_never_enter_the_library(self):
        for filename in ("../p.png", "C:\\p.png", "con.png", "face.mp4", "a.png."):
            with self.assertRaises(HTTPException):
                self.library.import_bytes(image_bytes(), **{**self.options, "filename": filename})
        for body in (b"not a photo", b"x" * (MAX_BYTES + 1)):
            with self.assertRaises(HTTPException):
                self.library.import_bytes(body, **self.options)
        self.assertEqual(self.library.catalog()["presenters"], [])

    def test_public_presets_require_actual_synthetic_provenance(self):
        with self.assertRaises(HTTPException):
            self.library.import_bytes(image_bytes(), **self.options, kind="public")
        result = self.library.import_bytes(image_bytes(), **self.options, kind="public",
                                            provenance={"synthetic": True, "generator": "local_sdxl", "license": "OpenRAIL++"})
        self.assertEqual(result["presenter"]["kind"], "public")

    def test_face_validation_and_runtime_readiness_are_separate(self):
        self.library.import_bytes(image_bytes(), **self.options)
        with patch("studio.presenter.inspect_portrait", return_value={"face_validated": True}), \
                patch("studio.presenter.configuration_status", return_value={"configured": False}):
            self.assertFalse(self.library.validate(self.identifier)["presenter"]["ready"])
        with patch("studio.presenter.configuration_status", return_value={"configured": True}):
            self.assertTrue(self.library.catalog()["presenters"][0]["ready"])
            self.assertTrue(self.library.resolve(self.identifier).is_file())

    def test_worker_snapshot_carries_stored_consent_and_immutable_image_hash(self):
        self.library.import_bytes(image_bytes(), **self.options)
        snapshot = self.library.snapshot(self.identifier)
        self.assertTrue(snapshot["consent_confirmed"])
        self.assertFalse(snapshot["face_validated"])
        self.assertEqual(snapshot["kind"], "personal")
        self.assertEqual(len(snapshot["sha256"]), 64)
        self.assertNotIn("image", snapshot)

    def test_import_validates_face_when_models_exist_without_claiming_unverified_runtime(self):
        with patch("studio.presenter.inspect_portrait", return_value={"face_validated": True}) as inspect, \
                patch("studio.presenter.configuration_status", return_value={"configured": False, "model_exists": True}):
            result = self.library.import_bytes(image_bytes(), **self.options)
            self.assertEqual(inspect.call_count, 1)
            self.assertFalse(result["presenter"]["ready"])
            self.assertTrue(self.library.snapshot(self.identifier)["face_validated"])

    def test_tampered_cache_is_not_given_to_the_renderer(self):
        self.library.import_bytes(image_bytes(), **self.options)
        path = self.library.resolve(self.identifier, require_ready=False)
        path.write_bytes(b"tampered")
        with self.assertRaises(HTTPException) as raised:
            self.library.resolve(self.identifier, require_ready=False)
        self.assertEqual(raised.exception.status_code, 409)

    def test_api_auth_origin_limits_and_image_reads(self):
        app = FastAPI()
        def authorize(request: Request, authorization: str | None = Header(default=None)):
            if authorization != "Bearer test-only" or request.headers.get("origin", "http://testserver") != "http://testserver":
                raise HTTPException(403)
        register_presenter_routes(app, authorize, Path(self.temporary.name))
        query = {"id": self.identifier, "name": "本人形象", "filename": "person.png", "consent_confirmed": "true"}
        headers = {"Authorization": "Bearer test-only", "Content-Type": "image/png"}
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/presenters").status_code, 403)
            self.assertEqual(client.post("/api/presenters/import", params=query, headers={**headers, "Origin": "https://foreign.example"}, content=image_bytes()).status_code, 403)
            response = client.post("/api/presenters/import", params=query, headers=headers, content=image_bytes())
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(response.json()["presenter"]["ready"])
            self.assertEqual(client.get(response.json()["presenter"]["preview_url"]).status_code, 403)
            picture = client.get(response.json()["presenter"]["preview_url"], headers=headers)
            self.assertEqual(picture.headers["content-type"], "image/png")
            self.assertIn("no-store", picture.headers["cache-control"])
            response = client.post("/api/presenters/import", params={**query, "id": str(uuid4())}, headers=headers,
                                   content=iter([b"x" * (MAX_BYTES // 2), b"x" * (MAX_BYTES // 2 + 1)]))
            self.assertEqual(response.status_code, 413)


if __name__ == "__main__":
    unittest.main()

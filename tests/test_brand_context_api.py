"""Integrated brand-memory, screenshot and story creation HTTP contracts.

Use the real app and persistence with workers disabled. No GPU model or external
provider is invoked; only service-readiness checks are mocked.
"""
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image

from studio.server import create_app


class BrandContextAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.inbox = self.root / "inbox"
        with patch("studio.server.detect_gpu", return_value={"available": False, "name": None, "memory_mb": None}):
            self.app = create_app(data_dir=self.data, inbox=self.inbox,
                                  token="brand-api-test", start_worker=False)
        self.client = TestClient(self.app)
        self.auth = {"Authorization": "Bearer brand-api-test"}
        self.source = self.inbox / "托管交车.mp4"
        self.source.write_bytes(b"source-version-for-http-contract-only")

    def tearDown(self):
        self.app.state.previews.stop()
        self.client.close()
        self.temp.cleanup()

    def get_context(self):
        result = self.client.get("/api/brand-context", headers=self.auth)
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def form(self, revision=0, **changes):
        return {"revision": revision, "facts": "车辆交接会核对车况。",
                "advantages": "交接记录可供双方核对。", "miniapp": "小程序显示订单记录。",
                "culture":"", "creative_rules": "围绕一个真实问题讲清楚，不堆砌卖点。", **changes}

    def put_context(self, body):
        return self.client.put("/api/brand-context", headers=self.auth, json=body)

    def screenshot(self, *, request_id=None, color="blue", headers=None):
        buffer = BytesIO()
        Image.new("RGB", (32, 48), color).save(buffer, format="PNG")
        return self.client.post("/api/brand-context/screenshots",
            params={"name": "真实小程序订单页.png", "request_id": request_id or str(uuid4())},
            headers={**(self.auth if headers is None else headers), "Content-Type": "image/png"},
            content=buffer.getvalue())

    def create_story(self, **changes):
        return self.client.post("/api/creations", headers=self.auth, json={
            "mode": "story", "sources": [self.source.name],
            "brief": "从交车经历切入，讲清托管中的责任和小程序记录。",
            "idempotency_key": "story-contract", "options": {}, **changes})

    def test_auth_is_required_for_brand_forms_screenshots_and_creation(self):
        self.assertEqual(self.client.get("/api/brand-context").status_code, 401)
        self.assertEqual(self.client.get("/api/brand-context", params={"token": "brand-api-test"}).status_code, 401)
        self.assertEqual(self.client.put("/api/brand-context", json=self.form()).status_code, 401)
        self.assertEqual(self.client.get("/api/brand-context/screenshots").status_code, 401)
        self.assertEqual(self.screenshot(headers={}).status_code, 401)
        self.assertEqual(self.client.post("/api/creations", json={}).status_code, 401)
        self.assertEqual(self.get_context()["revision"], 0)

    def test_flat_form_round_trip_survives_a_fresh_app_instance(self):
        initial = self.get_context()
        self.assertEqual(set(initial), {"revision", "facts", "advantages", "miniapp", "culture", "creative_rules", "screenshots", "confirmed_facts"})
        response = self.put_context(self.form(initial["revision"]))
        self.assertEqual(response.status_code, 200, response.text)
        saved = response.json()
        self.assertEqual({k:v for k,v in saved.items() if k!='confirmed_facts'}, {**self.form(1), "screenshots": []})
        self.assertEqual(len(saved['confirmed_facts']),3)
        self.assertTrue(all(set(row)=={'id','category','text','source'} for row in saved['confirmed_facts']))
        self.assertEqual(self.get_context(), saved)
        with patch("studio.server.detect_gpu", return_value={"available": False, "name": None, "memory_mb": None}):
            reopened = create_app(data_dir=self.data, inbox=self.inbox, token="brand-api-test", start_worker=False)
        with TestClient(reopened) as client:
            self.assertEqual(client.get("/api/brand-context", headers=self.auth).json(), saved)

    def test_stale_revision_returns_conflict_without_overwriting_latest_form(self):
        first = self.put_context(self.form())
        self.assertEqual(first.status_code, 200, first.text)
        stale = self.put_context(self.form(facts="旧窗口尝试覆盖的新文字"))
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(self.get_context(), first.json())

    def test_form_schema_and_limits_reject_invalid_writes_atomically(self):
        original = self.get_context()
        invalid = [{"revision": True}, {"revision": "0"}, {"revision": -1},
                   {"unexpected": "not a form field"}, {"screenshots": []},
                   {"facts": None}, {"advantages": 123}, {"miniapp": []}]
        invalid.extend({field: "字" * 5001} for field in ("facts", "advantages", "miniapp", "creative_rules"))
        for values in invalid:
            with self.subTest(values=str(values)[:80]):
                response = self.put_context(self.form(**values))
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(self.get_context(), original)
        for values in ({"facts": "有空字符\x00的事实"}, {"facts": "字" * 2001},
                       {"creative_rules": "字" * 1001}):
            with self.subTest(values=str(values)[:80]):
                response = self.put_context(self.form(**values))
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(self.get_context(), original)

    def test_five_thousand_character_form_boundary_accepts_valid_individual_facts(self):
        facts = "\n".join(letter * 1666 for letter in ("甲", "乙", "丙"))
        self.assertEqual(len(facts), 5000)
        result = self.put_context(self.form(facts=facts))
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["facts"], facts)

    def test_screenshot_does_not_advance_text_revision_or_conflict_with_pending_form(self):
        pending = self.form(self.get_context()["revision"])
        request_id = str(uuid4())
        response = self.screenshot(request_id=request_id)
        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()["screenshot"]
        self.assertEqual(self.get_context()["revision"], pending["revision"])
        self.assertEqual(self.get_context()["screenshots"], [item])
        saved = self.put_context(pending)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["revision"], 1)
        self.assertEqual(saved.json()["screenshots"], [item])
        self.assertEqual(self.screenshot(request_id=request_id).json()["screenshot"], item)
        self.assertEqual(self.screenshot(request_id=request_id, color="red").status_code, 409)
        self.assertEqual(self.get_context()["revision"], 1)
        self.assertEqual(self.client.get(item["url"]).status_code, 401)
        image = self.client.get(item["url"], headers=self.auth)
        self.assertEqual(image.status_code, 200)
        self.assertTrue(image.headers["content-type"].startswith("image/png"))
        self.assertEqual(image.headers["cache-control"], "no-store")
        self.assertNotIn("path", item)

    def test_story_requires_service_readiness_and_does_not_queue_on_failure(self):
        with patch("studio.server.production_capabilities", return_value={"modes": {"story": {"ready": False}}}):
            result = self.create_story()
        self.assertEqual(result.status_code, 503, result.text)
        self.assertIn("知识故事", result.json()["detail"])
        self.assertEqual(self.app.state.store.list(), [])

    def test_story_contract_requires_sources_and_rejects_manual_narration_fields(self):
        invalid = [{"sources": []}, {"sources": [self.source.name] * 13}, {"brief": " "},
                   {"voice_id": "manual-voice"},
                   {"script_id": str(uuid4())}, {"brand": {"name": "混入的单次品牌"}},
                   {"visual_fill": "loop"}, {"unknown": True}, {"options": {"subtitles": "false"}},
                   {"options": {"subtitles": 0}}, {"options": {"target_duration": "45"}}]
        with patch("studio.server.production_capabilities", return_value={"modes": {"story": {"ready": True}}}):
            for change in invalid:
                with self.subTest(change=change):
                    response = self.create_story(**change)
                    self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.app.state.store.list(), [])

    def test_story_queues_without_manual_script_and_replay_keeps_same_job(self):
        options = {"subtitles": False, "music": False, "aspect": "9:16", "target_duration": 45,
                   "opening_title": "托管之前先确认什么"}
        with patch("studio.server.production_capabilities", return_value={"modes": {"story": {"ready": True}}}):
            response = self.create_story(options=options)
            replay = self.create_story(options=options)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(replay.status_code, 200, replay.text)
        job = response.json()["job"]
        self.assertEqual(replay.json()["job"]["id"], job["id"])
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["mode"], "story")
        self.assertEqual(job["sources"], [self.source.name])
        stored = self.app.state.store.get(job["id"])["settings"]["creation"]
        self.assertEqual(stored["version"], 3)
        self.assertEqual(stored["mode"], "story")
        self.assertEqual(stored["options"], options)
        self.assertEqual(stored["requested_options"], options)
        self.assertNotIn("spoken_script", stored)
        self.assertNotIn("voice_id", stored)

    def test_story_defaults_and_explicit_subtitle_choices_are_preserved_in_all_source_modes(self):
        with patch("studio.server.production_capabilities", return_value={"modes": {"story": {"ready": True}}}), \
             patch("studio.captions.configuration_status", return_value={"configured": True}):
            default = self.create_story(idempotency_key="story-default")
            self.assertEqual(default.status_code, 201, default.text)
            defaults = self.app.state.store.get(default.json()["job"]["id"])["settings"]["creation"]["options"]
            self.assertTrue(defaults["subtitles"])
            self.assertTrue(defaults["music"])
            for mode in ("story", "vlog", "edit"):
                for subtitles in (False, True):
                    with self.subTest(mode=mode, subtitles=subtitles):
                        response = self.create_story(mode=mode, options={"subtitles": subtitles},
                            idempotency_key=f"{mode}-subtitles-{subtitles}")
                        self.assertIn(response.status_code, (200, 201), response.text)
                        job = response.json()["job"]
                        persisted = self.app.state.store.get(job["id"])["settings"]["creation"]
                        self.assertIs(persisted["options"]["subtitles"], subtitles)
                        self.assertIs(job["creation_plan"]["resolved"]["subtitles"], subtitles)

    def test_story_worker_reaches_review_and_persists_generated_script_for_knowledge(self):
        from studio.knowledge import KnowledgeStore

        spoken = "交车前，先把责任说清楚。\n核对记录，才能让后续沟通有依据。"
        storyboard = {"title": "托管交车先核对什么", "angle": "从一次交接解释记录的作用",
                      "scenes": [{"source_index": 0, "start": 0, "narration": line}
                                 for line in spoken.splitlines()]}

        def build(paths, brief, options, settings, *, work_dir, data, stop_event, on_stage):
            self.assertEqual([Path(path).resolve() for path in paths], [self.source.resolve()])
            self.assertIn("交车经历", brief)
            self.assertEqual(Path(data).resolve(), self.data.resolve())
            self.assertFalse(options["subtitles"])
            self.assertNotIn("voice_id", options)
            self.assertNotIn("spoken_script", options)
            self.assertIs(stop_event, self.app.state.worker.stop_event)
            Path(work_dir).mkdir(parents=True, exist_ok=True)
            for stage in ("transcribing", "planning_story", "preparing_voice", "planning_visuals"):
                on_stage(stage)
            return {"story_mode": True, "storyboard": storyboard, "spoken_script": spoken,
                    "duration": 12.0, "narration": {"duration": 12.0}, "warnings": [],
                    "resolved": dict(options), "settings": dict(settings)}

        def render(plan, output, settings, *, on_stage):
            self.assertEqual(plan["spoken_script"], spoken)
            self.assertFalse(plan["resolved"]["subtitles"])
            Path(output).write_bytes(b"fixture rendered story, never external publication")
            on_stage("checking")
            return {"duration": 12.0, "qc": {"passed": True}, "warnings": []}

        with patch("studio.server.production_capabilities", return_value={"modes": {"story": {"ready": True}}}):
            response = self.create_story(options={"subtitles": False, "music": True})
        self.assertEqual(response.status_code, 201, response.text)
        identifier = response.json()["job"]["id"]
        creation = self.app.state.store.get(identifier)["settings"]["creation"]
        self.assertNotIn("spoken_script", creation)
        self.assertNotIn("voice_id", creation)
        claimed = self.app.state.store.claim()
        self.assertEqual(claimed["id"], identifier)
        self.assertEqual(claimed["status"], "analyzing")
        with patch("studio.story.build_story_plan", side_effect=build) as build_plan, \
             patch("studio.narrated.render_narrated_plan", side_effect=render) as render_plan:
            self.app.state.worker.process(claimed)
        processed = self.app.state.store.get(identifier)
        self.assertEqual(processed["status"], "review", processed.get("error"))
        build_plan.assert_called_once()
        render_plan.assert_called_once()
        detail = self.client.get(f"/api/jobs/{identifier}", headers=self.auth)
        self.assertEqual(detail.status_code, 200, detail.text)
        job = detail.json()["job"]
        self.assertEqual(job["status"], "review", job.get("error"))
        self.assertTrue(job["has_video"])
        self.assertEqual(job["production"]["stage"], "ready")
        self.assertEqual(job["spoken_script"], spoken)
        self.assertEqual(job["creation_plan"]["spoken_script"], spoken)
        self.assertEqual(job["creation_plan"]["storyboard"], storyboard)
        self.assertEqual(job["creation_plan"]["summary"], [storyboard["title"], storyboard["angle"],
                                                          *["段落：" + line for line in spoken.splitlines()]])
        self.assertFalse(job["creation_plan"]["resolved"]["subtitles"])
        saved = self.client.put(f"/api/knowledge/{identifier}", headers=self.auth,
            json={"favorite": True, "notes": "用真实交车引出可核对记录。", "learn_fields": ["opening", "structure", "subtitles"]})
        self.assertEqual(saved.status_code, 200, saved.text)
        snapshot = saved.json()["item"]["snapshot"]
        self.assertEqual(snapshot["mode"], "story")
        self.assertEqual(snapshot["spoken_script"], spoken)
        self.assertFalse(snapshot["options"]["subtitles"])
        reopened = KnowledgeStore(self.data, self.app.state.store).get(identifier)
        self.assertEqual(reopened["snapshot"], snapshot)
        self.assertEqual(reopened["learning_profile"]["opening"]["text"], spoken.splitlines()[0])


if __name__ == "__main__":
    unittest.main()

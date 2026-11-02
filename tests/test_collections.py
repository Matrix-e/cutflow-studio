"""Collection schedules, metadata rights and browser leases; no live requests."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from studio.collection_service import CollectionWorker,register_collection_routes
from studio.collection_store import CollectionConflict,CollectionStore,canonical_url
from studio.server import create_app


class FakeProvider:
    def __init__(self,configured=True,repeat=False):
        self.configured,self.repeat,self.calls = configured,repeat,[]
    def configuration_status(self):
        return {"configured":self.configured,"status":"ready" if self.configured else "not_configured"}
    def search(self,keyword,count=10,cursor=0,search_id="",publish_time=0,sort_type=0):
        self.calls.append((keyword,count,cursor))
        start = 0 if self.repeat else int(cursor)
        return {"items":[{"item_id":str(100000+i),"title":"Test "+str(i),"author":"Author",
                           "source_url":f"https://www.douyin.com/video/{100000+i}?tracking=ignored"} for i in range(start,start+count)],
                "cursor":cursor+count,"search_id":"search-test","has_more":True}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = CollectionStore(self.root / "collections.sqlite3")
    def tearDown(self):
        self.temp.cleanup()

    def test_hourly_schedule_pause_resume_and_active_dedup(self):
        task = self.store.create_task("汽车托管",interval_seconds=3600,now=100)
        first = self.store.queue_due(now=100)
        self.assertEqual(len(first),1)
        self.assertEqual(self.store.queue_due(now=100),[])
        same,created = self.store.enqueue(task["id"],now=101)
        self.assertFalse(created)
        self.assertEqual(same["id"],first[0]["id"])
        self.store.update_task(task["id"],{"enabled":False},now=102)
        self.assertEqual(self.store.queue_due(now=4000),[])
        self.store.update_task(task["id"],{"enabled":True},now=5000)
        self.assertEqual(self.store.get_task(task["id"])["next_run_at"],5000)
        self.store.queue_due(now=5000)
        self.assertEqual(len(self.store.list_runs()),1)

    def test_interval_below_hour_rejected(self):
        with self.assertRaises(ValueError):
            self.store.create_task("keyword",interval_seconds=3599)

    def test_browser_limits_and_claimed_settings_are_stable(self):
        task = self.store.create_task("原关键词",max_items=10,now=0)
        run = self.store.claim_browser("local-agent",now=1)
        self.store.update_task(task["id"],{"keyword":"新关键词","max_items":1},now=2)
        result = self.store.complete_browser(run["id"],run["lease_token"],[{"item_id":"1"},{"item_id":"2"}],now=3)
        self.assertEqual(result["status"],"succeeded")
        self.assertEqual(result["task_snapshot"]["keyword"],"原关键词")

    def test_official_not_configured_makes_no_provider_calls(self):
        provider = FakeProvider(configured=False)
        self.store.create_task("AI沙龙",provider="official",now=0)
        CollectionWorker(self.store,provider).tick(now=1)
        self.assertEqual(provider.calls,[])
        self.assertEqual(self.store.list_runs()[0]["status"],"not_configured")

    def test_official_worker_does_not_claim_browser_tasks(self):
        provider = FakeProvider()
        self.store.create_task("汽车租赁沙龙",provider="browser",now=0)
        CollectionWorker(self.store,provider).tick(now=1)
        self.assertEqual(provider.calls,[])
        self.assertEqual(self.store.list_runs()[0]["status"],"queued")

    def test_official_pagination_caps_total_at_fifty(self):
        provider = FakeProvider()
        self.store.create_task("AI沙龙",provider="official",max_items=50,now=0)
        CollectionWorker(self.store,provider).tick(now=1)
        self.assertEqual([call[1] for call in provider.calls],[20,20,10])
        run = self.store.list_runs()[0]
        self.assertEqual((run["status"],run["items_seen"],run["items_added"]),("succeeded",50,50))
        self.assertTrue(all(item["media_availability"]=="reference_only" for item in self.store.list_items()))

    def test_item_id_dedup_and_rights_preserved(self):
        first,added = self.store.add_item({"item_id":"12345","title":"first"})
        self.assertTrue(added)
        self.store.update_rights(first["id"],"licensed","合同由用户线下保存")
        second,added = self.store.add_item({"item_id":"12345","title":"updated","rights_status":"owned"})
        self.assertFalse(added)
        self.assertEqual(second["rights_status"],"licensed")
        self.assertEqual(second["rights_note"],"合同由用户线下保存")
        self.assertFalse(second["rights_verified"])

    def test_manual_numeric_and_official_opaque_ids_merge_by_link(self):
        first,_ = self.store.add_item({"item_id":"12345","source_url":"https://www.douyin.com/video/12345"})
        second,new = self.store.add_item({"item_id":"opaque_id_abc","source_url":"https://www.douyin.com/video/12345"})
        third,newer = self.store.add_item({"item_id":"opaque_id_abc","title":"same"})
        self.assertFalse(new)
        self.assertFalse(newer)
        self.assertEqual(first["id"],third["id"])

    def test_source_links_canonical_and_short_links_unresolved(self):
        self.assertEqual(canonical_url("https://www.douyin.com/video/12345?x=1#frag"),("https://www.douyin.com/video/12345","12345"))
        self.assertEqual(canonical_url("https://v.douyin.com/AbcDEF/?x=1"),("https://v.douyin.com/AbcDEF/",None))
        for url in ("https://evil.com/video/12345","https://douyin.com.evil.com/video/12345","file:///etc/passwd","https://user:secret@www.douyin.com/video/1"):
            with self.assertRaises(ValueError):
                canonical_url(url)

    def test_restart_interrupts_official_but_preserves_schedule(self):
        task = self.store.create_task("keyword",provider="official",now=0)
        self.store.queue_due(now=1)
        self.store.claim_official(now=2)
        due = self.store.get_task(task["id"])["next_run_at"]
        self.store.recover(now=3)
        self.assertEqual(self.store.list_runs()[0]["status"],"interrupted")
        self.assertEqual(self.store.get_task(task["id"])["next_run_at"],due)

    def browser_run(self):
        self.store.create_task("汽车托管",now=0)
        return self.store.claim_browser("local-agent",now=1)

    def test_browser_claim_is_single_and_requires_valid_lease(self):
        run = self.browser_run()
        self.assertIsNone(self.store.claim_browser("local-agent",now=2))
        self.assertIsNone(self.store.claim_browser("another-agent",now=2))
        self.assertNotIn("lease_hash",run)
        self.assertNotIn("lease_token",self.store.get_run(run["id"]))
        with self.assertRaises(CollectionConflict):
            self.store.complete_browser(run["id"],"wrong",[],now=3)

    def test_active_browser_lease_shows_busy_until_expiry(self):
        run = self.browser_run()
        with patch("studio.collection_store.time.time",return_value=100):
            state = self.store.browser_status()
            self.assertTrue(state["busy"])
            self.assertEqual(state["status"],"online")
            self.assertEqual(state["active_run_id"],run["id"])
        with patch("studio.collection_store.time.time",return_value=902):
            self.assertEqual(self.store.browser_status()["status"],"offline")

    def test_completed_browser_replay_is_idempotent_and_does_not_grant_rights(self):
        run = self.browser_run()
        items=[{"item_id":"12345","title":"example","rights_status":"owned","local_filename":"video.mp4",
                "sha256":"a"*64,"size_bytes":100,"download_status":"downloaded"}]
        first = self.store.complete_browser(run["id"],run["lease_token"],items,now=2)
        second = self.store.complete_browser(run["id"],run["lease_token"],items,now=3)
        self.assertEqual(first["id"],second["id"])
        self.assertEqual(len(self.store.list_items()),1)
        item = self.store.list_items()[0]
        self.assertEqual(item["rights_status"],"unknown")
        self.assertEqual(item["media_availability"],"local_quarantine")
        self.assertFalse(item["local_media_verified"])
        self.assertEqual(sorted(path.name for path in self.root.iterdir()),["collections.sqlite3"])

    def test_expired_browser_run_not_automatically_replayed(self):
        run = self.browser_run()
        self.store.expire_leases(now=902)
        self.assertEqual(self.store.get_run(run["id"])["status"],"interrupted")
        self.assertIsNone(self.store.claim_browser("local-agent",now=902))
        with self.assertRaises(CollectionConflict):
            self.store.complete_browser(run["id"],run["lease_token"],[],now=902)

    def test_browser_fail_login_required_uses_safe_fixed_message(self):
        run = self.browser_run()
        result = self.store.fail_browser(run["id"],run["lease_token"],"login_required",now=3)
        self.assertEqual(result["status"],"failed")
        self.assertEqual(result["error_code"],"login_required")

    def test_partial_failure_preserves_downloaded_metadata_without_granting_rights(self):
        run = self.browser_run()
        item = {"item_id":"12345","title":"already acquired","rights_status":"owned",
                "local_filename":"12345.mp4","sha256":"b"*64,"size_bytes":100,"download_status":"downloaded"}
        failed = self.store.fail_browser(run["id"],run["lease_token"],"captcha_required",items=[item],now=3)
        self.assertEqual((failed["status"],failed["items_seen"],failed["items_added"]),("failed",1,1))
        saved = self.store.list_items()[0]
        self.assertEqual(saved["media_availability"],"local_quarantine")
        self.assertEqual(saved["rights_status"],"unknown")
        with self.assertRaises(CollectionConflict):
            self.store.fail_browser(run["id"],run["lease_token"],"captcha_required",items=[{"item_id":"99999"}],now=4)
        with self.assertRaises(CollectionConflict):
            self.store.complete_browser(run["id"],run["lease_token"],[{"item_id":"99999"}],now=4)
        self.assertEqual(len(self.store.list_items()),1)

    def test_partial_failure_validates_lease_and_snapshot_cap_before_any_insert(self):
        task = self.store.create_task("limited",max_items=1,now=0)
        run = self.store.claim_browser("local-agent",now=1)
        self.store.update_task(task["id"],{"max_items":10},now=2)
        with self.assertRaises(CollectionConflict):
            self.store.fail_browser(run["id"],"wrong","captcha_required",items=[{"item_id":"1"}],now=3)
        with self.assertRaises(ValueError):
            self.store.fail_browser(run["id"],run["lease_token"],"captcha_required",items=[{"item_id":"1"},{"item_id":"2"}],now=3)
        self.assertEqual(self.store.get_run(run["id"])["status"],"running")
        self.assertEqual(self.store.list_items(),[])
        with self.assertRaises(CollectionConflict):
            self.store.fail_browser(run["id"],run["lease_token"],"captcha_required",items=[{"item_id":"1"}],now=902)
        self.assertEqual(self.store.list_items(),[])

    def test_live_browser_lease_survives_server_restart(self):
        run = self.browser_run()
        self.store.recover(now=3)
        self.assertEqual(self.store.get_run(run["id"])["status"],"running")
        self.assertEqual(self.store.complete_browser(run["id"],run["lease_token"],[],now=4)["status"],"succeeded")

    def test_invalid_browser_result_is_failed_not_fake_success(self):
        run = self.browser_run()
        result = self.store.complete_browser(run["id"],run["lease_token"],[{"source_url":"https://evil.com"}],now=3)
        self.assertEqual(result["status"],"failed")
        self.assertEqual(result["items_added"],0)


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        app = FastAPI()
        def authorize(authorization:str | None=Header(default=None)):
            if authorization!="Bearer test":
                raise HTTPException(401,"unauthorized")
        self.worker = register_collection_routes(app,authorize,self.root / "inbox",self.root,FakeProvider(configured=False))
        self.client = TestClient(app)
        self.auth = {"Authorization":"Bearer test"}
    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def test_all_endpoints_authenticate_and_no_inbox_is_created(self):
        for route in ("config","tasks","items","runs"):
            self.assertEqual(self.client.get("/api/collections/"+route).status_code,401)
        self.assertEqual(self.client.post("/api/collections/browser/claim",json={"agent_id":"local"}).status_code,401)
        self.assertFalse((self.root / "inbox").exists())

    def test_task_create_pause_run_and_listing(self):
        response = self.client.post("/api/collections/tasks",headers=self.auth,json={"keyword":"AI沙龙","interval_seconds":3600})
        self.assertEqual(response.status_code,201)
        task = response.json()["task"]
        self.assertEqual(task["provider"],"browser")
        changed = self.client.patch(f"/api/collections/tasks/{task['id']}",headers=self.auth,json={"enabled":False})
        self.assertFalse(changed.json()["task"]["enabled"])
        run = self.client.post(f"/api/collections/tasks/{task['id']}/run",headers=self.auth).json()["run"]
        claimed = self.client.post("/api/collections/browser/claim",headers=self.auth,json={"agent_id":"local"}).json()["run"]
        self.assertEqual(run["id"],claimed["id"])

    def test_import_short_link_and_update_rights(self):
        response = self.client.post("/api/collections/items/import",headers=self.auth,json={"source_url":"https://v.douyin.com/ABCdef/","title":"reference"})
        self.assertEqual(response.status_code,201)
        item = response.json()["item"]
        self.assertEqual(item["media_availability"],"reference_only")
        changed = self.client.patch(f"/api/collections/items/{item['id']}",headers=self.auth,json={"rights_status":"licensed","rights_note":"用户确认授权范围"})
        self.assertEqual(changed.json()["item"]["rights_status"],"licensed")
        self.assertFalse(changed.json()["item"]["rights_verified"])

    def test_browser_complete_lease_boundaries_and_no_leak(self):
        self.client.post("/api/collections/tasks",headers=self.auth,json={"keyword":"汽车托管"})
        run = self.client.post("/api/collections/browser/claim",headers=self.auth,json={"agent_id":"local"}).json()["run"]
        denied = self.client.post(f"/api/collections/browser/runs/{run['id']}/complete",headers=self.auth,json={"lease_token":"wrong","items":[]})
        self.assertEqual(denied.status_code,409)
        done = self.client.post(f"/api/collections/browser/runs/{run['id']}/complete",headers=self.auth,json={"lease_token":run["lease_token"],"items":[{"item_id":"12345","title":"example"}]})
        self.assertEqual(done.status_code,200)
        self.assertNotIn(run["lease_token"],self.client.get("/api/collections/runs",headers=self.auth).text)

    def test_parent_api_auth_limits_and_worker_lifespan(self):
        app = create_app(data_dir=self.root / "parent-data",inbox=self.root / "parent-inbox",token="test",start_worker=True)
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/collections/tasks").status_code,401)
            self.assertTrue(app.state.collection_worker.thread.is_alive())
            for settings in ({"max_items":11},{"sort_type":1},{"publish_time":7}):
                response = client.post("/api/collections/tasks",headers=self.auth,json={"keyword":"汽车托管","provider":"browser",**settings})
                self.assertEqual(response.status_code,400,response.text)
            allowed = client.post("/api/collections/tasks",headers=self.auth,json={"keyword":"汽车托管","provider":"browser","interval_seconds":3600})
            self.assertEqual(allowed.status_code,201)
        self.assertFalse(app.state.collection_worker.thread.is_alive())

    def test_browser_fail_route_saves_partial_items(self):
        self.client.post("/api/collections/tasks",headers=self.auth,json={"keyword":"汽车托管"})
        run = self.client.post("/api/collections/browser/claim",headers=self.auth,json={"agent_id":"local"}).json()["run"]
        response = self.client.post(f"/api/collections/browser/runs/{run['id']}/fail",headers=self.auth,
            json={"lease_token":run["lease_token"],"code":"captcha_required","items":[{"item_id":"12345"}]})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()["run"]["items_added"],1)
        self.assertEqual(len(self.client.get("/api/collections/items",headers=self.auth).json()["items"]),1)


if __name__ == "__main__":
    unittest.main()

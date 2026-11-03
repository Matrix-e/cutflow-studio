"""Technical evidence stays inspectable but cannot enter production delivery."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from studio.server import create_app
from studio.store import StateConflict,Store


class DiagnosticJobTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.inbox=self.root/'inbox'
        self.app=create_app(data_dir=self.root/'data',inbox=self.inbox,token='diagnostic-test-token',start_worker=False)
        self.client=TestClient(self.app)
        self.auth={'Authorization':'Bearer diagnostic-test-token'}
        self.store=self.app.state.store

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def make_job(self,source='source.mp4',status='review'):
        path=self.inbox/source
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(b'fixture-source-unchanged')
        response=self.client.post('/api/jobs',headers=self.auth,json={'source':source,'title':'Project video'})
        self.assertEqual(response.status_code,201,response.text)
        job=response.json()['job']
        output=self.root/'data'/'renders'/(job['id']+'.mp4')
        output.write_bytes(b'fixture-render-evidence-unchanged')
        if status!='queued':
            self.store.transition(job['id'],['queued'],status,'fixture execution record',
                                  plan={'duration':1,'clips':[{'source_in':0,'source_out':1}]},
                                  result={'qc':{'passed':True},'duration':1},output_path=str(output),progress=100)
        return self.store.get(job['id'])

    def test_mark_preserves_source_render_plan_report_and_audit(self):
        original=self.make_job()
        events_before=self.store.events(original['id'])
        marked=self.store.mark_technical_test(original['id'],'Confirmed synthetic AV fixture; source hash checked separately')
        self.assertEqual(marked['job_kind'],'technical_test')
        self.assertIn('synthetic',marked['diagnostic_reason'])
        for key in ('source','status','plan','result','output_path','created_at','publication'):
            self.assertEqual(marked[key],original[key])
        self.assertEqual((self.inbox/original['source']).read_bytes(),b'fixture-source-unchanged')
        self.assertEqual(Path(original['output_path']).read_bytes(),b'fixture-render-evidence-unchanged')
        events_after=self.store.events(original['id'])
        self.assertEqual(events_after[:-1],events_before)
        self.assertIn('技术测试',events_after[-1]['message'])
        self.store.mark_technical_test(original['id'],marked['diagnostic_reason'])
        self.assertEqual(self.store.events(original['id']),events_after)

    def test_production_lists_counts_and_assets_exclude_only_registered_test(self):
        diagnostic=self.make_job('engine-proof.mp4')
        production=self.make_job('qa-colorbar-client-interview.mp4')
        self.store.mark_technical_test(diagnostic['id'],'Confirmed test evidence')
        jobs=self.client.get('/api/jobs',headers=self.auth).json()['jobs']
        self.assertEqual([row['id'] for row in jobs],[production['id']])
        tests=self.client.get('/api/jobs?kind=technical_test',headers=self.auth).json()['jobs']
        self.assertEqual([row['id'] for row in tests],[diagnostic['id']])
        status=self.client.get('/api/status',headers=self.auth).json()
        self.assertEqual(status['counts'],{'review':1})
        self.assertEqual(status['technical_test_count'],1)
        assets=self.client.get('/api/assets',headers=self.auth).json()['assets']
        self.assertEqual([row['source'] for row in assets],[production['source']])
        self.assertEqual(len(self.store.list(kind='all')),2)
        self.assertEqual(self.store.counts(kind='all'),{'review':2})

    def test_diagnostic_detail_and_video_remain_available_as_evidence(self):
        job=self.make_job()
        self.store.mark_technical_test(job['id'],'Synthetic codec validation only')
        detail=self.client.get('/api/jobs/'+job['id'],headers=self.auth)
        self.assertEqual(detail.status_code,200)
        self.assertEqual(detail.json()['job']['job_kind'],'technical_test')
        self.assertEqual(detail.json()['job']['diagnostic_reason'],'Synthetic codec validation only')
        video=self.client.get('/api/jobs/'+job['id']+'/video',headers=self.auth)
        self.assertEqual(video.status_code,200)
        self.assertEqual(video.content,b'fixture-render-evidence-unchanged')

    def test_diagnostic_source_cannot_create_another_production_job(self):
        job=self.make_job()
        self.store.mark_technical_test(job['id'],'Confirmed technical sample')
        response=self.client.post('/api/jobs',headers=self.auth,json={'source':job['source'],'title':'renamed',
                                  'settings':{'remove_silence':False},'idempotency_key':'new-production-request'})
        self.assertEqual(response.status_code,409)
        self.assertEqual(response.json()['code'],'technical_test_source')
        self.assertEqual(len(self.store.list(kind='all')),1)

    def test_direct_approve_publish_and_retry_are_blocked(self):
        for index,state in enumerate(('review','approved','failed')):
            job=self.make_job(f'fixture-{index}.mp4',status=state)
            self.store.mark_technical_test(job['id'],'Confirmed technical sample')
            for action in ('approve','publish','retry'):
                with self.subTest(state=state,action=action):
                    response=self.client.post(f"/api/jobs/{job['id']}/{action}",headers=self.auth)
                    self.assertEqual(response.status_code,409,response.text)
            self.assertEqual(self.store.get(job['id'])['status'],state)

    def test_store_atomic_transition_blocks_stale_production_action(self):
        job=self.make_job(status='approved')
        self.store.mark_technical_test(job['id'],'Classification before queued publish transaction')
        for state in ('queued','analyzing','rendering','approved','publishing','published'):
            with self.subTest(state=state),self.assertRaises(StateConflict):
                self.store.transition(job['id'],['approved'],state,'stale action')

    def test_mark_refuses_active_and_published_states(self):
        for index,state in enumerate(('queued','analyzing','rendering','publishing','published')):
            job=self.make_job(f'active-{index}.mp4',status=state)
            with self.subTest(state=state),self.assertRaises(StateConflict):
                self.store.mark_technical_test(job['id'],'Cannot reclassify while active or published')
            self.assertEqual(self.store.get(job['id'])['job_kind'],'production')

    def test_classification_requires_reason_and_has_no_public_route(self):
        job=self.make_job()
        for reason in ('','  ',None,'x'*2001):
            with self.assertRaises(ValueError):
                self.store.mark_technical_test(job['id'],reason)
        response=self.client.post(f"/api/jobs/{job['id']}/mark-technical-test",headers=self.auth,json={'reason':'client request'})
        self.assertIn(response.status_code,(404,405))
        self.assertEqual(self.store.get(job['id'])['job_kind'],'production')

    def test_public_kind_filter_has_no_all_records_shortcut(self):
        response=self.client.get('/api/jobs?kind=all',headers=self.auth)
        self.assertEqual(response.status_code,422)


class DiagnosticMigrationTests(unittest.TestCase):
    def test_legacy_database_migrates_as_production_without_rewriting_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'legacy.sqlite3'
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.executescript('''CREATE TABLE jobs (
                    id TEXT PRIMARY KEY,source TEXT NOT NULL,title TEXT NOT NULL,fingerprint TEXT NOT NULL,
                    idempotency_key TEXT UNIQUE,source_size INTEGER NOT NULL,source_mtime_ns INTEGER NOT NULL,
                    settings TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                    plan TEXT,result TEXT,output_path TEXT,error TEXT,publication TEXT,progress INTEGER NOT NULL DEFAULT 0
                );''')
                conn.execute('''INSERT INTO jobs(id,source,title,fingerprint,source_size,source_mtime_ns,settings,status,created_at,updated_at,plan,result)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',('legacy','synthetic-looking-name.mp4','Original title','fingerprint',17,19,
                    '{}','review','before','before',json.dumps({'duration':1}),json.dumps({'qc':{'passed':True}})))
            store=Store(path)
            record=store.get('legacy')
            self.assertEqual(record['job_kind'],'production')
            self.assertIsNone(record['diagnostic_reason'])
            self.assertEqual(record['source'],'synthetic-looking-name.mp4')
            self.assertEqual(record['plan'],{'duration':1})
            self.assertEqual(record['updated_at'],'before')
            self.assertEqual(store.counts(),{'review':1})
            self.assertEqual(store.technical_test_sources(),set())
            self.assertEqual(Store(path).get('legacy'),record)


if __name__=='__main__':
    unittest.main()

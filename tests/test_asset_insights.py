import hashlib
from contextlib import closing
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import patch
from fastapi.testclient import TestClient
from studio.asset_insights import frame_review, fingerprint, AssetInsights, read_director_excerpt, legacy_summary_notice, PIPELINE_LOCK
from studio.auto_assembly import _fingerprint
from studio.server import create_app


class AssetInsightTests(unittest.TestCase):
    def test_recommendation_penalizes_black_frames(self):
        info = {'display_width':1920,'display_height':1080}
        good = frame_review(info,[bytes([100])*4096]*3)
        black = frame_review(info,[bytes(4096)]*3)
        self.assertEqual(good['score'],100)
        self.assertEqual(black['score'],40)
        self.assertIn('技术初评',good['scope'])

    def test_auth_and_path_boundaries(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve()
            client=TestClient(create_app(data_dir=root/'data',inbox=root/'inbox',token='test',start_worker=False))
            route='/api/assets/insights?source=../secret.mp4'
            for method in (client.get,client.post):
                self.assertEqual(method(route).status_code,401)
                self.assertEqual(method(route,headers={'Authorization':'Bearer test'}).status_code,400)

    def test_partial_results_and_cache_invalidation(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=root/'source.mp4';source.write_bytes(b'first')
            manager=AssetInsights(root/'data');key=fingerprint(source)
            with patch('studio.asset_insights.analyze_visuals',return_value={'frames':[{'time':1,'image':'test'}],'has_audio':True}),patch.dict('os.environ',{'CUTFLOW_IDEA_PYTHON':'missing'}):
                manager.run(source,key)
            result=manager.read(source)
            self.assertEqual(result['status'],'partial')
            self.assertNotIn('recommendation',result)
            source.write_bytes(b'new source bytes')
            self.assertNotEqual(fingerprint(source),key)
            self.assertEqual(manager.read(source)['status'],'pending')
            manager.pool.shutdown()


class CommittedAssetEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.data=self.root/'data';self.data.mkdir()
        self.source=self.root/'video.mp4';self.source.write_bytes(b'original source')
        self.identifier=str(uuid4())
        self.folder=self.data/'productions'/self.identifier;self.folder.mkdir(parents=True)
        self.database=self.data/'productions.sqlite3'
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('CREATE TABLE productions (id TEXT, checkpoints_json TEXT, updated_at TEXT, status TEXT)')
            db.commit()
        self.evidence={'sources':[{'index':0,'fingerprint':_fingerprint(self.source),'duration':18,'coverage':18,
                                  'sampled_ranges':[[0,5],[10,15]]}],
                       'speech':[{'source_index':0,'start':1,'end':4,'text':'准备去市区看看，还没有决定。'},
                                 {'source_index':0,'start':6,'end':8,'text':'这个方案可能还要沟通。'},
                                 {'source_index':0,'start':11,'end':15,'text':'并没有确认托管。'}]}

    def commit(self, evidence=None, status='running'):
        raw=json.dumps(evidence if evidence is not None else self.evidence,ensure_ascii=False).encode('utf-8')
        (self.folder/'analysis.json').write_bytes(raw)
        checkpoint={'analysis':{'sha256':hashlib.sha256(raw).hexdigest()}}
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('DELETE FROM productions')
            db.execute('INSERT INTO productions VALUES(?,?,?,?)',(self.identifier,json.dumps(checkpoint),'2026-11-04',status))
            db.commit()

    def manager(self):
        manager=AssetInsights(self.data)
        self.addCleanup(manager.pool.shutdown)
        return manager

    def test_committed_original_words_replace_old_summary_and_preserve_frame_review(self):
        self.commit(status='blocked')  # Analysis may be complete even if rendering is blocked.
        manager=self.manager();folder=manager.root/fingerprint(self.source);folder.mkdir()
        manager.save(folder,{'status':'ready','frames':[{'time':2,'image':'data:image/jpeg;base64,ZmFrZQ=='}],
                             'has_audio':True,'summary':'已经完成并确认托管。',
                             'recommendation':{'score':92,'scope':'仅技术初评'}})
        result=manager.read(self.source)
        self.assertEqual(result['status'],'ready')
        self.assertEqual(result['summary_kind'],'director_speech_excerpt')
        self.assertIn('0:01–0:04 原声：“准备去市区看看，还没有决定。”',result['summary'])
        self.assertNotIn('已经完成并确认托管',result['summary'])
        self.assertIn('并没有确认托管。',result['summary'])
        self.assertEqual(result['recommendation'],{'score':92,'scope':'仅技术初评'})
        self.assertEqual(result['frames'][0]['time'],2)
        self.assertEqual(result['analyzed_ranges'],[[0,5],[10,15]])
        self.assertFalse(result['human_verified'])

    def test_uncommitted_or_overwritten_analysis_is_not_used(self):
        (self.folder/'analysis.json').write_text(json.dumps(self.evidence),encoding='utf-8')
        self.assertIsNone(read_director_excerpt(self.data,self.source))
        self.commit()
        (self.folder/'analysis.json').write_text('{}',encoding='utf-8')
        self.assertIsNone(read_director_excerpt(self.data,self.source))

    def test_changed_source_rejects_former_analysis(self):
        self.commit()
        self.source.write_bytes(b'changed source data')
        self.assertIsNone(read_director_excerpt(self.data,self.source))

    def test_invalid_time_positions_do_not_become_summary(self):
        for field,value in [('start',True),('start',float('nan')),('start',-1),('end',25),('end',.5)]:
            with self.subTest(field=field,value=value):
                evidence=json.loads(json.dumps(self.evidence));evidence['speech'][0][field]=value
                self.commit(evidence)
                self.assertIsNone(read_director_excerpt(self.data,self.source))
        evidence=json.loads(json.dumps(self.evidence));evidence['speech'][1]['start']=3
        self.commit(evidence)
        self.assertIsNone(read_director_excerpt(self.data,self.source))

    def test_evidence_schema_and_coverage_fail_closed(self):
        for value in ([],{'sources':None,'speech':[]},{'sources':[None],'speech':[]},
                      {**self.evidence,'sources':[{**self.evidence['sources'][0],'coverage':float('inf')}]},
                      {**self.evidence,'sources':[{**self.evidence['sources'][0],'duration':float('inf')}]},
                      {**self.evidence,'sources':[{**self.evidence['sources'][0],'coverage':50}]}):
            with self.subTest(evidence=value):
                self.commit(value)
                self.assertIsNone(read_director_excerpt(self.data,self.source))

    def test_quotes_sorted_without_rewriting_intent_or_negation(self):
        self.evidence['speech'].reverse();self.commit()
        result=read_director_excerpt(self.data,self.source)
        self.assertEqual([s['start'] for s in result['speech_excerpts']],[1,6,11])
        self.assertEqual(result['speech_excerpts'][0]['text'],'准备去市区看看，还没有决定。')
        self.assertEqual(result['speech_excerpts'][-1]['text'],'并没有确认托管。')

    def test_no_speech_does_not_infer_an_event_from_frames(self):
        self.evidence['speech']=[];self.commit()
        result=read_director_excerpt(self.data,self.source)
        self.assertEqual(result['speech_excerpts'],[])
        self.assertIn('不据此推断事件结果',result['summary'])

    def test_unreviewed_legacy_summary_is_marked_once_without_mutating_cache(self):
        original={'status':'ready','has_audio':True,'summary':'已抵达市区。','summary_basis':'覆盖20秒'}
        result=legacy_summary_notice(original)
        self.assertEqual(result['summary'],'原声自动摘要（未经校对，非事实认证）：已抵达市区。')
        self.assertEqual(original['summary'],'已抵达市区。')
        self.assertEqual(legacy_summary_notice(result),result)
        no_audio={**original,'has_audio':False}
        self.assertEqual(legacy_summary_notice(no_audio),no_audio)

    def test_shared_pipeline_lock_serializes_legacy_gpu_and_reuses_new_checkpoint(self):
        manager=self.manager();key=fingerprint(self.source)
        model=self.root/'model';model.mkdir()
        python=self.root/'python';python.write_text('test')
        reached_lock=threading.Event();acquired=threading.Event()

        class ObservedLock:
            def __enter__(self):
                reached_lock.set();PIPELINE_LOCK.acquire();acquired.set()
            def __exit__(self,*args):PIPELINE_LOCK.release()

        PIPELINE_LOCK.acquire()
        thread=None
        try:
            with patch('studio.asset_insights.PIPELINE_LOCK',ObservedLock()), \
                 patch('studio.asset_insights.analyze_visuals',return_value={'frames':[{'time':1,'image':'test'}],'has_audio':True}), \
                 patch.dict('os.environ',{'CUTFLOW_IDEA_PYTHON':str(python),'CUTFLOW_ASSET_WHISPER_MODEL':str(model)}), \
                 patch('studio.asset_insights.subprocess.run') as gpu:
                thread=threading.Thread(target=manager.run,args=(self.source,key));thread.start()
                self.assertTrue(reached_lock.wait(3),'background insight did not reach the shared lock')
                self.assertFalse(acquired.is_set())
                gpu.assert_not_called()
                self.commit()  # Production publishes evidence while holding the shared GPU lock.
                PIPELINE_LOCK.release()
                thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertTrue(acquired.is_set())
                gpu.assert_not_called()
                self.assertEqual(manager.read(self.source)['summary_kind'],'director_speech_excerpt')
        finally:
            if not acquired.is_set() and PIPELINE_LOCK.locked():PIPELINE_LOCK.release()
            if thread:thread.join(3)

    def test_legacy_gpu_executes_with_shared_lock_held(self):
        manager=self.manager();key=fingerprint(self.source)
        model=self.root/'model';model.mkdir()
        python=self.root/'python';python.write_text('test')
        observed=[]
        def run(command,**kwargs):
            observed.append(PIPELINE_LOCK.locked())
            (manager.root/key/'speech.json').write_text(json.dumps({'summary':'他说准备出发。'}),encoding='utf-8')
            return SimpleNamespace(returncode=0)
        with patch('studio.asset_insights.analyze_visuals',return_value={'frames':[{'time':1,'image':'test'}],'has_audio':True}), \
             patch.dict('os.environ',{'CUTFLOW_IDEA_PYTHON':str(python),'CUTFLOW_ASSET_WHISPER_MODEL':str(model)}), \
             patch('studio.asset_insights.subprocess.run',side_effect=run):
            manager.run(self.source,key)
        self.assertEqual(observed,[True])
        self.assertFalse(PIPELINE_LOCK.locked())
        self.assertIn('非事实认证',manager.read(self.source)['summary'])

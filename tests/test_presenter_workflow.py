import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from PIL import Image
from fastapi.testclient import TestClient
from studio.server import create_app


class PresenterWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = create_app(data_dir=self.root/'data', inbox=self.root/'inbox', token='test', start_worker=False)
        self.client = TestClient(self.app)
        self.auth = {'Authorization': 'Bearer test'}
        self.ident = str(uuid4())
        raw = io.BytesIO()
        Image.new('RGB', (512, 512), (125, 90, 80)).save(raw, format='PNG')
        self.raw = raw.getvalue()
        self.body = {'mode':'talking_head','sources':[],'brief':'欢迎了解沙龙活动','spoken_script':'欢迎参加沙龙。',
                     'voice_id':'serena','options':{'source_audio':'mute'},'idempotency_key':'person-test',
                     'presenter':{'kind':'personal','portrait_id':self.ident,'consent_confirmed':True}}
        self.caps = {'configured':True,'model_exists':True,'reason':None,'runtime_verified':True}

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def import_ready(self):
        with patch('studio.presenter.configuration_status',return_value=self.caps), patch('studio.presenter.inspect_portrait',return_value={'face_validated':True}):
            return self.client.post('/api/presenters/import',params={'filename':'me.png','name':'我的形象','id':self.ident,'consent_confirmed':'true'},headers={**self.auth,'Content-Type':'image/png'},content=self.raw)

    def test_portrait_routes_require_auth_and_import_is_not_video(self):
        self.assertEqual(self.client.get('/api/presenters').status_code,401)
        self.assertEqual(self.import_ready().status_code,200)
        self.assertEqual(self.app.state.store.list(),[])
        self.assertEqual(self.client.get(f'/api/presenters/{self.ident}/image',headers=self.auth).status_code,200)

    def test_person_creation_snapshot_and_idempotency_without_video_sources(self):
        self.import_ready()
        with patch('studio.presenter.configuration_status',return_value=self.caps), patch('studio.narration.configuration_status',return_value={'configured':True}):
            first=self.client.post('/api/creations',headers=self.auth,json=self.body)
            self.assertEqual(first.status_code,201,first.text)
            job=first.json()['job']
            self.assertEqual(job['sources'],[])
            self.assertEqual(job['settings']['creation']['portrait_snapshot']['id'],self.ident)
            repeat=self.client.post('/api/creations',headers=self.auth,json=self.body)
            self.assertEqual(repeat.json()['job']['id'],job['id'])
            self.assertFalse(repeat.json()['created'])
            mismatch={**self.body,'presenter':{**self.body['presenter'],'kind':'public'}}
            self.assertEqual(self.client.post('/api/creations',headers=self.auth,json=mismatch).status_code,409)

    def test_rejects_unsupported_looks_loop_or_missing_consent(self):
        for change in ({'presenter':{**self.body['presenter'],'consent_confirmed':False}},
                       {'presenter':{**self.body['presenter'],'background_id':'fake-background'}},
                       {'visual_fill':'loop'}, {'options':{'source_audio':'low'}}, {'sources':['video.mp4']}):
            with self.subTest(change=change):
                self.assertEqual(self.client.post('/api/creations',headers=self.auth,json={**self.body,**change}).status_code,422)

    def test_worker_reuses_one_voice_for_lipsync_and_final_composition(self):
        self.import_ready()
        def voice(text,ident,path,**kwargs):
            Path(path).write_bytes(b'audio')
            return {'output_path':str(path),'duration':2.0}
        def animate(portrait,audio,path,**kwargs):
            Path(path).write_bytes(b'animated')
            return {'output_path':str(path),'qc':{'passed':True}}
        def plan(paths,text,ident,options,settings,**kwargs):
            self.assertIn('prepared_narration',kwargs)
            self.assertTrue(Path(paths[0]).is_file())
            self.assertEqual(options['source_audio'],'mute')
            return {'narration':kwargs['prepared_narration'],'duration':2.0,'warnings':[]}
        def render(plan,output,settings):
            Path(output).write_bytes(b'final-video')
            return {'qc':{'passed':True},'duration':2.0}
        with patch('studio.presenter.configuration_status',return_value=self.caps), patch('studio.narration.configuration_status',return_value={'configured':True}), patch('studio.narration.synthesize',side_effect=voice) as synth, patch('studio.presenter.render_presenter',side_effect=animate), patch('studio.narrated.build_narrated_plan',side_effect=plan), patch('studio.narrated.render_narrated_plan',side_effect=render):
            result=self.client.post('/api/creations',headers=self.auth,json=self.body)
            job=result.json()['job']
            self.app.state.worker.process(self.app.state.store.claim())
            self.assertEqual(self.app.state.store.get(job['id'])['status'],'review')
            synth.assert_called_once()

if __name__=='__main__':
    unittest.main()

import tempfile
import unittest
from unittest.mock import patch
import shutil
import subprocess
from pathlib import Path
from fastapi.testclient import TestClient
from studio.server import create_app, validate_job_sources


class VlogIntegration(unittest.TestCase):
    def test_submission_worker_and_real_encoded_output(self):
        if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
            self.skipTest('FFmpeg required')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            app = create_app(data_dir=root/'data', inbox=root/'inbox', token='vlog-test', start_worker=False)
            source = root/'inbox'/'voice.mp4'
            subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y',
                '-f','lavfi','-i','testsrc2=size=320x320:rate=30',
                '-f','lavfi','-i','sine=frequency=400:sample_rate=48000',
                '-t','2','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',str(source)],check=True)
            auth = {'Authorization':'Bearer vlog-test'}
            with TestClient(app) as client:
                body = {'mode':'vlog','sources':['voice.mp4'],'brief':'原声记录',
                    'idempotency_key':'vlog-integration','options':{'music':True,'subtitles':False,
                        'allow_supplemental_visuals':False,'allow_generated_dialogue':False}}
                response = client.post('/api/creations',headers=auth,json=body)
                self.assertEqual(response.status_code,201,response.text)
                job_id = response.json()['job']['id']
                job = app.state.store.claim()
                self.assertEqual(len(validate_job_sources(job,root/'inbox',app.state.store)),1)
                app.state.worker.process(job)
                result = app.state.store.get(job_id)
                self.assertEqual(result['status'],'review',result.get('error'))
                self.assertTrue(result['result']['qc']['passed'])
                self.assertTrue(result['result']['metadata']['has_audio'])
                self.assertAlmostEqual(result['result']['duration'],2,delta=.15)
                self.assertEqual(client.get(f'/api/jobs/{job_id}/video',headers=auth).status_code,200)
                stages = client.get(f'/api/jobs/{job_id}',headers=auth).json()['job']['progress_detail']['stages']
                self.assertNotIn('transcribing',[s['id'] for s in stages])
                for option in ['allow_supplemental_visuals','allow_generated_dialogue']:
                    with patch('studio.supplements.configuration_status',return_value={'visuals':False,'dialogue':False}):
                        response = client.post('/api/creations',headers=auth,json={**body,
                            'idempotency_key':option,'options':{option:True}})
                    self.assertEqual(response.status_code,503,response.text)
            app.state.previews.stop()


if __name__ == '__main__':
    unittest.main()

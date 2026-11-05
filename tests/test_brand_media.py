from io import BytesIO
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI,Header,HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from studio.brand_media import register_brand_media_routes,resolve_screenshot,production_assets,bind_screenshot,MAX_BYTES
from studio import brand_context


def png(color='blue'):
    stream=BytesIO();Image.new('RGB',(32,48),color).save(stream,format='PNG');return stream.getvalue()


class BrandMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.data=Path(self.temp.name).resolve()
        app=FastAPI()
        def auth(authorization:str=Header(default='')):
            if authorization!='Bearer test':raise HTTPException(401,'unauthorized')
        register_brand_media_routes(app,auth,self.data)
        self.client=TestClient(app);self.auth={'Authorization':'Bearer test'}
        self.route='/api/brand-context/screenshots';self.params={'name':'小程序订单页','request_id':str(uuid4())}

    def tearDown(self):self.client.close();self.temp.cleanup()

    def post(self,raw=None,params=None,headers=None):
        return self.client.post(self.route,params=params or self.params,headers=self.auth if headers is None else headers,
                                content=png() if raw is None else raw)

    def test_auth_and_valid_round_trip(self):
        self.assertEqual(self.post(headers={}).status_code,401)
        self.assertEqual(self.client.get(self.route).status_code,401)
        response=self.post();self.assertEqual(response.status_code,200,response.text)
        item=response.json()['screenshot'];self.assertNotIn('path',item)
        self.assertEqual((item['width'],item['height']),(32,48))
        self.assertEqual(self.client.get(item['url']).status_code,401)
        result=self.client.get(item['url'],headers=self.auth)
        self.assertEqual(result.status_code,200);self.assertTrue(result.headers['content-type'].startswith('image/png'))
        self.assertEqual(self.client.get(self.route,headers=self.auth).json()['screenshots'],[item])
        self.assertTrue(resolve_screenshot(self.data,item['id']).is_file())

    def test_idempotency_and_conflict(self):
        one=self.post().json()['screenshot'];two=self.post().json()['screenshot']
        self.assertEqual(one,two)
        self.assertEqual(self.post(png('red')).status_code,409)
        self.assertEqual(len(self.client.get(self.route,headers=self.auth).json()['screenshots']),1)

    def test_rejects_malicious_nonimage_and_ids(self):
        for raw in (b'<svg onload="bad()"/>',b'not an image',b'GIF89a'+b'x'*80):
            self.assertEqual(self.post(raw).status_code,400)
        for value in ('../../secret','abc',uuid4().hex):
            self.assertEqual(self.post(params={**self.params,'request_id':value}).status_code,400)
        self.assertEqual(self.post(params={**self.params,'name':'\x00private'}).status_code,400)
        self.assertEqual(self.client.get(self.route+'/not-an-id',headers=self.auth).status_code,404)
        self.assertEqual(self.post(headers={**self.auth,'Content-Length':str(MAX_BYTES+1)}).status_code,413)

    def test_exif_is_removed_and_image_is_not_upscaled(self):
        image=Image.new('RGB',(32,48),'red');exif=Image.Exif();exif[270]='private description';exif[274]=6
        stream=BytesIO();image.save(stream,format='JPEG',exif=exif)
        response=self.post(stream.getvalue());self.assertEqual(response.status_code,200,response.text)
        item=response.json()['screenshot'];self.assertEqual((item['width'],item['height']),(48,32))
        with Image.open(resolve_screenshot(self.data,item['id'])) as clean:
            self.assertFalse(clean.getexif());self.assertNotIn('exif',clean.info)

    def test_symlink_escape_is_not_served(self):
        item=self.post().json()['screenshot'];target=resolve_screenshot(self.data,item['id'])
        outside=self.data/'outside.png';outside.write_bytes(png('red'))
        target.unlink()
        try:target.symlink_to(outside)
        except OSError:self.skipTest('symlink permission unavailable')
        self.assertEqual(self.client.get(item['url'],headers=self.auth).status_code,404)

    def evidence(self,**changes):
        return {'kind':'miniapp_screenshot','capture_mode':'local_miniapp','page_route':'pagesC/carpark/smartHosting',
                'source_reference':'carRental/pagesC/carpark/smartHosting.vue:12','source_sha256':'a'*64,
                'fact_ids':['user-miniapp-name'],'purpose':'介绍汽车助理页面入口',
                'verification_scope':'仅核验本地页面和入口；不证明线上服务、审批或实际收益。',
                'review_status':'confirmed',**changes}

    def bind(self,item,**changes):
        return self.client.put(self.route+'/'+item['id']+'/evidence',headers=self.auth,
                               json={'expected_sha256':item['sha256'],'evidence':self.evidence(**changes)})

    def test_reviewed_fact_binding_enters_production_with_exact_normalized_hash(self):
        item=self.post().json()['screenshot'];path=resolve_screenshot(self.data,item['id'])
        self.assertEqual(item['sha256'],hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(production_assets(self.data),[])
        response=self.bind(item);self.assertEqual(response.status_code,200,response.text)
        assets=production_assets(self.data,['user-miniapp-name'])
        self.assertEqual(len(assets),1);asset=assets[0]
        for field in ('id','sha256','width','height'):self.assertEqual(asset[field],item[field])
        self.assertEqual(asset['fact_ids'],['user-miniapp-name']);self.assertNotIn('path',asset)
        self.assertEqual(asset['verification_scope'],self.evidence()['verification_scope'])
        self.assertEqual(production_assets(self.data,[]),[])
        self.assertEqual(self.bind(item).json()['screenshot']['evidence_revision'],1)
        model=brand_context.model_context(self.data,query='Super Ecar 名称')
        self.assertEqual(model['brand_assets'][0]['id'],item['id'])
        self.assertEqual(model['confirmed_facts'][0]['id'],'user-miniapp-name')
        self.assertEqual(model['fact_sources']['user-miniapp-name'],'用户本次明确说明的小程序名称')

    def test_unknown_facts_and_source_less_confirmation_are_rejected(self):
        item=self.post().json()['screenshot']
        for changes in ({'fact_ids':['missing']},{'fact_ids':['source-miniapp-hosting']},
                        {'source_reference':''},{'verification_scope':''},{'purpose':''},{'fact_ids':[]},
                        {'page_route':''},{'kind':'unknown'},{'capture_mode':'unknown'}):
            with self.subTest(changes=changes):self.assertEqual(self.bind(item,**changes).status_code,400)
        self.assertEqual(production_assets(self.data),[])

    def test_website_is_never_mislabeled_or_selected_as_miniapp(self):
        item=self.post().json()['screenshot']
        self.assertEqual(self.bind(item,capture_mode='website').status_code,400)
        website=self.bind(item,kind='website_screenshot',capture_mode='website',page_route='https://example.com')
        self.assertEqual(website.status_code,200)
        self.assertEqual(production_assets(self.data),[])
        self.assertEqual(self.client.get(self.route,headers=self.auth).json()['screenshots'][0]['kind'],'website_screenshot')

    def test_modified_png_is_not_served_or_provided_to_director(self):
        item=self.post().json()['screenshot'];self.bind(item)
        path=resolve_screenshot(self.data,item['id']);path.write_bytes(png('red'))
        self.assertEqual(self.client.get(item['url'],headers=self.auth).status_code,409)
        self.assertEqual(production_assets(self.data),[])
        self.assertEqual(self.bind(item).status_code,409)

    def test_expected_version_hash_guards_frozen_production(self):
        item=self.post().json()['screenshot'];self.bind(item)
        with self.assertRaises(HTTPException) as error:resolve_screenshot(self.data,item['id'],'b'*64)
        self.assertEqual(error.exception.status_code,409)
        self.assertEqual(resolve_screenshot(self.data,item['id'],item['sha256']).name,item['id']+'.png')

    def test_legacy_image_can_be_reviewed_without_replacing_original_bytes(self):
        item=self.post().json()['screenshot'];path=resolve_screenshot(self.data,item['id']);before=path.read_bytes()
        manifest=path.parent/'manifest.json';records=json.loads(manifest.read_text())
        records[0].pop('content_sha256');manifest.write_text(json.dumps(records),encoding='utf-8')
        self.assertEqual(production_assets(self.data),[])
        bound=bind_screenshot(self.data,item['id'],self.evidence(),expected_sha256=hashlib.sha256(before).hexdigest())
        self.assertEqual(bound['id'],item['id']);self.assertEqual(path.read_bytes(),before)
        self.assertEqual(len(production_assets(self.data)),1)

    def test_unconfirmed_review_and_removed_facts_do_not_enter_automatic_production(self):
        item=self.post().json()['screenshot'];self.bind(item,review_status='needs_review')
        self.assertEqual(production_assets(self.data),[])
        self.bind(item)
        current=brand_context.default_context();current['facts']=[f for f in current['facts'] if f['id']!='user-miniapp-name']
        brand_context.save_context(self.data,current,0)
        self.assertEqual(production_assets(self.data),[])

    def test_metadata_route_is_authenticated_bounded_and_does_not_overwrite_ids(self):
        item=self.post().json()['screenshot'];route=self.route+'/'+item['id']+'/evidence'
        body={'expected_sha256':item['sha256'],'evidence':self.evidence()}
        self.assertEqual(self.client.put(route,json=body).status_code,401)
        self.assertEqual(self.client.put(route,headers=self.auth,content=b'x'*12001).status_code,413)
        self.assertEqual(self.bind(item,id='override').status_code,400)
        self.assertEqual(self.client.put(route,headers=self.auth,json={**body,'expected_sha256':'bad'}).status_code,400)
        self.assertEqual(self.client.put(route,headers=self.auth,json={**body,'expected_sha256':'b'*64}).status_code,409)

    def test_miniapp_request_recalls_bound_fact_without_exceeding_retrieval_limit(self):
        context=brand_context.default_context()
        context['facts'] += [{'id':'ordinary-'+str(i),'category':'facts','status':'confirmed','source':'已保存资料',
                             'text':'小程序平台资料说明'+str(i)+'。'} for i in range(14)]
        context['facts'].append({'id':'assistant-entry','category':'miniapp','status':'confirmed',
                                'source':'本地页面及已审核截图，仅证明入口','text':'可打开汽车助理页面提问，不代表申请已通过。'})
        brand_context.save_context(self.data,context,0)
        item=self.post().json()['screenshot'];self.bind(item,fact_ids=['assistant-entry'])
        selected=brand_context.model_context(self.data,query='用小程序截图介绍平台')
        self.assertLessEqual(len(selected['confirmed_facts']),8)
        self.assertEqual(selected['confirmed_facts'][0]['id'],'user-miniapp-name')
        self.assertIn('assistant-entry',[fact['id'] for fact in selected['confirmed_facts']])
        self.assertEqual(selected['brand_assets'][0]['fact_ids'],['assistant-entry'])
        self.assertIn('不代表申请已通过',brand_context.confirmed_text(selected))

    def test_malformed_metadata_types_are_rejected_without_server_error(self):
        item=self.post().json()['screenshot']
        for field in ('kind','capture_mode','review_status','source_sha256'):
            for value in ([],{},None,False):
                with self.subTest(field=field,value=value):
                    self.assertEqual(self.bind(item,**{field:value}).status_code,400)

    def test_invalid_legacy_image_is_not_confirmed_even_when_hash_matches(self):
        item=self.post().json()['screenshot'];path=resolve_screenshot(self.data,item['id'])
        manifest=path.parent/'manifest.json';records=json.loads(manifest.read_text())
        records[0].pop('content_sha256');manifest.write_text(json.dumps(records),encoding='utf-8')
        path.write_bytes(b'not an image')
        response=self.client.put(self.route+'/'+item['id']+'/evidence',headers=self.auth,
            json={'expected_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'evidence':self.evidence()})
        self.assertEqual(response.status_code,409)
        self.assertNotIn('evidence',json.loads(manifest.read_text())[0])


if __name__=='__main__':unittest.main()

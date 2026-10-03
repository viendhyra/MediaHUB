"""Проверки обновлений и доставки APK без Linux-служб и настоящего GitHub."""
import ast,asyncio,json,os,re,time,tempfile,unittest,ipaddress
from pathlib import Path
from unittest.mock import patch,AsyncMock
import httpx
from fastapi import FastAPI,Form,HTTPException
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient
from starlette.requests import Request

class DistributionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        """Загрузить реальные обработчики в изолированное приложение."""
        self.temp=tempfile.TemporaryDirectory();root=Path(self.temp.name);(root/'static').mkdir()
        self.app=FastAPI();self.scope=dict(Path=Path,re=re,time=time,json=json,os=os,ipaddress=ipaddress,asyncio=asyncio,httpx=httpx,Request=Request,Form=Form,FileResponse=FileResponse,HTTPException=HTTPException,app=self.app,APP_VERSION='21.99',UPDATE_REPO='viendhyra/MediaHUB',UPDATE_CACHE={},UPDATE_CHECK_LOCK=asyncio.Lock(),BASE_DIR=root,setup_read_state=lambda:{})
        names={'setup_request_allowed','require_setup_access','update_version_tuple','github_update_info','updates_check','updates_install','apps_info','apps_download'}
        tree=ast.parse((Path(__file__).parents[1]/'app.py').read_text(encoding='utf-8'))
        nodes=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in names]
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'app.py','exec'),self.scope)

    def tearDown(self):
        """Удалить временный APK и настройки проверки."""
        self.temp.cleanup()

    async def test_numeric_versions(self):
        self.assertGreater(self.scope['update_version_tuple']('21.100'),self.scope['update_version_tuple']('21.99'))
        for bad in ('latest','../22','22.0;echo x',''):
            with self.assertRaises(ValueError):self.scope['update_version_tuple'](bad)

    async def test_commit_pinning_and_cache(self):
        calls=[];sha='a'*40
        def handler(request):
            calls.append(str(request.url))
            if request.url.host=='api.github.com':return httpx.Response(200,json={'sha':sha})
            self.assertIn('/'+sha+'/',request.url.path)
            return httpx.Response(200,text='22.0\n' if request.url.path.endswith('VERSION.txt') else '# История\n\n## 22.0\nНовая версия\n\n## 21.99\nСтарые новости')
        original=httpx.AsyncClient
        with patch.object(httpx,'AsyncClient',side_effect=lambda **kwargs:original(**dict(kwargs,transport=httpx.MockTransport(handler)))):
            result=await self.scope['github_update_info']();cached=await self.scope['github_update_info']()
        self.assertTrue(result['available']);self.assertEqual(result['commit'],sha);self.assertEqual(result,cached)
        self.assertEqual(len(calls),3);self.assertNotIn('Старые новости',result['notes'])

    async def test_offline_github_is_recoverable(self):
        original=httpx.AsyncClient
        def handler(request):raise httpx.ConnectError('offline',request=request)
        with patch.object(httpx,'AsyncClient',side_effect=lambda **kwargs:original(**dict(kwargs,transport=httpx.MockTransport(handler)))):
            result=await self.scope['github_update_info']()
        self.assertFalse(result['available']);self.assertIn('error',result)

    async def test_github_retries_after_ipv4_failure(self):
        original=httpx.AsyncClient;clients=[];sha='b'*40
        def handler(request):
            if len(clients)==1:raise httpx.ConnectError('ipv4 down',request=request)
            if request.url.host=='api.github.com':return httpx.Response(200,json={'sha':sha})
            return httpx.Response(200,text='99.0\n' if request.url.path.endswith('VERSION.txt') else '# История\n\n## 99.0\nНовое')
        def client(**kwargs):clients.append(kwargs);return original(**dict(kwargs,transport=httpx.MockTransport(handler)))
        with patch.object(httpx,'AsyncClient',side_effect=client):
            result=await self.scope['github_update_info'](force=True)
        self.assertEqual(len(clients),2);self.assertTrue(result['available']);self.assertNotIn('error',result)

    async def test_untrusted_commit_is_rejected(self):
        self.scope['github_update_info']=AsyncMock(return_value={'available':True,'commit':'a'*40})
        with TestClient(self.app,client=('192.168.1.20',1234)) as client:
            response=client.post('/api/updates/install',data={'commit':'bad; touch /tmp/x'},headers={'X-MediaHub-Setup':'1'})
        self.assertEqual(response.status_code,409)

    async def test_setup_requires_private_client_and_header(self):
        self.scope['github_update_info']=AsyncMock(return_value={'available':False})
        with patch.dict(os.environ,{'MEDIAHUB_ALLOW_PUBLIC_SETUP':'0'}):
            with TestClient(self.app,client=('8.8.8.8',1234)) as client:self.assertEqual(client.get('/api/updates').status_code,403)
            with TestClient(self.app,client=('192.168.1.20',1234)) as client:self.assertEqual(client.post('/api/updates/install',data={'commit':'a'*40}).status_code,403)

    async def test_missing_and_present_apk(self):
        with TestClient(self.app) as client:
            self.assertFalse(client.get('/api/apps').json()['available'])
            self.assertEqual(client.get('/api/apps/android/download').status_code,404)
            static=self.scope['BASE_DIR']/'static';(static/'test.apk').write_bytes(b'PK-test-apk')
            (static/'android.json').write_text(json.dumps({'filename':'test.apk','version':'1.0'}))
            self.assertTrue(client.get('/api/apps').json()['available'])
            response=client.get('/api/apps/android/download');self.assertEqual(response.content,b'PK-test-apk');self.assertIn('attachment',response.headers['content-disposition'])
            (static/'android.json').write_text(json.dumps({'filename':'../../outside.apk'}))
            self.assertFalse(client.get('/api/apps').json()['available'])

"""Друзья: сопряжение хабов по коду, подписанные запросы, полка друга."""
import asyncio
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from fastapi.testclient import TestClient
import ast, hashlib, hmac, secrets, re, os, shutil, subprocess, json, threading, types, sys, ipaddress
from contextvars import ContextVar
from urllib.parse import quote
from fastapi import FastAPI, Form, Query, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse, Response, FileResponse, StreamingResponse
from starlette.requests import Request

portal=types.ModuleType('federation_test_portal');sys.modules[portal.__name__]=portal
portal.__dict__.update(dict(Path=Path,ipaddress=ipaddress,hashlib=hashlib,hmac=hmac,secrets=secrets,re=re,os=os,shutil=shutil,
    subprocess=subprocess,json=json,threading=threading,ContextVar=ContextVar,quote=quote,time=time,sqlite3=sqlite3,asyncio=asyncio,httpx=httpx,
    FastAPI=FastAPI,Form=Form,Query=Query,HTTPException=HTTPException,UploadFile=UploadFile,File=File,JSONResponse=JSONResponse,
    Response=Response,FileResponse=FileResponse,StreamingResponse=StreamingResponse,Request=Request,app=FastAPI(),CACHE_DB=Path('unused'),
    find_library_row=lambda kind,item_key='',path='':{},row_media=lambda r,kind=None:{'title':r['title'],'kind':kind,'year':r['year'],'poster':r['poster'],'externalId':r['external_id']}))
tree=ast.parse((Path(__file__).parents[1]/'app.py').read_text(encoding='utf-8'))
nodes=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and (n.name in {'account_access','cache_db'} or n.name.startswith(('auth_','family_','fed_','federation_','friends_')))
    or isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id.startswith(('AUTH_','FAMILY_','FED_')) for t in n.targets)]
exec(compile(ast.Module(body=nodes,type_ignores=[]),'app.py','exec'),portal.__dict__)


class FakeResponse:
    def __init__(self,status,payload):self.status_code=status;self.payload=payload
    def json(self):
        if isinstance(self.payload,Exception):raise self.payload
        return self.payload


class FakeClient:
    """Сеть между хабами: ответы задаёт тест через FakeClient.handler(method,url,params,data)."""
    handler=None
    def __init__(self,*args,**kwargs):pass
    async def __aenter__(self):return self
    async def __aexit__(self,*args):return False
    async def get(self,url,params=None):return FakeClient.handler('GET',url,params,None)
    async def post(self,url,data=None):return FakeClient.handler('POST',url,None,data)
    async def request(self,method,url,headers=None,data=None):return FakeClient.handler(method,url,headers,data)


class FederationTests(unittest.TestCase):
    def setUp(self):
        """Хаб B — этот портал; хаб A имитирует тест."""
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name);self.connections=[]
        original=sqlite3.connect
        def tracked(*args,**kwargs):
            kwargs['check_same_thread']=False
            con=original(*args,**kwargs);self.connections.append(con);return con
        fake_httpx=types.SimpleNamespace(AsyncClient=FakeClient,ConnectError=httpx.ConnectError,ConnectTimeout=httpx.ConnectTimeout,HTTPError=httpx.HTTPError)
        self.patches=[patch.object(sqlite3,'connect',tracked),patch.object(portal,'CACHE_DB',root/'db.sqlite'),patch.object(portal,'httpx',fake_httpx)]
        for p in self.patches:p.start()
        portal.AUTH_FAILURES.clear();portal.FED_NONCES.clear();portal.FED_ATTEMPTS.clear()
        self.admin=TestClient(portal.app);r=self.admin.post('/api/auth/login',data={'login':'admin','password':'admin'});self.admin.headers['Authorization']='Bearer '+r.json()['token']
        self.admin.post('/api/auth/accounts',data={'login':'alice','password':'test-pass'})
        self.user=TestClient(portal.app);r=self.user.post('/api/auth/login',data={'login':'alice','password':'test-pass'});self.user.headers['Authorization']='Bearer '+r.json()['token']
        self.admin.post('/api/auth/addresses',data={'external':'http://b.example.ru:18090'})
        self.a_id='a'*32;self.guest=TestClient(portal.app)

    def tearDown(self):
        for c in (self.admin,self.user,self.guest):c.close()
        for con in self.connections:con.close()
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()

    def befriend(self):
        """Хаб A добавляет B по коду; B проверяет адрес A ответным запросом."""
        code=self.admin.get('/api/friends').json()['me']['code']
        FakeClient.handler=lambda m,url,params,data:FakeResponse(200,{'hubId':self.a_id,'nonce':params['nonce']}) if url=='http://a.example.ru:18090/api/federation/verify' else FakeResponse(404,{})
        r=self.guest.post('/api/federation/hello',data={'code':code.lower(),'hub_id':self.a_id,'name':'Хаб Андрея','address':'a.example.ru:18090','nonce':'n'*20})
        self.assertEqual(r.status_code,200,r.text);return r.json()

    def signed(self,path,secret,method='GET',stamp=None,nonce=None,address='http://a.example.ru:18090'):
        """Заголовки подписанного запроса от хаба A."""
        stamp=str(stamp or int(time.time()));nonce=nonce or secrets.token_hex(16)
        return {'X-MH-Hub':self.a_id,'X-MH-Time':stamp,'X-MH-Nonce':nonce,'X-MH-Address':address,'X-MH-Sign':portal.fed_sign(secret,method,path,stamp,nonce,address)}

    def test_pairing_signed_library_and_removal(self):
        """Неверный код и недоступный адрес отклоняются; полка отдаётся только по верной подписи, без повтора."""
        code=self.admin.get('/api/friends').json()['me']['code']
        self.assertRegex(code,r'^MH-[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}$')
        self.assertNotIn('me',self.user.get('/api/friends').json())
        self.assertEqual(self.guest.post('/api/federation/hello',data={'code':'MH-AAAA-AAAA-AAAA','hub_id':self.a_id,'address':'a.example.ru:18090','nonce':'n'*20}).status_code,400)
        FakeClient.handler=lambda *a:(_ for _ in ()).throw(httpx.ConnectError('closed'))
        r=self.guest.post('/api/federation/hello',data={'code':code,'hub_id':self.a_id,'address':'a.example.ru:18090','nonce':'n'*20})
        self.assertIn('проброс порта',r.json()['detail'])
        answer=self.befriend()
        self.assertEqual((answer['address'],len(answer['secret'])>30),('http://b.example.ru:18090',True))
        friends=self.admin.get('/api/friends').json()['items'];self.assertEqual((friends[0]['name'],friends[0]['address']),('Хаб Андрея','http://a.example.ru:18090'))
        with portal.cache_db() as con:
            for path,title in [('/m/movies/Film (2020)','Film'),('/m/movies/Hidden (2021)','Hidden')]:
                con.execute("insert into library_cache(kind,item_key,title,year,path,has_file,poster,external_id) values('movies',?,?,?,?,1,'https://img/p.jpg','7')",(title,title,'2020',path))
                con.execute('insert into title_owners values(?,1,?)',(path,time.time()))
            con.execute("insert into friend_hidden values('/m/movies/Hidden (2021)',0)");con.commit()
        headers=self.signed('/api/federation/library?kind=movies',answer['secret'])
        r=self.guest.get('/api/federation/library?kind=movies',headers=headers)
        self.assertEqual([x['title'] for x in r.json()],['Film']);self.assertNotIn('/m/',json.dumps(r.json()))
        self.assertEqual(self.guest.get('/api/federation/library?kind=movies',headers=headers).status_code,401)
        self.assertEqual(self.guest.get('/api/federation/library',headers=self.signed('/api/federation/library','wrong')).status_code,401)
        self.assertEqual(self.guest.get('/api/federation/ping',headers=self.signed('/api/federation/ping',answer['secret'],stamp=int(time.time())-900)).status_code,401)
        # Друг сменил внешний адрес — у нас он обновился сам.
        self.guest.get('/api/federation/ping',headers=self.signed('/api/federation/ping',answer['secret'],address='http://new-a.example.ru:19000'))
        self.assertEqual(self.admin.get('/api/friends').json()['items'][0]['address'],'http://new-a.example.ru:19000')
        self.assertEqual(self.user.post('/api/friends/add',data={'address':'c.example.ru','code':'x'}).status_code,403)
        self.assertEqual(self.guest.post('/api/federation/remove',headers=self.signed('/api/federation/remove',answer['secret'],method='POST')).status_code,200)
        self.assertEqual(self.admin.get('/api/friends').json()['items'],[])

    def test_add_friend_and_read_shelf(self):
        """Мы добавляем друга C: сохраняем секрет, читаем его полку, помечаем то, что у нас уже есть."""
        with portal.cache_db() as con:
            con.execute("insert into library_cache(kind,item_key,title,year,path,has_file,external_id) values('movies','m','Матрица','1999','/m/movies/Матрица (1999)',1,'603')");con.commit()
        calls=[]
        def network(method,url,params,data):
            calls.append((method,url,data if isinstance(data,dict) else params))
            if url.endswith('/api/federation/hello'):return FakeResponse(200,{'hubId':'c'*32,'name':'Хаб Саши','address':'http://c.example.ru:18090','secret':'s'*40})
            if '/api/federation/library' in url:
                return FakeResponse(200,[{'id':'1','title':'Матрица','year':'1999','kind':'movies','externalId':'603','genres':[],'overview':'','poster':'','catalog':'tmdb','addedAt':0},
                                         {'id':'2','title':'Дюна','year':'2021','kind':'movies','externalId':'438631','genres':[],'overview':'','poster':'','catalog':'tmdb','addedAt':time.time()}])
            return FakeResponse(404,{})
        FakeClient.handler=network
        r=self.admin.post('/api/friends/add',data={'address':'c.example.ru:18090','code':'mh-abcd-efgh-jkmn'})
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(calls[0][2]['address'],'http://b.example.ru:18090');self.assertEqual(calls[0][2]['code'],'MH-ABCD-EFGH-JKMN')
        shelf=self.user.get('/api/friends/'+'c'*32+'/library?kind=movies').json()
        self.assertEqual([(x['title'],x['haveIt'],x['friend']) for x in shelf],[('Матрица',True,'Хаб Саши'),('Дюна',False,'Хаб Саши')])
        self.assertEqual(self.user.get('/api/friends/'+'x'*32+'/library').status_code,404)
        # «Новое у друзей»: свежее и чего у нас нет; полка друга берётся из кэша.
        portal.FED_NEW.clear();before=len(calls)
        self.assertEqual([(x['title'],x['friend']) for x in self.user.get('/api/friends/new').json()],[('Дюна','Хаб Саши')])
        self.assertEqual(len(self.user.get('/api/friends/new').json()),1)
        self.assertEqual(len(calls)-before,1)


class FriendDownloadTests(unittest.TestCase):
    def setUp(self):
        """Хаб с папкой фильма; раздача файлов другу и раскладка скачанного у себя."""
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.connections=[]
        original=sqlite3.connect
        def tracked(*args,**kwargs):
            kwargs['check_same_thread']=False
            con=original(*args,**kwargs);self.connections.append(con);return con
        self.film=self.root/'movies'/'Film (2020)';(self.film/'Subs').mkdir(parents=True)
        (self.film/'Film.mkv').write_bytes(b'0123456789');(self.film/'Subs'/'ru.srt').write_bytes(b'sub');(self.film/'sample.mkv').write_bytes(b'x')
        stubs=dict(VIDEO_EXTS={'.mkv'},SUB_EXTS={'.srt'},MOVIES_ROOT=self.root/'movies',TV_ROOT=self.root/'tv',ANIME_ROOT=self.root/'anime',
                   save_manual_meta=lambda con,path,kind,title,year='',poster='',overview='',*a,**k:None,upsert_live_library_item=lambda *a,**k:None,
                   reset_fs_scan_cache=lambda:None,log_activity=lambda *a,**k:None)
        self.patches=[patch.object(sqlite3,'connect',tracked),patch.object(portal,'CACHE_DB',self.root/'db'/'db.sqlite'),patch.object(portal,'fed_start_job',lambda job_id:None)]
        self.patches+=[patch.object(portal,k,v,create=True) for k,v in stubs.items()]
        (self.root/'db').mkdir()
        for p in self.patches:p.start()
        portal.AUTH_FAILURES.clear();portal.FED_NONCES.clear()
        self.admin=TestClient(portal.app);self.admin.headers['Authorization']='Bearer '+self.admin.post('/api/auth/login',data={'login':'admin','password':'admin'}).json()['token']
        self.admin.post('/api/auth/accounts',data={'login':'alice','password':'test-pass'})
        self.user=TestClient(portal.app);self.user.headers['Authorization']='Bearer '+self.user.post('/api/auth/login',data={'login':'alice','password':'test-pass'}).json()['token']
        self.guest=TestClient(portal.app);self.hub='a'*32;self.secret='s'*40
        with portal.cache_db() as con:
            con.execute('insert into title_owners values(?,1,0)',(str(self.film),))
            con.execute("insert into friend_hubs(hub_id,name,address,secret,status,created_at) values(?,?,?,?,'active',0)",(self.hub,'Хаб Андрея','http://a.example.ru:18090',self.secret));con.commit()
        self.id=hashlib.sha256(str(self.film).encode()).hexdigest()[:24]

    def tearDown(self):
        for c in (self.admin,self.user,self.guest):c.close()
        for con in self.connections:con.close()
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()

    def signed(self,path):
        """Подписанный запрос от друга."""
        stamp=str(int(time.time()));nonce=secrets.token_hex(16)
        return {'X-MH-Hub':self.hub,'X-MH-Time':stamp,'X-MH-Nonce':nonce,'X-MH-Address':'','X-MH-Sign':portal.fed_sign(self.secret,'GET',path,stamp,nonce,'')}

    def test_serving_files_with_resume_and_hidden(self):
        """Друг получает состав без сэмплов и докачивает файл с середины; скрытый тайтл не отдаётся."""
        path=f'/api/federation/files?id={self.id}'
        files=self.guest.get(path,headers=self.signed(path)).json()['files']
        self.assertEqual([(f['rel'],f['size']) for f in files],[('Film.mkv',10),('Subs/ru.srt',3)])
        path=f'/api/federation/file?id={self.id}&n=0'
        r=self.guest.get(path,headers={**self.signed(path),'Range':'bytes=4-'})
        self.assertEqual((r.status_code,r.content,r.headers['content-range']),(206,b'456789','bytes 4-9/10'))
        with portal.cache_db() as con:con.execute('insert into friend_hidden values(?,0)',(str(self.film),));con.commit()
        path=f'/api/federation/files?id={self.id}'
        self.assertEqual(self.guest.get(path,headers=self.signed(path)).status_code,404)

    def test_jobs_rights_and_finish_into_library(self):
        """Задание видит автор и админ; готовое ложится «Название (Год)», второе — «— от друга»; автор и событие."""
        r=self.user.post(f'/api/friends/{self.hub}/download',data={'id':'b'*24,'kind':'movies','title':'Дюна','year':'2021','poster':'file:///etc/passwd'})
        self.assertEqual(r.status_code,200,r.text);job=r.json()['id']
        self.assertEqual(self.user.post(f'/api/friends/{self.hub}/download',data={'id':'b'*24,'kind':'movies','title':'Дюна'}).json()['id'],job)
        self.assertEqual([x['title'] for x in self.user.get('/api/friends/downloads').json()['items']],['Дюна'])
        self.assertEqual(len(self.admin.get('/api/friends/downloads').json()['items']),1)
        alice=self.user.get('/api/auth/me').json()['id']
        for expected in ['Дюна (2021)','Дюна (2021) — от друга']:
            stage=portal.fed_staging(job);stage.mkdir(parents=True);(stage/'Dune.mkv').write_bytes(b'video')
            dest=portal.fed_finish(job)
            self.assertEqual((dest.name,(dest/'Dune.mkv').read_bytes()),(expected,b'video'))
            portal.fed_job_update(job,status='downloading')
        self.assertEqual(json.loads(portal.fed_job(job)['meta'])['poster'],'')
        with portal.cache_db() as con:
            self.assertEqual(con.execute('select user_id from title_owners where path=?',(str(self.root/'movies'/'Дюна (2021)'),)).fetchone()['user_id'],alice)
            self.assertEqual(con.execute("select count(*) from family_events where type='added' and title='Дюна'").fetchone()[0],2)
        other=self.admin.post(f'/api/friends/{self.hub}/download',data={'id':'c'*24,'kind':'movies','title':'Другое'}).json()['id']
        self.assertEqual(self.user.post(f'/api/friends/downloads/{other}/cancel').status_code,403)
        self.assertEqual(self.admin.post(f'/api/friends/downloads/{other}/cancel').status_code,200)
        self.assertEqual(self.user.post(f'/api/friends/{self.hub}/download',data={'id':'../x','kind':'movies','title':'x'}).status_code,400)

    def test_transfer_resumes_partial_file(self):
        """Передача: список файлов, докачка недокачанного с нужного байта, раскладка в медиатеку."""
        job=self.user.post(f'/api/friends/{self.hub}/download',data={'id':'b'*24,'kind':'movies','title':'Дюна','year':'2021'}).json()['id']
        stage=portal.fed_staging(job);stage.mkdir(parents=True);(stage/'Dune.mkv').write_bytes(b'DUNE-')
        ranges=[]
        class Stream:
            def __init__(self,headers):self.status_code=206 if headers.get('Range') else 200;self.start=int(headers.get('Range','bytes=0-')[6:-1])
            async def __aenter__(self):return self
            async def __aexit__(self,*a):return False
            async def aiter_bytes(self,size):
                yield b'DUNE-VIDEO'[self.start:]
        class Client(FakeClient):
            def stream(self,method,url,headers=None):ranges.append(headers.get('Range'));return Stream(headers)
        FakeClient.handler=lambda m,url,headers,data:FakeResponse(200,{'files':[{'n':0,'rel':'Dune.mkv','size':10},{'n':1,'rel':'Subs/ru.srt','size':0}]})
        with patch.object(portal,'httpx',types.SimpleNamespace(AsyncClient=Client,Timeout=httpx.Timeout,ConnectError=httpx.ConnectError,ConnectTimeout=httpx.ConnectTimeout,HTTPError=httpx.HTTPError)):
            asyncio.run(portal.fed_run_job(job))
        done=portal.fed_job(job)
        self.assertEqual((done['status'],done['error'],done['done']),('done','',10))
        self.assertEqual(ranges,['bytes=5-',None])
        self.assertEqual((self.root/'movies'/'Дюна (2021)'/'Dune.mkv').read_bytes(),b'DUNE-VIDEO')


if __name__=='__main__':
    unittest.main()

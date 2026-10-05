"""Реальные маршруты: вход, роли, миграция, личный прогресс и пульт."""
import asyncio
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
import httpx
from fastapi.testclient import TestClient
import ast, hashlib, hmac, secrets, re, os, shutil, subprocess, json, threading, types, sys, ipaddress
from contextvars import ContextVar
from urllib.parse import quote
from fastapi import FastAPI, Form, Query, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse, Response, FileResponse, StreamingResponse
from starlette.requests import Request

# Реальные функции без Linux-зависимого установщика и фоновых процессов.
portal=types.ModuleType('account_test_portal');sys.modules[portal.__name__]=portal
portal.__dict__.update(dict(Path=Path,ipaddress=ipaddress,hashlib=hashlib,hmac=hmac,secrets=secrets,re=re,os=os,shutil=shutil,
    subprocess=subprocess,json=json,threading=threading,ContextVar=ContextVar,quote=quote,
    time=time,sqlite3=sqlite3,asyncio=asyncio,FastAPI=FastAPI,Form=Form,Query=Query,HTTPException=HTTPException,
    UploadFile=UploadFile,File=File,JSONResponse=JSONResponse,Response=Response,FileResponse=FileResponse,StreamingResponse=StreamingResponse,Request=Request,
    app=FastAPI(),CACHE_DB=Path('unused'),MEDIA_ROOT=Path('unused'),TV_ROOT=Path('unused'),MOVIES_ROOT=Path('unused'),ANIME_ROOT=Path('unused'),
    VIDEO_EXTS={'.mp4','.mkv'},PLAYER_PROBE_CACHE={},PLAYER_TRANSCODES=set(),PLAYER_THUMB_ROOT=Path('unused'),PLAYER_THUMB_LOCK=threading.Lock(),library_files=None))
tree=ast.parse((Path(__file__).parents[1]/'app.py').read_text(encoding='utf-8'))
names={'account_access','cache_db','safe_media_path','is_media_root','_project_root_for','_check_project_file'}
nodes=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and (n.name in names or n.name.startswith(('auth_','player_','remote_')))
    or isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id.startswith(('AUTH_','REMOTE_','FAMILY_')) for t in n.targets)]
exec(compile(ast.Module(body=nodes,type_ignores=[]),'app.py','exec'),portal.__dict__)
@portal.app.get('/api/version')
def version():
    """Публичный маршрут обнаружения портала."""
    return {'version':'test'}
@portal.app.get('/api/setup/config')
def setup_config():
    """Маршрут, защищённый ролью администратора."""
    return {}


class AccountTests(unittest.TestCase):
    def setUp(self):
        """Изолировать базу и файлы, не выполняя startup домашнего сервера."""
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        self.project=root/'media'/'tv'/'Проект';self.project.mkdir(parents=True)
        self.video=self.project/'01.mp4';self.video.write_bytes(b'video')
        self.connections=[]
        original_connect=sqlite3.connect
        def tracked_connect(*args,**kwargs):
            kwargs["check_same_thread"]=False
            con=original_connect(*args,**kwargs);self.connections.append(con);return con
        self.patches=[patch.object(sqlite3,'connect',tracked_connect),patch.object(portal,'CACHE_DB',root/'db.sqlite'),patch.object(portal,'MEDIA_ROOT',root/'media'),patch.object(portal,'TV_ROOT',root/'media'/'tv'),patch.object(portal,'MOVIES_ROOT',root/'media'/'movies'),patch.object(portal,'ANIME_ROOT',root/'media'/'anime')]
        for p in self.patches:p.start()
        portal.AUTH_FAILURES.clear();portal.REMOTE_DEVICES.clear()
        self.admin=TestClient(portal.app);self.user=TestClient(portal.app)
        self.login(self.admin,'admin','admin')
        self.admin.post('/api/auth/accounts',data={'login':'alice','password':'test-pass'})
        self.login(self.user,'alice','test-pass')

    def tearDown(self):
        """Закрыть клиенты и удалить временную медиатеку."""
        self.admin.close();self.user.close()
        for con in self.connections:con.close()
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()

    def login(self,client,login,password):
        """Приложение использует Bearer; веб — HttpOnly cookie."""
        r=client.post('/api/auth/login',data={'login':login,'password':password})
        self.assertEqual(r.status_code,200,r.text)
        client.headers['Authorization']='Bearer '+r.json()['token']
        return r

    def test_login_roles_csrf_and_revocation(self):
        """Обычный аккаунт не получает управление, подмена user_id не даёт чужой пароль."""
        guest=TestClient(portal.app)
        self.assertEqual(guest.get('/api/player/resume').status_code,401)
        self.assertEqual(guest.get('/api/version').status_code,200)
        self.assertEqual(guest.post('/api/auth/login',data={'login':'admin','password':'wrong'}).status_code,401)
        self.assertEqual(guest.post('/api/auth/login',data={'login':'admin','password':'admin'},headers={'Origin':'https://foreign.example'}).status_code,403)
        self.assertEqual(self.user.get('/api/auth/accounts').status_code,403)
        self.assertEqual(self.user.get('/api/setup/config').status_code,403)
        self.assertEqual(self.user.post('/api/scan').status_code,403)
        self.assertEqual(self.user.post('/api/auth/password',data={'user_id':1,'password':'other-pass'}).status_code,403)
        r=self.login(guest,'admin','admin');self.assertIn('HttpOnly',r.headers['set-cookie'])
        del guest.headers['Authorization']
        self.assertEqual(guest.post('/api/auth/logout').status_code,403)
        self.assertEqual(guest.post('/api/auth/logout',headers={'X-MediaHub-Auth':'1'}).status_code,200)
        self.assertEqual(guest.get('/api/auth/me').status_code,401);guest.close()
        user_id=self.user.get('/api/auth/me').json()['id']
        self.assertEqual(self.admin.post('/api/auth/password',data={'user_id':user_id,'password':'new-pass'}).status_code,200)
        self.assertEqual(self.user.get('/api/auth/me').status_code,401)
        self.login(self.user,'alice','new-pass')
        self.assertEqual(self.user.post('/api/auth/password',data={'password':'my-pass','current_password':'wrong'}).status_code,400)
        self.assertEqual(self.user.post('/api/auth/password',data={'password':'my-pass','current_password':'new-pass'}).status_code,200)
        self.assertEqual(self.user.get('/api/auth/me').status_code,200)

    def test_independent_progress_project_and_resume(self):
        """Один файл имеет две позиции; история проекта и продолжение не смешиваются."""
        for client,position in [(self.admin,30),(self.user,80)]:
            r=client.post('/api/player/progress',data={'path':str(self.video),'position':position,'duration':1000})
            self.assertEqual(r.status_code,200,r.text)
        listing={'items':[{'video':True,'path':str(self.video),'rel':'01.mp4'}]}
        with patch.object(portal,'library_files',AsyncMock(return_value=listing)):
            for client,position in [(self.admin,30),(self.user,80)]:
                self.assertEqual(client.get('/api/player/resume').json()['items'][0]['position'],position)
                self.assertEqual(client.get('/api/player/project',params={'path':str(self.project)}).json()['items'][0]['position'],position)
        self.admin.post('/api/player/progress',data={'path':str(self.video),'position':900,'duration':1000})
        self.assertEqual(self.admin.get('/api/player/resume').json()['items'],[])
        self.assertEqual(self.user.get('/api/player/resume').json()['items'][0]['position'],80)

    def test_remote_devices_are_private(self):
        """Другой аккаунт не видит устройство и не посылает ему команду."""
        self.admin.get('/api/remote/poll',params={'device':'device-tv','wait':0})
        self.assertEqual(self.user.get('/api/remote/devices').json()['devices'],[])
        self.assertEqual(self.user.post('/api/remote/send',data={'device':'device-tv','action':'pause'}).status_code,404)
        self.user.get('/api/remote/poll',params={'device':'device-tv','wait':0})
        self.user.post('/api/remote/send',data={'device':'device-tv','action':'pause'})
        self.assertEqual(self.admin.get('/api/remote/poll',params={'device':'device-tv','wait':0}).json()['commands'],[])
        self.assertEqual(len(self.user.get('/api/remote/poll',params={'device':'device-tv','wait':0}).json()['commands']),1)

    def test_legacy_history_migrates_once(self):
        """Старый путь переносится только admin; поздний прогресс не перезаписывается."""
        newdb=Path(self.tmp.name)/'legacy.sqlite'
        with sqlite3.connect(newdb) as con:
            con.execute('create table playback_history(path text primary key,project text,position real,duration real,completed integer,signature text,updated_at real)')
            con.execute('insert into playback_history values(?,?,?,?,?,?,?)',(str(self.video),str(self.project),50,1000,0,portal.player_signature(self.video),time.time()))
        with patch.object(portal,'CACHE_DB',newdb):
            with portal.cache_db() as con:
                self.assertEqual(con.execute('select user_id,position from account_playback_history').fetchone()['position'],50)
                con.execute('update account_playback_history set position=100');con.commit()
            with portal.cache_db() as con:
                self.assertEqual(con.execute('select position from account_playback_history').fetchone()['position'],100)
                self.assertEqual(con.execute('select count(*) from playback_history').fetchone()[0],1)

    def test_parallel_requests_keep_account_context(self):
        """Контекст не смешивается при одновременных запросах и asyncio.to_thread."""
        async def run():
            transport=httpx.ASGITransport(app=portal.app)
            async with httpx.AsyncClient(transport=transport,base_url='http://testserver') as client:
                responses=await asyncio.gather(*[client.get('/api/auth/me',headers=dict(c.headers)) for c in [self.admin,self.user]*5])
                self.assertEqual([r.json()['login'] for r in responses],['admin','alice']*5)
        asyncio.run(run())

    def test_external_login_addresses_and_browser_accounts(self):
        """Снаружи заводской пароль не принимается; адреса сохраняются; браузер переключает аккаунты без пароля."""
        outside={'X-Forwarded-For':'8.8.8.8'}
        guest=TestClient(portal.app)
        r=guest.post('/api/auth/login',data={'login':'admin','password':'admin'},headers=outside)
        self.assertEqual(r.status_code,403);self.assertIn('по умолчанию',r.json()['detail'])
        self.assertEqual(guest.post('/api/auth/login',data={'login':'alice','password':'test-pass'},headers=outside).status_code,200)
        self.assertTrue(self.admin.get('/api/auth/addresses').json()['defaultPassword'])
        self.assertEqual(self.user.get('/api/auth/addresses').status_code,403)
        self.assertEqual(self.admin.post('/api/auth/addresses',data={'external':'bad address!'}).status_code,400)
        r=self.admin.post('/api/auth/addresses',data={'external':'Home.Example.ru:18090\nhttps://media.example.ru/ home.example.ru:18090'})
        self.assertEqual(r.json()['external'],['http://home.example.ru:18090','https://media.example.ru'])
        self.assertEqual(self.user.get('/api/auth/me').json()['addresses'][-2:],['http://home.example.ru:18090','https://media.example.ru'])
        # Веб: два входа в одном браузере, переключение без пароля, выход оставляет второй аккаунт.
        web=TestClient(portal.app);auth={'X-MediaHub-Auth':'1'}
        for login,password in [('admin','admin'),('alice','test-pass')]:
            self.assertEqual(web.post('/api/auth/login',data={'login':login,'password':password}).status_code,200)
        known=web.get('/api/auth/known').json()['items']
        self.assertEqual(sorted(x['login'] for x in known),['admin','alice']);self.assertTrue(next(x for x in known if x['login']=='alice')['current'])
        self.assertEqual(web.post('/api/auth/switch',data={'login':'admin'}).status_code,403)
        self.assertEqual(web.post('/api/auth/switch',data={'login':'admin'},headers={**auth,'Origin':'https://foreign.example'}).status_code,403)
        self.assertEqual(web.post('/api/auth/switch',data={'login':'admin'},headers=auth).status_code,200)
        self.assertEqual(web.get('/api/auth/me').json()['login'],'admin')
        self.assertEqual(web.post('/api/auth/logout',headers=auth).status_code,200)
        self.assertEqual(web.get('/api/auth/me').status_code,401)
        self.assertEqual([x['login'] for x in web.get('/api/auth/known').json()['items']],['alice'])
        self.assertEqual(web.post('/api/auth/switch',data={'login':'admin'},headers=auth).status_code,401)
        self.assertEqual(web.post('/api/auth/switch',data={'login':'alice'},headers=auth).status_code,200)
        self.assertEqual(web.get('/api/auth/me').json()['login'],'alice')
        guest.close();web.close()

    def test_avatars_presets_photos_and_rights(self):
        """Аватарка: встроенная и фото; чужую меняет только админ; фото доступно без входа по текущему адресу."""
        r=self.user.post('/api/auth/avatar',data={'preset':3});self.assertEqual(r.json()['avatarPreset'],3)
        self.assertEqual(self.user.post('/api/auth/avatar',data={'preset':99}).status_code,400)
        admin_id=self.admin.get('/api/auth/me').json()['id'];alice_id=self.user.get('/api/auth/me').json()['id']
        self.assertEqual(self.user.post('/api/auth/avatar',data={'preset':1,'user_id':admin_id}).status_code,403)
        self.assertEqual(self.admin.post('/api/auth/avatar',data={'preset':5,'user_id':alice_id}).json()['avatarPreset'],5)
        with patch.object(portal,'auth_avatar_image',lambda data:b'\xff\xd8'+data):
            first=self.user.post('/api/auth/avatar',files={'image':('me.jpg',b'one','image/jpeg')}).json()['avatar']
            second=self.user.post('/api/auth/avatar',files={'image':('me.jpg',b'two','image/jpeg')}).json()['avatar']
        self.assertRegex(second,r'^/api/avatar/%d-[0-9a-f]{16}\.jpg$'%alice_id)
        guest=TestClient(portal.app)
        r=guest.get(second);self.assertEqual((r.status_code,r.content),(200,b'\xff\xd8two'))
        self.assertEqual(guest.get(first).status_code,404)
        self.assertEqual(guest.get('/api/avatar/1-0123456789abcdef.png').status_code,404)
        self.assertEqual(self.user.get('/api/auth/me').json()['avatar'],second)
        self.assertEqual([x['avatar'] for x in self.admin.get('/api/auth/accounts').json()['items'] if x['id']==alice_id],[second])
        r=self.user.post('/api/auth/avatar',data={'preset':0}).json();self.assertEqual((r['avatar'],r['avatarPreset']),('',0))
        self.assertEqual(guest.get(second).status_code,404);guest.close()

    def test_stream_limit_encodes_video_and_accounts_for_audio(self):
        """H.264 перекодируется при лимите, вместо обхода ограничения через copy."""
        for bitrate,scale in [(1000,'854'),(2000,'854'),(4000,'1280')]:
            args=portal.player_stream_args('ffmpeg',self.video,{'remux':True},50,0,'auto',bitrate)
            self.assertEqual(args[args.index('-c:v')+1],'libx264')
            self.assertEqual(args[args.index('-maxrate')+1],str(bitrate-128)+'k')
            self.assertIn(scale,args[args.index('-vf')+1])

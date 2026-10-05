"""«Семья»: общее хранилище, автор тайтла, личная библиотека и права."""
import asyncio
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
import ast, hashlib, hmac, secrets, re, os, shutil, subprocess, json, threading, types, sys, ipaddress
from contextvars import ContextVar
from datetime import datetime, timezone
from urllib.parse import quote
from fastapi import FastAPI, Form, Query, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse, Response, FileResponse, StreamingResponse
from starlette.requests import Request

portal=types.ModuleType('family_test_portal');sys.modules[portal.__name__]=portal
portal.__dict__.update(dict(datetime=datetime,timezone=timezone,Path=Path,ipaddress=ipaddress,hashlib=hashlib,hmac=hmac,secrets=secrets,re=re,os=os,shutil=shutil,
    subprocess=subprocess,json=json,threading=threading,ContextVar=ContextVar,quote=quote,time=time,sqlite3=sqlite3,asyncio=asyncio,
    FastAPI=FastAPI,Form=Form,Query=Query,HTTPException=HTTPException,UploadFile=UploadFile,File=File,JSONResponse=JSONResponse,
    Response=Response,FileResponse=FileResponse,StreamingResponse=StreamingResponse,Request=Request,app=FastAPI(),CACHE_DB=Path('unused'),
    find_library_row=lambda kind,item_key='',path='':{},row_media=lambda r,kind=None:{'title':r['title'],'kind':kind}))
tree=ast.parse((Path(__file__).parents[1]/'app.py').read_text(encoding='utf-8'))
names={'account_access','cache_db','favorites','favorite_add','favorite_delete'}
nodes=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and (n.name in names or n.name.startswith(('auth_','family_')))
    or isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and (t.id.startswith(('AUTH_','FAMILY_'))) for t in n.targets)]
exec(compile(ast.Module(body=nodes,type_ignores=[]),'app.py','exec'),portal.__dict__)


@portal.app.post('/api/library/rename')
def rename(path:str=Form(...)):
    """Маршрут правки: та же проверка, что в app.py."""
    portal.family_require_edit('movies','',path)
    return {'ok':True}


@portal.app.post('/api/library/delete')
def delete(path:str=Form(...)):
    """Удаление — только администратор (общий фильтр запросов)."""
    return {'ok':True}


@portal.app.post('/api/torrent-upload')
def upload():
    """Загрузка доступна любому аккаунту."""
    return {'ok':True}


class FamilyTests(unittest.TestCase):
    def setUp(self):
        """Временная база, admin и два обычных аккаунта."""
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        self.connections=[]
        original=sqlite3.connect
        def tracked(*args,**kwargs):
            kwargs['check_same_thread']=False
            con=original(*args,**kwargs);self.connections.append(con);return con
        self.patches=[patch.object(sqlite3,'connect',tracked),patch.object(portal,'CACHE_DB',root/'db.sqlite')]
        for p in self.patches:p.start()
        portal.AUTH_FAILURES.clear()
        self.admin=self.client('admin','admin')
        for login in ('alice','bob'):self.admin.post('/api/auth/accounts',data={'login':login,'password':'test-pass'})
        self.alice=self.client('alice','test-pass');self.bob=self.client('bob','test-pass')
        self.ids={u['login']:u['id'] for u in self.admin.get('/api/auth/accounts').json()['items']}
        self.old='/media/movies/Old (2001)';self.watched='/media/movies/Watched (2002)'
        with portal.cache_db() as con:
            con.execute('insert into account_playback_history values(?,?,?,?,?,?,?,?)',(self.ids['alice'],self.watched+'/w.mkv',self.watched,10,100,0,'s',time.time()));con.commit()

    def tearDown(self):
        """Закрыть клиентов и базу."""
        for c in (self.admin,self.alice,self.bob):c.close()
        for con in self.connections:con.close()
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()

    def client(self,login,password):
        """Клиент с Bearer-токеном, как у приложения."""
        c=TestClient(portal.app);r=c.post('/api/auth/login',data={'login':login,'password':password})
        self.assertEqual(r.status_code,200,r.text);c.headers['Authorization']='Bearer '+r.json()['token'];return c

    def view(self,login,items,scope='mine',owner=''):
        """family_view от имени аккаунта, как его вызывает /api/library-cached."""
        with portal.cache_db() as con:user=dict(con.execute('select * from accounts where login=?',(login,)).fetchone())
        token=portal.AUTH_USER.set(user)
        try:return [x['path'] for x in portal.family_view('movies',[dict(x) for x in items],scope,owner)]
        finally:portal.AUTH_USER.reset(token)

    def as_user(self,login,fn,*args):
        """Вызвать функцию от имени аккаунта (заявки и метки загрузок)."""
        with portal.cache_db() as con:user=dict(con.execute('select * from accounts where login=?',(login,)).fetchone())
        token=portal.AUTH_USER.set(user)
        try:return fn(*args)
        finally:portal.AUTH_USER.reset(token)

    def test_migration_new_titles_and_personal_library(self):
        """Старое — у admin, у alice только начатое; новая загрузка — у автора; «Семья» видит всё."""
        items=[{'path':self.old,'title':'Old'},{'path':self.watched,'title':'Watched'}]
        self.assertEqual(self.view('admin',items),[self.old,self.watched])
        self.assertEqual(self.view('alice',items),[self.watched])
        self.assertEqual(self.view('bob',items),[])
        self.assertEqual(self.view('bob',items,'family'),[self.old,self.watched])
        # Прямая загрузка bob и заявка Sonarr/Radarr alice.
        self.as_user('bob',portal.family_mark_download,'ABC123')
        self.as_user('alice',portal.family_claim,'movies','603','Матрица')
        with portal.cache_db() as con:
            con.execute("insert into download_jobs(hash,kind,status,final_path) values('abc123','movies','done','/media/movies/Bob Film (2024)')");con.commit()
        new=items+[{'path':'/media/movies/Bob Film (2024)','title':'Bob Film'},{'path':'/media/movies/Матрица (1999)','title':'Матрица','externalId':'603'}]
        self.assertEqual(self.view('bob',new),['/media/movies/Bob Film (2024)'])
        self.assertEqual(self.view('alice',new),[self.watched,'/media/movies/Матрица (1999)'])
        self.assertEqual(self.view('admin',new),[self.old,self.watched])
        self.assertEqual(self.view('admin',new,owner='bob'),['/media/movies/Bob Film (2024)'])
        # Добавить чужой тайтл к себе и убрать — у автора он остаётся.
        self.assertEqual(self.bob.post('/api/my-library/add',data={'path':self.old}).json()['inMyLibrary'],True)
        self.assertEqual(self.bob.post('/api/my-library/add',data={'path':'/nope'}).status_code,404)
        self.assertIn(self.old,self.view('bob',new))
        self.bob.post('/api/my-library/remove',data={'path':self.old})
        self.assertNotIn(self.old,self.view('bob',new));self.assertIn(self.old,self.view('admin',new))
        family={x['login']:x for x in self.alice.get('/api/family').json()['items']}
        self.assertEqual((family['bob']['uploaded'],family['alice']['library']),(1,2))

    def test_edit_by_owner_delete_by_admin_and_moves(self):
        """Править — автор или админ, удалять — только админ; переименование переносит библиотеки."""
        items=[{'path':self.old,'title':'Old'}]
        self.view('admin',items)
        self.as_user('bob',portal.family_mark_download,'h1')
        with portal.cache_db() as con:
            con.execute("insert into download_jobs(hash,kind,status,final_path) values('h1','movies','done','/media/movies/Bob (2024)')");con.commit()
        self.view('bob',items+[{'path':'/media/movies/Bob (2024)','title':'Bob'}])
        self.assertEqual(self.bob.post('/api/torrent-upload').status_code,200)
        self.assertEqual(self.bob.post('/api/library/rename',data={'path':self.old}).status_code,403)
        self.assertEqual(self.bob.post('/api/library/rename',data={'path':'/media/movies/Bob (2024)'}).status_code,200)
        self.assertEqual(self.alice.post('/api/library/rename',data={'path':'/media/movies/Bob (2024)'}).status_code,403)
        self.assertEqual(self.admin.post('/api/library/rename',data={'path':'/media/movies/Bob (2024)'}).status_code,200)
        self.assertEqual(self.bob.post('/api/library/delete',data={'path':'/media/movies/Bob (2024)'}).status_code,403)
        self.assertEqual(self.admin.post('/api/library/delete',data={'path':'/media/movies/Bob (2024)'}).status_code,200)
        self.alice.post('/api/my-library/add',data={'path':'/media/movies/Bob (2024)'})
        with portal.cache_db() as con:
            portal.family_move(con,'/media/movies/Bob (2024)','/media/movies/Bob Renamed (2024)');con.commit()
            rows={(r['user_id'],r['path']) for r in con.execute('select * from account_library')}
            owner=con.execute('select user_id from title_owners where path=?',('/media/movies/Bob Renamed (2024)',)).fetchone()['user_id']
        self.assertIn((self.ids['alice'],'/media/movies/Bob Renamed (2024)'),rows)
        self.assertNotIn((self.ids['alice'],'/media/movies/Bob (2024)'),rows)
        self.assertEqual(owner,self.ids['bob'])

    def test_catalog_marks_family_titles_and_download_owners(self):
        """Скачанное в семье помечается автором и «в моей библиотеке»; загрузка знает своего автора."""
        self.view('admin',[{'path':self.old,'title':'Old'}])
        self.as_user('bob',portal.family_mark_download,'ABCDEF')
        cards=[{'inLibrary':True,'path':self.old},{'inLibrary':False,'path':''}]
        marked=self.as_user('bob',portal.family_annotate,cards)
        self.assertEqual((marked[0]['owner'],marked[0]['inMyLibrary']),('admin',False))
        self.assertNotIn('owner',marked[1])
        self.assertEqual(self.as_user('admin',portal.family_annotate,[{'inLibrary':True,'path':self.old}])[0]['inMyLibrary'],True)
        self.assertEqual(portal.family_download_owners()['abcdef'],(self.ids['bob'],'bob'))

    def test_events_presence_and_new_in_family(self):
        """Новый тайтл bob — событие и «Новое в семье» у alice; «смотрит» видно, пока bob это не скрыл."""
        self.view('admin',[{'path':self.old,'title':'Old'}])
        new='/media/movies/Bob Film (2024)'
        self.as_user('bob',portal.family_mark_download,'h2')
        with portal.cache_db() as con:
            con.execute("insert into download_jobs(hash,kind,status,final_path) values('h2','movies','done',?)",(new,))
            con.execute("insert into library_cache(kind,item_key,title,path,has_file) values('movies','bob','Bob Film',?,1)",(new,));con.commit()
        self.view('bob',[{'path':self.old,'title':'Old'},{'path':new,'title':'Bob Film'}])
        feed=self.alice.get('/api/family/feed').json()
        self.assertEqual([(e['title'],e['user']['login'],e['inMyLibrary']) for e in feed['events']],[('Bob Film','bob',False)])
        self.assertEqual(feed['unread'],1)
        self.assertEqual(self.bob.get('/api/family/feed').json()['events'],[])
        self.assertEqual(self.alice.post('/api/family/feed/seen',data={'last_id':feed['lastId']}).status_code,200)
        self.assertEqual(self.alice.get('/api/family/feed').json()['unread'],0)
        self.assertEqual([(x['title'],x['owner']) for x in self.alice.get('/api/family/new').json()],[('Bob Film','bob')])
        self.alice.post('/api/my-library/add',data={'path':new})
        self.assertEqual(self.alice.get('/api/family/new').json(),[])
        # bob смотрит фильм: alice видит это, пока bob не скрыл.
        with portal.cache_db() as con:
            con.execute('insert into account_playback_history values(?,?,?,?,?,?,?,?)',(self.ids['bob'],new+'/film.mkv',new,30,100,0,'s',time.time()));con.commit()
        bob=next(x for x in self.alice.get('/api/family/feed').json()['presence'] if x['login']=='bob')
        self.assertEqual((bob['online'],bob['watching']['title']),(True,'Bob Film'))
        self.assertEqual(self.bob.post('/api/auth/activity',data={'show':0}).json()['showActivity'],False)
        bob=next(x for x in self.alice.get('/api/family/feed').json()['presence'] if x['login']=='bob')
        self.assertIsNone(bob['watching'])

    def test_download_cards_new_episodes_and_own_feed(self):
        """Загрузка — карточка с прогрессом у автора и во «Всей семье»; новые серии — событие; свои события — в mine."""
        self.view('admin',[{'path':self.old,'title':'Old'}])
        self.as_user('bob',portal.family_mark_download,'H3')
        with portal.cache_db() as con:
            con.execute("insert into download_jobs(hash,kind,media_title,season,status) values('h3','movies','Дюна',1,'downloading')")
            con.execute("insert into download_meta(hash,title,year,poster) values('h3','Дюна','2021','https://img/d.jpg')");con.commit()
        torrents=[{'hash':'H3','progress':0.425,'state':'downloading','name':'Dune.2021'},{'hash':'zzz','progress':0.1}]
        bob=self.as_user('bob',portal.family_download_cards,'movies',torrents)
        self.assertEqual([(c['title'],c['downloadProgress'],c['downloadLabel'],c['owner'],c['path']) for c in bob],[('Дюна',42.5,'Скачивается','bob','')])
        self.assertEqual(self.as_user('alice',portal.family_download_cards,'movies',torrents),[])
        self.assertEqual(len(self.as_user('alice',portal.family_download_cards,'movies',torrents,'family')),1)
        self.assertEqual(len(self.as_user('alice',portal.family_download_cards,'movies',torrents,'mine','bob')),1)
        self.assertEqual(self.as_user('bob',portal.family_download_cards,'tv',torrents),[])
        self.assertEqual(portal.family_download_state({'state':'stalledUP','progress':1}),'Переношу в библиотеку')
        # Тайтл уже есть — круг прогресса на его карточке, а не вторая карточка.
        merged=portal.family_with_downloads([{'path':'/media/movies/Дюна (2021)','title':'Дюна','year':'2021'}],bob)
        self.assertEqual((len(merged),merged[0]['downloadProgress'],merged[0]['path']),(1,42.5,'/media/movies/Дюна (2021)'))
        self.assertEqual(len(portal.family_with_downloads([{'path':self.old,'title':'Old'}],bob)),2)
        # Скачались новые серии сериала, который уже есть: событие «новые серии» от bob.
        show='/media/tv/Сериал'
        with portal.cache_db() as con:
            portal.family_job_events(con)
            con.execute("insert into library_cache(kind,item_key,title,path,has_file) values('tv','s','Сериал',?,1)",(show,))
            con.execute('insert into title_owners values(?,?,?)',(show,self.ids['admin'],time.time()-7200))
            con.execute("update download_jobs set status='organized',completed_at='2026-10-05T10:00:00',final_path=? where hash='h3'",(show+'/Season 2',));con.commit()
            portal.family_job_events(con);portal.family_job_events(con)
        feed=self.alice.get('/api/family/feed').json()
        self.assertEqual([(e['type'],e['title'],e['kind'],e['user']['login']) for e in feed['events']],[('updated','Сериал','tv','bob')])
        self.assertEqual(feed['mine'],[])
        self.assertEqual([(e['type'],e['title']) for e in self.bob.get('/api/family/feed').json()['mine']],[('updated','Сериал')])

    def test_watched_marks_are_personal_and_reset_by_new_episodes(self):
        """«Просмотрено» у каждого своё; число досмотренных серий; новые серии снимают отметку тайтла."""
        show='/media/tv/Show'
        with portal.cache_db() as con:
            con.execute('insert into account_watched values(?,?,?)',(self.ids['alice'],show,time.time()))
            for n in (1,2):con.execute('insert into account_playback_history values(?,?,?,?,?,1,?,?)',(self.ids['alice'],f'{show}/e{n}.mkv',show,10,10,'s',time.time()))
            con.commit()
        def marks(login):
            with portal.cache_db() as con:user=dict(con.execute('select * from accounts where login=?',(login,)).fetchone())
            token=portal.AUTH_USER.set(user)
            try:return [(x['played'],x['watchedCount']) for x in portal.family_view('tv',[{'path':show,'title':'Show'}],'family')]
            finally:portal.AUTH_USER.reset(token)
        self.assertEqual(marks('alice'),[(True,2)])
        self.assertEqual(marks('bob'),[(False,0)])
        self.assertEqual(self.as_user('alice',portal.family_mark_watched,[{'projectPath':show},{'title':'без пути'}])[0]['played'],True)
        with portal.cache_db() as con:
            portal.family_move(con,show,show+' (2020)');con.commit()
            self.assertEqual(con.execute('select project from account_watched').fetchone()['project'],show+' (2020)')

    def test_personal_favorites_summary_owner_and_hidden(self):
        """Избранное у каждого своё; сводка семьи и смена автора — у админа; «скрыто от друзей» видно в карточке."""
        self.assertEqual(self.alice.post('/api/favorites',data={'kind':'movies','external_id':'1','title':'Дюна'}).status_code,200)
        key=self.alice.get('/api/favorites').json()[0]['fav_key']
        self.assertEqual(self.bob.get('/api/favorites').json(),[])
        self.bob.post('/api/favorites',data={'kind':'movies','external_id':'1','title':'Дюна'})
        self.assertEqual(self.alice.delete('/api/favorites/'+key,headers={'X-MediaHub-Auth':'1'}).status_code,200)
        self.assertEqual((self.alice.get('/api/favorites').json(),len(self.bob.get('/api/favorites').json())),([],1))
        self.bob.delete('/api/favorites/'+key)
        with portal.cache_db() as con:self.assertIsNone(con.execute('select 1 from favorites where fav_key=?',(key,)).fetchone())
        self.view('admin',[{'path':self.old,'title':'Old'},{'path':self.watched,'title':'Watched'}])
        self.alice.post('/api/my-library/remove',data={'path':self.watched})
        self.admin.post('/api/my-library/remove',data={'path':self.watched})
        summary=self.admin.get('/api/family/summary').json()
        self.assertEqual([o['path'] for o in summary['orphans']],[self.watched])
        self.assertEqual(next(u for u in summary['users'] if u['login']=='admin')['uploaded'],2)
        self.assertEqual(self.alice.get('/api/family/summary').status_code,403)
        self.assertEqual(self.admin.post('/api/family/owner',data={'path':self.watched,'login':'bob'}).status_code,200)
        self.assertIn(self.watched,self.view('bob',[{'path':self.watched,'title':'Watched'}]))
        self.assertEqual(self.bob.post('/api/library/rename',data={'path':self.watched}).status_code,200)
        with portal.cache_db() as con:con.execute('insert into friend_hidden values(?,0)',(self.watched,));con.commit()
        with portal.cache_db() as con:user=dict(con.execute("select * from accounts where login='bob'").fetchone())
        token=portal.AUTH_USER.set(user)
        try:item=portal.family_view('movies',[{'path':self.watched,'title':'Watched'}])[0]
        finally:portal.AUTH_USER.reset(token)
        self.assertTrue(item['hiddenFromFriends'])


if __name__=='__main__':
    unittest.main()

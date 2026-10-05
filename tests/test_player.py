"""Изолированные проверки плеера: Range, границы медиатеки и история."""
import ast
import hashlib
import threading
from urllib.parse import quote
import asyncio
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
from fastapi import FastAPI, Form, Query, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.testclient import TestClient


class PlayerTests(unittest.TestCase):
    def setUp(self):
        """Загрузить реальные обработчики в временную медиатеку."""
        self.temp=tempfile.TemporaryDirectory();root=Path(self.temp.name);self.media=root/'media';self.project=self.media/'tv'/'Проект';self.project.mkdir(parents=True)
        self.video=self.project/'Серия 01.mp4';self.video.write_bytes(bytes(range(256))*4)
        self.db=root/'history.db';self.connections=[]
        def db():
            con=sqlite3.connect(self.db,check_same_thread=False);con.row_factory=sqlite3.Row
            con.execute('create table if not exists account_playback_history(user_id integer,path text,project text,position real,duration real,completed integer,signature text,updated_at real,primary key(user_id,path))')
            con.execute('create table if not exists account_watched(user_id integer,project text,created_at real,primary key(user_id,project))')
            con.execute('create table if not exists library_cache(path text,title text,poster text,kind text,has_file integer)');self.connections.append(con);return con
        self.scope=dict(Path=Path,re=re,time=time,asyncio=asyncio,shutil=shutil,subprocess=subprocess,json=json,os=os,hashlib=hashlib,quote=quote,
            HTTPException=HTTPException,Form=Form,Query=Query,FileResponse=FileResponse,Response=Response,StreamingResponse=StreamingResponse,
            app=FastAPI(),MEDIA_ROOT=self.media,MOVIES_ROOT=self.media/'movies',TV_ROOT=self.media/'tv',ANIME_ROOT=self.media/'anime',
            VIDEO_EXTS={'.mp4','.mkv','.webm'},PLAYER_PROBE_CACHE={},PLAYER_TRANSCODES=set(),cache_db=db,auth_user_id=lambda:1,
            PLAYER_THUMB_ROOT=root/'previews',PLAYER_THUMB_LOCK=threading.Lock())
        tree=ast.parse((Path(__file__).resolve().parents[1]/'app.py').read_text(encoding='utf-8'))
        nodes=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and (n.name.startswith('player_') or n.name in {'safe_media_path','_project_root_for','_check_project_file'})]
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'app.py','exec'),self.scope)
        self.client=TestClient(self.scope['app'])

    def tearDown(self):
        """Удалить только временные файлы проверки."""
        self.client.close()
        for con in self.connections:con.close()
        self.temp.cleanup()

    def test_range_and_head(self):
        url='/api/player/file';params={'path':str(self.video)}
        r=self.client.get(url,params=params,headers={'Range':'bytes=100-199'})
        self.assertEqual(r.status_code,206);self.assertEqual(r.content,self.video.read_bytes()[100:200]);self.assertEqual(r.headers['content-range'],'bytes 100-199/1024')
        self.assertEqual(self.client.head(url,params=params).content,b'')
        self.assertEqual(self.client.get(url,params=params,headers={'Range':'bytes=2000-'}).status_code,416)

    def test_thumbnail_cache_invalidation_and_frame_bounds(self):
        """Повторный запрос берёт кэш, замена видео создаёт новый кадр, неверные кадры отвергаются."""
        calls=[]
        def generate(args,**kwargs):
            calls.append(args);Path(args[-1]).write_bytes(b'\xff\xd8frame\xff\xd9')
        self.scope['player_binary']=lambda name:'ffmpeg'
        self.scope['player_probe']=lambda f:{'duration':100}
        with patch.object(subprocess,'run',side_effect=generate):
            params={'path':str(self.video),'frame':0}
            first=self.client.get('/api/player/thumbnail',params=params)
            self.assertEqual(first.status_code,200);self.assertEqual(first.headers['content-type'],'image/jpeg')
            self.assertEqual(self.client.get('/api/player/thumbnail',params=params).content,first.content)
            self.assertEqual(len(calls),1);self.assertEqual(calls[0][calls[0].index('-ss')+1],'12.0')
            self.video.write_bytes(b'changed video')
            self.assertEqual(self.client.get('/api/player/thumbnail',params=params).status_code,200);self.assertEqual(len(calls),2)
            params['frame']=2
            self.assertEqual(self.client.get('/api/player/thumbnail',params=params).status_code,200)
            self.assertEqual(calls[-1][calls[-1].index('-ss')+1],'65.0')
            params['frame']=3
            self.assertEqual(self.client.get('/api/player/thumbnail',params=params).status_code,422)
            self.assertFalse(list(self.scope['PLAYER_THUMB_ROOT'].glob('*.tmp.jpg')))

    def test_reject_outside_and_nonvideo(self):
        outside=Path(self.temp.name)/'secret.mp4';outside.write_bytes(b'private')
        note=self.project/'note.txt';note.write_text('private')
        for f in (outside,note):self.assertEqual(self.client.get('/api/player/file',params={'path':str(f)}).status_code,400)

    def test_subtitles_utf8_and_cp1251(self):
        f=self.project/'Серия 01.rus.srt';text='1\r\n00:00:01,200 --> 00:00:03,000\r\nПривет\r\n'
        for encoding in ('utf-8-sig','cp1251'):
            f.write_bytes(text.encode(encoding));r=self.client.get('/api/player/subtitle',params={'path':str(f)})
            self.assertEqual(r.status_code,200);self.assertTrue(r.text.startswith('WEBVTT\n'));self.assertIn('00:00:01.200',r.text);self.assertIn('Привет',r.text)

    def test_credits_threshold_and_short_video(self):
        """Титры длинной серии учитываются, короткий ролик не завершён сразу."""
        for duration,position,expected in ((1440,1259,False),(1440,1260,True),(100,0,False),(100,84,False),(100,85,True)):
            with self.subTest(duration=duration,position=position):
                result=self.client.post('/api/player/progress',data={'path':str(self.video),'position':position,'duration':duration})
                self.assertEqual(result.status_code,200);self.assertEqual(result.json()['completed'],expected)
        result=self.client.post('/api/player/progress',data={'path':str(self.video),'position':10,'duration':100,'ended':1})
        self.assertTrue(result.json()['completed'])

    def test_existing_credits_history_selects_next_episode(self):
        """Старая позиция в титрах отмечается просмотренной и предлагает следующую серию."""
        following=self.project/'Серия 02.mp4';following.write_bytes(b'next episode')
        with self.scope['cache_db']() as con:
            con.execute('insert into account_playback_history values(?,?,?,?,?,?,?,?)',(1,str(self.video),str(self.project),1260,1440,0,self.scope['player_signature'](self.video),time.time()));con.commit()
        self.scope['library_files']=AsyncMock(return_value={'items':[{'path':str(f),'rel':f.name,'video':True} for f in (self.video,following)]})
        self.scope['player_probe']=lambda f:{'probeAvailable':False}
        self.assertEqual(self.client.get('/api/player/resume').json()['items'],[])
        project=self.client.get('/api/player/project',params={'path':str(self.project)}).json()
        self.assertEqual(project['watched'],1);self.assertEqual(project['resume']['path'],str(following))
        session=self.client.get('/api/player/session',params={'path':str(self.project)}).json()
        self.assertEqual(session['file'],str(following))
        explicit=self.client.get('/api/player/session',params={'path':str(self.project),'file':str(self.video)}).json()
        self.assertEqual(explicit['file'],str(self.video));self.assertEqual(explicit['position'],0)

    def test_progress_resume_completion_and_replaced_file(self):
        data={'path':str(self.video),'position':35,'duration':100}
        self.assertEqual(self.client.post('/api/player/progress',data=data).status_code,200)
        self.assertEqual(self.scope['player_saved'](self.video)['position'],35)
        self.assertEqual(len(self.scope['player_resume_items']()),1)
        self.client.post('/api/player/progress',data={**data,'position':99})
        self.assertEqual(self.scope['player_resume_items'](),[])
        self.video.write_bytes(b'replaced');self.assertEqual(self.scope['player_saved'](self.video),{})

    def test_progress_reject_nan_and_invalid_duration(self):
        for position,duration in [('nan',100),('inf',100),(-1,100),(1,0),(1,'inf')]:
            r=self.client.post('/api/player/progress',data={'path':str(self.video),'position':position,'duration':duration});self.assertEqual(r.status_code,400)

    def test_probe_avoids_unsupported_codec_and_caches(self):
        data={'format':{'duration':'125'},'streams':[{'codec_type':'video','codec_name':'hevc','pix_fmt':'yuv420p10le'},{'codec_type':'audio','codec_name':'flac','tags':{'language':'rus'}}]}
        with patch.dict(self.scope,player_binary=lambda name:'ffprobe'),patch.object(subprocess,'run',return_value=subprocess.CompletedProcess([],0,json.dumps(data).encode())) as run:
            result=self.scope['player_probe'](self.video);self.assertFalse(result['direct']);self.assertEqual(result['audio'][0]['label'],'rus');self.assertEqual(result['duration'],125)
            self.scope['player_probe'](self.video);self.assertEqual(run.call_count,1)

    def test_session_natural_order_selection_and_no_other_project(self):
        second=self.project/'Серия 10.mp4';second.write_bytes(b'video')
        async def listing(path):
            return {'items':[{'path':str(f),'rel':f.name,'video':True} for f in [second,self.video]]}
        with patch.dict(self.scope,library_files=listing,player_binary=lambda name:'',player_probe=lambda f:{'duration':100,'audio':[],'direct':True,'probeAvailable':False}):
            r=self.client.get('/api/player/session',params={'path':str(self.project),'file':str(second)})
            self.assertEqual(r.status_code,200);self.assertEqual(r.json()['items'][0]['path'],str(self.video))
            other=self.media/'tv'/'Other';other.mkdir();f=other/'Video.mp4';f.write_bytes(b'other')
            self.assertEqual(self.client.get('/api/player/session',params={'path':str(self.project),'file':str(f)}).status_code,400)

    def test_stream_missing_encoder_and_busy(self):
        probe={'probeAvailable':True,'audio':[],'duration':100}
        with patch.dict(self.scope,player_probe=lambda f:probe,player_binary=lambda name:''):
            self.assertEqual(self.client.get('/api/player/stream',params={'path':str(self.video)}).status_code,503)
        with patch.dict(self.scope,player_probe=lambda f:probe,player_binary=lambda name:'ffmpeg',PLAYER_TRANSCODES={1,2}):
            self.assertEqual(self.client.get('/api/player/stream',params={'path':str(self.video)}).status_code,429)
            self.assertEqual(self.client.get('/api/player/stream',params={'path':str(self.video),'start':101}).status_code,400)

    def test_cancelled_stream_releases_slot(self):
        """Отмена очистки не должна навсегда занимать место кодировщика."""
        async def run():
            proc=AsyncMock();proc.stdout.read.side_effect=[b'header',b'fragment']
            probe={'probeAvailable':True,'audio':[],'duration':100}
            with patch.dict(self.scope,player_probe=lambda f:probe,player_binary=lambda name:'ffmpeg',player_stop_process=AsyncMock(side_effect=asyncio.CancelledError)),patch.object(asyncio,'create_subprocess_exec',return_value=proc):
                response=await self.scope['player_stream'](str(self.video),0,0,'auto',bitrate=0,timeline='accurate')
                await anext(response.body_iterator)
                with self.assertRaises(asyncio.CancelledError):await response.body_iterator.aclose()
                self.assertEqual(self.scope['PLAYER_TRANSCODES'],set())
        asyncio.run(run())

    def test_fast_stream_copies_h264_and_scales_hevc(self):
        """H.264 сохраняет качество, HEVC имеет ограниченный быстрый режим."""
        build=self.scope['player_stream_args']
        args=build('ffmpeg',self.video,{'remux':True},35,1,'auto')
        self.assertEqual(args[args.index('-c:v')+1],'copy');self.assertNotIn('-vf',args)
        args=build('ffmpeg',self.video,{'remux':False,'defaultQuality':'480'},35,1,'auto')
        self.assertEqual(args[args.index('-preset')+1],'ultrafast');self.assertIn('480',args[args.index('-vf')+1]);self.assertNotIn('-re',args)
        args=build('ffmpeg',self.video,{'remux':True},35,1,'720')
        self.assertEqual(args[args.index('-c:v')+1],'libx264')

    def test_keyframe_seek_anchor_and_audio_clock(self):
        """Общая точка seek исключает разный старт звука и копируемого видео."""
        with patch.dict(self.scope,player_binary=lambda _: 'ffprobe'):
            data={'frames':[{'best_effort_timestamp_time':'10.01'},{'best_effort_timestamp_time':'20.02'}]}
            with patch.object(subprocess,'run',return_value=type('Result',(),{'stdout':json.dumps(data)})()):
                self.assertEqual(self.scope['player_seek_anchor'](self.video,13.25),10.01)
            with patch.object(subprocess,'run',side_effect=subprocess.TimeoutExpired('ffprobe',5)):
                self.assertIsNone(self.scope['player_seek_anchor'](self.video,13.25))
        args=self.scope['player_stream_args']('ffmpeg',self.video,{'remux':True},10.01,0,'auto')
        self.assertIn('-noaccurate_seek',args);self.assertLess(args.index('-noaccurate_seek'),args.index('-i'))
        args=self.scope['player_stream_args']('ffmpeg',self.video,{'remux':False},13.25,0,'720')
        self.assertNotIn('-r',args);self.assertEqual(args[args.index('-fps_mode')+1],'passthrough')
        self.assertEqual(args[args.index('-af')+1],'aresample=async=1:first_pts=0')

    def test_project_progress_and_next_after_completed(self):
        """Экран серий показывает историю и следующую после законченной серии."""
        second=self.project/'Серия 02.mp4';second.write_bytes(b'video')
        async def listing(path):
            return {'items':[{'path':str(f),'rel':f.name,'name':f.name,'video':True} for f in [self.video,second]]}
        self.client.post('/api/player/progress',data={'path':str(self.video),'position':99,'duration':100})
        with patch.dict(self.scope,library_files=listing):
            r=self.client.get('/api/player/project',params={'path':str(self.project)});data=r.json()
            self.assertEqual(r.status_code,200);self.assertEqual(data['watched'],1);self.assertEqual(data['count'],2);self.assertEqual(data['resume']['path'],str(second));self.assertTrue(data['items'][0]['completed'])


if __name__=='__main__':unittest.main()

"""Пульт: телефон ставит команду, телевизор получает её долгим опросом."""
import ast
import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path
from fastapi import FastAPI, Form, Query, HTTPException
from fastapi.testclient import TestClient


class RemoteTests(unittest.TestCase):
    def setUp(self):
        """Загрузить обработчики пульта и проверки путей во временную медиатеку."""
        self.temp=tempfile.TemporaryDirectory();self.media=Path(self.temp.name)/'media';self.project=self.media/'tv'/'Проект';self.project.mkdir(parents=True)
        self.video=self.project/'Серия 01.mp4';self.video.write_bytes(b'0'*64)
        (self.media/'tv'/'Другой').mkdir();self.other=self.media/'tv'/'Другой'/'Серия 01.mp4';self.other.write_bytes(b'0'*64)
        self.scope=dict(Path=Path,time=time,asyncio=asyncio,HTTPException=HTTPException,Form=Form,Query=Query,app=FastAPI(),auth_user_id=lambda:1,
            MEDIA_ROOT=self.media,MOVIES_ROOT=self.media/'movies',TV_ROOT=self.media/'tv',ANIME_ROOT=self.media/'anime',VIDEO_EXTS={'.mp4','.mkv'})
        tree=ast.parse((Path(__file__).resolve().parents[1]/'app.py').read_text(encoding='utf-8'))
        names={'safe_media_path','is_media_root','_project_root_for','_check_project_file','player_file'}
        nodes=[n for n in tree.body if (isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and (n.name.startswith('remote_') or n.name in names))
            or (isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id.startswith('REMOTE_') for t in n.targets))]
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'app.py','exec'),self.scope)
        # Один цикл событий на все запросы — как у uvicorn; иначе asyncio.Event не будит соседний запрос.
        self.client=TestClient(self.scope['app']).__enter__()

    def tearDown(self):
        """Удалить только временные файлы проверки."""
        self.client.__exit__(None,None,None);self.temp.cleanup()

    def poll(self,ack=0,wait=0):
        return self.client.get('/api/remote/poll',params={'device':'tv-livingroom','name':'Гостиная','ack':ack,'wait':wait}).json()['commands']

    def test_unknown_device_is_offline(self):
        r=self.client.post('/api/remote/send',data={'device':'tv-nowhere','action':'toggle'})
        self.assertEqual(r.status_code,404);self.assertEqual(self.client.get('/api/remote/devices').json()['devices'],[])

    def test_play_command_is_delivered_until_acknowledged(self):
        """Команда остаётся в очереди до подтверждения: обрыв опроса её не теряет."""
        self.assertEqual(self.poll(),[])
        devices=self.client.get('/api/remote/devices').json()['devices']
        self.assertEqual([d['name'] for d in devices],['Гостиная'])
        r=self.client.post('/api/remote/send',data={'device':'tv-livingroom','action':'play','path':str(self.project),'file':str(self.video),'title':'Проект','restart':1})
        self.assertEqual(r.status_code,200,r.text)
        first=self.poll();self.assertEqual(len(first),1);self.assertEqual(first[0]['action'],'play');self.assertTrue(first[0]['restart'])
        self.assertEqual(first[0]['file'],str(self.video.resolve()));self.assertNotIn('at',first[0])
        self.assertEqual(self.poll(),first)
        self.assertEqual(self.poll(ack=first[0]['id']),[])

    def test_new_play_replaces_pending_play(self):
        self.poll()
        for _ in range(2):self.client.post('/api/remote/send',data={'device':'tv-livingroom','action':'play','path':str(self.project)})
        self.client.post('/api/remote/send',data={'device':'tv-livingroom','action':'toggle'})
        self.assertEqual([c['action'] for c in self.poll()],['play','toggle'])

    def test_rejects_foreign_paths_and_actions(self):
        self.poll()
        send=lambda **data:self.client.post('/api/remote/send',data={'device':'tv-livingroom',**data}).status_code
        self.assertEqual(send(action='play',path=str(self.media/'tv')),400)
        self.assertEqual(send(action='play',path=self.temp.name),400)
        self.assertEqual(send(action='play',path=str(self.project),file=str(self.other)),400)
        self.assertEqual(send(action='reboot'),400)
        self.assertEqual(self.client.get('/api/remote/poll',params={'device':'../x'}).status_code,422)

    def test_long_poll_wakes_on_command(self):
        """Ожидающий опрос просыпается сразу, а не по таймауту."""
        self.poll();result={}
        def wait():result['commands']=self.poll(wait=10);result['at']=time.time()
        worker=threading.Thread(target=wait);worker.start();time.sleep(0.3)
        sent=time.time();self.client.post('/api/remote/send',data={'device':'tv-livingroom','action':'seek','seconds':120})
        worker.join(5)
        self.assertEqual(result['commands'][0]['seconds'],120.0);self.assertLess(result['at']-sent,2)

    def test_state_is_shown_to_phone(self):
        self.poll()
        self.client.post('/api/remote/state',data={'device':'tv-livingroom','title':'Проект','subtitle':'S1 · E1','playing':1,'position':30,'duration':1400})
        state=self.client.get('/api/remote/devices').json()['devices'][0]['state']
        self.assertTrue(state['playing']);self.assertEqual(state['position'],30.0)
        self.client.post('/api/remote/state',data={'device':'tv-livingroom','active':0})
        self.assertEqual(self.client.get('/api/remote/devices').json()['devices'][0]['state'],{})
        # Приложение на ТВ свернули — телефон больше не предлагает этот ТВ до следующего опроса.
        self.client.post('/api/remote/state',data={'device':'tv-livingroom','active':0,'away':1})
        self.assertEqual(self.client.get('/api/remote/devices').json()['devices'],[])
        self.poll();self.assertEqual(len(self.client.get('/api/remote/devices').json()['devices']),1)

    def test_transfer_keeps_position_and_devices_are_independent(self):
        """Перенос несёт секунду файла; команда уходит только выбранному устройству."""
        self.poll();self.client.get('/api/remote/poll',params={'device':'phone-anna','name':'Телефон','kind':'phone','wait':0})
        self.assertEqual(self.client.get('/api/remote/poll',params={'device':'x-device','kind':'fridge'}).status_code,422)
        kinds={d['id']:d['kind'] for d in self.client.get('/api/remote/devices').json()['devices']}
        self.assertEqual(kinds,{'tv-livingroom':'tv','phone-anna':'phone'})
        self.client.post('/api/remote/send',data={'device':'tv-livingroom','action':'play','path':str(self.project),'file':str(self.video),'start':754.5})
        self.client.post('/api/remote/send',data={'device':'tv-livingroom','action':'play','path':str(self.project),'start':10})
        self.assertNotIn('start',self.poll()[0])
        self.client.post('/api/remote/send',data={'device':'tv-livingroom','action':'play','path':str(self.project),'file':str(self.video),'start':754.5})
        self.assertEqual([c['start'] for c in self.poll() if c['action']=='play'],[754.5])
        self.assertEqual(self.client.get('/api/remote/poll',params={'device':'phone-anna','kind':'phone','wait':0}).json()['commands'],[])
        self.client.post('/api/remote/state',data={'device':'phone-anna','title':'Проект','file':str(self.video),'section':'anime','playing':1,'position':5,'duration':10})
        self.assertEqual({d['id']:d['state'].get('section') for d in self.client.get('/api/remote/devices').json()['devices']},{'tv-livingroom':None,'phone-anna':'anime'})


if __name__=='__main__':
    unittest.main()

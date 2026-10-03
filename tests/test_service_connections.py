"""Автоподключение сервисов: повторный запуск и сохранение схем API."""
import ast,io,json,tempfile,types,unittest,urllib.request,urllib.error,urllib.parse
from pathlib import Path
from unittest.mock import Mock

class ServiceConnectionTests(unittest.TestCase):
    def test_connect_is_idempotent_and_preserves_schema_defaults(self):
        """Повторное подключение не дублирует корни, клиентов и приложения."""
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);env=root/'mediahub.env';env.write_text('MEDIA_ROOT=/mnt/media\nTMDB_API_KEY=private-test-value\n')
            records={};calls=[]
            def opener(request,timeout):
                route=request.full_url.split('/api/',1)[1].split('/',1)[1];port=request.full_url.split(':')[2].split('/')[0];key=(port,route)
                calls.append((key,request.data))
                if route=='system/status':return io.BytesIO(b'{}')
                if port=='8080':return io.BytesIO(b'')
                if route.endswith('/schema'):
                    implementations=['QBittorrent'] if route.startswith('downloadclient') else ['Radarr','Sonarr']
                    return io.BytesIO(json.dumps([{'id':999,'implementation':name,'fields':[{'name':field,'value':value} for field,value in [('host','unset'),('port',0),('baseUrl',''),('prowlarrUrl',''),('apiKey',''),('futureField','preserved')]]} for name in implementations]).encode())
                if request.data is not None:
                    data=json.loads(request.data);records.setdefault(key,[]).append(data);return io.BytesIO(json.dumps(data).encode())
                return io.BytesIO(json.dumps(records.get(key,[])).encode())
            net=types.SimpleNamespace(request=types.SimpleNamespace(Request=urllib.request.Request,urlopen=opener),parse=urllib.parse,error=urllib.error)
            log=Mock();scope=dict(Path=Path,json=json,time=types.SimpleNamespace(sleep=lambda _:None),ET=types.SimpleNamespace(parse=lambda _:types.SimpleNamespace(getroot=lambda:types.SimpleNamespace(findtext=lambda _: 'test-api-key'))),urllib=net,media_root=lambda:root/'media',ENV_FILE=env,log=log)
            tree=ast.parse((Path(__file__).parents[1]/'system_setup.py').read_text(encoding='utf-8'))
            node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='connect_services')
            exec(compile(ast.Module(body=[node],type_ignores=[]),'system_setup.py','exec'),scope)
            scope['connect_services']();first={key:len(value) for key,value in records.items()}
            scope['connect_services']();self.assertEqual(first,{key:len(value) for key,value in records.items()})
            self.assertEqual(sum(len(v) for k,v in records.items() if k[1]=='rootfolder'),3)
            for key,values in records.items():
                if key[1] not in {'downloadclient','applications'}:continue
                for value in values:
                    self.assertNotIn('id',value)
                    self.assertEqual(next(f['value'] for f in value['fields'] if f['name']=='futureField'),'preserved')
            self.assertIn('TMDB_API_KEY=private-test-value',env.read_text())
            self.assertNotIn('test-api-key',str(log.call_args_list))

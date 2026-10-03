"""Установка APK на ТВ из настроек без настоящего adb и телевизора."""
import ast,asyncio,ipaddress,os,re,shutil,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch,AsyncMock,MagicMock
from fastapi import FastAPI,Form,HTTPException
from starlette.requests import Request

class TvInstallTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        """Загрузить функции установки на ТВ в изолированное окружение."""
        self.temp=tempfile.TemporaryDirectory();root=Path(self.temp.name);(root/'static').mkdir()
        self.scope=dict(Path=Path,re=re,os=os,time=time,shutil=shutil,asyncio=asyncio,ipaddress=ipaddress,Request=Request,Form=Form,HTTPException=HTTPException,app=FastAPI(),BASE_DIR=root)
        tree=ast.parse((Path(__file__).parents[1]/'app.py').read_text(encoding='utf-8'))
        names={'tv_install_target','tv_step','tv_install_job'}
        nodes=[n for n in tree.body if (isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in names) or (isinstance(n,ast.Assign) and any(getattr(t,'id','') in {'TV_INSTALL','TV_INSTALL_ERRORS'} for t in n.targets))]
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'app.py','exec'),self.scope)
        self.scope.update(TV_ADB_HOME=root/'adb',apps_info=lambda:{'available':True,'version':'0.7.1','filename':'MediaHub-0.7.1.apk'})

    def tearDown(self):
        self.temp.cleanup()

    def test_target_validation(self):
        target=self.scope['tv_install_target']
        self.assertEqual(target('192.168.1.50'),'192.168.1.50:5555')
        self.assertEqual(target(' 10.0.0.5:41234 '),'10.0.0.5:41234')
        for bad in ('8.8.8.8','127.0.0.1','169.254.1.2','tv.local','192.168.1.5;reboot','192.168.1.300','192.168.1.5:70000',''):
            with self.assertRaises(ValueError,msg=bad):target(bad)
        with self.assertRaises(ValueError):target('192.168.1.50',None)

    async def run_job(self,install_output):
        """Прогнать установку с поддельным adb: первое get-state ждёт разрешения на ТВ."""
        calls=[];states=iter(['unauthorized','device'])
        async def fake_run(*command,timeout=30,env=None):
            calls.append(command[1:])
            if command[1]=='connect':return 0,'connected to 192.168.1.50:5555'
            if 'get-state' in command:return 0,next(states)
            if 'install' in command:return 0,install_output
            return 0,''
        self.scope['tv_run']=fake_run
        process=MagicMock();process.wait=AsyncMock(return_value=0)
        with patch.object(shutil,'which',return_value='/usr/bin/adb'),patch.object(asyncio,'create_subprocess_exec',AsyncMock(return_value=process)),patch.object(asyncio,'sleep',AsyncMock()):
            await self.scope['tv_install_job']('192.168.1.50:5555','','')
        return calls

    async def test_install_waits_for_permission_and_launches(self):
        calls=await self.run_job('Performing Streamed Install\nSuccess')
        state=self.scope['TV_INSTALL']
        self.assertEqual(state['status'],'done')
        self.assertTrue(any('Разрешить отладку' in line for line in state['log']))
        install=next(c for c in calls if 'install' in c)
        self.assertEqual(Path(install[-1]).name,'MediaHub-0.7.1.apk')
        self.assertTrue(any('monkey' in c for c in calls));self.assertIn(('kill-server',),calls)

    async def test_signature_conflict_is_explained(self):
        await self.run_job('Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE: signatures do not match]')
        state=self.scope['TV_INSTALL']
        self.assertEqual(state['status'],'error');self.assertIn('другой подписью',state['message'])

if __name__=='__main__':
    unittest.main()

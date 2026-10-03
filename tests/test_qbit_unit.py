"""Поиск службы qBittorrent: портал видит и ручную установку, а не только созданную MediaHUB."""
import ast,time,types,unittest
from pathlib import Path
from typing import Dict

class QbitUnitTests(unittest.TestCase):
    def scope(self,active=(),existing=(),instances=''):
        """Загрузить поиск службы с поддельным systemctl."""
        calls=[]
        def run(command,**kwargs):calls.append(command);return types.SimpleNamespace(stdout=instances,returncode=0)
        scope=dict(Dict=Dict,time=time,subprocess=types.SimpleNamespace(run=run,PIPE=-1,DEVNULL=-3),systemctl_active=lambda unit:unit in active,unit_exists=lambda unit:unit in existing)
        tree=ast.parse((Path(__file__).parents[1]/'system_setup.py').read_text(encoding='utf-8'))
        names={'qbit_unit','_detect_qbit_unit'}
        nodes=[n for n in tree.body if (isinstance(n,ast.FunctionDef) and n.name in names) or (isinstance(n,(ast.Assign,ast.AnnAssign)) and getattr(getattr(n,'target',None) or n.targets[0],'id','') in {'QBIT_UNITS','QBIT_UNIT_CACHE'})]
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'system_setup.py','exec'),scope)
        return scope,calls

    def test_manual_service_is_used_when_running(self):
        scope,_=self.scope(active={'qbittorrent.service'},existing={'qbittorrent.service'})
        self.assertEqual(scope['qbit_unit'](),'qbittorrent.service')

    def test_user_instance_is_found(self):
        scope,_=self.scope(active={'qbittorrent-nox@media.service'},instances='qbittorrent-nox@media.service loaded active running qBittorrent\n')
        self.assertEqual(scope['qbit_unit'](),'qbittorrent-nox@media.service')

    def test_stopped_existing_service_beats_default(self):
        scope,_=self.scope(existing={'qbittorrent.service'})
        self.assertEqual(scope['qbit_unit'](),'qbittorrent.service')

    def test_default_for_new_install_and_cache(self):
        scope,calls=self.scope()
        self.assertEqual(scope['qbit_unit'](),'qbittorrent-nox.service');scope['qbit_unit']()
        self.assertEqual(len(calls),1)

if __name__=='__main__':
    unittest.main()

"""Кэш поставщиков должен выдавать релизы с действующим токеном."""
import ast
from pathlib import Path
from contextlib import contextmanager
import unittest
from unittest.mock import Mock


class ProviderFeedTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_release_has_download_payload(self):
        tree=ast.parse((Path(__file__).resolve().parents[1]/'app.py').read_text(encoding='utf-8'))
        node=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='feed')
        node.decorator_list=[]
        row={'title':'Anime 01','guid':'https://site.test/topic/1','indexer_id':5,'indexer':'AnimeLayer',
             'download_url':'magnet:?xt=urn:btih:abc','size':123,'seeders':8,'peers':2,'published_at':'2026-10-03'}
        con=Mock();con.execute.return_value.fetchall.return_value=[row]
        @contextmanager
        def database():
            yield con
        store=Mock(return_value='download-token')
        scope={'Query':lambda value,**kw:value,'cache_db':database,'store_release':store}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'app.py','exec'),scope)
        items=await scope['feed'](kind='anime',source='providers')
        self.assertEqual(items[0]['token'],'download-token')
        kind,payload=store.call_args.args
        self.assertEqual(kind,'anime')
        self.assertEqual(payload['indexerId'],5)
        self.assertEqual(payload['magnetUrl'],row['download_url'])
        self.assertEqual(payload['protocol'],'torrent')

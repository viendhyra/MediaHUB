"""Запасное скачивание после ошибки индексера без настоящих загрузок."""
import ast
from pathlib import Path
import unittest
from unittest.mock import AsyncMock
import httpx


class GrabFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def check_fallback(self, status, body):
        """Ошибка прямого адреса должна приводить к чтению страницы релиза."""
        tree=ast.parse((Path(__file__).resolve().parents[1]/'app.py').read_text(encoding='utf-8'))
        node=next(x for x in tree.body if isinstance(x,ast.AsyncFunctionDef) and x.name=='_grab_release_direct')
        from urllib.parse import urljoin
        page='https://site.test/content.php?id=74645'
        magnet='magnet:?xt=urn:btih:0123456789012345678901234567890123456789'
        pages=AsyncMock(return_value=([{'type':'torrent','url':'https://site.test/file.torrent'}],{}))
        fetch=AsyncMock(return_value=(b'',magnet,''))
        qbit=AsyncMock()
        qbit.post.return_value=httpx.Response(200,text='Ok.')
        transport=httpx.MockTransport(lambda request:httpx.Response(status,content=body,request=request))
        client=httpx.AsyncClient
        scope={'httpx':type('Http',(),{'AsyncClient':staticmethod(lambda **kw:client(transport=transport,**kw))}),
               'torrent_links_from_page':pages,'fetch_torrent_bytes':fetch,'qbit_login':AsyncMock(return_value=qbit),
               'QBIT_URL':'http://qbit.test','urljoin_safe':urljoin,'page_fetch_error_text':str}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'app.py','exec'),scope)
        result=await scope['_grab_release_direct']({'downloadUrl':'https://indexer.test/download','infoUrl':page},'movies')
        self.assertTrue(result[0])
        pages.assert_awaited_once_with(page,return_meta=True)
        fetch.assert_awaited_once_with('https://site.test/file.torrent',referer=page)
        self.assertEqual(qbit.post.call_args.kwargs['data']['urls'],magnet)
        qbit.post.assert_awaited_once()

    async def test_http_500_uses_release_page(self):
        await self.check_fallback(500,b'indexer failed')

    async def test_html_uses_release_page(self):
        await self.check_fallback(200,b'<!doctype html><html>login</html>')

    async def test_lowercase_doctype_is_not_torrent(self):
        await self.check_fallback(200,b'doctype html')


class ExpiredCacheTests(unittest.IsolatedAsyncioTestCase):
    def load(self, names, scope):
        """Берёт нужные функции из app.py без запуска всего сервера."""
        tree=ast.parse((Path(__file__).resolve().parents[1]/'app.py').read_text(encoding='utf-8'))
        nodes=[x for x in tree.body if isinstance(x,(ast.FunctionDef,ast.AsyncFunctionDef)) and x.name in names]
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'app.py','exec'),scope)
        return scope

    def test_cache_error_is_recognised(self):
        import re
        s=self.load({'_release_cache_expired','_release_queries'},{'re':re})
        self.assertTrue(s['_release_cache_expired']('HTTP 404 {"message":"Couldn\'t find requested release in cache, try searching again"}'))
        self.assertFalse(s['_release_cache_expired']('HTTP 500 Download selectors didn\'t match'))
        queries=s['_release_queries']('Курьер / Runner (2026) WEB-DL 1080p [мультираздача]')
        self.assertIn('Курьер 2026',queries)
        self.assertIn('Runner 2026',queries)

    async def test_research_finds_same_release(self):
        import re
        seen=[]
        def answer(request):
            seen.append(dict(request.url.params))
            rows=[{'guid':'other','title':'Курьер (2019)','indexerId':7},
                  {'guid':'g-1','title':'Курьер / Runner (2026) WEB-DL 1080p','indexerId':7,'downloadUrl':'fresh'}]
            return httpx.Response(200,json=rows if request.url.params.get('query')=='Курьер 2026' else [])
        transport=httpx.MockTransport(answer)
        client=httpx.AsyncClient
        scope={'re':re,'PROWLARR_KEY':'k','PROWLARR_URL':'http://prowlarr.test',
               'httpx':type('Http',(),{'AsyncClient':staticmethod(lambda **kw:client(transport=transport,**kw)),'Timeout':httpx.Timeout})}
        s=self.load({'_release_queries','_refresh_release_payload'},scope)
        fresh=await s['_refresh_release_payload']({'guid':'g-1','indexerId':7,'title':'Курьер / Runner (2026) WEB-DL 1080p [мультираздача]'})
        self.assertEqual(fresh['downloadUrl'],'fresh')
        self.assertTrue(all(x['indexerIds']=='7' for x in seen))


class GrabErrorTextTests(unittest.TestCase):
    def test_selectors_error_explains_tracker_login(self):
        import re,json
        tree=ast.parse((Path(__file__).resolve().parents[1]/'app.py').read_text(encoding='utf-8'))
        nodes=[x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name in {'_prowlarr_error_message','_grab_error_text','_page_needs_login'}]
        scope={'re':re,'json':json}
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'app.py','exec'),scope)
        raw='HTTP 500 { "message": "Download selectors didn\\u0027t match for https://dxp.ru/content.php?id=74645", "description": "x"}'
        text=scope['_grab_error_text'](raw,{'indexer':'DXP'})
        self.assertIn('DXP не отдал торрент',text)
        self.assertIn('Indexers → DXP → Test',text)
        self.assertNotIn('NzbDrone',text)
        from bs4 import BeautifulSoup
        self.assertTrue(scope['_page_needs_login']('https://dxp.ru/login.php?returnto=torrent-74645',BeautifulSoup('<p>x</p>','html.parser')))
        self.assertTrue(scope['_page_needs_login']('https://site.test/t/1',BeautifulSoup('<form><input type="password"></form>','html.parser')))
        self.assertFalse(scope['_page_needs_login']('https://site.test/t/1',BeautifulSoup('<a href="x.torrent">x</a>','html.parser')))

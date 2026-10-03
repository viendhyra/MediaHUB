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

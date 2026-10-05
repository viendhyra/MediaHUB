"""Проверки импорта страниц без запуска служб и изменений рабочей базы."""
import ast
import asyncio
import ipaddress
import json
import re
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from bs4 import BeautifulSoup
from fastapi import File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse


def load_parser():
    """Загрузить реальные функции приложения без стартовых обращений к службам."""
    names = {'_blocked_fetch_host', 'clean_page_title', 'page_media_metadata',
             'page_link_candidates', 'fetch_page_resource', 'decode_page_html',
             'torrent_links_from_page', 'fetch_torrent_bytes', '_magnet_name',
             'torrent_page_preview', 'torrent_upload', 'page_dns_failure', 'resolve_page_dns', 'PageDnsTransport', '_fetch_page_once', 'page_fetch_error_text', '_page_needs_login', 'urlparse_host'}
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'app.py').read_text(encoding='utf-8'))
    nodes = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in names:
            node.decorator_list = []
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(isinstance(x, ast.Name) and x.id == 'SITE_TITLE_NOISE' for x in node.targets):
            nodes.append(node)
    scope = dict(re=re, json=json, ipaddress=ipaddress, time=time, httpx=httpx,
                 asyncio=asyncio, Form=Form, File=File, HTTPException=HTTPException,
                 UploadFile=UploadFile, JSONResponse=JSONResponse,
                 PAGE_IMPORT_CACHE={}, PAGE_DNS_CACHE={}, outbound_proxy=lambda: "", page_outbound_proxy=lambda: "", TORRENT_CATEGORIES={'movies', 'tv', 'anime', 'manual'})
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'app.py', 'exec'), scope)
    return scope


class PageImportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.scope = load_parser()

    def test_links_buttons_scripts_and_duplicates(self):
        html = '''<a href="/file.torrent?key=abc">1080p</a>
          <button data-url="/index.php?do=download&amp;id=3">720p</button>
          <a href="magnet:?xt=urn:btih:aaa&amp;dn=Film">M</a>
          <script>{"link":"https:\\/\\/site.test\\/other.torrent"}</script>
          <a href="magnet:?xt=urn:btih:aaa&amp;tr=tracker">duplicate</a>
          <a href="https://utorrent.com/downloads/complete/">Download torrent client</a>
          <button onclick="location.href='/dl.php?id=5'">4K</button>'''
        links, truncated = self.scope['page_link_candidates'](BeautifulSoup(html, 'html.parser'), html, 'https://site.test/page')
        self.assertEqual(len(links), 5)
        self.assertFalse(truncated)
        self.assertIn('https://site.test/file.torrent?key=abc', [x['url'] for x in links])
        self.assertEqual(sum(x['type'] == 'magnet' for x in links), 1)

    def test_metadata_prefers_project_over_organization_and_logo(self):
        html = '''<title>site.test :: Film S02 (2026) WEBRip 1080p</title>
          <img src="/logo.png"><article><b>Название:</b> Фильм<br>
          <b>Год выхода:</b> 2026<br><b>О фильме:</b> Описание проекта.<br>
          <img src="/cover.jpg"><script type="application/ld+json">
          {"@graph":[{"@type":"Organization","name":"Wrong company"},
          {"@type":"TVSeries","name":"Original","genre":["Драма"],"seasonNumber":2}]}</script></article>'''
        meta = self.scope['page_media_metadata'](BeautifulSoup(html, 'html.parser'), html, 'https://site.test/page')
        self.assertEqual(meta['title'], 'Фильм')
        self.assertEqual(meta['year'], '2026')
        self.assertEqual(meta['poster'], 'https://site.test/cover.jpg')
        self.assertEqual(meta['season'], 2)
        self.assertEqual(meta['overview'], 'Описание проекта.')

    def test_legacy_encoding(self):
        data = '<meta charset="windows-1251"><h1>Адский рай</h1>'.encode('cp1251')
        response = httpx.Response(200, content=data)
        self.assertIn('Адский рай', self.scope['decode_page_html'](response))

    async def test_private_redirect_is_rejected(self):
        calls = []
        async def handler(request):
            calls.append(str(request.url))
            return httpx.Response(302, headers={'location': 'http://127.0.0.1/secret'})
        original = httpx.AsyncClient
        with patch.object(httpx, 'AsyncClient', side_effect=lambda **kwargs: original(**dict(kwargs,transport=httpx.MockTransport(handler)))):
            with self.assertRaisesRegex(ValueError, 'нельзя'):
                await self.scope['fetch_page_resource']('https://site.test/page')
        self.assertEqual(len(calls), 1)

    async def test_resource_size_limit(self):
        async def handler(request):
            return httpx.Response(200, content=b'x' * 100)
        original = httpx.AsyncClient
        with patch.object(httpx, 'AsyncClient', side_effect=lambda **kwargs: original(**dict(kwargs,transport=httpx.MockTransport(handler)))):
            with self.assertRaisesRegex(ValueError, 'слишком большой'):
                await self.scope['fetch_page_resource']('https://site.test/page', limit=10)

    async def test_dns_fallback_keeps_host_sni_and_original_url(self):
        seen=[]
        async def lookup(host):
            self.assertEqual(host,'site.test')
            return ['93.184.216.34']
        self.scope['resolve_page_dns']=lookup
        async def handle(transport,request):
            if request.url.host=='site.test':
                raise httpx.ConnectError('[Errno -2] Name or service not known',request=request)
            seen.append(request)
            return httpx.Response(200,content=b'ok')
        with patch.object(httpx.AsyncHTTPTransport,'handle_async_request',handle):
            async with httpx.AsyncClient(transport=self.scope['PageDnsTransport']()) as client:
                response=await client.get('https://site.test/movie')
        self.assertEqual(str(response.url),'https://site.test/movie')
        self.assertEqual(seen[0].headers['host'],'site.test')
        self.assertEqual(seen[0].extensions['sni_hostname'],'site.test')
        self.assertEqual(seen[0].url.host,'93.184.216.34')

    async def test_connection_refused_does_not_use_dns_fallback(self):
        async def lookup(host):
            raise AssertionError('DNS fallback must not run for connection refused')
        self.scope['resolve_page_dns']=lookup
        async def handle(transport,request):
            raise httpx.ConnectError('Connection refused',request=request)
        with patch.object(httpx.AsyncHTTPTransport,'handle_async_request',handle):
            async with httpx.AsyncClient(transport=self.scope['PageDnsTransport']()) as client:
                with self.assertRaises(httpx.ConnectError):await client.get('https://site.test/movie')

    async def test_doh_rejects_private_answers(self):
        async def handler(request):
            return httpx.Response(200,json={'Status':0,'Answer':[{'type':1,'TTL':300,'data':'127.0.0.1'}]})
        original=httpx.AsyncClient
        with patch.object(httpx,'AsyncClient',side_effect=lambda **kwargs: original(**dict(kwargs,transport=httpx.MockTransport(handler)))):
            with self.assertRaises(ValueError):await self.scope['resolve_page_dns']('site.test')

    async def test_doh_caches_public_answer(self):
        calls=[]
        async def handler(request):
            calls.append(request)
            return httpx.Response(200,json={'Status':0,'Answer':[{'type':1,'TTL':300,'data':'93.184.216.34'}]})
        original=httpx.AsyncClient
        with patch.object(httpx,'AsyncClient',side_effect=lambda **kwargs: original(**dict(kwargs,transport=httpx.MockTransport(handler)))):
            self.assertEqual(await self.scope['resolve_page_dns']('site.test'),['93.184.216.34'])
            self.assertEqual(await self.scope['resolve_page_dns']('site.test'),['93.184.216.34'])
        self.assertEqual(len(calls),1)

    async def preview(self):
        """Создать предпросмотр с двумя разными раздачами."""
        links = [{'type': 'torrent', 'url': 'https://site.test/one.torrent', 'title': '720p'},
                 {'type': 'torrent', 'url': 'https://site.test/two.torrent', 'title': '1080p'}]
        async def parser(url, return_meta=False):
            return links, {'title': 'Фильм', 'sourceUrl': url}
        self.scope['torrent_links_from_page'] = parser
        return await self.scope['torrent_page_preview']('https://site.test/page')

    async def upload(self, **changes):
        """Вызвать загрузку с обычными значениями формы."""
        args = dict(file=None, magnet='', page_url='https://site.test/page', category='anime',
                    media_title='', season=2, paused='0', selected_url='', page_token='')
        args.update(changes)
        return await self.scope['torrent_upload'](**args)

    async def test_multiple_links_require_choice(self):
        preview = await self.preview()
        response = await self.upload(page_token=preview['pageToken'])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(json.loads(response.body)['links']), 2)

    async def test_selected_file_only_and_referer(self):
        preview = await self.preview()
        calls = []
        async def fetch(url, referer=''):
            calls.append((url, referer))
            return b'd4:infodee', '', ''
        async def qbit():
            raise RuntimeError('reached-qbit')
        self.scope.update(fetch_torrent_bytes=fetch, qbit_login=qbit)
        with self.assertRaisesRegex(RuntimeError, 'reached-qbit'):
            await self.upload(page_token=preview['pageToken'], selected_url=preview['links'][1]['url'])
        self.assertEqual(calls, [('https://site.test/two.torrent', 'https://site.test/page')])

    async def test_forged_and_expired_choices(self):
        preview = await self.preview()
        with self.assertRaises(HTTPException) as caught:
            await self.upload(page_token=preview['pageToken'], selected_url='https://else.test/file.torrent')
        self.assertEqual(caught.exception.status_code, 400)
        with self.assertRaises(HTTPException) as caught:
            await self.upload(page_token='expired')
        self.assertEqual(caught.exception.status_code, 409)

    async def test_metadata_survives_page_without_torrents(self):
        async def fetch(url, **kwargs):
            return httpx.Response(200, content=b'<h1>Film</h1>', request=httpx.Request('GET', url)), ''
        self.scope['fetch_page_resource'] = fetch
        links, meta = await self.scope['torrent_links_from_page']('https://site.test/page', True)
        self.assertEqual(links, [])
        self.assertEqual(meta['title'], 'Film')
        self.assertIn('error', meta)


if __name__ == '__main__':
    unittest.main()

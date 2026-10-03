"""Обложки: публичные адреса, проверка изображения и повторное использование кэша."""
import ast
import asyncio
import hashlib
import ipaddress
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient


class ArtworkTests(unittest.TestCase):
    def setUp(self):
        """Подменить только сеть и каталог кэша обработчика из приложения."""
        self.temp=tempfile.TemporaryDirectory()
        self.fetch=AsyncMock(return_value=(httpx.Response(200,content=b'\xff\xd8\xff' + b'image'),None))
        scope=dict(asyncio=asyncio,hashlib=hashlib,ipaddress=ipaddress,httpx=httpx,
                   HTTPException=HTTPException,Query=Query,FileResponse=FileResponse,
                   app=FastAPI(),ARTWORK_ROOT=Path(self.temp.name),ARTWORK_SLOTS=asyncio.Semaphore(3),fetch_page_resource=self.fetch)
        tree=ast.parse((Path(__file__).parents[1]/'app.py').read_text(encoding='utf-8'))
        nodes=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in {'artwork','_blocked_fetch_host'}]
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'app.py','exec'),scope)
        self.client=TestClient(scope['app'])

    def tearDown(self):
        """Закрыть тестовый клиент и удалить временный кэш."""
        self.client.close();self.temp.cleanup()

    def test_public_image_is_cached(self):
        """Повторный запрос не обращается к внешнему сайту."""
        for _ in range(2):
            response=self.client.get('/api/artwork',params={'url':'https://example.org/poster.jpg'})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.headers['content-type'],'image/jpeg')
        self.assertEqual(self.fetch.await_count,1)

    def test_internal_addresses_and_html_are_rejected(self):
        """Локальные адреса и HTML вместо обложки не сохраняются."""
        for url in ['http://127.0.0.1/','http://192.168.10.58/','file:///tmp/picture','http://localhost/']:
            self.assertEqual(self.client.get('/api/artwork',params={'url':url}).status_code,400)
        self.fetch.assert_not_awaited()
        self.fetch.return_value=(httpx.Response(200,content=b'<html>blocked</html>'),None)
        self.assertEqual(self.client.get('/api/artwork',params={'url':'https://example.org/picture'}).status_code,502)
        self.assertEqual(list(Path(self.temp.name).iterdir()),[])

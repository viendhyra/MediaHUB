"""Постеры библиотеки после удаления Jellyfin: битые ссылки заменяются TMDB."""
import ast
import json
import re
import sqlite3
import time
import unittest
from contextlib import contextmanager
from difflib import SequenceMatcher
from pathlib import Path

try:
    import httpx
except ImportError:  # на Windows без зависимостей проекта
    class _Status(Exception):
        def __init__(self,code):self.response=type("R",(),{"status_code":code})()
    httpx=type("httpx",(),{"HTTPStatusError":_Status})


class LibraryPosterTests(unittest.TestCase):
    def setUp(self):
        """Взять функцию из cache_refresh.py, сеть TMDB подменить словарём."""
        self.con=sqlite3.connect(":memory:");self.con.row_factory=sqlite3.Row
        self.con.execute("""create table library_cache(kind text,item_key text,title text,year text,poster text,
                            path text,catalog_source text,external_id text,extra_json text)""")
        self.con.execute("create table manual_meta(path text,poster text)")
        self.con.execute("create table source_state(source text,ok int,count int)")
        self.con.execute("create table tmdb_detail_cache(cache_key text,payload text)")
        self.tmdb={};self.searches=[]
        def detail(client,media,tmdb_id):
            hit=self.tmdb.get((media,tmdb_id))
            if isinstance(hit,Exception):raise hit
            if hit is None:
                if hasattr(httpx,'Request'):raise httpx.HTTPStatusError('Not found',request=httpx.Request('GET','http://test/'),response=httpx.Response(404))
                raise httpx.HTTPStatusError(404)
            return hit
        def find(client,kind,title,year="",tmdb_id=None):
            self.searches.append(title);return None
        @contextmanager
        def client(timeout):yield None
        scope=dict(json=json,re=re,time=time,sqlite3=sqlite3,httpx=httpx,SequenceMatcher=SequenceMatcher,
                   JELLYFIN_KEY="",TMDB_KEY="key",external_client=client,_tmdb_detail=detail,_tmdb_find_ru=find,
                   download_meta_index=lambda con:{},download_meta_for=lambda index,path,title:None,
                   state=lambda con,source,ok,count=0,error="",started=None:None)
        tree=ast.parse((Path(__file__).parents[1]/"cache_refresh.py").read_text(encoding="utf-8"))
        nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in {"repair_jellyfin_posters","_norm_title"}]
        exec(compile(ast.Module(body=nodes,type_ignores=[]),"cache_refresh.py","exec"),scope)
        self.repair=scope["repair_jellyfin_posters"]

    def tearDown(self):
        self.con.close()

    def add(self,key,kind,title,poster,extra,path=""):
        self.con.execute("insert into library_cache values(?,?,?,?,?,?,?,?,?)",
                         (kind,key,title,"2026",poster,path or "/m/"+key,"filesystem","",json.dumps(extra)))

    def poster(self,key):
        return self.con.execute("select poster from library_cache where item_key=?",(key,)).fetchone()[0]

    def test_tmdb_id_from_jellyfin_gives_poster(self):
        self.add("a","anime","Фрирен","/api/jellyfin-image/1",{"providerIds":{"Tmdb":"209867"}},path="/m/a")
        self.con.execute("insert into manual_meta values('/m/a','/api/jellyfin-image/1')")
        self.tmdb[("tv","209867")]={"name":"Фрирен","poster_path":"/p.jpg"}
        self.repair(self.con)
        self.assertEqual(self.poster("a"),"https://image.tmdb.org/t/p/w500/p.jpg")
        self.assertEqual(self.con.execute("select poster from manual_meta").fetchone()[0],"https://image.tmdb.org/t/p/w500/p.jpg")

    def test_other_media_type_needs_matching_title(self):
        self.add("b","anime","Сасаки","/api/jellyfin-image/2",{"providerIds":{"Tmdb":"5"}})
        self.tmdb[("movie","5")]={"title":"Совсем другой фильм","poster_path":"/x.jpg"}
        self.repair(self.con)
        self.assertIsNone(self.poster("b"))
        self.assertEqual(self.searches,["Сасаки"])

    def test_tmdb_offline_keeps_row_for_retry(self):
        self.add("c","tv","Сериал","/api/jellyfin-image/3",{"providerIds":{"Tmdb":"7"}})
        self.tmdb[("tv","7")]=ConnectionError("down")
        self.repair(self.con)
        self.assertEqual(self.poster("c"),"/api/jellyfin-image/3")

    def test_detail_cache_used_without_network(self):
        self.add("e","anime","Авантюрист","/api/jellyfin-image/5",{"providerIds":{"Tmdb":"9"}})
        self.con.execute("insert into tmdb_detail_cache values('anime|tmdb|9',?)",(json.dumps({"poster":"https://image.tmdb.org/t/p/w500/c.jpg"}),))
        self.tmdb[("tv","9")]=ConnectionError("down")
        self.repair(self.con)
        self.assertEqual(self.poster("e"),"https://image.tmdb.org/t/p/w500/c.jpg")

    def test_network_stops_after_three_failures(self):
        for n in range(5):
            self.add(f"f{n}","tv",f"Сериал {n}",f"/api/jellyfin-image/f{n}",{"providerIds":{"Tmdb":str(100+n)}})
            self.tmdb[("tv",str(100+n))]=ConnectionError("down")
        calls=[]
        self.tmdb=type("D",(dict,),{"get":lambda d,k:(calls.append(k),dict.get(d,k))[1]})(self.tmdb)
        self.repair(self.con)
        self.assertEqual(len({k[1] for k in calls}),3)

    def test_regular_posters_untouched(self):
        self.add("d","movies","Фильм","https://image.tmdb.org/t/p/original/z.jpg",{})
        self.repair(self.con)
        self.assertEqual(self.poster("d"),"https://image.tmdb.org/t/p/original/z.jpg")


if __name__=="__main__":
    unittest.main()

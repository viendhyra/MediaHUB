"""Раздача-коллекция фильмов раскладывается по отдельным папкам «Название (Год)»."""
import ast,re,shutil,sqlite3,tempfile,unittest
from datetime import datetime,timezone
from pathlib import Path

ORGANIZER=Path(__file__).parents[1]/'download_organizer.py'

def load(movies):
    """Функции организатора без подключения к qBittorrent (верхний код скрипта не выполняется)."""
    tree=ast.parse(ORGANIZER.read_text(encoding='utf-8'))
    consts={'SEASON_WORDS','VIDEO_EXTS','DISC_MARKERS','EXTRA_DIR_WORDS','COLLECTION_TABLES','COLLECTION_WORDS'}
    nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) or (isinstance(n,ast.Assign) and getattr(n.targets[0],'id','') in consts)]
    scope={'re':re,'shutil':shutil,'Path':Path,'datetime':datetime,'timezone':timezone,'MOVIES':Path(movies),'TV':Path(movies)/'tv','ANIME':Path(movies)/'anime'}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),'download_organizer.py','exec'),scope)
    return scope

class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=Path(tempfile.mkdtemp());self.movies=self.tmp/'movies';self.movies.mkdir()
        self.inbox=self.tmp/'inbox';self.inbox.mkdir();self.o=load(self.movies)

    def tearDown(self):shutil.rmtree(self.tmp,ignore_errors=True)

    def make(self,root,files):
        for rel,size in files.items():
            f=root/rel;f.parent.mkdir(parents=True,exist_ok=True);f.write_bytes(b'x'*size)
        return root

    def test_flat_collection_with_numbers_and_subtitles(self):
        src=self.make(self.inbox/'Гарри Поттер. Коллекция (2001-2011) BDRip 1080p',{
            '01. Гарри Поттер и философский камень (2001) BDRip.mkv':1000,
            '02. Гарри Поттер и Тайная комната (2002) BDRip.mkv':1100,
            '07. Гарри Поттер и Дары Смерти. Часть 1 (2010) BDRip.mkv':900,
            '08. Гарри Поттер и Дары Смерти. Часть 2 (2011) BDRip.mkv':950,
            'Subs/01. Гарри Поттер и философский камень (2001) BDRip.rus.srt':5,
            'Sample/sample.mkv':50,'info.nfo':1})
        parts=self.o['collection_parts'](src)
        self.assertEqual([(p['title'],p['year']) for p in parts],[
            ('Гарри Поттер и философский камень','2001'),('Гарри Поттер и Тайная комната','2002'),
            ('Гарри Поттер и Дары Смерти Часть 1','2010'),('Гарри Поттер и Дары Смерти Часть 2','2011')])
        folders=self.o['organize_collection'](src,parts,src.name)
        self.assertEqual([f.name for f in folders],['Гарри Поттер и философский камень (2001)','Гарри Поттер и Тайная комната (2002)',
                                                     'Гарри Поттер и Дары Смерти Часть 1 (2010)','Гарри Поттер и Дары Смерти Часть 2 (2011)'])
        first=self.movies/'Гарри Поттер и философский камень (2001)'
        self.assertTrue((first/'01. Гарри Поттер и философский камень (2001) BDRip.rus.srt').is_file())
        # Сэмпл — тоже видео, поэтому остаток уходит в доп. материалы, а не теряется.
        extras=[d for d in self.movies.iterdir() if d.name.endswith('доп. материалы')]
        self.assertEqual(len(extras),1);self.assertTrue((extras[0]/'Sample'/'sample.mkv').is_file())

    def test_collection_with_folder_per_movie(self):
        src=self.make(self.inbox/'Harry.Potter.Collection.2001-2011.BDRip',{
            'Harry.Potter.and.the.Sorcerers.Stone.2001.BDRip/movie.mkv':1000,
            'Harry.Potter.and.the.Sorcerers.Stone.2001.BDRip/rus.ac3':300,
            'Harry.Potter.and.the.Chamber.of.Secrets.2002.BDRip/movie.mkv':1000})
        parts=self.o['collection_parts'](src)
        self.assertEqual([(p['title'],p['year']) for p in parts],[
            ('Harry Potter and the Chamber of Secrets','2002'),('Harry Potter and the Sorcerers Stone','2001')])
        self.o['organize_collection'](src,parts,src.name)
        self.assertTrue((self.movies/'Harry Potter and the Sorcerers Stone (2001)'/'rus.ac3').is_file())
        self.assertFalse(src.exists())

    def test_not_a_collection(self):
        cases={
            'series':{'Show.S01E01.mkv':1000,'Show.S01E02.mkv':1000},
            'cd parts':{'Movie.2004.CD1.avi':700,'Movie.2004.CD2.avi':700},
            'one movie with sample':{'Movie.2004.mkv':1000,'Movie.2004.sample.mkv':900},
            'numbered without years':{'Мультфильм 1.mkv':1000,'Мультфильм 2.mkv':1000},
            'one movie with bonus':{'Movie.2004.mkv':1000,'Extras/Making of.mkv':900},
            'bluray':{'BDMV/STREAM/00001.m2ts':1000,'BDMV/STREAM/00002.m2ts':1000},
        }
        for name,files in cases.items():
            with self.subTest(name):
                self.assertEqual(self.o['collection_parts'](self.make(self.inbox/name,files)),[])

    def test_sequels_with_years_and_numbers(self):
        src=self.make(self.inbox/'Saw',{'Пила.2004.mkv':1000,'Пила 2.2005.mkv':1000,'1917.2019.mkv':1000})
        self.assertEqual(sorted((p['title'],p['year']) for p in self.o['collection_parts'](src)),
                         [('1917','2019'),('Пила','2004'),('Пила 2','2005')])

    def test_conflict_moves_nothing(self):
        src=self.make(self.inbox/'pack',{'A.2001.mkv':1000,'B.2002.mkv':1000})
        self.make(self.movies,{'B (2002)/B.2002.mkv':10})
        parts=self.o['collection_parts'](src)
        with self.assertRaises(RuntimeError):self.o['organize_collection'](src,parts,'pack')
        self.assertTrue((src/'A.2001.mkv').is_file());self.assertFalse((self.movies/'A (2001)').exists())

    def test_leftovers_follow_first_movie(self):
        src=self.make(self.movies/'Pack (2001-2002)',{'A.2001.mkv':1000,'B.2002.mkv':1000,'info.nfo':3})
        folders=self.o['organize_collection'](src,self.o['collection_parts'](src),src.name)
        self.assertFalse(src.exists());self.assertTrue((folders[0]/'info.nfo').is_file())
        self.assertEqual([self.o['films_word'](n) for n in (1,2,5,11,22)],['1 фильм','2 фильма','5 фильмов','11 фильмов','22 фильма'])

    def test_collection_name(self):
        name=self.o['collection_name']
        self.assertEqual(name(['Гарри Поттер и философский камень','Гарри Поттер и Тайная комната']),'Гарри Поттер')
        self.assertEqual(name(['Пила','Пила 2']),'Пила')
        self.assertEqual(name(['The Matrix','The Matrix Reloaded']),'The Matrix')
        self.assertEqual(name(['Чужой','Хищник'],'Чужой и Хищник. Коллекция (1979-2004) BDRip'),'Чужой и Хищник')

    def test_save_collection_keeps_order_and_single_membership(self):
        con=sqlite3.connect(':memory:');a,b,c=(str(self.movies/n) for n in 'abc')
        first=self.o['save_collection'](con,'movies','Первая',[b,a])
        rows=con.execute('select path from library_collection_items where collection_id=? order by position',(first,)).fetchall()
        self.assertEqual([Path(r[0]).name for r in rows],['b','a'])
        # Фильм переходит в новую коллекцию, а опустевшая старая исчезает.
        second=self.o['save_collection'](con,'movies','Вторая',[a,b,c])
        self.assertEqual(con.execute('select id from library_collections').fetchall(),[(second,)])
        self.assertEqual(con.execute('select count(*) from library_collection_items').fetchone()[0],3)

    def test_clean_title_drops_dangling_bracket(self):
        self.assertEqual(self.o['clean_title']('Гарри Поттер (2001) BDRip'),'Гарри Поттер')

if __name__=='__main__':
    unittest.main()

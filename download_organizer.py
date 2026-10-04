#!/usr/bin/env python3
import os,re,shutil,sqlite3,time
from pathlib import Path
from datetime import datetime,timezone
import httpx

QBIT_URL=os.getenv("QBIT_URL","http://127.0.0.1:8080")
QBIT_USER=os.getenv("QBIT_USER","")
QBIT_PASS=os.getenv("QBIT_PASS","")
CACHE_DB=Path(os.getenv("MEDIAHUB_CACHE_DB","/var/lib/mediahub/cache.db"))
INBOX=Path(os.getenv("INBOX_ROOT","/mnt/media/inbox"))
MOVIES=Path(os.getenv("MOVIES_ROOT","/mnt/media/movies"))
TV=Path(os.getenv("TV_ROOT","/mnt/media/tv"))
ANIME=Path(os.getenv("ANIME_ROOT","/mnt/media/anime"))


def clean_title(name):
    s=Path(name).stem.replace("_"," ").replace("."," ")
    s=re.split(r'\bS\d{1,2}(?:E\d{1,3})?\b',s,maxsplit=1,flags=re.I)[0]
    s=re.split(r'\b(2160p|1080p|720p|WEB[- ]?DL|WEBRip|BluRay|BDRip|HDTV|HEVC|x264|x265|HDR|REMUX)\b',s,maxsplit=1,flags=re.I)[0]
    s=re.sub(r'\b(19|20)\d{2}\b.*$','',s).strip()
    # «Фильм (2001)» после отсечения года оставлял висящую скобку.
    return re.sub(r'\s+',' ',s).strip(" -_.([") or Path(name).stem

SEASON_WORDS={"перв":1,"втор":2,"трет":3,"четв":4,"пят":5,"шест":6,"седьм":7,
              "восьм":8,"девят":9,"десят":10}

def season_number(name):
    """Тот же разбор сезона, что и в app.py: S04E1-24, Сезон 4, 4 сезон, 4x01,
    а также «[ТВ-2]» и «второй сезон» из аниме-раздач."""
    text=" "+(name or "")+" "
    for pattern in (
        r"(?<![a-zа-я0-9])s\s*(\d{1,3})(?=\s*(?:e\s*\d|[^0-9a-zа-я]|$))",
        r"(?:seasons?|сезон)\s*(\d{1,3})(?![0-9])",
        r"(?<![a-zа-я0-9])(\d{1,3})\s*(?:-?[йяе]\w{0,2}\s*)?сезон",
        r"(?<![a-zа-я0-9])(\d{1,2})x\d{1,3}(?![0-9])",
        r"(?<![a-zа-я0-9])(?:тв|tv)\s*[-–—]?\s*(\d{1,2})(?![0-9])",
        r"(?<![a-zа-я0-9])(\d{1,2})\s*(?:-?(?:st|nd|rd|th))?\s+season",
    ):
        m=re.search(pattern,text,re.I)
        if m:
            n=int(m.group(1))
            if 0<n<=200:
                return n
    for word,value in SEASON_WORDS.items():
        if re.search(r"(?<![a-zа-я])"+word+r"\w*\s+сезон",text,re.I):
            return value
    return 1

def year_of(name):
    m=re.search(r'\b((?:19|20)\d{2})\b',name or "")
    return m.group(1) if m else ""

def norm_folder_key(name):
    s=re.sub(r'\s*\((?:19|20)\d{2}\)\s*$','',(name or "").strip())
    return re.sub(r'[^0-9a-zа-яё]+','',s.casefold())

def movie_folder_name(title,year=""):
    """Имя папки фильма: «Название (Год)», как ждёт Jellyfin."""
    base=re.sub(r'\s+',' ',(title or "").strip()).strip(" -_.")
    if not base:
        return ""
    if re.search(r'\((?:19|20)\d{2}\)\s*$',base):
        return base
    inner=year_of(base)
    if inner:
        base=re.sub(r'\s*\b'+inner+r'\b\s*$','',base).strip(" -_.") or base
        year=year or inner
    year=str(year or "").strip()[:4]
    return f"{base} ({year})" if re.fullmatch(r'(?:19|20)\d{2}',year) else base

def movie_folder(title,year=""):
    """Папка фильма в медиатеке: уже существующая или новая «Название (Год)».

    Годится и папка без года, созданная прошлыми версиями, но не папка того же
    фильма с другим годом — ремейк должен лежать отдельно.
    """
    wanted=movie_folder_name(title,year)
    if not wanted:
        wanted=(title or "").strip() or "Без названия"
    want_year=year_of(wanted)
    key=norm_folder_key(wanted)
    if (MOVIES/wanted).is_dir() or not key:
        return MOVIES/wanted
    try:
        for child in MOVIES.iterdir():
            if not child.is_dir() or norm_folder_key(child.name)!=key:
                continue
            have_year=year_of(child.name)
            if not have_year or not want_year or have_year==want_year:
                return child
    except Exception:
        pass
    return MOVIES/wanted

def season_root_source(src):
    if not src.is_dir(): return False
    try:
        return any(x.is_dir() and re.match(r'^(?:season\s*\d+|s\d{1,2})\b',x.name,re.I) for x in src.iterdir())
    except Exception:return False

def _season_folder_number(name):
    """Season number when `name` itself looks like a season folder (Season 02, S2, ...)."""
    if re.match(r'^(?:season\s*\d+|s\d{1,2})\b',name,re.I):
        return season_number(name)
    return None

def _remap_season_relpath(rel):
    """Same rule as app.py: merge mismatched season-folder spellings onto
    "Season NN" instead of letting them sit as a duplicate sibling folder."""
    parts=rel.parts
    if len(parts)>1:
        n=_season_folder_number(parts[0])
        if n:
            return Path(f"Season {n:02d}",*parts[1:])
    return rel

def dest_for(src,kind,title,season,year=""):
    # У файла в запасе имя без расширения: папка «Фильм.mkv» никому не нужна.
    title=(title or "").strip() or clean_title(src.name) or (src.stem if src.is_file() else src.name)
    if kind=="movies":
        # У фильма всегда своя папка «Название (Год)»: и одиночный файл, и
        # папка раздачи переезжают в неё, поэтому в корне movies не остаётся
        # голых видеофайлов без карточки.
        folder=movie_folder(title,year_of(title) or year or year_of(src.name))
        return folder/src.name if src.is_file() else folder
    root=ANIME if kind=="anime" else TV
    if season_root_source(src): return root/title
    season_dir=root/title/f"Season {max(1,int(season or 1)):02d}"
    return season_dir/src.name if src.is_file() else season_dir

def move_merge(src,dst):
    dst.parent.mkdir(parents=True,exist_ok=True)
    if not dst.exists():
        shutil.move(str(src),str(dst)); return
    if src.is_file():
        if dst.exists() and src.stat().st_size==dst.stat().st_size:
            src.unlink(); return
        raise RuntimeError(f"Конфликт: {dst}")
    # preflight
    for f in src.rglob('*'):
        if not f.is_file(): continue
        t=dst/_remap_season_relpath(f.relative_to(src))
        if t.exists() and (not t.is_file() or f.stat().st_size!=t.stat().st_size):
            raise RuntimeError(f"Конфликт: {t}")
    for f in [p for p in src.rglob('*') if p.is_file()]:
        t=dst/_remap_season_relpath(f.relative_to(src)); t.parent.mkdir(parents=True,exist_ok=True)
        if t.exists(): f.unlink()
        else: shutil.move(str(f),str(t))
    for d in sorted([p for p in src.rglob('*') if p.is_dir()],key=lambda p:len(p.parts),reverse=True):
        try:d.rmdir()
        except OSError:pass
    try:src.rmdir()
    except OSError:pass

VIDEO_EXTS={".mkv",".mp4",".avi",".m4v",".ts",".m2ts",".mts",".mov",".webm",
            ".vob",".iso",".img",".mpg",".mpeg",".m2v",".wmv",".flv",".divx",
            ".rmvb",".3gp",".ogm",".mk3d",".asf",".f4v",".wtv"}
DISC_MARKERS={"bdmv","video_ts","avchd"}
EXTRA_DIR_WORDS=("sample","extra","bonus","featurette","behind the scenes","trailer","доп.","доп ","дополнит","бонус","трейлер")

def _is_extra(f,root):
    """Сэмпл, трейлер или бонус раздачи — не отдельный фильм коллекции."""
    try:parents=f.relative_to(root).parts[:-1]
    except ValueError:parents=()
    if any(p.casefold().startswith(EXTRA_DIR_WORDS) for p in parents):return True
    return bool(re.search(r'(?<![a-zа-я])(sample|trailer|трейлер|тизер)(?![a-zа-я])',f.stem,re.I))

def _episode_like(name):
    """Похоже на серию сериала, а не на отдельный фильм."""
    return bool(re.search(r'(?<![a-z0-9])(s\d{1,2}\s*e\d{1,3}|\d{1,2}x\d{2,3}|e\d{2,3}|ep\.?\s*\d{1,3})(?![0-9])|сери[яи]|episode',name,re.I))

def part_label_title(label):
    """Название и год фильма из имени файла или папки части коллекции.

    Номер части в начале («01. », «2 - ») отбрасывается; «1917.2019» и «21.2008»
    так не режутся — после номера должна идти буква.
    """
    label=re.sub(r'^\s*\d{1,2}\s*[.)\-_]\s*(?=[^\W\d_])','',label or "")
    # Год — последнее «19xx/20xx» не в начале имени: у «1917.2019» это 2019, а не название.
    years=[m for m in re.finditer(r'(?<!\d)((?:19|20)\d{2})(?!\d)',label) if m.start()>0]
    head=label[:years[-1].start()] if years else label
    s=re.sub(r'\[(.*?)\]',' ',head.replace("_"," ").replace("."," "))
    s=re.split(r'\b(2160p|1080p|720p|WEB[- ]?DL|WEBRip|BluRay|BDRip|HDTV|HEVC|x264|x265|HDR|REMUX)\b',s,maxsplit=1,flags=re.I)[0]
    return re.sub(r'\s+',' ',s).strip(" -_.(["),years[-1].group(1) if years else ""

def collection_parts(src):
    """Фильмы раздачи-коллекции («Гарри Поттер: 8 фильмов») или [] для одного фильма.

    Коллекцией считается папка с двумя и больше полноразмерными видео, которые
    различаются названием или годом. Серии сериала, части одного фильма
    (CD1/CD2), Blu-ray-структура и сэмплы коллекцией не считаются — такие
    раздачи раскладываются как раньше, одной папкой.
    """
    if not src.is_dir():return []
    try:
        if any(d.is_dir() and d.name.casefold() in DISC_MARKERS for d in src.rglob('*')):return []
        videos=[f for f in src.rglob('*') if f.is_file() and f.suffix.lower() in VIDEO_EXTS]
        sizes={f:f.stat().st_size for f in videos}
    except OSError:
        return []
    main=[f for f in videos if not _is_extra(f,src)]
    if len(main)<2:return []
    largest=max(sizes[f] for f in main)
    # Фильм коллекции — не меньше трети самого большого файла: бонусы обычно короче.
    main=sorted((f for f in main if sizes[f]>=largest*0.3),key=lambda f:str(f).casefold())
    if len(main)<2 or sum(_episode_like(f.name) for f in main)*2>=len(main):return []
    parts=[]
    for f in main:
        # Своя подпапка у фильма — её имя обычно точнее имени файла.
        own=f.parent if f.parent!=src and sum(g.parent==f.parent for g in main)==1 else None
        title,year=part_label_title(f.stem)
        if own:
            dir_title,dir_year=part_label_title(own.name)
            if dir_year or not year:title,year=dir_title or title,dir_year or year
        parts.append({"file":f,"dir":own,"title":title,"year":year})
    keys={(re.sub(r'\d+','',norm_folder_key(p["title"])),p["year"]) for p in parts}
    if len(keys)<len(parts) or not all(norm_folder_key(p["title"]) for p in parts):return []
    return parts

def _sidecars(video,root):
    """Субтитры и внешние дорожки фильма: файлы раздачи с тем же началом имени."""
    stem=video.stem.casefold()
    out=[]
    for f in root.rglob('*'):
        if f.is_file() and f!=video and f.suffix.lower() not in VIDEO_EXTS and f.name.casefold().startswith(stem+"."):
            out.append(f)
    return out

def collection_plan(src,parts):
    """Куда переедет каждый фильм коллекции.

    Возвращает переносы [(источник, назначение)] и папки фильмов по порядку частей.
    """
    plan=[];folders=[]
    own_dirs={p["dir"] for p in parts if p["dir"]}
    for p in parts:
        folder=movie_folder(p["title"],p["year"]);folders.append(folder)
        if p["dir"]:
            plan.append((p["dir"],folder));continue
        plan.append((p["file"],folder/p["file"].name))
        plan+=[(side,folder/side.name) for side in _sidecars(p["file"],src) if side.parent not in own_dirs]
    return plan,folders

def _plan_conflicts(plan):
    """Проверить все переносы до первого: коллекция не должна разъехаться наполовину."""
    for s,d in plan:
        files=[(s,d)] if s.is_file() else [(f,d/f.relative_to(s)) for f in s.rglob('*') if f.is_file()]
        for f,t in files:
            if t.exists() and (not t.is_file() or f.stat().st_size!=t.stat().st_size):
                raise RuntimeError(f"Конфликт: {t}")

def organize_collection(src,parts,collection_title):
    """Разложить коллекцию: у каждого фильма своя папка «Название (Год)».

    Бонусы, оставшиеся после фильмов, уходят в отдельную папку
    «<коллекция> — доп. материалы», мелочь вроде .nfo остаётся во входящих.
    Возвращает список итоговых папок фильмов.
    """
    plan,folders=collection_plan(src,parts)
    _plan_conflicts(plan)
    for s,d in plan:
        if s.exists():move_merge(s,d)
    left=[f for f in src.rglob('*') if f.is_file() and f.suffix.lower() in VIDEO_EXTS]
    if left:
        name=(clean_title(collection_title+".mkv") or "Коллекция")+" — доп. материалы"
        move_merge(src,MOVIES/name)
    elif src.exists():
        # Обложка и .nfo раздачи — к первому фильму, чтобы в медиатеке не оставалась
        # пустая папка коллекции. При совпадении имён мелочь просто остаётся на месте.
        try:move_merge(src,folders[0])
        except (OSError,RuntimeError):pass
    return folders

COLLECTION_TABLES=(
    """create table if not exists library_collections(
        id integer primary key autoincrement,kind text not null,title text not null,
        created_at text,updated_at text)""",
    """create table if not exists library_collection_items(
        collection_id integer not null,path text not null,position integer not null default 0,
        primary key(collection_id,path))""",
)
COLLECTION_WORDS=r'\b(коллекция|collection|трилогия|trilogy|дилогия|квадрилогия|квадрология|антология|anthology|сага|saga|все части|complete)\b'

def collection_name(titles,fallback=""):
    """Имя коллекции: общее начало названий фильмов («Гарри Поттер»),
    иначе имя раздачи без слов «коллекция», «трилогия» и т. п."""
    words=[t.split() for t in titles if t]
    common=[]
    for group in zip(*words):
        if len({w.casefold().strip(".:,-") for w in group})!=1:break
        common.append(group[0])
    # «Гарри Поттер и …» — союз в конце общего начала не нужен.
    while common and (len(common[-1].strip(".:,-"))<=2 or common[-1].casefold().strip(".:,-") in {"and","the","of"}):
        common.pop()
    name=" ".join(common).strip(" .:,-")
    if len(name)>=3:return name
    s=re.sub(COLLECTION_WORDS,"",clean_title((fallback or "")+".mkv"),flags=re.I)
    return re.sub(r'\s+',' ',s).strip(" .:,-/") or "Коллекция"

def collection_key(path):
    """Путь проекта в том виде, в каком его хранит медиатека."""
    return str(Path(path).resolve()).rstrip("/")

def save_collection(con,kind,title,paths):
    """Создать коллекцию из папок проектов в заданном порядке; возвращает её id.

    Фильм состоит не больше чем в одной коллекции: из прежней он уходит,
    опустевшие коллекции удаляются.
    """
    for sql in COLLECTION_TABLES:con.execute(sql)
    keys=list(dict.fromkeys(collection_key(p) for p in paths if p))
    stamp=datetime.now(timezone.utc).isoformat()
    cid=con.execute("insert into library_collections(kind,title,created_at,updated_at) values(?,?,?,?)",
                    (kind,(title or "").strip() or "Коллекция",stamp,stamp)).lastrowid
    set_collection_items(con,cid,keys)
    return cid

def set_collection_items(con,cid,paths):
    """Заменить состав коллекции, сохранив порядок `paths`."""
    keys=list(dict.fromkeys(collection_key(p) for p in paths if p))
    con.execute("delete from library_collection_items where collection_id=?",(cid,))
    for path in keys:
        con.execute("delete from library_collection_items where path=?",(path,))
    con.executemany("insert into library_collection_items(collection_id,path,position) values(?,?,?)",
                    [(cid,p,i) for i,p in enumerate(keys)])
    con.execute("delete from library_collections where id not in (select distinct collection_id from library_collection_items)")

def films_word(n):
    """«1 фильм», «2 фильма», «5 фильмов»."""
    n=abs(int(n))
    if n%10==1 and n%100!=11:return f"{n} фильм"
    if 2<=n%10<=4 and not 12<=n%100<=14:return f"{n} фильма"
    return f"{n} фильмов"

def db():
    CACHE_DB.parent.mkdir(parents=True,exist_ok=True)
    con=sqlite3.connect(CACHE_DB,timeout=8); con.row_factory=sqlite3.Row
    con.execute("pragma busy_timeout=8000")
    con.execute("""create table if not exists download_jobs(
        hash text primary key,kind text not null,media_title text,season integer,
        release_title text,category text,status text,created_at text,
        completed_at text,final_path text,error text)""")
    con.execute("""create table if not exists activity_log(
        id integer primary key autoincrement,at text not null,action text not null,
        title text,details text,ok integer not null default 1)""")
    con.commit(); return con

def login():
    """Подключиться к qBittorrent.

    Установщик отключает авторизацию qBittorrent для localhost, поэтому пары
    логин/пароль обычно нет. Раньше организатор в этом случае молча выходил —
    и скачанные файлы никто не переносил в медиатеку.
    """
    c=httpx.Client(timeout=12)
    if QBIT_USER and QBIT_PASS:
        try:
            r=c.post(QBIT_URL+"/api/v2/auth/login",data={"username":QBIT_USER,"password":QBIT_PASS})
            if r.text.strip()=="Ok.":
                return c
        except Exception as e:
            print("qBittorrent login failed:",e)
        c.close(); return None
    try:
        r=c.get(QBIT_URL+"/api/v2/app/version")
        if r.status_code<300:
            return c
        print("qBittorrent requires authentication, but QBIT_USER/QBIT_PASS are empty")
    except Exception as e:
        print("qBittorrent is unreachable:",e)
    c.close(); return None

def main():
    """Разложить завершённые загрузки qBittorrent по медиатеке."""
    for p in [INBOX/"movies",INBOX/"tv",INBOX/"anime",INBOX/"manual",MOVIES,TV,ANIME]: p.mkdir(parents=True,exist_ok=True)
    c=login()
    if not c:
        print("qBittorrent is not reachable; organizer skipped")
        return

    # Keep category save paths deterministic.
    for cat,path in {"movies":INBOX/"movies","tv":INBOX/"tv","anime":INBOX/"anime","manual":INBOX/"manual"}.items():
        r=c.post(QBIT_URL+"/api/v2/torrents/createCategory",data={"category":cat,"savePath":str(path)})
        if r.status_code>=300:
            c.post(QBIT_URL+"/api/v2/torrents/editCategory",data={"category":cat,"savePath":str(path)})

    try:
        torrents=c.get(QBIT_URL+"/api/v2/torrents/info").json()
    except Exception as e:
        print("qBittorrent list failed:",e); c.close(); return

    with db() as con:
        jobs={r["hash"]:dict(r) for r in con.execute("select * from download_jobs").fetchall()}
        # Год карточки, если торрент добавляли со страницы тайтла: он попадёт в имя
        # папки фильма, даже когда в названии раздачи года нет.
        meta_years={}
        try:
            for r in con.execute("select hash,year from download_meta").fetchall():
                if r["year"]: meta_years[r["hash"]]=str(r["year"])[:4]
        except Exception:
            pass
        changed=False
        organized_any=False
        for t in torrents:
            cat=t.get("category") or ""
            if cat not in {"movies","tv","anime"}: continue
            if float(t.get("progress") or 0)<0.9999: continue
            h=t.get("hash"); job=jobs.get(h,{})
            content=Path(t.get("content_path") or "")
            if not content.exists():
                possible=Path(t.get("save_path") or "")/(t.get("name") or "")
                content=possible if possible.exists() else content
            if not content.exists():
                con.execute("insert or replace into download_jobs(hash,kind,media_title,season,release_title,category,status,created_at,error) values(?,?,?,?,?,?,?,?,?)",
                            (h,cat,job.get("media_title") or "",job.get("season") or 1,t.get("name") or "",cat,"error",job.get("created_at") or datetime.now(timezone.utc).isoformat(),"Файлы загрузки не найдены"));changed=True;continue
            title=(job.get("media_title") or "").strip() or clean_title(t.get("name") or content.name)
            season=int(job.get("season") or 0) or season_number(t.get("name") or content.name)
            # Несколько фильмов в одной раздаче — у каждого своя карточка, а не одна
            # «папка с восемью сериями» под именем того фильма, со страницы которого скачали.
            parts=collection_parts(content) if cat=="movies" else []
            dest=None if parts else dest_for(content,cat,title,season,meta_years.get(h,""))
            try:
                c.post(QBIT_URL+"/api/v2/torrents/pause",data={"hashes":h})
                if parts:
                    folders=organize_collection(content,parts,t.get("name") or content.name)
                    # Карточка со страницы (постер, описание) относится к одному фильму —
                    # привязываем её только к нему, а не ко всей коллекции.
                    want=norm_folder_key(title)
                    dest=next((f for f in folders if norm_folder_key(f.name)==want),None)
                    # Фильмы раздачи сразу собраны в коллекцию библиотеки — видно, что это одна серия.
                    save_collection(con,"movies",collection_name([p["title"] for p in parts],t.get("name") or content.name),folders)
                    print("collection:",", ".join(f.name for f in folders))
                else:
                    move_merge(content,dest)
                c.post(QBIT_URL+"/api/v2/torrents/delete",data={"hashes":h,"deleteFiles":"false"})
                stamp=datetime.now(timezone.utc).isoformat()
                con.execute("insert or replace into download_jobs(hash,kind,media_title,season,release_title,category,status,created_at,completed_at,final_path,error) values(?,?,?,?,?,?,?,?,?,?,?)",
                            (h,cat,title,season,t.get("name") or "",cat,"organized",job.get("created_at") or stamp,stamp,str(dest or ""),""))
                details=(f"Коллекция: {films_word(len(folders))} — "+", ".join(f.name for f in folders)) if parts else str(dest)
                con.execute("insert into activity_log(at,action,title,details,ok) values(?,?,?,?,1)",(stamp,"organized",title,details));changed=True;organized_any=True
                print(f"organized: {t.get('name')} -> {dest}")
            except Exception as e:
                stamp=datetime.now(timezone.utc).isoformat()
                con.execute("insert or replace into download_jobs(hash,kind,media_title,season,release_title,category,status,created_at,error) values(?,?,?,?,?,?,?,?,?)",
                            (h,cat,title,season,t.get("name") or "",cat,"error",job.get("created_at") or stamp,str(e)))
                con.execute("insert into activity_log(at,action,title,details,ok) values(?,?,?,?,0)",(stamp,"organizer-error",title,str(e)));changed=True
                print("organize failed:",e)
        if changed: con.commit()
    c.close()

    # Refresh Jellyfin and local cache best-effort — only when something actually
    # moved. Refreshing on every idle minute risked colliding with the next run's
    # refresh and cancelling Jellyfin's own post-scan cleanup mid-flight, which is
    # how a removed title could keep reappearing in its library instead of being
    # purged for good.
    if organized_any:
        try:httpx.post("http://127.0.0.1:8090/api/refresh-jellyfin",timeout=5)
        except Exception:pass
        os.system("systemctl start mediahub-local-cache.service >/dev/null 2>&1 || true")

if __name__=="__main__":
    main()

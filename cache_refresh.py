#!/usr/bin/env python3
import os
import re
import sys
import json
import time
import sqlite3
import hashlib
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
from bs4 import BeautifulSoup

RADARR_URL=os.getenv("RADARR_URL","http://127.0.0.1:7878")
SONARR_URL=os.getenv("SONARR_URL","http://127.0.0.1:8989")
PROWLARR_URL=os.getenv("PROWLARR_URL","http://127.0.0.1:9696")
JELLYFIN_URL=os.getenv("JELLYFIN_URL","http://127.0.0.1:8096")

def _persistent_value(name,default=""):
    # Background timers may have been started with an old systemd environment.
    # Read MediaHub's root-only secret store and env file directly.
    try:
        data=json.loads(Path("/var/lib/mediahub/secrets.json").read_text(encoding="utf-8"))
        v=str((data or {}).get(name) or "").strip()
        if v:return v
    except Exception:
        pass
    try:
        for raw in Path("/etc/mediahub.env").read_text(encoding="utf-8",errors="replace").splitlines():
            line=raw.strip()
            if not line or line.startswith("#") or "=" not in line:continue
            k,v=line.split("=",1)
            if k.strip()==name and v.strip():return v.strip()
    except Exception:
        pass
    return str(os.getenv(name,default) or "").strip()

TMDB_KEY=_persistent_value("TMDB_API_KEY")

def tmdb_auth_params(extra=None):
    cred=TMDB_KEY.strip()
    params=dict(extra or {})
    if cred and len(cred)<=64:
        params["api_key"]=cred
    return params

def tmdb_auth_headers():
    cred=TMDB_KEY.strip()
    if cred and len(cred)>64:
        return {"Authorization":f"Bearer {cred}","Accept":"application/json"}
    return {"Accept":"application/json"}

OUTBOUND_PROXY=_persistent_value("MEDIAHUB_OUTBOUND_PROXY")

def external_client(timeout=20.0,headers=None):
    kw={
        "timeout":httpx.Timeout(timeout,connect=min(8.0,timeout)),
        "follow_redirects":True,
        "trust_env":False,
        "headers":{"User-Agent":"MediaHub/20.6",**(headers or {})},
        "limits":httpx.Limits(max_connections=3,max_keepalive_connections=2,keepalive_expiry=20.0),
    }
    if OUTBOUND_PROXY:
        kw["proxy"]=OUTBOUND_PROXY
    return httpx.Client(**kw)

def external_request(client,method,url,*,params=None,headers=None,json_body=None,retries=3):
    last=None
    for attempt in range(max(1,retries)):
        try:
            r=client.request(method,url,params=params,headers=headers,json=json_body)
            if r.status_code in {429,500,502,503,504} and attempt+1<retries:
                time.sleep(0.7*(attempt+1));continue
            r.raise_for_status();return r
        except (httpx.ConnectError,httpx.ConnectTimeout,httpx.ReadTimeout,httpx.ReadError,httpx.RemoteProtocolError) as e:
            last=e
            if attempt+1<retries:
                time.sleep(0.7*(attempt+1));continue
            raise
        except Exception as e:
            last=e;raise
    if last:raise last
    raise RuntimeError("Внешний запрос не выполнен")
JELLYFIN_KEY=""
TVMAZE_ENABLED=os.getenv("TVMAZE_ENABLED","1").strip().lower() not in {"0","false","no","off"}
KINOPOISK_API_KEY=_persistent_value("KINOPOISK_API_KEY")
KINOPOISK_API_URL=os.getenv("KINOPOISK_API_URL","https://kinopoiskapiunofficial.tech").rstrip("/")
CACHE_DB=Path(os.getenv("MEDIAHUB_CACHE_DB","/var/lib/mediahub/cache.db"))
ANILIBERTY_URL=os.getenv("ANILIBERTY_DISCOVERY_URL","https://aniliberty.top/")
ANILIST_URL="https://graphql.anilist.co"

MOVIES_ROOT=os.getenv("MOVIES_ROOT","/mnt/media/movies")
TV_ROOT=os.getenv("TV_ROOT","/mnt/media/tv")
ANIME_ROOT=os.getenv("ANIME_ROOT","/mnt/media/anime")
VIDEO_EXTS={".mkv",".mp4",".avi",".m4v",".ts",".m2ts",".mts",".mov",".webm",
            ".vob",".iso",".img",".mpg",".mpeg",".m2v",".wmv",".flv",".divx",
            ".rmvb",".3gp",".ogm",".mk3d",".asf",".f4v",".wtv"}
DISC_MARKERS={"bdmv","video_ts","avchd"}
CATEGORIES={"movies":2000,"tv":5000,"anime":5070,"games":4050}

def now():
    return datetime.now(timezone.utc).isoformat()

def xml_key(paths):
    for p in paths:
        f=Path(p)
        if f.exists():
            try:
                n=ET.parse(f).getroot().find("ApiKey")
                if n is not None and n.text:
                    return n.text.strip()
            except Exception:
                pass
    return ""

RADARR_KEY=os.getenv("RADARR_API_KEY") or xml_key([
    "/var/lib/radarr/.config/Radarr/config.xml","/var/lib/radarr/config.xml"])
SONARR_KEY=os.getenv("SONARR_API_KEY") or xml_key([
    "/var/lib/sonarr/.config/Sonarr/config.xml","/var/lib/sonarr/config.xml"])
PROWLARR_KEY=os.getenv("PROWLARR_API_KEY") or xml_key([
    "/var/lib/prowlarr/.config/Prowlarr/config.xml","/var/lib/prowlarr/config.xml"])

def db():
    CACHE_DB.parent.mkdir(parents=True,exist_ok=True)
    con=sqlite3.connect(CACHE_DB,timeout=8)
    con.row_factory=sqlite3.Row
    con.execute("pragma busy_timeout=8000")
    try: con.execute("pragma journal_mode=WAL")
    except Exception: pass
    con.execute("create table if not exists meta(key text primary key,value text)")
    con.execute("""create table if not exists bindings(
      kind text not null,indexer_id integer not null,enabled integer not null default 1,
      primary key(kind,indexer_id))""")
    con.execute("""create table if not exists catalog(
      kind text,mode text,rank integer,title text,year text,overview text,
      poster text,external_id integer,primary key(kind,mode,rank))""")
    cols={r[1] for r in con.execute("pragma table_info(catalog)").fetchall()}
    if "catalog_source" not in cols:
        con.execute("alter table catalog add column catalog_source text")
    if "extra_json" not in cols:
        con.execute("alter table catalog add column extra_json text")
    con.execute("""create table if not exists provider_feed(
      kind text,indexer_id integer,indexer text,title text,size integer,seeders integer,
      peers integer,published_at text,guid text,download_url text,
      primary key(kind,indexer_id,guid))""")
    con.execute("""create table if not exists source_state(
      source text primary key,ok integer not null default 0,item_count integer not null default 0,
      last_success text,last_error text,duration_ms integer not null default 0)""")
    con.execute("""create table if not exists library_cache(
      kind text not null,item_key text not null,title text not null,year text,
      overview text,poster text,path text,added_at text,has_file integer not null default 0,
      catalog_source text,external_id text,extra_json text,
      primary key(kind,item_key))""")
    con.execute("""create table if not exists external_discovery(
      source text not null,item_key text not null,title text not null,year text,
      overview text,poster text,url text,added_at text,extra_json text,
      primary key(source,item_key))""")
    con.commit()
    return con

def state(con,source,ok,count=0,error="",started=None):
    ms=int((time.monotonic()-started)*1000) if started else 0
    old=con.execute("select last_success from source_state where source=?",(source,)).fetchone()
    success=now() if ok else (old["last_success"] if old else None)
    con.execute("""insert or replace into source_state
      (source,ok,item_count,last_success,last_error,duration_ms)
      values(?,?,?,?,?,?)""",(source,1 if ok else 0,count,success,error[:500],ms))

def get_json(url,key,path,timeout=10):
    with httpx.Client(timeout=httpx.Timeout(timeout,connect=4.0),trust_env=False) as c:
        r=c.get(url+path,headers={"X-Api-Key":key} if key else {})
        r.raise_for_status()
        return r.json()

def poster(images):
    return next((i.get("remoteUrl") for i in (images or []) if i.get("coverType")=="poster"),None)


def has_cyrillic(value):
    return bool(re.search(r"[А-Яа-яЁё]",value or ""))

def fs_title(name):
    s=(name or "").replace("_"," ").replace("."," ")
    s=re.sub(r"\[(.*?)\]"," ",s)
    s=re.sub(r"\b(2160p|1080p|720p|web[- ]?dl|webrip|bluray|bdrip|hdtv|hevc|x264|x265|hdr|remux)\b.*$",
             "",s,flags=re.I)
    # Папка фильма называется «Название (Год)», а год карточка показывает
    # отдельным полем — в заголовке он только дублировался бы.
    trimmed=re.sub(r"\s*\((?:19|20)\d{2}\)\s*$","",s).strip()
    if trimmed:
        s=trimmed
    s=re.sub(r"\s+"," ",s).strip(" -_.")
    return s or name

def fs_year(name):
    m=re.search(r"\b((?:19|20)\d{2})\b",name or "")
    return m.group(1) if m else ""

def scan_media_root(root,kind):
    root=Path(root)
    if not root.exists():
        return []
    out=[]
    for child in sorted(root.iterdir(),key=lambda x:x.name.casefold()):
        if child.name.startswith("."):
            continue
        if child.is_file() and child.suffix.lower() not in VIDEO_EXTS:
            continue
        folder=child if child.is_dir() else child.parent
        count=0
        newest=0.0
        try:
            for f in folder.rglob("*"):
                if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
                    count+=1
                    try:newest=max(newest,f.stat().st_mtime)
                    except OSError:pass
        except Exception:
            pass
        if not count:
            # Blu-ray и DVD лежат папками BDMV / VIDEO_TS — это тоже фильм.
            try:
                disc=folder.name.lower() in DISC_MARKERS or any(
                    x.is_dir() and x.name.lower() in DISC_MARKERS for x in folder.iterdir())
            except Exception:
                disc=False
            if disc:
                count=1
                try:newest=folder.stat().st_mtime
                except OSError:newest=0.0
        if not count:
            continue
        out.append({
            "path":str(folder),
            "title":fs_title(folder.name),
            "year":fs_year(folder.name),
            "count":count,
            "added":datetime.fromtimestamp(newest,tz=timezone.utc).isoformat() if newest else now()
        })
    return out

def download_meta_index(con):
    """Карточки, сохранённые при добавлении торрента по ссылке на страницу.

    Ключи — итоговый путь загрузки и нормализованное название, чтобы папку в
    медиатеке можно было связать с постером и описанием со страницы.
    """
    index={}
    try:
        rows=con.execute("""select m.title as mtitle, m.year, m.poster, m.overview, m.source_url,
                                   j.final_path, j.media_title, j.release_title
                            from download_meta m left join download_jobs j on j.hash=m.hash""").fetchall()
    except Exception:
        return index
    for r in rows:
        meta={"title":r["mtitle"] or "","year":r["year"] or "","poster":r["poster"] or "",
              "overview":r["overview"] or "","sourceUrl":r["source_url"] or ""}
        if not (meta["poster"] or meta["overview"]):
            continue
        for key in (str(r["final_path"] or "").rstrip("/"),):
            if key:
                index[key]=meta
        for name in (r["media_title"],r["release_title"],r["mtitle"]):
            n=_norm_title(name)
            if n:
                index.setdefault("title:"+n,meta)
    return index


def download_meta_for(index,path,title):
    if not index:
        return None
    hit=index.get(str(path or "").rstrip("/"))
    if hit:
        return hit
    n=_norm_title(title)
    if n and "title:"+n in index:
        return index["title:"+n]
    # Папка вида «Название - 8 серия»: пробуем сопоставить по началу имени.
    for key,meta in index.items():
        if not key.startswith("title:"):
            continue
        base=key[6:]
        if base and n and (n.startswith(base) or base.startswith(n)):
            return meta
    return None


def manual_meta_index(con):
    """Карточки, привязанные пользователем вручную (таблица manual_meta)."""
    index={}
    try:
        rows=con.execute("select * from manual_meta").fetchall()
    except Exception:
        return index
    for r in rows:
        path=str(r["path"] or "").rstrip("/")
        if path:
            try:extra=json.loads((r["extra_json"] if "extra_json" in r.keys() else None) or "{}")
            except Exception:extra={}
            if not isinstance(extra,dict):extra={}
            index[path]={"title":r["title"] or "","year":r["year"] or "","poster":r["poster"] or "",
                         "overview":r["overview"] or "","sourceUrl":r["source_url"] or "",
                         "episodes":int(r["episodes"] or 0),"manual":True,"kind":r["kind"] or "",
                         "genres":[g for g in (extra.get("genres") or []) if isinstance(g,str)],
                         "rating":extra.get("rating")}
    return index


def apply_manual_library(con):
    """Наложить ручные карточки на библиотеку последним шагом синхронизации.

    Radarr, Sonarr, Jellyfin и TMDB перезаписывают строки библиотеки по своим
    данным. Если к проекту привязана страница-источник, карточка целиком берётся
    с неё: пустое поле страницы остаётся пустым, а не добирается из каталогов.
    """
    manual=manual_meta_index(con)
    updated=0
    for path,meta in manual.items():
        rows=con.execute("select * from library_cache where rtrim(path,'/')=?",(path,)).fetchall()
        for row in rows:
            try:extra=json.loads(row["extra_json"] or "{}")
            except Exception:extra={}
            extra.update({"manualMetadata":True,"needsMetadata":False})
            title=meta["title"] or row["title"] or ""
            year=meta["year"] or row["year"] or ""
            poster_url=meta["poster"] or row["poster"]
            overview=meta["overview"] or row["overview"] or ""
            if meta["sourceUrl"]:
                folder=Path(path).stem if Path(path).suffix.lower() in VIDEO_EXTS else Path(path).name
                title=meta["title"] or fs_title(folder)
                year=meta["year"] or fs_year(folder)
                poster_url=meta["poster"] or None
                overview=meta["overview"] or ""
                for k in ("tmdbId","jellyfinId","providerIds","originalTitle"):
                    extra.pop(k,None)
                extra.update({"sourceUrl":meta["sourceUrl"],"fromPage":True,
                              "genres":meta["genres"],"rating":meta["rating"],
                              "runtime":0,"studio":"","network":"","status":"local","localizedTitle":""})
            if meta["episodes"]>0:
                extra["episodesTotal"]=meta["episodes"]
            con.execute("""update library_cache set title=?,year=?,overview=?,poster=?,extra_json=?
                           where kind=? and item_key=?""",
                        (title,year,overview,poster_url,json.dumps(extra,ensure_ascii=False),
                         row["kind"],row["item_key"]))
            updated+=1
    return updated


def merge_filesystem_library(con):
    """Files physically present in the media roots are part of the library
    even when Radarr/Sonarr did not import them."""
    meta_index=download_meta_index(con)
    manual_index=manual_meta_index(con)
    for kind,root in (("movies",MOVIES_ROOT),("tv",TV_ROOT),("anime",ANIME_ROOT)):
        started=time.monotonic()
        try:
            entries=scan_media_root(root,kind)
            arr_paths={
                str(r["path"] or "").rstrip("/")
                for r in con.execute(
                    "select path from library_cache where kind=? and path is not null",(kind,)
                ).fetchall()
            }
            for x in entries:
                p=x["path"].rstrip("/")
                if p in arr_paths:
                    continue
                key="fs-"+hashlib.sha1(f"{kind}|{p}".encode("utf-8")).hexdigest()[:20]
                # Данные, привязанные пользователем вручную, не затираем.
                prev=con.execute("select * from library_cache where kind=? and item_key=?",(kind,key)).fetchone()
                prev_extra={}
                if prev:
                    try:prev_extra=json.loads(prev["extra_json"] or "{}")
                    except Exception:prev_extra={}
                if prev_extra.get("manualMetadata"):
                    continue
                # Ручная привязка важнее всего, затем данные страницы, с которой
                # раздача была скачана.
                page=manual_index.get(p) or download_meta_for(meta_index,p,x["title"])
                extra={
                    "genres":[],
                    "runtime":0,
                    "rating":None,
                    "status":"local",
                    "studio":"",
                    "network":"",
                    "catalog":"filesystem",
                    "videoCount":x["count"],
                    "needsMetadata":not bool(page)
                }
                if page:
                    extra["sourceUrl"]=page.get("sourceUrl") or ""
                    extra["fromPage"]=True
                    if page.get("manual"):
                        extra["manualMetadata"]=True
                    if int(page.get("episodes") or 0)>0:
                        extra["episodesTotal"]=int(page["episodes"])
                con.execute("""insert or replace into library_cache
                    (kind,item_key,title,year,overview,poster,path,added_at,has_file,
                     catalog_source,external_id,extra_json)
                    values(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (kind,key,(page or {}).get("title") or x["title"],
                     (page or {}).get("year") or x["year"],
                     (page or {}).get("overview") or f"Локальная медиатека · {x['count']} видео",
                     (page or {}).get("poster") or None,
                     p,x["added"],1,"filesystem","",
                     json.dumps(extra,ensure_ascii=False)))
            state(con,f"Filesystem {kind}",True,len(entries),started=started)
        except Exception as e:
            state(con,f"Filesystem {kind}",False,error=str(e),started=started)
    con.commit()


def _norm_title(value):
    s=(value or "").casefold().replace("ё","е")
    s=re.sub(r"[^0-9a-zа-я]+"," ",s,flags=re.I)
    return re.sub(r"\s+"," ",s).strip()

def _merge_extra(raw, updates):
    try: data=json.loads(raw or "{}")
    except Exception: data={}
    data.update({k:v for k,v in updates.items() if v not in (None,"")})
    return json.dumps(data,ensure_ascii=False)

def _normalized_media_path(value):
    if not value:return ""
    p=Path(value)
    try:
        if p.suffix.lower() in VIDEO_EXTS:
            p=p.parent
        return str(p.resolve()).rstrip("/")
    except Exception:
        return str(p).rstrip("/")

def refresh_jellyfin_library(con):
    """Use Jellyfin's already-resolved metadata as the first localization
    source. This makes MediaHub and Jellyfin show the same Russian title when
    Jellyfin already knows it."""
    if not JELLYFIN_KEY:
        return
    started=time.monotonic()
    try:
        with httpx.Client(timeout=httpx.Timeout(16,connect=4),headers={"X-Emby-Token":JELLYFIN_KEY},trust_env=False) as c:
            r=c.get(JELLYFIN_URL+"/Items",params={
                "Recursive":"true",
                "IncludeItemTypes":"Movie,Series",
                "Fields":"Path,Overview,Genres,ProviderIds,CommunityRating,ProductionYear,OriginalTitle,Studios,DateCreated",
                "EnableImages":"true",
                "ImageTypeLimit":"1"
            })
            r.raise_for_status()
            items=(r.json() or {}).get("Items") or []
    except Exception as e:
        state(con,"Jellyfin metadata",False,error=str(e),started=started)
        return

    rows=con.execute("select * from library_cache where path is not null and path!=''").fetchall()
    by_path={_normalized_media_path(r["path"]):r for r in rows}
    updated=0
    for item in items:
        p=_normalized_media_path(item.get("Path"))
        if not p:continue
        row=by_path.get(p)
        if not row:
            # Movie paths can point one level below the cached folder.
            row=next((rr for rp,rr in by_path.items() if p.startswith(rp+"/") or rp.startswith(p+"/")),None)
        if not row:continue
        # Карточку, привязанную пользователем вручную, Jellyfin не переписывает.
        try:row_extra=json.loads(row["extra_json"] or "{}")
        except Exception:row_extra={}
        if row_extra.get("manualMetadata"):continue

        current=row["title"] or ""
        name=item.get("Name") or ""
        original=item.get("OriginalTitle") or current
        # Only replace the visible title if Jellyfin actually has a Russian one.
        title=name if has_cyrillic(name) else current
        poster_url=row["poster"]
        image_tags=item.get("ImageTags") or {}
        if image_tags.get("Primary") and item.get("Id"):
            poster_url=f"/api/jellyfin-image/{item['Id']}"
        studios=item.get("Studios") or []
        studio=(studios[0].get("Name") if studios and isinstance(studios[0],dict) else "") or ""
        extra=_merge_extra(row["extra_json"],{
            "originalTitle":original,
            "localizedTitle":title if has_cyrillic(title) else "",
            "genres":item.get("Genres") or [],
            "rating":item.get("CommunityRating"),
            "studio":studio,
            "jellyfinId":item.get("Id"),
            "providerIds":item.get("ProviderIds") or {},
        })
        con.execute("""update library_cache set title=?,year=?,overview=?,poster=?,extra_json=?
                       where kind=? and item_key=?""",
                    (title,str(item.get("ProductionYear") or row["year"] or ""),
                     item.get("Overview") or row["overview"] or "",poster_url,extra,
                     row["kind"],row["item_key"]))
        updated+=1
    state(con,"Jellyfin metadata",True,updated,started=started)
    con.commit()

def _tmdb_detail(client,media,tmdb_id):
    r=external_request(client,"GET",f"https://api.themoviedb.org/3/{media}/{tmdb_id}",params=tmdb_auth_params({"language":"ru-RU"}),headers=tmdb_auth_headers(),retries=3)
    return r.json()

def _tmdb_find_ru(client,kind,title,year="",tmdb_id=None):
    if not TMDB_KEY:return None
    media="movie" if kind=="movies" else "tv"
    if tmdb_id:
        try:
            d=_tmdb_detail(client,media,tmdb_id)
            vis=d.get("title") or d.get("name") or ""
            if has_cyrillic(vis):return d
        except Exception:pass
    params=tmdb_auth_params({"language":"ru-RU","query":title,"include_adult":"false","page":1})
    if year:
        params["year" if media=="movie" else "first_air_date_year"]=year
    try:
        r=external_request(client,"GET",f"https://api.themoviedb.org/3/search/{media}",params=params,headers=tmdb_auth_headers(),retries=3)
        results=r.json().get("results") or []
    except Exception:return None
    if not results:return None
    q=_norm_title(title)
    def sc(x):
        candidates=[x.get("original_title"),x.get("original_name"),x.get("title"),x.get("name")]
        return max([SequenceMatcher(None,q,_norm_title(v)).ratio() for v in candidates if v] or [0])
    results.sort(key=sc,reverse=True)
    for x in results[:5]:
        vis=x.get("title") or x.get("name") or ""
        if not has_cyrillic(vis):continue
        try:return _tmdb_detail(client,media,x.get("id"))
        except Exception:return x
    return None

def refresh_tmdb_library_localization(con):
    """Fallback Russian localization for library rows not localized by Jellyfin."""
    if not TMDB_KEY:
        state(con,"TMDB library RU",False,error="TMDB_API_KEY not configured")
        return
    started=time.monotonic();updated=0
    rows=con.execute("select * from library_cache order by added_at desc limit 250").fetchall()
    with external_client(20) as client:
        for row in rows:
            current=row["title"] or ""
            if has_cyrillic(current):continue
            try:extra=json.loads(row["extra_json"] or "{}")
            except Exception:extra={}
            # Данные, подставленные пользователем со страницы-источника, не трогаем.
            if extra.get("manualMetadata"):continue
            query=extra.get("originalTitle") or current
            tmdb_id=None
            if row["kind"]=="movies" and row["catalog_source"]=="radarr" and str(row["external_id"] or "").isdigit():
                tmdb_id=int(row["external_id"])
            d=_tmdb_find_ru(client,row["kind"],query,str(row["year"] or ""),tmdb_id)
            if not d:continue
            title=d.get("title") or d.get("name") or ""
            if not has_cyrillic(title):continue
            genres=[g.get("name") for g in (d.get("genres") or []) if isinstance(g,dict) and g.get("name")]
            companies=d.get("production_companies") or []
            networks=d.get("networks") or []
            studio=(companies[0].get("name") if companies else "") or (networks[0].get("name") if networks else "") or ""
            poster_url=("https://image.tmdb.org/t/p/w500"+d["poster_path"]) if d.get("poster_path") else row["poster"]
            newextra=_merge_extra(row["extra_json"],{
                "originalTitle":extra.get("originalTitle") or current,
                "localizedTitle":title,
                "genres":genres,
                "rating":d.get("vote_average"),
                "runtime":d.get("runtime") or ((d.get("episode_run_time") or [0])[0] if d.get("episode_run_time") else 0),
                "status":d.get("status") or "",
                "studio":studio,
                "tmdbId":d.get("id"),
            })
            con.execute("""update library_cache set title=?,year=?,overview=?,poster=?,extra_json=?
                           where kind=? and item_key=?""",
                        (title,str((d.get("release_date") or d.get("first_air_date") or "")[:4] or row["year"] or ""),
                         d.get("overview") or row["overview"] or "",poster_url,newextra,row["kind"],row["item_key"]))
            updated+=1
    state(con,"TMDB library RU",True,updated,started=started);con.commit()

def _store_external(con,source,key,title,year,overview,poster,url,extra):
    con.execute("""insert or replace into external_discovery
      (source,item_key,title,year,overview,poster,url,added_at,extra_json)
      values(?,?,?,?,?,?,?,?,?)""",
      (source,key,title,year,overview,poster,url,now(),json.dumps(extra,ensure_ascii=False)))

def refresh_tvmaze_series(con):
    if not TVMAZE_ENABLED:
        state(con,"TVmaze series",True,0);return
    if not TMDB_KEY:
        state(con,"TVmaze series",True,0);return
    # TMDB is the primary source. If the TV new shelf already exists, avoid a
    # redundant TVmaze call that can add seconds of SSL/network delay. TVmaze
    # is only a fallback when TMDB could not provide fresh series.
    row=con.execute("select count(*) as n from catalog where kind='tv' and mode='new'").fetchone()
    if row and int(row["n"] or 0)>0:
        state(con,"TVmaze series",True,0);return
    started=time.monotonic();today=datetime.now(timezone.utc).date();shows={}
    try:
        with external_client(18) as c:
            for delta in range(0,7):
                day=today+timedelta(days=delta)
                r=external_request(c,"GET","https://api.tvmaze.com/schedule/web",params={"date":day.isoformat()},retries=3)
                for ep in r.json() or []:
                    show=ep.get("_embedded",{}).get("show") or ep.get("show") or {}
                    sid=show.get("id")
                    if not sid:continue
                    premiered=show.get("premiered") or ""
                    # Prioritize new series and first seasons.
                    season=int(ep.get("season") or 0)
                    if premiered:
                        try:newish=(today-datetime.fromisoformat(premiered).date()).days<=180
                        except Exception:newish=False
                    else:newish=False
                    if not newish and season not in (0,1):continue
                    shows[sid]=show
        con.execute("delete from external_discovery where source='TVmazeSeries'")
        count=0
        with external_client(20) as tc:
            for show in list(shows.values())[:35]:
                name=show.get("name") or ""
                year=(show.get("premiered") or "")[:4]
                d=_tmdb_find_ru(tc,"tv",name,year)
                if not d:continue
                title=d.get("name") or ""
                if not has_cyrillic(title):continue
                poster_url=("https://image.tmdb.org/t/p/w500"+d["poster_path"]) if d.get("poster_path") else ((show.get("image") or {}).get("medium"))
                key=f"tvmaze-{show.get('id')}"
                _store_external(con,"TVmazeSeries",key,title,(d.get("first_air_date") or year)[:4],d.get("overview") or "",poster_url,show.get("url") or "",{
                    "source":"TVmaze","originalTitle":name,"tmdbId":d.get("id"),"catalog":"external","kind":"tv"
                })
                count+=1
                if count>=20:break
        state(con,"TVmaze series",True,count,started=started);con.commit()
    except Exception as e:
        state(con,"TVmaze series",False,error=str(e),started=started);con.commit()

def refresh_kinopoisk_movies(con):
    if not KINOPOISK_API_KEY:
        con.execute("delete from source_state where source='Kinopoisk premieres'")
        con.commit();return
    started=time.monotonic();today=datetime.now(timezone.utc).date()
    months=[today,(today.replace(day=28)+timedelta(days=4)).replace(day=1)]
    month_names=["JANUARY","FEBRUARY","MARCH","APRIL","MAY","JUNE","JULY","AUGUST","SEPTEMBER","OCTOBER","NOVEMBER","DECEMBER"]
    try:
        con.execute("delete from external_discovery where source='KinopoiskMovies'")
        count=0;seen=set()
        with external_client(20,headers={"X-API-KEY":KINOPOISK_API_KEY}) as c:
            for dt in months:
                r=external_request(c,"GET",KINOPOISK_API_URL+"/api/v2.2/films/premieres",params={"year":dt.year,"month":month_names[dt.month-1]},retries=3)
                for x in (r.json() or {}).get("items") or []:
                    kid=x.get("kinopoiskId")
                    if not kid or kid in seen:continue
                    seen.add(kid)
                    title=x.get("nameRu") or ""
                    if not has_cyrillic(title):continue
                    _store_external(con,"KinopoiskMovies",f"kp-{kid}",title,str(x.get("year") or ""),"Премьера по данным КиноПоиск.",x.get("posterUrlPreview") or x.get("posterUrl") or "",f"https://www.kinopoisk.ru/film/{kid}/",{
                        "source":"КиноПоиск","originalTitle":x.get("nameEn") or "","kinopoiskId":kid,"catalog":"external","kind":"movies","premiereRu":x.get("premiereRu") or ""
                    })
                    count+=1
                    if count>=24:break
                if count>=24:break
        state(con,"Kinopoisk premieres",True,count,started=started);con.commit()
    except Exception as e:
        state(con,"Kinopoisk premieres",False,error=str(e),started=started);con.commit()

def refresh_local(con):
    # Radarr
    started=time.monotonic()
    try:
        rows=get_json(RADARR_URL,RADARR_KEY,"/api/v3/movie",8) if RADARR_KEY else []
        # Чистим только строки Radarr: локальные папки и карточки, привязанные
        # вручную, должны пережить синхронизацию.
        con.execute("delete from library_cache where kind='movies' and catalog_source='radarr'")
        for x in rows:
            extra={
                "genres":x.get("genres") or [],
                "runtime":x.get("runtime") or 0,
                "rating":((x.get("ratings") or {}).get("value")
                          or ((x.get("ratings") or {}).get("imdb") or {}).get("value")),
                "status":x.get("status") or "",
                "studio":x.get("studio") or "",
                "catalog":"radarr",
                "providerIds":{"Tmdb":str(x.get("tmdbId") or ""),"Imdb":str(x.get("imdbId") or "")},
            }
            con.execute("""insert or replace into library_cache
              (kind,item_key,title,year,overview,poster,path,added_at,has_file,
               catalog_source,external_id,extra_json)
              values(?,?,?,?,?,?,?,?,?,?,?,?)""",
              ("movies",str(x.get("id")),x.get("title") or "",str(x.get("year") or ""),
               x.get("overview") or "",poster(x.get("images")),x.get("path") or "",
               x.get("added") or "",1 if x.get("hasFile") else 0,"radarr",
               str(x.get("tmdbId") or ""),json.dumps(extra,ensure_ascii=False)))
        state(con,"Radarr library",True,len(rows),started=started)
    except Exception as e:
        state(con,"Radarr library",False,error=str(e),started=started)

    # Sonarr
    started=time.monotonic()
    try:
        rows=get_json(SONARR_URL,SONARR_KEY,"/api/v3/series",8) if SONARR_KEY else []
        # Точную сводку по сезонам (учитывает файлы на диске) переносим в новые
        # строки: иначе после синхронизации бейджи снова показывали данные
        # Sonarr, который мог ещё не импортировать свежие серии.
        prev_summary={}
        for r in con.execute("""select item_key,extra_json from library_cache
                                where kind in ('tv','anime') and catalog_source='sonarr'""").fetchall():
            try:
                summary=(json.loads(r["extra_json"] or "{}") or {}).get("seasonsSummary")
            except Exception:
                summary=None
            if summary:
                prev_summary[str(r["item_key"])]=summary
        con.execute("delete from library_cache where kind in ('tv','anime') and catalog_source='sonarr'")
        for x in rows:
            kind="anime" if x.get("seriesType")=="anime" or str(x.get("path") or "").startswith(ANIME_ROOT) else "tv"
            extra={
                "genres":x.get("genres") or [],
                "runtime":x.get("runtime") or 0,
                "rating":((x.get("ratings") or {}).get("value")
                          or ((x.get("ratings") or {}).get("tvdb") or {}).get("value")),
                "status":x.get("status") or "",
                "network":x.get("network") or "",
                "studio":x.get("network") or "",
                "catalog":"sonarr",
                "providerIds":{"Tvdb":str(x.get("tvdbId") or ""),"Tmdb":str(x.get("tmdbId") or ""),"Imdb":str(x.get("imdbId") or "")},
                "seasons":x.get("seasons") or [],
            }
            carried=prev_summary.get(str(x.get("id")))
            if carried:
                extra["seasonsSummary"]=carried
            con.execute("""insert or replace into library_cache
              (kind,item_key,title,year,overview,poster,path,added_at,has_file,
               catalog_source,external_id,extra_json)
              values(?,?,?,?,?,?,?,?,?,?,?,?)""",
              (kind,str(x.get("id")),x.get("title") or "",str(x.get("year") or ""),
               x.get("overview") or "",poster(x.get("images")),x.get("path") or "",
               x.get("added") or "",1 if (x.get("statistics") or {}).get("episodeFileCount") else 0,
               "sonarr",str(x.get("tvdbId") or ""),json.dumps(extra,ensure_ascii=False)))
        state(con,"Sonarr library",True,len(rows),started=started)
    except Exception as e:
        state(con,"Sonarr library",False,error=str(e),started=started)

    merge_filesystem_library(con)
    refresh_jellyfin_library(con)
    refresh_tmdb_library_localization(con)
    try:apply_manual_library(con)
    except Exception as e:state(con,"Manual metadata",False,error=str(e))
    con.execute("insert or replace into meta(key,value) values('last_local_refresh',?)",(now(),))
    con.commit()

def tmdb_rows(client,kind,mode):
    """Resilient RU discovery for movies/TV.

    New releases use /discover with a recent date window. This avoids empty
    regional /now_playing lists and gives MediaHub a stable definition of
    "новинки" on every server.
    """
    if not TMDB_KEY:
        return []
    today=datetime.now(timezone.utc).date()
    if kind=="movies" and mode=="new":
        attempts=[
            ("/discover/movie",{"region":"RU","include_video":"false","sort_by":"primary_release_date.desc",
                "primary_release_date.gte":(today-timedelta(days=120)).isoformat(),"primary_release_date.lte":today.isoformat(),"vote_count.gte":"1"}),
            ("/movie/now_playing",{"region":"RU"}),("/movie/now_playing",{})]
    elif kind=="movies" and mode=="popular":
        attempts=[("/movie/popular",{"region":"RU"}),("/movie/popular",{})]
    elif kind=="movies":
        attempts=[("/trending/movie/day",{})]
    elif kind=="tv" and mode=="new":
        attempts=[
            ("/discover/tv",{"sort_by":"first_air_date.desc","first_air_date.gte":(today-timedelta(days=150)).isoformat(),
                "first_air_date.lte":today.isoformat(),"vote_count.gte":"1"}),
            ("/tv/on_the_air",{})]
    elif kind=="tv" and mode=="popular":
        attempts=[("/tv/popular",{})]
    else:
        attempts=[("/trending/tv/day",{})]

    out=[]; seen=set(); rank=0; last_error=None
    for path,base in attempts:
        before=len(out)
        for page in range(1,4):
            params={"language":"ru-RU","page":page,"include_adult":"false"};params.update(base)
            try:
                r=external_request(client,"GET","https://api.themoviedb.org/3"+path,params=tmdb_auth_params(params),headers=tmdb_auth_headers(),retries=3)
            except Exception as e:
                last_error=e;break
            for x in r.json().get("results",[]):
                title=x.get("title") or x.get("name") or "";mid=x.get("id")
                if not title or not mid or mid in seen:continue
                seen.add(mid);rank+=1
                extra={"rating":x.get("vote_average"),"genres":[],"catalog":"tmdb","localizedTitle":title,
                       "originalTitle":x.get("original_title") or x.get("original_name") or "","feed":mode,"locale":"ru-RU"}
                out.append((kind,mode,rank,title,(x.get("release_date") or x.get("first_air_date") or "")[:4],x.get("overview") or "",
                            "https://image.tmdb.org/t/p/w500"+x["poster_path"] if x.get("poster_path") else None,mid,"tmdb",json.dumps(extra,ensure_ascii=False)))
                if rank>=36:return out
        if len(out)>before:return out
    if not out and last_error:raise last_error
    return out

def tmdb_anime_rows(client,mode):
    if not TMDB_KEY:return []
    today=datetime.now(timezone.utc).date()
    base={"with_genres":"16","with_original_language":"ja","include_adult":"false"}
    if mode=="new":
        base.update({"sort_by":"first_air_date.desc","first_air_date.gte":(today-timedelta(days=210)).isoformat(),
                     "first_air_date.lte":today.isoformat(),"vote_count.gte":"1"})
    elif mode=="upcoming":
        base.update({"sort_by":"popularity.desc","first_air_date.gte":today.isoformat()})
    else:
        base["sort_by"]="popularity.desc"
    attempts=[base]
    # If language metadata is incomplete, retain the animation filter as a
    # fallback instead of showing no anime shelf at all.
    if mode in {"new","popular"}:
        fallback=dict(base);fallback.pop("with_original_language",None);attempts.append(fallback)
    out=[];seen=set();rank=0;last_error=None
    for opts in attempts:
        before=len(out)
        for page in range(1,4):
            params={"language":"ru-RU","page":page};params.update(opts)
            try:
                r=external_request(client,"GET","https://api.themoviedb.org/3/discover/tv",params=tmdb_auth_params(params),headers=tmdb_auth_headers(),retries=3)
            except Exception as e:
                last_error=e;break
            for x in r.json().get("results",[]):
                title=x.get("name") or "";mid=x.get("id")
                if not title or not mid or mid in seen:continue
                seen.add(mid);rank+=1
                extra={"rating":x.get("vote_average"),"genres":[],"catalog":"tmdb","localizedTitle":title,
                       "originalTitle":x.get("original_name") or "","anime":True,"locale":"ru-RU","feed":mode}
                out.append(("anime",mode,rank,title,(x.get("first_air_date") or "")[:4],x.get("overview") or "",
                            "https://image.tmdb.org/t/p/w500"+x["poster_path"] if x.get("poster_path") else None,mid,"tmdb",json.dumps(extra,ensure_ascii=False)))
                if rank>=36:return out
        if len(out)>before:return out
    if not out and last_error:raise last_error
    return out

def strip_html(text):
    return re.sub(r"<[^>]+>"," ",text or "").replace("&quot;",'"').replace("&#039;","'").strip()

def anilist_rows(client,mode):
    if mode=="popular":
        args="type:ANIME,isAdult:false,sort:TRENDING_DESC"
    elif mode=="new":
        args="type:ANIME,isAdult:false,status:RELEASING,sort:TRENDING_DESC"
    else:
        args="type:ANIME,isAdult:false,status:NOT_YET_RELEASED,sort:POPULARITY_DESC"
    query=f"""
    query {{
      Page(page:1,perPage:30) {{
        media({args}) {{
          id idMal
          title {{ romaji english native }}
          description seasonYear format status episodes duration
          averageScore popularity genres
          coverImage {{ large extraLarge }}
          studios(isMain:true) {{ nodes {{ name }} }}
          nextAiringEpisode {{ episode timeUntilAiring }}
        }}
      }}
    }}
    """
    r=external_request(client,"POST",ANILIST_URL,json_body={"query":query},
                  headers={"Accept":"application/json","Content-Type":"application/json"},retries=3)
    rows=((r.json().get("data") or {}).get("Page") or {}).get("media") or []
    out=[]
    for i,x in enumerate(rows,1):
        t=x.get("title") or {}
        studio=((x.get("studios") or {}).get("nodes") or [{}])[0].get("name","")
        extra={
            "genres":x.get("genres") or [],
            "runtime":x.get("duration") or 0,
            "rating":(float(x.get("averageScore") or 0)/10.0) if x.get("averageScore") else None,
            "status":x.get("status") or "",
            "studio":studio,
            "catalog":"anilist",
            "originalTitle":t.get("romaji") or t.get("native") or "",
            "episodes":x.get("episodes"),
            "format":x.get("format"),
            "popularity":x.get("popularity"),
            "idMal":x.get("idMal"),
            "nextAiringEpisode":x.get("nextAiringEpisode"),
        }
        out.append(("anime",mode,i,
                    t.get("english") or t.get("romaji") or t.get("native") or "",
                    str(x.get("seasonYear") or ""),strip_html(x.get("description")),
                    ((x.get("coverImage") or {}).get("extraLarge")
                     or (x.get("coverImage") or {}).get("large")),
                    x.get("id"),"anilist",json.dumps(extra,ensure_ascii=False)))
    return out

def refresh_catalog(con):
    # v20.2: the home screen is RU-only. Old releases could leave AniList
    # English rows in SQLite forever, so remove those legacy catalog cards
    # before rebuilding Russian TMDB shelves. AniList is still queried below
    # for aliases/metadata, but it is not a visible home recommendation source.
    con.execute("delete from catalog where catalog_source='anilist'")
    with external_client(22) as client:
        if TMDB_KEY:
            for kind in ("movies","tv"):
                for mode in ("new","popular"):
                    started=time.monotonic()
                    try:
                        rows=tmdb_rows(client,kind,mode)
                        if rows:
                            con.execute("delete from catalog where kind=? and mode=?",(kind,mode))
                            con.executemany("""insert or replace into catalog
                                (kind,mode,rank,title,year,overview,poster,external_id,catalog_source,extra_json)
                                values(?,?,?,?,?,?,?,?,?,?)""",rows)
                            state(con,f"TMDB {kind} {mode}",True,len(rows),started=started)
                        else:
                            state(con,f"TMDB {kind} {mode}",False,error="TMDB вернул 0 карточек; сохранён предыдущий кэш",started=started)
                    except Exception as e:
                        state(con,f"TMDB {kind} {mode}",False,error=str(e),started=started)

            for mode in ("new","popular"):
                started=time.monotonic()
                try:
                    rows=tmdb_anime_rows(client,mode)
                    if rows:
                        con.execute("delete from catalog where kind='anime' and mode=?",(mode,))
                        con.executemany("""insert or replace into catalog
                            (kind,mode,rank,title,year,overview,poster,external_id,catalog_source,extra_json)
                            values(?,?,?,?,?,?,?,?,?,?)""",rows)
                        state(con,f"TMDB anime {mode}",True,len(rows),started=started)
                    else:
                        state(con,f"TMDB anime {mode}",False,error="TMDB вернул 0 карточек; сохранён предыдущий кэш",started=started)
                except Exception as e:
                    state(con,f"TMDB anime {mode}",False,error=str(e),started=started)

        # AniList remains available for detailed metadata and title aliases.
        started=time.monotonic()
        try:
            rows=anilist_rows(client,"popular")
            state(con,"AniList metadata",True,len(rows),started=started)
        except Exception as e:
            state(con,"AniList metadata",False,error=str(e),started=started)

        # Last-resort migration guard for caches created by older MediaHub builds.
        # We never replace an English recommendation with guessed transliteration:
        # if a real Russian localization is absent, the card is omitted.
        for old in con.execute("select rowid,title,catalog_source from catalog").fetchall():
            if (old["catalog_source"] or "")!="tmdb" and not has_cyrillic(old["title"] or ""):
                con.execute("delete from catalog where rowid=?",(old["rowid"],))
        con.commit()

def refresh_aniliberty(con):
    started=time.monotonic()
    try:
        with external_client(18,headers={"User-Agent":"Mozilla/5.0 MediaHub/20.6"}) as c:
            r=external_request(c,"GET",ANILIBERTY_URL,retries=3)
        soup=BeautifulSoup(r.text,"html.parser")
        found=[]
        seen=set()
        for a in soup.find_all("a",href=True):
            href=a.get("href") or ""
            text=" ".join(a.stripped_strings).strip()
            if not text or len(text)<3 or len(text)>160:
                continue
            low=href.lower()
            if not any(k in low for k in ("release","anime","catalog")):
                continue
            # Skip UI/navigation labels.
            if text.casefold() in {"релизы","расписание","приложения","поддержать проект","торренты","rss"}:
                continue
            key=hashlib.sha1((href+"|"+text).encode("utf-8")).hexdigest()[:20]
            if key in seen:
                continue
            seen.add(key)
            img=a.find("img")
            src=""
            if img:
                src=img.get("src") or img.get("data-src") or ""
                if src.startswith("/"):
                    src="https://aniliberty.top"+src
            url=href
            if url.startswith("/"):
                url="https://aniliberty.top"+url
            found.append((key,text,src,url))
            if len(found)>=24:
                break

        if found:
            con.execute("delete from external_discovery where source='AniLiberty'")
            for i,(key,title,img,url) in enumerate(found):
                con.execute("""insert or replace into external_discovery
                  (source,item_key,title,year,overview,poster,url,added_at,extra_json)
                  values(?,?,?,?,?,?,?,?,?)""",
                  ("AniLiberty",key,title,"","Обнаружено в публичном каталоге AniLiberty.",
                   img,url,now(),json.dumps({"rank":i+1},ensure_ascii=False)))
            state(con,"AniLiberty discovery",True,len(found),started=started)
        else:
            state(con,"AniLiberty discovery",False,error="Источник ответил, но релизы не распознаны; сохранён предыдущий кэш",started=started)
    except Exception as e:
        state(con,"AniLiberty discovery",False,error=str(e),started=started)
    con.commit()

def get_bindings(con):
    out={}
    for kind in CATEGORIES:
        rows=con.execute("select indexer_id,enabled from bindings where kind=?",(kind,)).fetchall()
        out[kind]={
            "configured":bool(rows),
            "ids":[int(r["indexer_id"]) for r in rows if int(r["enabled"] or 0)==1],
        }
    return out

def _indexer_category_ids(indexer):
    caps=(indexer or {}).get("capabilities") or {}
    cats=caps.get("categories") or []
    out=[]
    def walk(v):
        if isinstance(v,dict):
            if "id" in v:
                try:out.append(int(v.get("id")))
                except Exception:pass
            for key in ("subCategories","subcategories","children"):
                if key in v:walk(v.get(key))
        elif isinstance(v,list):
            for x in v:walk(x)
    walk(cats)
    return out

def indexer_supports_kind(indexer,kind):
    ids=_indexer_category_ids(indexer)
    if ids:
        if kind=="movies":return any(2000<=x<3000 for x in ids)
        if kind=="games":return any(4000<=x<5000 for x in ids)
        if kind=="anime":return 5070 in ids
        if kind=="tv":return any(5000<=x<6000 and x!=5070 for x in ids)
    name=str((indexer or {}).get("name") or "").casefold()
    if kind=="anime":return any(k in name for k in ("anime","anidub","anilibr","belka"))
    if kind=="games":return any(k in name for k in ("game","игр"))
    # Unknown/general torrent trackers are useful for films and TV, but should
    # not be sprayed into Anime/Games automatically.
    return kind in {"movies","tv"}

def _release_category_ids(row):
    out=[]
    for c in row.get("categories") or []:
        try:
            if isinstance(c,dict):out.append(int(c.get("id")))
            else:out.append(int(c))
        except Exception:pass
    return out

def release_matches_kind(row,kind):
    ids=_release_category_ids(row)
    if not ids:
        return True
    if kind=="movies":
        return any(2000<=x<3000 for x in ids)
    if kind=="games":
        return any(4000<=x<5000 for x in ids)
    if kind=="anime":
        return any(x==5070 for x in ids) or any("anime" in str(c.get("name") or "").lower() for c in (row.get("categories") or []) if isinstance(c,dict))
    if kind=="tv":
        # Anime is a TV subcategory in Newznab. Keep it out of ordinary TV
        # when the result is explicitly tagged only as Anime.
        tv=[x for x in ids if 5000<=x<6000]
        return bool(tv) and not (set(tv)=={5070})
    return True

def provider_fetch(indexer,kind):
    idx=int(indexer["id"])
    name=indexer.get("name") or str(idx)
    started=time.monotonic()
    try:
        with httpx.Client(timeout=httpx.Timeout(14,connect=5),
                          headers={"X-Api-Key":PROWLARR_KEY},trust_env=False) as c:
            params=[("query",""),("type","search"),("categories",str(CATEGORIES[kind])),
                    ("indexerIds",str(idx)),("limit","35"),("offset","0")]
            r=c.get(PROWLARR_URL+"/api/v1/search",params=params)
            if r.status_code==400:
                # Several torrent indexers require a non-empty q. They remain
                # perfectly usable for normal MediaHub search, but cannot
                # provide a generic "new releases" feed. Do not mark them dead.
                return {"ok":True,"skipped":True,"kind":kind,"id":idx,"name":name,"rows":[],
                        "duration":int((time.monotonic()-started)*1000),
                        "error":"Лента без поискового запроса не поддерживается"}
            r.raise_for_status()
            rows=[x for x in (r.json() or []) if release_matches_kind(x,kind) and (_release_category_ids(x) or kind in {"movies","tv"} or indexer_supports_kind(indexer,kind))]
        return {"ok":True,"skipped":False,"kind":kind,"id":idx,"name":name,"rows":rows,
                "duration":int((time.monotonic()-started)*1000),"error":""}
    except Exception as e:
        return {"ok":False,"skipped":False,"kind":kind,"id":idx,"name":name,"rows":[],
                "duration":int((time.monotonic()-started)*1000),"error":str(e)}

def refresh_providers(con):
    if not PROWLARR_KEY:
        state(con,"Prowlarr indexers",False,error="API key not found")
        con.commit()
        return
    started=time.monotonic()
    try:
        with httpx.Client(timeout=httpx.Timeout(8,connect=4),
                          headers={"X-Api-Key":PROWLARR_KEY},trust_env=False) as c:
            r=c.get(PROWLARR_URL+"/api/v1/indexer")
            r.raise_for_status()
            indexers=[x for x in r.json() if x.get("enable",True)]
        state(con,"Prowlarr indexers",True,len(indexers),started=started)
    except Exception as e:
        state(con,"Prowlarr indexers",False,error=str(e),started=started)
        con.commit()
        return

    bindings=get_bindings(con)
    tasks=[]
    for kind in CATEGORIES:
        cfg=bindings[kind]
        if cfg["configured"]:
            use=[x for x in indexers if int(x.get("id",0)) in cfg["ids"]]
        else:
            use=[x for x in indexers if indexer_supports_kind(x,kind)]
        for x in use:
            tasks.append((x,kind))

    results=[]
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures=[ex.submit(provider_fetch,x,k) for x,k in tasks]
        for f in as_completed(futures):
            results.append(f.result())

    usable=sum(len(res.get("rows") or []) for res in results if res.get("ok") and not res.get("skipped"))
    if usable:
        con.execute("delete from provider_feed")
    for res in results:
        source=f"Prowlarr {res['name']} {res['kind']}"
        if res.get("skipped"):
            con.execute("delete from source_state where source=?",(source,))
            continue
        state(con,source,res["ok"],len(res["rows"]),res["error"])
        if not res["ok"] or not usable:
            continue
        for x in res["rows"]:
            guid=str(x.get("guid") or x.get("downloadUrl") or x.get("title") or "")
            if not guid:
                continue
            con.execute("""insert or replace into provider_feed
              (kind,indexer_id,indexer,title,size,seeders,peers,published_at,guid,download_url)
              values(?,?,?,?,?,?,?,?,?,?)""",
              (res["kind"],res["id"],x.get("indexer") or res["name"],x.get("title") or "",
               int(x.get("size") or 0),int(x.get("seeders") or 0),int(x.get("peers") or 0),
               x.get("publishDate") or "",guid,x.get("downloadUrl") or ""))
    con.commit()

mode="local" if "--local" in sys.argv else "full"

with db() as con:
    refresh_local(con)
    if mode=="full":
        # Remove stale diagnostic cards left by older MediaHub versions.
        con.execute("delete from source_state where source in ('AniList new','AniList popular','AniList upcoming','TMDB movies trending','TMDB tv trending','TMDB anime trending','TMDB anime upcoming')")
        con.execute("delete from catalog where mode in ('trending','upcoming')")
        con.commit()
        refresh_catalog(con)
        refresh_aniliberty(con)
        refresh_tvmaze_series(con)
        refresh_kinopoisk_movies(con)
        refresh_providers(con)
        con.execute("insert or replace into meta(key,value) values('last_refresh',?)",(now(),))
        con.commit()

# Ask the running MediaHub process to warm release availability after a full
# discovery refresh. This is fire-and-forget; cache refresh stays successful
# even if the web service is restarting.
if mode=="full":
    try:
        httpx.post("http://127.0.0.1:8090/api/releases/prefetch-home",timeout=3.0)
    except Exception:
        pass

print(f"MediaHub cache refresh complete: {mode}")

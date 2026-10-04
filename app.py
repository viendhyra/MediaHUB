
import os, re, shutil, subprocess, sqlite3, xml.etree.ElementTree as ET, json, hashlib, asyncio, sys, ipaddress, time
import threading, secrets, hmac
from contextvars import ContextVar
from urllib.parse import quote
from pathlib import Path
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
import httpx
from fastapi import FastAPI, Form, Query, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, Response, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
from system_setup import component_status as setup_component_status, host_info as setup_host_info, read_state as setup_read_state, log_tail as setup_log_tail, RECOMMENDED as SETUP_RECOMMENDED, storage_overview as setup_storage_overview, queue_storage_request

BASE_DIR=Path(__file__).resolve().parent
VERSION_FILE=BASE_DIR/"VERSION.txt"
try:
    APP_VERSION=VERSION_FILE.read_text(encoding="utf-8").strip() or "unknown"
except Exception:
    APP_VERSION="unknown"
SETUP_SCRIPT=BASE_DIR/"system_setup.py"

RADARR_URL=os.getenv("RADARR_URL","http://127.0.0.1:7878")
SONARR_URL=os.getenv("SONARR_URL","http://127.0.0.1:8989")
PROWLARR_URL=os.getenv("PROWLARR_URL","http://127.0.0.1:9696")
QBIT_URL=os.getenv("QBIT_URL","http://127.0.0.1:8080")
JELLYFIN_URL=os.getenv("JELLYFIN_URL","http://127.0.0.1:8096")

MEDIA_ROOT=Path(os.getenv("MEDIA_ROOT","/mnt/media"))
MOVIES_ROOT=Path(os.getenv("MOVIES_ROOT","/mnt/media/movies"))
TV_ROOT=Path(os.getenv("TV_ROOT","/mnt/media/tv"))
ANIME_ROOT=Path(os.getenv("ANIME_ROOT","/mnt/media/anime"))
INBOX_ROOT=Path(os.getenv("INBOX_ROOT","/mnt/media/inbox"))

QBIT_USER=os.getenv("QBIT_USER","")
QBIT_PASS=os.getenv("QBIT_PASS","")

# Persistent integration secrets. Environment variables are still supported,
# but MediaHub also keeps a root-only copy so a valid key is not lost when a
# service is restarted or a systemd EnvironmentFile is temporarily stale.
SECRETS_FILE=Path("/var/lib/mediahub/secrets.json")
ENV_FILE=Path("/etc/mediahub.env")

def _read_env_file_values():
    out={}
    try:
        for raw in ENV_FILE.read_text(encoding="utf-8",errors="replace").splitlines():
            line=raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k,v=line.split("=",1)
            out[k.strip()]=v.strip()
    except Exception:
        pass
    return out

def _read_secret_values():
    try:
        data=json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data,dict) else {}
    except Exception:
        return {}

def _persist_secret_values(values):
    values={str(k):str(v) for k,v in (values or {}).items() if v is not None}
    if not values:
        return
    SECRETS_FILE.parent.mkdir(parents=True,exist_ok=True)
    current=_read_secret_values()
    current.update(values)
    tmp=SECRETS_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(current,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(SECRETS_FILE)
    try: SECRETS_FILE.chmod(0o600)
    except Exception: pass

def _persistent_value(name,default=""):
    secret=str(_read_secret_values().get(name) or "").strip()
    if secret:
        return secret
    disk=str(_read_env_file_values().get(name) or "").strip()
    if disk:
        return disk
    return str(os.getenv(name,default) or "").strip()

TMDB_KEY=_persistent_value("TMDB_API_KEY")

def tmdb_auth_params(extra=None, credential=None):
    """Accept either TMDB v3 API Key or API Read Access Token in one field."""
    cred=(TMDB_KEY if credential is None else str(credential or "")).strip()
    params=dict(extra or {})
    if cred and len(cred) <= 64:
        params["api_key"]=cred
    return params

def tmdb_auth_headers(credential=None):
    cred=(TMDB_KEY if credential is None else str(credential or "")).strip()
    if cred and len(cred) > 64:
        return {"Authorization":f"Bearer {cred}","Accept":"application/json"}
    return {"Accept":"application/json"}

def tmdb_credential_type(credential=None):
    cred=(TMDB_KEY if credential is None else str(credential or "")).strip()
    if not cred:return ""
    return "Read Access Token" if len(cred)>64 else "v3 API Key"

def outbound_proxy():
    """Optional proxy used only for public Internet catalogs.

    MediaHub deliberately ignores inherited HTTP_PROXY/HTTPS_PROXY variables by
    default. A stale localhost proxy was able to make TMDB/AniList/TVmaze fail
    with Connection refused while local services still looked healthy.
    """
    return _persistent_value("MEDIAHUB_OUTBOUND_PROXY").strip()

def external_async_client(timeout=20.0):
    kwargs={
        "timeout":httpx.Timeout(timeout,connect=min(8.0,timeout)),
        "follow_redirects":True,
        "trust_env":False,
        "headers":{"User-Agent":f"MediaHub/{APP_VERSION}"},
        "limits":httpx.Limits(max_connections=3,max_keepalive_connections=2,keepalive_expiry=20.0),
    }
    proxy=outbound_proxy()
    if proxy:
        kwargs["proxy"]=proxy
    return httpx.AsyncClient(**kwargs)

async def external_request(client,method,url,*,params=None,headers=None,json_body=None,retries=3):
    """Resilient request for public catalogs.

    Retry transient network failures and 429/5xx responses.  Authentication
    errors are returned immediately so a bad key is not hidden behind retries.
    """
    last=None
    for attempt in range(max(1,retries)):
        try:
            r=await client.request(method,url,params=params,headers=headers,json=json_body)
            if r.status_code in {429,500,502,503,504} and attempt+1<retries:
                await asyncio.sleep(0.7*(attempt+1))
                continue
            r.raise_for_status()
            return r
        except (httpx.ConnectError,httpx.ConnectTimeout,httpx.ReadTimeout,httpx.ReadError,httpx.RemoteProtocolError) as e:
            last=e
            if attempt+1<retries:
                await asyncio.sleep(0.7*(attempt+1))
                continue
            raise
        except Exception as e:
            last=e
            raise
    if last:
        raise last
    raise RuntimeError("Внешний запрос не выполнен")
JELLYFIN_KEY=""  # Сохранены только старые API для совместимости; новый комплект автономен.
JELLYFIN_USER_ID=os.getenv("JELLYFIN_USER_ID","").strip()
JELLYFIN_PUBLIC_URL=os.getenv("JELLYFIN_PUBLIC_URL","").strip().rstrip("/")
CACHE_DB=Path(os.getenv("MEDIAHUB_CACHE_DB","/var/lib/mediahub/cache.db"))
CATEGORIES={"movies":2000,"tv":5000,"anime":5070,"games":4050}

# v20.7 browse catalog. The values are TMDB genre ids. Anime is backed by
# TMDB TV discovery with Animation + Japanese original language forced on every
# request, so the visible filters below are additional anime flavours.
BROWSE_GENRES={
    "movies":[
        (28,"Боевик"),(12,"Приключения"),(16,"Анимация"),(35,"Комедия"),
        (80,"Криминал"),(99,"Документальное"),(18,"Драма"),(10751,"Семейное"),
        (14,"Фэнтези"),(36,"История"),(27,"Ужасы"),(10402,"Музыка"),
        (9648,"Детектив"),(10749,"Мелодрама"),(878,"Фантастика"),(53,"Триллер"),
        (10752,"Военное"),(37,"Вестерн"),
    ],
    "tv":[
        (10759,"Экшен и приключения"),(16,"Анимация"),(35,"Комедия"),(80,"Криминал"),
        (99,"Документальное"),(18,"Драма"),(10751,"Семейное"),(10762,"Детское"),
        (9648,"Детектив"),(10764,"Реалити"),(10765,"Фантастика и фэнтези"),
        (10766,"Мыльная опера"),(10768,"Военное"),(37,"Вестерн"),
    ],
    "anime":[
        (10759,"Экшен и приключения"),(35,"Комедия"),(18,"Драма"),(10751,"Семейное"),
        (9648,"Мистика"),(10765,"Фантастика и фэнтези"),(10768,"Военное"),(37,"Вестерн"),
    ],
}

BROWSE_MOODS=[
    {"id":"light","label":"Лёгкое","emoji":"☀","hint":"без тяжёлого послевкусия"},
    {"id":"funny","label":"Смешное","emoji":"☺","hint":"комедийное настроение"},
    {"id":"tense","label":"Напряжённое","emoji":"⚡","hint":"держит в напряжении"},
    {"id":"dark","label":"Мрачное","emoji":"◐","hint":"темнее и загадочнее"},
    {"id":"family","label":"Семейное","emoji":"⌂","hint":"для совместного просмотра"},
    {"id":"romantic","label":"Романтичное","emoji":"♡","hint":"отношения и чувства"},
    {"id":"adventure","label":"Приключение","emoji":"✦","hint":"дорога, открытия, экшен"},
    {"id":"epic","label":"Эпичное","emoji":"♜","hint":"масштабное фэнтези и фантастика"},
    {"id":"smart","label":"Подумать","emoji":"◇","hint":"загадки, фантастика, детектив"},
    {"id":"cozy","label":"Уютное на вечер","emoji":"☕","hint":"спокойная вечерняя подборка"},
    {"id":"highrated","label":"Высокий рейтинг","emoji":"★","hint":"проверенное зрителями"},
]

GENRE_ALIASES={
    "боевик":"action","экшен":"action","приключ":"adventure","комед":"comedy","смеш":"comedy",
    "кримин":"crime","документ":"documentary","драм":"drama","семейн":"family","фэнтез":"fantasy",
    "истор":"history","ужас":"horror","хоррор":"horror","музык":"music","детектив":"mystery",
    "мистик":"mystery","романт":"romance","мелодрам":"romance","фантаст":"scifi","триллер":"thriller",
    "военн":"war","вестерн":"western","анимац":"animation","мульт":"animation",
}
GENRE_CANON_IDS={
    "movies":{"action":28,"adventure":12,"animation":16,"comedy":35,"crime":80,"documentary":99,"drama":18,"family":10751,"fantasy":14,"history":36,"horror":27,"music":10402,"mystery":9648,"romance":10749,"scifi":878,"thriller":53,"war":10752,"western":37},
    "tv":{"action":10759,"adventure":10759,"animation":16,"comedy":35,"crime":80,"documentary":99,"drama":18,"family":10751,"fantasy":10765,"horror":9648,"mystery":9648,"romance":10766,"scifi":10765,"thriller":9648,"war":10768,"western":37},
    "anime":{"action":10759,"adventure":10759,"animation":16,"comedy":35,"crime":80,"drama":18,"family":10751,"fantasy":10765,"horror":9648,"mystery":9648,"romance":18,"scifi":10765,"thriller":9648,"war":10768,"western":37},
}
MOOD_ALIASES={
    "light":["легк","ненапряж","простое","расслаб"],
    "funny":["смеш","весел","комед"],
    "tense":["напряж","динамич","остросюжет"],
    "dark":["мрач","темн","жутк","страш"],
    "family":["семейн","с детьми","для детей"],
    "romantic":["романт","про любовь","любовн"],
    "adventure":["приключ","путешеств"],
    "epic":["эпич","масштаб","героичес"],
    "smart":["подумать","умн","головолом","необычн","загад"],
    "cozy":["уют","на вечер","вечером","спокойн"],
    "highrated":["высокий рейтинг","лучшее","топ","хороший рейтинг"],
}

def _release_category_ids(row):
    out=[]
    for c in (row or {}).get("categories") or []:
        try:
            out.append(int(c.get("id")) if isinstance(c,dict) else int(c))
        except Exception:
            pass
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
        if any(x==5070 for x in ids):
            return True
        return any("anime" in str(c.get("name") or "").lower() for c in (row or {}).get("categories") or [] if isinstance(c,dict))
    if kind=="tv":
        tv=[x for x in ids if 5000<=x<6000]
        return bool(tv) and set(tv)!={5070}
    return True

VIDEO_EXTS={".mkv",".mp4",".avi",".m4v",".ts",".m2ts",".mts",".mov",".webm",
            ".vob",".iso",".img",".mpg",".mpeg",".m2v",".wmv",".flv",".divx",
            ".rmvb",".3gp",".ogm",".mk3d",".asf",".f4v",".wtv"}
# Blu-ray и DVD лежат структурой папок, а не одним файлом.
DISC_MARKERS={"bdmv","video_ts","avchd"}
SUB_EXTS={".srt",".ass",".ssa",".sub",".vtt"}

# Как формат ведёт себя в Jellyfin. «direct» — играет как есть, «transcode» —
# играет, но сервер перекодирует (нагрузка на процессор), «none» — не играет:
# образ диска нужно распаковать, а редкие контейнеры Плеер MediaHUB не открывает.
DIRECT_PLAY_EXTS={".mkv",".mp4",".m4v",".webm",".mov",".ts",".m2ts",".mts",".mk3d"}
TRANSCODE_EXTS={".avi",".mpg",".mpeg",".m2v",".wmv",".flv",".divx",".vob",
                ".asf",".f4v",".ogm",".3gp",".wtv"}
UNPLAYABLE_EXTS={".iso",".img",".rmvb"}
DISC_PSEUDO_EXT=".bdmv"   # папка BDMV / VIDEO_TS, а не файл

FORMAT_SUPPORT_NOTE={
    "direct":"",
    "transcode":"плеер перекодирует при просмотре",
    "none":"Плеер MediaHUB не воспроизводит этот формат",
}

def format_support(ext):
    """Формат файла и то, как он будет воспроизводиться.

    Возвращает (подпись формата, уровень поддержки). Уровень нужен и
    проводнику, и фильтру релизов, поэтому правило одно на весь проект.
    """
    ext=(ext or "").lower()
    if not ext.startswith("."):
        ext="."+ext if ext else ""
    if ext==DISC_PSEUDO_EXT:
        return "BDMV/DVD","transcode"
    label=ext[1:].upper() if ext else ""
    if ext in DIRECT_PLAY_EXTS:
        return label,"direct"
    if ext in TRANSCODE_EXTS:
        return label,"transcode"
    if ext in UNPLAYABLE_EXTS:
        return label,"none"
    return label,"unknown"

SUPPORT_RANK={"none":0,"unknown":1,"transcode":2,"direct":3}

def formats_summary(counts):
    """Свести форматы папки к подписи и худшему уровню поддержки внутри."""
    items=sorted(counts.items(),key=lambda kv:(-kv[1],kv[0]))
    labels=[];worst="direct";bad=[]
    for ext,_n in items:
        label,support=format_support(ext)
        if label and label not in labels:
            labels.append(label)
        if SUPPORT_RANK.get(support,1)<SUPPORT_RANK.get(worst,3):
            worst=support
        if support=="none" and label not in bad:
            bad.append(label)
    if not items:
        return "","unknown",[]
    return ", ".join(labels[:3])+("…" if len(labels)>3 else ""),worst,bad

MANAGED_SERVICES = {
    "qbittorrent": "qbittorrent-nox.service",  # заменяется настоящей службой в managed_services()
    "radarr": "radarr.service",
    "sonarr": "sonarr.service",
    "prowlarr": "prowlarr.service",
    "provider-cache": "mediahub-cache.timer",
    "local-cache": "mediahub-local-cache.timer",
    "organizer": "mediahub-organizer.timer",
    "media-vpn": "media-vpn.service",
}

STARTUP_SERVICES = [
    "qbittorrent-nox.service","radarr.service",
    "sonarr.service","prowlarr.service","mediahub-cache.timer",
    "mediahub-local-cache.timer","mediahub-organizer.timer"
]

def managed_services():
    """Службы для страницы «Сервисы»; qBittorrent мог быть установлен под другим именем."""
    return {**MANAGED_SERVICES,"qbittorrent":__import__("system_setup").qbit_unit()}

def startup_services():
    """Службы «Запустить стек» с настоящим именем службы qBittorrent."""
    qbit=__import__("system_setup").qbit_unit()
    return [qbit if svc=="qbittorrent-nox.service" else svc for svc in STARTUP_SERVICES]

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

app=FastAPI(title="MediaHub")

# v21: compress HTML/CSS/JSON on the wire. The shell alone is ~150 KB of text,
# so gzip removes most of the first-paint transfer on a LAN client.
app.add_middleware(GZipMiddleware,minimum_size=800)

AUTH_USER=ContextVar('mediahub_account',default=None)
AUTH_TOKEN=ContextVar('mediahub_token',default='')
AUTH_FAILURES={}


def auth_password(password,salt=None):
    """Хранить только соль и медленный PBKDF2-хеш пароля."""
    salt=salt or secrets.token_hex(16)
    return salt+':'+hashlib.pbkdf2_hmac('sha256',password.encode(),salt.encode(),200000).hex()


def auth_user_id():
    """Идентификатор аккаунта текущего запроса, включая рабочие потоки."""
    user=AUTH_USER.get()
    if not user:raise HTTPException(401,'Войдите в аккаунт')
    return user['id']


def auth_public(user):
    """Описание аккаунта без хеша пароля."""
    return {'id':user['id'],'login':user['login'],'isAdmin':bool(user['is_admin'])}


def auth_resolve(token):
    """Проверить сохранённую сессию; в базе хранится только хеш токена."""
    if not token or len(token)>128:return None
    with cache_db() as con:
        row=con.execute('select a.* from accounts a join account_sessions s on s.user_id=a.id where s.token_hash=? and s.expires>?',(hashlib.sha256(token.encode()).hexdigest(),time.time())).fetchone()
    return dict(row) if row else None


def auth_public_ip(value):
    """Адрес из интернета, а не из домашней сети, Tailscale или самого сервера."""
    try:addr=ipaddress.ip_address((value or '').strip())
    except ValueError:return False
    return not (addr.is_private or addr.is_loopback or addr.is_link_local or addr in ipaddress.ip_network('100.64.0.0/10'))


def auth_external(request):
    """Запрос пришёл снаружи: напрямую через проброс порта или через прокси в домашней сети."""
    if auth_public_ip(request.client.host if request.client else ''):return True
    # Прокси дописывает адрес клиента последним; начало заголовка может подделать сам клиент.
    return auth_public_ip(request.headers.get('x-forwarded-for','').split(',')[-1])


def auth_normalize_address(value):
    """Привести внешний адрес к виду http(s)://хост[:порт]; домен, IP и порт допускаются."""
    value=(value or '').strip().rstrip('/')
    if not value:return ''
    if '://' not in value:value='http://'+value
    m=re.fullmatch(r'(https?)://([A-Za-z0-9.-]{1,253}|\[[0-9A-Fa-f:]+\])(?::(\d{1,5}))?',value,re.I)
    if not m or (m.group(3) and not 1<=int(m.group(3))<=65535) or '..' in m.group(2) or m.group(2).startswith(('.','-')):
        raise HTTPException(400,f'Не похоже на адрес: {value}. Пример: http://мой-дом.ddns.net:18090')
    return m.group(1).lower()+'://'+m.group(2).lower()+(':'+m.group(3) if m.group(3) else '')


def auth_lan_addresses():
    """Адреса портала в домашней сети: по ним приложение подключается, когда устройство дома."""
    port=int(os.getenv('MEDIAHUB_PORT') or 8090);found=[]
    try:found=subprocess.run(['hostname','-I'],capture_output=True,text=True,timeout=2).stdout.split()
    except Exception:pass
    ips=[ip for ip in found if re.fullmatch(r'\d+\.\d+\.\d+\.\d+',ip) and not auth_public_ip(ip) and not ip.startswith(('127.','172.17.'))]
    return [f'http://{ip}:{port}' for ip in ips[:3]]


def auth_external_addresses():
    """Внешние адреса, которые администратор сохранил для подключения из интернета."""
    with cache_db() as con:row=con.execute("select value from meta where key='connect_addresses'").fetchone()
    try:return [x for x in json.loads(row['value']) if isinstance(x,str)][:5] if row else []
    except ValueError:return []


def auth_addresses():
    """Все адреса портала по порядку: сначала домашние, затем внешние."""
    return list(dict.fromkeys(auth_lan_addresses()+auth_external_addresses()))


def auth_known_tokens(request):
    """Сессии аккаунтов, в которые уже входили в этом браузере (HttpOnly-cookie)."""
    return [t for t in request.cookies.get('mh_known','').split('.') if 20<=len(t)<=128][:8]


def auth_set_known(response,request,tokens):
    """Запомнить сессии браузера для быстрого переключения аккаунтов без пароля."""
    tokens=list(dict.fromkeys(tokens))[-8:]
    if tokens:response.set_cookie('mh_known','.'.join(tokens),max_age=30*86400,httponly=True,samesite='strict',secure=request.url.scheme=='https')
    else:response.delete_cookie('mh_known')


def auth_same_origin(request):
    """Запрет входа и переключения со сторонних сайтов: Origin должен совпадать с адресом портала."""
    origin=request.headers.get('origin')
    if origin and origin.rstrip('/')!=str(request.base_url).rstrip('/'):raise HTTPException(403,'Вход с другого сайта запрещён')


@app.middleware('http')
async def account_access(request:Request,call_next):
    """Защитить API, разделить историю и запретить управление сервером обычным аккаунтам."""
    path=request.url.path
    if not path.startswith('/api/') or path in {'/api/version','/api/discovery','/api/apps','/api/apps/android/download','/api/auth/login','/api/auth/known','/api/auth/switch'}:
        return await call_next(request)
    bearer=request.headers.get('authorization','')
    token=bearer[7:] if bearer.startswith('Bearer ') else request.cookies.get('mh_session','')
    user=await asyncio.to_thread(auth_resolve,token)
    if not user:return JSONResponse({'detail':'Войдите в аккаунт'},status_code=401,headers={'Cache-Control':'no-store'})
    if request.method not in {'GET','HEAD','OPTIONS'} and not bearer.startswith('Bearer ') and request.headers.get('x-mediahub-auth')!='1':
        return JSONResponse({'detail':'Запрос требует подтверждения сессии'},status_code=403)
    personal=path.startswith('/api/auth/') or path in {'/api/player/progress','/api/remote/poll','/api/remote/state','/api/remote/send'}
    administrative=path.startswith(('/api/setup','/api/updates','/api/auth/accounts','/api/auth/addresses','/api/activity','/api/service','/api/storage','/api/apps/tv-install','/api/preferences'))
    if not user['is_admin'] and (administrative or (request.method not in {'GET','HEAD','OPTIONS'} and not personal)):
        return JSONResponse({'detail':'Доступно только администратору'},status_code=403)
    context=AUTH_USER.set(user);session=AUTH_TOKEN.set(hashlib.sha256(token.encode()).hexdigest())
    try:
        response=await call_next(request)
        response.headers['Cache-Control']='private, no-store'
        return response
    finally:AUTH_USER.reset(context);AUTH_TOKEN.reset(session)


@app.post('/api/auth/login')
async def auth_login(request:Request,login:str=Form(...),password:str=Form(...)):
    """Выдать сессию после проверки пароля; ограничить перебор по адресу клиента."""
    auth_same_origin(request)
    address=request.client.host if request.client else ''
    if address and not auth_public_ip(address):address=request.headers.get('x-forwarded-for','').split(',')[-1].strip() or address
    now=time.time();attempts=[x for x in AUTH_FAILURES.get(address,[]) if now-x<600]
    if len(attempts)>=10:raise HTTPException(429,'Слишком много попыток. Повторите через 10 минут')
    if len(AUTH_FAILURES)>1024:AUTH_FAILURES.clear()
    AUTH_FAILURES[address]=attempts+[now]
    if len(login)>64 or len(password)>256:raise HTTPException(401,'Неверный логин или пароль')
    with cache_db() as con:row=con.execute('select * from accounts where login=?',(login.strip().casefold(),)).fetchone()
    stored=row['password_hash'] if row else auth_password('invalid')
    calculated=await asyncio.to_thread(auth_password,password,stored.split(':')[0])
    if not row or not hmac.compare_digest(stored,calculated):raise HTTPException(401,'Неверный логин или пароль')
    # Через проброс порта пароль идёт по http открыто: заводской admin/admin снаружи угадывается первым.
    if auth_external(request) and password=='admin':raise HTTPException(403,'Из интернета нельзя войти с паролем по умолчанию. Смените пароль дома: «Настройки → Аккаунт»')
    AUTH_FAILURES.pop(address,None);token=secrets.token_urlsafe(32)
    with cache_db() as con:
        con.execute('delete from account_sessions where expires<?',(now,))
        con.execute('insert into account_sessions values(?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),row['id'],now+30*86400));con.commit()
    response=JSONResponse({'token':token,'user':auth_public(row),'addresses':await asyncio.to_thread(auth_addresses)},headers={'Cache-Control':'no-store'})
    response.set_cookie('mh_session',token,max_age=30*86400,httponly=True,samesite='strict',secure=request.url.scheme=='https')
    # Прежний вход того же аккаунта в этом браузере заменяется новым, остальные аккаунты остаются.
    known=[t for t in auth_known_tokens(request) if (u:=await asyncio.to_thread(auth_resolve,t)) and u['id']!=row['id']]
    auth_set_known(response,request,known+[token])
    return response


@app.get('/api/auth/me')
def auth_me():
    """Текущий аккаунт и адреса портала (дома и снаружи) для веба и приложения."""
    return {**auth_public(AUTH_USER.get()),'addresses':auth_addresses()}


@app.get('/api/auth/known')
def auth_known(request:Request):
    """Аккаунты, в которые входили в этом браузере: выбрать свой можно без пароля."""
    current=request.cookies.get('mh_session','');items=[]
    for token in auth_known_tokens(request):
        user=auth_resolve(token)
        if user and all(x['id']!=user['id'] for x in items):items.append({**auth_public(user),'current':hmac.compare_digest(token,current)})
    return {'items':items}


@app.post('/api/auth/switch')
def auth_switch(request:Request,login:str=Form(...)):
    """Переключить браузер на другой сохранённый аккаунт без повторного ввода пароля."""
    auth_same_origin(request)
    if request.headers.get('x-mediahub-auth')!='1':raise HTTPException(403,'Запрос требует подтверждения сессии')
    for token in auth_known_tokens(request):
        user=auth_resolve(token)
        if user and user['login']==login.strip().casefold():
            response=JSONResponse({'user':auth_public(user)},headers={'Cache-Control':'no-store'})
            response.set_cookie('mh_session',token,max_age=30*86400,httponly=True,samesite='strict',secure=request.url.scheme=='https')
            return response
    raise HTTPException(401,'Вход в этот аккаунт истёк. Введите пароль')


@app.post('/api/auth/logout')
def auth_logout(request:Request):
    """Завершить только текущую сессию; остальные аккаунты этого браузера остаются для выбора."""
    with cache_db() as con:con.execute('delete from account_sessions where token_hash=?',(AUTH_TOKEN.get(),));con.commit()
    response=JSONResponse({'ok':True});response.delete_cookie('mh_session')
    auth_set_known(response,request,[t for t in auth_known_tokens(request) if hashlib.sha256(t.encode()).hexdigest()!=AUTH_TOKEN.get()])
    return response


@app.get('/api/auth/addresses')
def auth_addresses_settings():
    """Адреса подключения для настроек администратора и предупреждение о заводском пароле."""
    with cache_db() as con:admin=con.execute("select password_hash from accounts where login='admin'").fetchone()
    default=bool(admin) and hmac.compare_digest(admin['password_hash'],auth_password('admin',admin['password_hash'].split(':')[0]))
    return {'local':auth_lan_addresses(),'external':auth_external_addresses(),'defaultPassword':default}


@app.post('/api/auth/addresses')
def auth_save_addresses(external:str=Form('')):
    """Сохранить внешние адреса (по одному в строке): IP или домен с портом проброса."""
    items=list(dict.fromkeys(x for x in (auth_normalize_address(v) for v in re.split(r'[\s,;]+',external)) if x))
    if len(items)>5:raise HTTPException(400,'Не больше пяти внешних адресов')
    with cache_db() as con:con.execute("insert or replace into meta(key,value) values('connect_addresses',?)",(json.dumps(items),));con.commit()
    return {'external':items}


@app.get('/api/auth/accounts')
def auth_accounts():
    """Список аккаунтов для администратора."""
    with cache_db() as con:return {'items':[auth_public(r) for r in con.execute('select * from accounts order by login')]}


@app.post('/api/auth/accounts')
def auth_create_account(login:str=Form(...),password:str=Form(...)):
    """Создать обычный аккаунт с независимой историей."""
    login=login.strip().casefold()
    if not re.fullmatch(r'[a-z0-9_.-]{1,64}',login):raise HTTPException(400,'Логин: латинские буквы, цифры, точка, дефис или подчёркивание')
    if not 4<=len(password)<=256:raise HTTPException(400,'Пароль: от 4 до 256 символов')
    with cache_db() as con:
        try:con.execute('insert into accounts(login,password_hash) values(?,?)',(login,auth_password(password)));con.commit()
        except sqlite3.IntegrityError:raise HTTPException(409,'Логин уже занят')
    return {'ok':True}


@app.post('/api/auth/password')
def auth_change_password(password:str=Form(...),current_password:str=Form(''),user_id:int=Form(0)):
    """Смена своего пароля или сброс администратором; остальные сессии отзываются."""
    user=AUTH_USER.get();target=user_id or user['id']
    if target!=user['id'] and not user['is_admin']:raise HTTPException(403,'Нельзя менять чужой пароль')
    if not 4<=len(password)<=256 or len(current_password)>256:raise HTTPException(400,'Пароль: от 4 до 256 символов')
    if target==user['id'] and not hmac.compare_digest(user['password_hash'],auth_password(current_password,user['password_hash'].split(':')[0])):
        raise HTTPException(400,'Текущий пароль неверен')
    with cache_db() as con:
        if not con.execute('select 1 from accounts where id=?',(target,)).fetchone():raise HTTPException(404,'Аккаунт не найден')
        con.execute('update accounts set password_hash=? where id=?',(auth_password(password),target))
        con.execute('delete from account_sessions where user_id=? and token_hash!=?',(target,AUTH_TOKEN.get() if target==user['id'] else ''));con.commit()
    return {'ok':True}


class CachedStatic(StaticFiles):
    """Static assets are requested with ?v=<app version>, so a long immutable
    cache is safe and removes a revalidation round trip on every page load."""
    def file_response(self,*args,**kwargs):
        resp=super().file_response(*args,**kwargs)
        resp.headers["Cache-Control"]="public, max-age=31536000, immutable"
        return resp

app.mount("/static",CachedStatic(directory=str(BASE_DIR/"static")),name="static")
templates=Jinja2Templates(directory=str(BASE_DIR/"templates"))

def unit_available(service):
    for base in ("/etc/systemd/system","/lib/systemd/system","/usr/lib/systemd/system"):
        if Path(base,service).exists():
            return True
    try:
        p=subprocess.run(["systemctl","list-unit-files",service,"--no-legend"],capture_output=True,text=True,timeout=4)
        return bool((p.stdout or "").strip())
    except Exception:
        return False

def setup_request_allowed(request:Request):
    if os.getenv("MEDIAHUB_ALLOW_PUBLIC_SETUP","0")=="1":
        return True
    host=(request.client.host if request.client else "") or ""
    try:
        addr=ipaddress.ip_address(host)
        return addr.is_private or addr.is_loopback or addr.is_link_local or addr in ipaddress.ip_network("100.64.0.0/10")
    except Exception:
        return False

def require_setup_access(request:Request):
    if not setup_request_allowed(request):
        raise HTTPException(403,"Установка системы доступна только из локальной сети")
    # No built-in password by design, but state-changing setup calls must come from
    # MediaHub's own JS. A foreign web page cannot add this custom header without CORS preflight.
    if request.method.upper() not in {"GET","HEAD","OPTIONS"} and request.headers.get("X-MediaHub-Setup") != "1":
        raise HTTPException(403,"Системная команда должна быть запущена из интерфейса MediaHub")

def update_env_values(values):
    """Atomically persist integration settings and refresh this process.

    Sensitive integration keys are additionally mirrored into the root-only
    secrets store. This makes a successful TMDB test survive app/systemd
    restarts and lets background workers recover even if their inherited
    environment is stale.
    """
    path=ENV_FILE
    path.parent.mkdir(parents=True,exist_ok=True)
    lines=path.read_text(encoding="utf-8",errors="replace").splitlines() if path.exists() else []
    found=set(); out=[]
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            k=line.split("=",1)[0].strip()
            if k in values:
                out.append(f"{k}={values[k]}"); found.add(k); continue
            if k in {"MEDIAHUB_USER","MEDIAHUB_PASS"}:
                continue
        out.append(line)
    for k,v in values.items():
        if k not in found: out.append(f"{k}={v}")
    tmp=path.with_suffix('.tmp')
    tmp.write_text("\n".join(out).rstrip()+"\n",encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)
    try: path.chmod(0o600)
    except Exception: pass

    secret_names={"TMDB_API_KEY","JELLYFIN_API_KEY","KINOPOISK_API_KEY","QBIT_PASS","MEDIAHUB_OUTBOUND_PROXY"}
    secret_updates={k:v for k,v in values.items() if k in secret_names and str(v).strip()}
    if secret_updates:
        _persist_secret_values(secret_updates)

    # Make values available immediately; saving TMDB no longer needs a restart.
    for k,v in values.items():
        os.environ[k]=str(v)
    global TMDB_KEY, QBIT_USER, QBIT_PASS, JELLYFIN_KEY, JELLYFIN_PUBLIC_URL
    if "TMDB_API_KEY" in values: TMDB_KEY=str(values["TMDB_API_KEY"]).strip()
    if "QBIT_USER" in values: QBIT_USER=str(values["QBIT_USER"])
    if "QBIT_PASS" in values: QBIT_PASS=str(values["QBIT_PASS"])

    if "JELLYFIN_PUBLIC_URL" in values: JELLYFIN_PUBLIC_URL=str(values["JELLYFIN_PUBLIC_URL"]).strip().rstrip("/")

    # Verify the important key really reached persistent storage.
    if "TMDB_API_KEY" in values and _persistent_value("TMDB_API_KEY") != str(values["TMDB_API_KEY"]).strip():
        raise RuntimeError("TMDB ключ не удалось сохранить в постоянное хранилище")

def safe_media_path(path:Path):
    try:
        path.resolve().relative_to(MEDIA_ROOT.resolve())
        return True
    except Exception:
        return False

def human(n):
    v=float(n)
    for u in ["B","KB","MB","GB","TB"]:
        if v<1024 or u=="TB": return f"{v:.1f} {u}"
        v/=1024

def clean_title(name):
    s=Path(name).stem.replace("_"," ").replace("."," ")
    s=re.sub(r'[\[\]\(\)]',' ',s)
    s=re.split(r'\bS\d{1,2}(?:E\d{1,3})?\b',s,maxsplit=1,flags=re.I)[0]
    s=re.split(r'\b\d{1,2}x\d{1,3}\b',s,maxsplit=1,flags=re.I)[0]
    s=re.split(r'\b(2160p|1080p|720p|WEB[- ]?DL|WEBRip|BluRay|BDRip|HDTV|HEVC|x264|x265|AVC|HDR|REMUX)\b',
               s,maxsplit=1,flags=re.I)[0]
    s=re.sub(r'\b(19|20)\d{2}\b.*$','',s).strip()
    return re.sub(r'\s+',' ',s).strip(" -_.")

def suggest_kind(path):
    low=str(path).lower()
    if "/anime/" in low:return "anime"
    if re.search(r'(^|[ ._\-\[])S\d{1,2}([E._ \]-]|$)|\b\d{1,2}x\d{1,3}\b',path.name,re.I):
        return "tv"
    if "/tv/" in low:return "tv"
    return "movies"

SEASON_WORDS={"перв":1,"втор":2,"трет":3,"четв":4,"пят":5,"шест":6,"седьм":7,
              "восьм":8,"девят":9,"десят":10}

def season_number(name):
    """Номер сезона из названия релиза или папки.

    Понимает «S04E1-24», «Сезон 4», «4 сезон», «4x01», «Season 4».
    Если сезонов несколько или их нет — возвращает 1, как и раньше.
    """
    text=" "+(name or "")+" "
    for pattern in (
        r"(?<![a-zа-я0-9])s\s*(\d{1,3})(?=\s*(?:e\s*\d|[^0-9a-zа-я]|$))",
        r"(?:seasons?|сезон)\s*(\d{1,3})(?![0-9])",
        r"(?<![a-zа-я0-9])(\d{1,3})\s*(?:-?[йяе]\w{0,2}\s*)?сезон",
        r"(?<![a-zа-я0-9])(\d{1,2})x\d{1,3}(?![0-9])",
        # «[ТВ-2]» и «TV-3» у аниме тоже означают номер сезона.
        r"(?<![a-zа-я0-9])(?:тв|tv)\s*[-–—]?\s*(\d{1,2})(?![0-9])",
        r"(?<![a-zа-я0-9])(\d{1,2})\s*(?:-?(?:st|nd|rd|th))?\s+season",
    ):
        m=re.search(pattern,text,re.I)
        if m:
            n=int(m.group(1))
            if 0<n<=200:
                return n
    for word,value in SEASON_WORDS.items():
        if re.search(rf"(?<![a-zа-я]){word}\w*\s+сезон",text,re.I):
            return value
    return 1

def folder_stats(path):
    total=0; videos=0; files=0; formats={}
    try:
        for f in path.rglob("*"):
            if f.is_file():
                files += 1
                try: total += f.stat().st_size
                except OSError: pass
                ext=f.suffix.lower()
                if ext in VIDEO_EXTS:
                    videos += 1
                    formats[ext]=formats.get(ext,0)+1
    except Exception:
        pass
    if not videos and has_disc_structure(path):
        videos=1
        formats[DISC_PSEUDO_EXT]=1
    return total, videos, files, formats



def _fs_library_title(name):
    s=(name or "").replace("_"," ").replace("."," ")
    s=re.sub(r"\[(.*?)\]"," ",s)
    s=re.sub(r"\b(2160p|1080p|720p|web[- ]?dl|webrip|bluray|bdrip|hdtv|hevc|x264|x265|hdr|remux)\b.*$","",s,flags=re.I)
    # Папка фильма называется «Название (Год)», а год карточка показывает
    # отдельным полем — в заголовке он только дублировался бы.
    trimmed=re.sub(r"\s*\((?:19|20)\d{2}\)\s*$","",s).strip()
    if trimmed:
        s=trimmed
    s=re.sub(r"\s+"," ",s).strip(" -_.")
    return s or name

def _fs_library_year(name):
    m=re.search(r"\b((?:19|20)\d{2})\b",name or "")
    return m.group(1) if m else ""

def _norm_folder_key(name):
    s=re.sub(r"\s*\((?:19|20)\d{2}\)\s*$","",(name or "").strip())
    return re.sub(r"[^0-9a-zа-яё]+","",s.casefold())

def movie_folder_name(title,year=""):
    """Имя папки фильма в стиле Jellyfin: «Название (Год)».

    Год не дублируется, если он уже есть в названии, а без года остаётся
    просто название. Папка выглядит одинаково и после ручного переноса,
    и после автоматической раскладки скачанного.
    """
    base=re.sub(r"\s+"," ",(title or "").strip()).strip(" -_.")
    if not base:
        return ""
    if re.search(r"\((?:19|20)\d{2}\)\s*$",base):
        return base
    inner=_fs_library_year(base)
    if inner:
        # «Rebel Ridge 2024» → «Rebel Ridge (2024)», без второго года в хвосте.
        base=re.sub(r"\s*\b"+inner+r"\b\s*$","",base).strip(" -_.") or base
        year=year or inner
    year=str(year or "").strip()[:4]
    return f"{base} ({year})" if re.fullmatch(r"(?:19|20)\d{2}",year) else base

def existing_movie_folder(root:Path,title,year=""):
    """Найти уже существующую папку фильма — с годом или без него.

    Без этого переход на «Название (Год)» создавал бы вторую папку рядом со
    старой «Название», и один фильм превращался бы в две карточки. Ремейк
    при этом остаётся отдельным: папка с чужим годом не подходит.
    """
    wanted=movie_folder_name(title,year)
    if not wanted:
        return None
    want_year=_fs_library_year(wanted)
    plain=re.sub(r"\s*\((?:19|20)\d{2}\)\s*$","",wanted).strip()
    key=_norm_folder_key(wanted)
    if not key:
        return None
    if (root/wanted).is_dir():
        return root/wanted
    try:
        children=[x for x in root.iterdir() if x.is_dir()]
    except Exception:
        return None
    for child in children:
        if _norm_folder_key(child.name)!=key:
            continue
        have_year=_fs_library_year(child.name)
        # Совпало название: подходит папка с тем же годом или вовсе без года,
        # созданная прошлыми версиями. «Дюна (2021)» для «Дюна (2024)» — нет.
        if not have_year or not want_year or have_year==want_year:
            return child
    return None

def has_disc_structure(path):
    """Blu-ray и DVD — это папки BDMV / VIDEO_TS, а не отдельный файл."""
    try:
        root=Path(path)
        if root.name.lower() in DISC_MARKERS:
            return True
        for child in root.iterdir():
            if child.is_dir() and child.name.lower() in DISC_MARKERS:
                return True
    except Exception:
        pass
    return False


def _video_count(path):
    count=0; newest=0.0
    try:
        for f in Path(path).rglob("*"):
            if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
                count+=1
                try:newest=max(newest,f.stat().st_mtime)
                except OSError:pass
    except Exception:
        pass
    if not count and has_disc_structure(path):
        # Диск целиком считаем одним фильмом: Jellyfin такую папку читает.
        try:newest=Path(path).stat().st_mtime
        except OSError:newest=0.0
        count=1
    return count,newest

def manual_meta_index():
    """Карточки, привязанные пользователем вручную, по пути на диске.

    Хранятся отдельно от library_cache, потому что синхронизация с Radarr и
    Sonarr удаляет и пересоздаёт строки библиотеки целиком — ручные данные
    иначе не пережили бы ни одного обновления.
    """
    index={}
    try:
        with cache_db() as con:
            rows=con.execute("select * from manual_meta").fetchall()
    except Exception:
        return index
    for r in rows:
        path=str(r["path"] or "").rstrip("/")
        if not path:
            continue
        extra=_json_dict(r["extra_json"]) if "extra_json" in r.keys() else {}
        index[path]={"title":r["title"] or "","year":r["year"] or "","poster":r["poster"] or "",
                     "overview":r["overview"] or "","sourceUrl":r["source_url"] or "",
                     "episodes":int(r["episodes"] or 0),
                     "genres":[g for g in (extra.get("genres") or []) if isinstance(g,str)],
                     "rating":extra.get("rating")}
    return index


# Поля, которые дописывают TMDB, Jellyfin и ARR. У карточки со страницы-источника
# их быть не должно: именно они давали чужой постер, год и жанры.
FOREIGN_META_FIELDS={"runtime":0,"studio":"","network":"","status":"local","localizedTitle":"",
                     "certification":""}


def apply_manual_meta(item,index=None):
    """Наложить ручные данные на карточку библиотеки, если они есть.

    Если привязана страница-источник, карточка целиком берётся с неё: пустое
    поле страницы остаётся пустым, а не добирается из TMDB или Jellyfin.
    """
    index=manual_meta_index() if index is None else index
    path=str(item.get("path") or "").rstrip("/")
    meta=index.get(path)
    if not meta:
        return item
    if meta.get("sourceUrl"):
        folder=Path(path).name if path else ""
        if Path(folder).suffix.lower() in VIDEO_EXTS:
            folder=Path(folder).stem
        item["title"]=meta.get("title") or (_fs_library_title(folder) if folder else "") or item.get("title") or ""
        item["year"]=meta.get("year") or (_fs_library_year(folder) if folder else "") or ""
        item["poster"]=meta.get("poster") or None
        item["overview"]=meta.get("overview") or ""
        item["genres"]=list(meta.get("genres") or [])
        item["rating"]=meta.get("rating") or None
        item.update(FOREIGN_META_FIELDS)
        item["sourceUrl"]=meta["sourceUrl"]
        item["fromPage"]=True
    else:
        if meta.get("title"):item["title"]=meta["title"]
        if meta.get("year"):item["year"]=meta["year"]
        if meta.get("poster"):item["poster"]=meta["poster"]
        if meta.get("overview"):item["overview"]=meta["overview"]
    if meta.get("episodes"):item["episodesTotal"]=meta["episodes"]
    item["manualMetadata"]=True
    item["needsMetadata"]=False
    return item


def save_manual_meta(con,path,kind,title,year="",poster="",overview="",source_url="",
                     episodes=0,genres=None,rating=None):
    """Записать ручную карточку проекта (по пути на диске)."""
    extra={"genres":list(genres or []),"rating":rating}
    con.execute("""insert or replace into manual_meta
        (path,kind,title,year,poster,overview,source_url,episodes,updated_at,extra_json)
        values(?,?,?,?,?,?,?,?,?,?)""",
        (str(path).rstrip("/"),kind,title or "",str(year or ""),poster or "",overview or "",
         source_url or "",int(episodes or 0),datetime.now(timezone.utc).isoformat(),
         json.dumps(extra,ensure_ascii=False)))


def download_meta_index():
    """Карточки со страниц-источников для уже скачанных раздач."""
    index={}
    try:
        with cache_db() as con:
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
        path=str(r["final_path"] or "").rstrip("/")
        if path:
            index[path]=meta
        for name in (r["media_title"],r["release_title"],r["mtitle"]):
            n=normalize_search_text(name)
            if n:
                index.setdefault("title:"+n,meta)
    return index


def download_meta_for(index,path,title):
    if not index:
        return None
    hit=index.get(str(path or "").rstrip("/"))
    if hit:
        return hit
    n=normalize_search_text(title)
    if n and "title:"+n in index:
        return index["title:"+n]
    for key,meta in index.items():
        if not key.startswith("title:"):
            continue
        base=key[6:]
        if base and n and (n.startswith(base) or base.startswith(n)):
            return meta
    return None


def _fs_entry_signature(child:Path):
    """Дешёвый отпечаток папки проекта: время изменения её и подпапок первого уровня.

    Новая серия в «Season 02» меняет время самой «Season 02», поэтому этого
    хватает, чтобы понять, что проект нужно пересчитать, без обхода всех файлов.
    """
    try:
        st=child.stat()
        if child.is_file():
            return f"f{st.st_mtime_ns}:{st.st_size}"
        parts=[str(st.st_mtime_ns)]
        for sub in child.iterdir():
            if sub.is_dir():
                try:parts.append(f"{sub.name}:{sub.stat().st_mtime_ns}")
                except OSError:pass
        return hashlib.sha1("|".join(sorted(parts)).encode("utf-8")).hexdigest()[:16]
    except OSError:
        return ""


def live_filesystem_items(kind,previous=None):
    """Проекты, физически лежащие в корне раздела.

    `previous` — прошлый скан по пути: у неизменившихся папок число видео берётся
    из него, поэтому повторный проход стоит лишь нескольких stat и библиотека
    видит новые, перенесённые и удалённые папки сразу.
    """
    previous=previous or {}
    root={"movies":MOVIES_ROOT,"tv":TV_ROOT,"anime":ANIME_ROOT}.get(kind)
    if not root:
        return []
    root=Path(root)
    if not root.exists():
        return []
    out=[]
    try: children=sorted(root.iterdir(),key=lambda x:x.name.casefold())
    except Exception:return []
    seen=set()
    meta_index=download_meta_index()
    manual_index=manual_meta_index()
    for child in children:
        if child.name.startswith('.'):
            continue
        if child.is_file() and child.suffix.lower() not in VIDEO_EXTS:
            continue
        # Одиночный видеофайл прямо в корне — это отдельный тайтл, а не «вся
        # папка movies»: раньше в карточке показывался корень медиатеки.
        single_file=child.is_file()
        p=str(child.resolve())
        if p in seen:continue
        seen.add(p)
        sig=_fs_entry_signature(child)
        prev=previous.get(p)
        if prev and sig and prev.get("fsSig")==sig:
            count=int(prev.get("videoCount") or 0); newest=float(prev.get("fsNewest") or 0)
        elif single_file:
            try:
                st=child.stat(); count,newest=1,st.st_mtime
            except OSError:
                continue
        else:
            count,newest=_video_count(child)
        if not count:continue
        base_name=child.stem if single_file else child.name
        title=_fs_library_title(base_name)
        year=_fs_library_year(base_name)
        added=datetime.fromtimestamp(newest,tz=timezone.utc).isoformat() if newest else datetime.now(timezone.utc).isoformat()
        # Папка скачана по ссылке на страницу — берём её карточку.
        page=manual_index.get(p) or download_meta_for(meta_index,p,title) or {}
        out.append({
            "kind":kind,"item_key":"live-"+hashlib.sha1(f"{kind}|{p}".encode('utf-8')).hexdigest()[:20],
            "title":page.get("title") or title,"year":page.get("year") or year,
            "overview":page.get("overview") or f"Локальная медиатека · {count} видео",
            "poster":page.get("poster") or None,"path":p,"added_at":added,"has_file":1,
            "catalog_source":"filesystem","external_id":"",
            "genres":[],"runtime":0,"rating":None,"status":"local","studio":"","network":"",
            "catalog":"filesystem","videoCount":count,"needsMetadata":not bool(page),
            "sourceUrl":page.get("sourceUrl") or "","fsSig":sig,"fsNewest":newest,
        })
    return out

FS_SCAN_TTL=timedelta(minutes=10)
_FS_SCAN_TASKS={}

def live_filesystem_items_cached(kind):
    """Список папок медиатеки из кэша.

    Полный обход каталогов на большой медиатеке занимает секунды, поэтому
    страница «Моя библиотека» читает последний известный результат, а свежий
    скан идёт в фоне.
    """
    raw=get_setting(f"fs_scan_{kind}","")
    data=None
    if raw:
        try:
            data=json.loads(raw)
        except Exception:
            data=None
    fresh=False
    if isinstance(data,dict) and isinstance(data.get("items"),list):
        try:
            dt=datetime.fromisoformat(data.get("updatedAt"))
            if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
            fresh=(datetime.now(timezone.utc)-dt)<FS_SCAN_TTL
        except Exception:
            fresh=False
    if data and not fresh:
        _schedule_fs_scan(kind)
    # Быстрый проход по корню раздела: изменившиеся папки пересчитываются сразу,
    # остальные берутся из прошлого скана. Раньше новая загрузка или перенос
    # появлялись в библиотеке только через 10 минут.
    prev={str(x.get("path") or ""):x for x in (data or {}).get("items") or [] if isinstance(x,dict)}
    items=live_filesystem_items(kind,prev)
    if not data or [(x.get("path"),x.get("fsSig")) for x in items]!=[(x.get("path"),x.get("fsSig")) for x in data["items"]]:
        _store_fs_scan(kind,items)
    return items

def _store_fs_scan(kind,items):
    try:
        set_setting(f"fs_scan_{kind}",json.dumps(
            {"updatedAt":datetime.now(timezone.utc).isoformat(),"items":items},ensure_ascii=False))
    except Exception:
        pass

def _schedule_fs_scan(kind):
    task=_FS_SCAN_TASKS.get(kind)
    if task and not task.done():
        return
    async def run():
        try:
            items=await asyncio.get_running_loop().run_in_executor(None,live_filesystem_items,kind)
            _store_fs_scan(kind,items)
        except Exception:
            pass
    try:
        _FS_SCAN_TASKS[kind]=asyncio.create_task(run())
    except RuntimeError:
        pass

def upsert_live_library_item(kind,path,title=None):
    p=Path(path)
    if not p.exists():return
    folder=p if p.is_dir() else p.parent
    count,newest=_video_count(folder)
    if not count:return
    rp=str(folder.resolve())
    key="fs-"+hashlib.sha1(f"{kind}|{rp}".encode('utf-8')).hexdigest()[:20]
    # Название папки — «Фильм (2024)», а в карточке год живёт отдельным полем.
    raw_title=(title or folder.name).strip()
    year=_fs_library_year(raw_title) or _fs_library_year(folder.name)
    title=_fs_library_title(raw_title)
    added=datetime.fromtimestamp(newest,tz=timezone.utc).isoformat() if newest else datetime.now(timezone.utc).isoformat()
    page=manual_meta_index().get(rp.rstrip("/")) or download_meta_for(download_meta_index(),rp,title) or {}
    extra={"genres":[],"runtime":0,"rating":None,"status":"local","studio":"","network":"","catalog":"filesystem","videoCount":count,"needsMetadata":not bool(page)}
    if page:
        extra["sourceUrl"]=page.get("sourceUrl") or ""
        extra["fromPage"]=True
    with cache_db() as con:
        # If ARR already owns exactly this path, don't create a duplicate.
        existing=con.execute("select item_key from library_cache where kind=? and rtrim(path,'/')=? limit 1",(kind,rp.rstrip('/'))).fetchone()
        if existing:return
        con.execute("""insert or replace into library_cache
          (kind,item_key,title,year,overview,poster,path,added_at,has_file,catalog_source,external_id,extra_json)
          values(?,?,?,?,?,?,?,?,?,?,?,?)""",
          (kind,key,page.get("title") or title,page.get("year") or year,
           page.get("overview") or f"Локальная медиатека · {count} видео",
           page.get("poster") or None,rp,added,1,"filesystem","",json.dumps(extra,ensure_ascii=False)))
        con.commit()


def remember_user_tracking(kind,item_key,external_id,title):
    """Запомнить, что тайтл в отслеживание отправил пользователь.

    Отдельная таблица, потому что строки library_cache пересоздаются при каждой
    синхронизации с Radarr и Sonarr — метка в них не выживает.
    """
    key=f"{kind}|{normalize_search_text(title) or item_key or external_id}"
    try:
        with cache_db() as con:
            con.execute("""insert or replace into user_tracking
                (track_key,kind,item_key,external_id,title,created_at)
                values(?,?,?,?,?,?)""",
                (key,kind,str(item_key or ""),str(external_id or ""),title or "",
                 datetime.now(timezone.utc).isoformat()))
            con.commit()
    except Exception:
        pass


def user_tracking_index():
    """Индексы «моего» отслеживания: по ключу, по внешнему id, по названию."""
    by_item=set(); by_ext=set(); by_title=set()
    try:
        with cache_db() as con:
            for r in con.execute("select * from user_tracking").fetchall():
                kind=r["kind"] or ""
                if r["item_key"]:by_item.add((kind,str(r["item_key"])))
                if r["external_id"]:by_ext.add((kind,str(r["external_id"])))
                n=normalize_search_text(r["title"])
                if n:by_title.add((kind,n))
    except Exception:
        pass
    return {"item":by_item,"ext":by_ext,"title":by_title}


def is_user_tracked(index,kind,item_key,external_id,title):
    if (kind,str(item_key or "")) in index["item"]:return True
    if external_id and (kind,str(external_id)) in index["ext"]:return True
    n=normalize_search_text(title)
    return bool(n and (kind,n) in index["title"])


def upsert_tracked_library_item(kind,item_key,title,year="",poster="",overview="",external_id="",catalog_source="radarr",path=""):
    """Remember a title that ARR now monitors but has no file for yet.

    Without this row the UI kept showing "добавить в отслеживание" until the
    next cache refresh, and a monitored title looked identical to a downloaded
    one. has_file=0 marks it as "ждём файлы".
    """
    extra={"genres":[],"catalog":catalog_source,"monitored":True,"awaitingFiles":True}
    with cache_db() as con:
        row=con.execute("select item_key,has_file from library_cache where kind=? and item_key=?",(kind,str(item_key))).fetchone()
        if row and int(row["has_file"] or 0):
            return
        con.execute("""insert or replace into library_cache
          (kind,item_key,title,year,overview,poster,path,added_at,has_file,catalog_source,external_id,extra_json)
          values(?,?,?,?,?,?,?,?,?,?,?,?)""",
          (kind,str(item_key),title or "",str(year or ""),overview or "",poster or "",path or "",
           datetime.now(timezone.utc).isoformat(),0,catalog_source,str(external_id or ""),
           json.dumps(extra,ensure_ascii=False)))
        con.commit()


def season_progress(extra):
    """Сколько сезонов и эпизодов реально есть.

    Приоритет у точной сводки `seasonsSummary`, которую пишет карта сезонов:
    она учитывает файлы на диске, а снимок Sonarr в библиотеке может отставать.
    Нулевой сезон (спецвыпуски) учитывается только если файлы там уже лежат,
    иначе полностью скачанный сериал вечно выглядел бы неполным.
    """
    summary=[s for s in (extra.get("seasonsSummary") or []) if isinstance(s,dict)]
    if summary:
        have_ep=total_ep=0; total_s=complete_s=partial_s=0
        for s in summary:
            ec=int(s.get("episodes") or 0); fc=int(s.get("downloaded") or 0)
            if int(s.get("season") or 0)==0 and not fc:
                continue
            have_ep+=fc; total_ep+=ec
            if ec or fc:
                total_s+=1
                if ec and fc>=ec: complete_s+=1
                elif fc: partial_s+=1
        return {"episodes":have_ep,"episodesTotal":total_ep,"seasons":total_s,
                "seasonsComplete":complete_s,"seasonsPartial":partial_s}
    seasons=[s for s in (extra.get("seasons") or []) if isinstance(s,dict)]
    have_ep=total_ep=0; total_s=complete_s=partial_s=0
    for s in seasons:
        st=s.get("statistics") or {}
        ec=int(st.get("episodeCount") or 0); fc=int(st.get("episodeFileCount") or 0)
        if s.get("seasonNumber")==0 and not fc:
            continue
        have_ep+=fc; total_ep+=ec
        if ec or fc:
            total_s+=1
            if ec and fc>=ec: complete_s+=1
            elif fc: partial_s+=1
    return {"episodes":have_ep,"episodesTotal":total_ep,"seasons":total_s,
            "seasonsComplete":complete_s,"seasonsPartial":partial_s}


def library_status_label(kind,has_file,progress,disk_videos=0,arr_known=True):
    """Единая подпись состояния: полностью, частично или только отслеживается."""
    seasons=progress.get("seasons") or 0
    complete=progress.get("seasonsComplete") or 0
    partial=progress.get("seasonsPartial") or 0
    have=progress.get("episodes") or 0
    total=progress.get("episodesTotal") or 0

    if kind in {"tv","anime"} and seasons:
        if have and complete>=seasons and (not total or have>=total):
            return "complete",f"✓ Все {seasons} сезон{_plural_season(seasons)}"
        if have:
            ep=f" · {have}/{total} эп." if total else f" · {have} эп."
            return "partial",f"◐ {complete + partial} из {seasons} сезон{_plural_season(seasons)}{ep}"
    if has_file or disk_videos:
        if kind in {"tv","anime"} and not arr_known and disk_videos:
            return "partial",f"◐ Есть файлы · {disk_videos} видео, Sonarr не импортировал"
        if kind=="movies" and not arr_known and disk_videos:
            return "complete","✓ Файл на диске"
        if total and have and have<total:
            return "partial",f"◐ {have}/{total} эп."
        return "complete","✓ В библиотеке"
    return "tracked","⏳ Отслеживается · файлов нет"


def _plural_season(n):
    n=abs(int(n or 0))
    if n%10==1 and n%100!=11: return ""
    if n%10 in (2,3,4) and n%100 not in (12,13,14): return "а"
    return "ов"


def library_item_state(row,disk_videos=0):
    """Shared badge/status data for one library_cache row."""
    d=dict(row)
    kind=d.get("kind") or ""
    extra=_json_dict(d.get("extra_json"))
    has_file=bool(d.get("has_file"))
    progress=season_progress(extra)
    arr_known=bool(has_file or progress.get("episodes"))
    status,label=library_status_label(kind,has_file,progress,disk_videos,arr_known)
    in_library=status in {"complete","partial"}
    return {"inLibrary":in_library,"hasFile":has_file or bool(disk_videos),"tracked":True,
            "awaitingFiles":status=="tracked",
            "libraryStatus":status,"libraryLabel":label,
            "libraryFiles":progress.get("episodes",0),"libraryTotal":progress.get("episodesTotal",0),
            "librarySeasons":progress.get("seasons",0),
            "librarySeasonsComplete":progress.get("seasonsComplete",0),
            "librarySeasonsPartial":progress.get("seasonsPartial",0),
            "diskVideos":int(disk_videos or 0),
            "monitored":bool(extra.get("monitored",True))}


def get_setting(key, default=""):
    try:
        with cache_db() as con:
            row=con.execute("select value from mediahub_settings where key=?",(key,)).fetchone()
        return row["value"] if row else default
    except Exception:
        return default

def set_setting(key, value):
    with cache_db() as con:
        con.execute("insert or replace into mediahub_settings(key,value) values(?,?)",(key,str(value)))
        con.commit()

def log_activity(action, title="", details="", ok=True):
    try:
        with cache_db() as con:
            con.execute("insert into activity_log(at,action,title,details,ok) values(?,?,?,?,?)",
                        (datetime.now(timezone.utc).isoformat(),str(action),str(title or ""),str(details or "")[:1000],1 if ok else 0))
            # Keep the DB tidy on long-running installations.
            con.execute("delete from activity_log where id not in (select id from activity_log order by id desc limit 2000)")
            con.commit()
    except Exception:
        pass

def _meminfo():
    vals={}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k,v=line.split(":",1); vals[k]=int(v.strip().split()[0])*1024
    except Exception:
        pass
    total=vals.get("MemTotal",0); avail=vals.get("MemAvailable",0)
    return total,max(0,total-avail)

def _uptime_seconds():
    try:return int(float(Path("/proc/uptime").read_text().split()[0]))
    except Exception:return 0

def _jellyfin_public_base(request:Request):
    if JELLYFIN_PUBLIC_URL:
        return JELLYFIN_PUBLIC_URL
    host=request.url.hostname or "127.0.0.1"
    scheme=request.url.scheme or "http"
    return f"{scheme}://{host}:8096"

async def _jellyfin_user_id():
    if JELLYFIN_USER_ID:
        return JELLYFIN_USER_ID
    if not JELLYFIN_KEY:
        return ""
    try:
        async with httpx.AsyncClient(timeout=8,trust_env=False) as c:
            r=await c.get(JELLYFIN_URL+"/Users",headers={"X-Emby-Token":JELLYFIN_KEY})
            r.raise_for_status(); users=r.json() or []
        enabled=[u for u in users if not ((u.get("Policy") or {}).get("IsDisabled"))]
        return str((enabled or users)[0].get("Id") or "") if (enabled or users) else ""
    except Exception:
        return ""

async def jellyfin_resume_items(request:Request, limit=18):
    uid=await _jellyfin_user_id()
    if not (uid and JELLYFIN_KEY):
        return []
    try:
        params={"Limit":str(max(40,int(limit)*3)),"Recursive":"true","Fields":"Overview,SeriesName,SeasonName,IndexNumber,ParentIndexNumber,RunTimeTicks,SeriesId,UserDataLastPlayedDate"}
        async with httpx.AsyncClient(timeout=10,trust_env=False) as c:
            r=await c.get(JELLYFIN_URL+f"/Users/{uid}/Items/Resume",headers={"X-Emby-Token":JELLYFIN_KEY},params=params)
            r.raise_for_status(); rows=(r.json() or {}).get("Items",[])
    except Exception:
        return []
    base=_jellyfin_public_base(request)
    out=[]
    groups={}
    for x in rows:
        ud=x.get("UserData") or {}; runtime=int(x.get("RunTimeTicks") or 0); pos=int(ud.get("PlaybackPositionTicks") or 0)
        progress=round(min(100,max(0,(pos/runtime*100) if runtime else 0)),1)
        typ=x.get("Type") or ""
        series=x.get("SeriesName") or ""
        ep=x.get("IndexNumber"); season=x.get("ParentIndexNumber")
        subtitle=""
        if series:
            code=(f"S{int(season):02d}E{int(ep):02d}" if isinstance(season,int) and isinstance(ep,int) else "")
            subtitle=" · ".join(v for v in [series,code] if v)
        # Для сериала показываем постер сериала, а не кадр серии.
        image_id=(x.get("SeriesId") or x.get("Id")) if series else x.get("Id")
        alt_id=x.get("Id") if series else (x.get("SeriesId") or "")
        item={
            "id":x.get("Id"),
            "title":series or x.get("Name") or "Без названия",
            "subtitle":subtitle or (x.get("Name") or ""),
            "overview":x.get("Overview") or "","progress":progress,"kind":"movies" if typ=="Movie" else "tv",
            "poster":(f"/api/jellyfin-image/{image_id}"+(f"?alt={alt_id}" if alt_id and alt_id!=image_id else "")) if image_id else None,
            "playUrl":f"{base}/web/#/details?id={x.get('Id')}","jellyfin":True,
            "seriesId":x.get("SeriesId") or "",
            "episode":ep if isinstance(ep,int) else 0,
            "season":season if isinstance(season,int) else 0,
            "lastPlayed":str(ud.get("LastPlayedDate") or ""),
        }
        # Одна карточка на проект: несколько недосмотренных серий одного сериала
        # схлопываются в последнюю по времени просмотра.
        gkey=item["seriesId"] or item["id"]
        prev=groups.get(gkey)
        if prev:
            newer=(item["lastPlayed"] or "")>(prev["lastPlayed"] or "")
            further=(item["season"],item["episode"])>(prev["season"],prev["episode"])
            if newer or (not item["lastPlayed"] and not prev["lastPlayed"] and further):
                groups[gkey]=item
                groups[gkey]["queued"]=prev.get("queued",1)+1
            else:
                prev["queued"]=prev.get("queued",1)+1
        else:
            item["queued"]=1
            groups[gkey]=item
    out=list(groups.values())
    out.sort(key=lambda z:z.get("lastPlayed") or "",reverse=True)
    for z in out:
        if z.get("queued",1)>1 and z.get("subtitle"):
            z["subtitle"]+=f" · ещё {z['queued']-1} в очереди"
    return out[:int(limit)]

def quality_rank(name):
    t=(name or "").upper()
    if "2160" in t or "4K" in t or "UHD" in t:
        return 40
    if "1080" in t:
        return 30
    if "720" in t:
        return 20
    if "480" in t or "SD" in t:
        return 10
    return 0

def quality_label(rank):
    return {40:"2160p",30:"1080p",20:"720p",10:"480p"}.get(int(rank or 0),"Неизвестно")

def systemctl_active(service):
    try:return subprocess.run(["systemctl","is-active","--quiet",service],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=3).returncode==0
    except Exception:return False

def systemctl_enabled(service):
    try:return subprocess.run(["systemctl","is-enabled","--quiet",service],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=3).returncode==0
    except Exception:return False

def run_systemctl(action,service):
    if action not in {"start","stop","restart"}: raise ValueError("bad action")
    return subprocess.run(["systemctl",action,service],capture_output=True,text=True,timeout=20)

async def get_json(base,key,path,params=None):
    async with httpx.AsyncClient(timeout=25,trust_env=False) as c:
        r=await c.get(base+path,headers={"X-Api-Key":key} if key else {},params=params)
        r.raise_for_status()
        return r.json()

async def post_json(base,key,path,data):
    async with httpx.AsyncClient(timeout=30,trust_env=False) as c:
        r=await c.post(base+path,headers={"X-Api-Key":key,"Content-Type":"application/json"},json=data)
        r.raise_for_status()
        return r.json() if r.text.strip() else {}

_jellyfin_refresh_last=0.0

async def jellyfin_refresh():
    """Ask Jellyfin to rescan — but not more than once per cooldown window.

    Back-to-back /Library/Refresh calls each restart Jellyfin's scan task,
    which cancels the previous scan's own post-scan "clean missing items"
    step before it finishes. That's how a title removed on disk (merged
    duplicates, manual deletes) could keep reappearing in Jellyfin's library
    instead of being purged for good. Skipping a call that lands inside the
    cooldown lets the in-flight scan actually complete its cleanup.
    """
    global _jellyfin_refresh_last
    if not JELLYFIN_KEY:return False
    now=time.monotonic()
    if now-_jellyfin_refresh_last<20:
        return True
    _jellyfin_refresh_last=now
    try:
        async with httpx.AsyncClient(timeout=15,trust_env=False) as c:
            r=await c.post(JELLYFIN_URL+"/Library/Refresh",headers={"X-Emby-Token":JELLYFIN_KEY})
            return r.status_code<300
    except Exception:return False

async def qbit_login():
    c=httpx.AsyncClient(timeout=12,trust_env=False)
    if QBIT_USER and QBIT_PASS:
        try:
            r=await c.post(QBIT_URL+"/api/v2/auth/login",data={"username":QBIT_USER,"password":QBIT_PASS})
            if r.text.strip()=="Ok.":
                return c
        except Exception:
            pass
        await c.aclose(); return None
    # v19 installer disables qBittorrent authentication only for localhost.
    # This lets MediaHub control its local qBittorrent without storing a password,
    # while remote WebUI clients still use qBittorrent's own authentication.
    try:
        r=await c.get(QBIT_URL+"/api/v2/app/version")
        if r.status_code<300:
            return c
    except Exception:
        pass
    await c.aclose(); return None


def cache_db():
    CACHE_DB.parent.mkdir(parents=True,exist_ok=True)
    con=sqlite3.connect(CACHE_DB,timeout=8)
    con.row_factory=sqlite3.Row
    con.execute("pragma busy_timeout=8000")
    try: con.execute("pragma journal_mode=WAL")
    except Exception: pass
    con.execute("create table if not exists meta(key text primary key,value text)")
    con.execute("""create table if not exists playback_history(
        path text primary key, project text not null, position real not null,
        duration real not null, completed integer not null default 0,
        signature text not null, updated_at real not null)""")
    con.execute("create table if not exists accounts(id integer primary key autoincrement,login text not null unique,password_hash text not null,is_admin integer not null default 0)")
    con.execute("create table if not exists account_sessions(token_hash text primary key,user_id integer not null,expires real not null)")
    con.execute("""create table if not exists account_playback_history(
        user_id integer not null,path text not null,project text not null,position real not null,
        duration real not null,completed integer not null default 0,signature text not null,updated_at real not null,
        primary key(user_id,path))""")
    if not con.execute("select 1 from accounts limit 1").fetchone():
        con.execute("insert or ignore into accounts(login,password_hash,is_admin) values(?,?,1)",('admin',auth_password('admin')))
    if not con.execute("select 1 from meta where key='account_history_migrated'").fetchone():
        admin=con.execute("select id from accounts where login='admin'").fetchone()
        if admin:con.execute("insert or ignore into account_playback_history select ?,path,project,position,duration,completed,signature,updated_at from playback_history",(admin['id'],))
        con.execute("insert or ignore into meta(key,value) values('account_history_migrated','1')")
    con.execute("""create table if not exists bindings(
        kind text not null,indexer_id integer not null,enabled integer not null default 1,
        primary key(kind,indexer_id))""")
    # Коллекции библиотеки (серия фильмов одной карточкой); та же схема в download_organizer.py.
    con.execute("""create table if not exists library_collections(
        id integer primary key autoincrement,kind text not null,title text not null,
        created_at text,updated_at text)""")
    con.execute("""create table if not exists library_collection_items(
        collection_id integer not null,path text not null,position integer not null default 0,
        primary key(collection_id,path))""")
    con.execute("""create table if not exists catalog(
        kind text,mode text,rank integer,title text,year text,overview text,
        poster text,external_id integer,primary key(kind,mode,rank))""")
    con.execute("""create table if not exists provider_feed(
        kind text,indexer_id integer,indexer text,title text,size integer,seeders integer,
        peers integer,published_at text,guid text,download_url text,
        primary key(kind,indexer_id,guid))""")
    con.execute("""create table if not exists search_release(
        token text primary key, kind text, title text, payload text, created_at text)""")
    con.execute("""create table if not exists favorites(
        fav_key text primary key, kind text not null, external_id text,
        title text not null, year text, overview text, poster text,
        genres text, rating real, runtime integer, status text,
        studio text, network text, catalog text, raw_json text,
        created_at text)""")
    con.execute("""create table if not exists download_jobs(
        hash text primary key, kind text not null, media_title text, season integer,
        release_title text, category text, status text, created_at text,
        completed_at text, final_path text, error text)""")
    con.execute("""create table if not exists mediahub_settings(
        key text primary key, value text not null)""")
    con.execute("""create table if not exists library_cache(
        kind text not null, item_key text not null, title text not null,
        year text, overview text, poster text, path text, added_at text,
        has_file integer not null default 0, catalog_source text,
        external_id text, extra_json text,
        primary key(kind,item_key))""")
    con.execute("""create table if not exists source_state(
        source text primary key, ok integer not null default 0,
        item_count integer not null default 0, last_success text,
        last_error text, duration_ms integer not null default 0)""")
    con.execute("""create table if not exists external_discovery(
        source text not null, item_key text not null, title text not null,
        year text, overview text, poster text, url text, added_at text,
        extra_json text, primary key(source,item_key))""")
    con.execute("""create table if not exists notes(
        id integer primary key autoincrement, text text not null,
        kind text not null default 'movies', status text not null default 'new',
        comment text, matched_json text, suggestions_json text,
        created_at text, updated_at text)""")
    con.execute("""create table if not exists page_watch(
        id integer primary key autoincrement, url text not null unique,
        title text, poster text, overview text, kind text default 'tv',
        category text default 'manual', mode text default 'release',
        auto_download integer not null default 0,
        last_signature text, last_items text, episodes integer not null default 0,
        status text default 'new', error text, unseen integer not null default 0,
        created_at text, checked_at text, changed_at text)""")
    con.execute("""create table if not exists user_tracking(
        track_key text primary key, kind text not null, item_key text,
        external_id text, title text, created_at text)""")
    con.execute("""create table if not exists feed_items(
        url text primary key, watch_id integer, title text, poster text, year text,
        overview text, has_torrent integer not null default 0,
        checked_at text, first_seen text, error text)""")
    con.execute("create index if not exists idx_feed_items_torrent on feed_items(has_torrent,checked_at)")
    con.execute("""create table if not exists site_bookmarks(
        id integer primary key autoincrement, title text, url text not null,
        note text, created_at text)""")
    con.execute("""create table if not exists manual_meta(
        path text primary key, kind text, title text, year text, poster text,
        overview text, source_url text, episodes integer not null default 0,
        updated_at text)""")
    con.execute("""create table if not exists download_meta(
        hash text primary key, title text, year text, poster text, overview text,
        source_url text, created_at text)""")
    con.execute("""create table if not exists api_cache(
        cache_key text primary key, payload text not null, updated_at text)""")
    con.execute("""create table if not exists tmdb_detail_cache(
        cache_key text primary key, kind text not null, external_id text not null,
        title text, payload text not null, in_library integer not null default 0,
        updated_at text)""")
    con.execute("create index if not exists idx_tmdb_detail_lib on tmdb_detail_cache(in_library)")
    con.execute("""create table if not exists disk_labels(
        device text primary key, label text not null, note text, updated_at text)""")
    con.execute("""create table if not exists activity_log(
        id integer primary key autoincrement, at text not null, action text not null,
        title text, details text, ok integer not null default 1)""")
    con.execute("""create table if not exists browse_cache(
        bucket text not null, kind text not null, external_id text not null, rank integer not null,
        title text not null, original_title text, year text, overview text, poster text,
        genres_json text, rating real, popularity real, vote_count integer, raw_json text,
        updated_at text not null, primary key(bucket,external_id))""")
    con.execute("""create table if not exists browse_state(
        bucket text primary key, kind text not null, next_page integer not null default 1,
        loaded_pages integer not null default 0, total_pages integer not null default 0,
        exhausted integer not null default 0, updated_at text, last_error text)""")
    con.execute("""create table if not exists release_prefetch(
        cache_key text primary key, kind text not null, catalog text, external_id text,
        title text not null, status text not null default 'queued', result_json text,
        result_count integer not null default 0, updated_at text, last_error text)""")
    con.execute("create index if not exists idx_release_prefetch_status on release_prefetch(status,updated_at)")
    con.execute("create index if not exists idx_release_prefetch_media on release_prefetch(kind,catalog,external_id)")
    con.execute("create index if not exists idx_browse_cache_kind on browse_cache(kind)")
    con.execute("create index if not exists idx_browse_cache_bucket_rank on browse_cache(bucket,rank)")

    # Upgrade old catalog table in-place.
    cols={r[1] for r in con.execute("pragma table_info(catalog)").fetchall()}
    if "catalog_source" not in cols:
        con.execute("alter table catalog add column catalog_source text")
    if "extra_json" not in cols:
        con.execute("alter table catalog add column extra_json text")
    # Жанры и рейтинг со страницы-источника: без них карточка добирала их из TMDB.
    cols={r[1] for r in con.execute("pragma table_info(manual_meta)").fetchall()}
    if "extra_json" not in cols:
        con.execute("alter table manual_meta add column extra_json text")
    con.commit()
    return con

def bound_indexers(kind):
    with cache_db() as con:
        rows=con.execute("select indexer_id from bindings where kind=? and enabled=1",(kind,)).fetchall()
    return [int(r["indexer_id"]) for r in rows]

def bindings_configured(kind):
    with cache_db() as con:
        row=con.execute("select count(*) as n from bindings where kind=?",(kind,)).fetchone()
    return bool(row and int(row["n"] or 0))

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
    return kind in {"movies","tv"}

async def enabled_indexer_ids(kind=None):
    if not PROWLARR_KEY:
        return []
    try:
        rows=await get_json(PROWLARR_URL,PROWLARR_KEY,"/api/v1/indexer")
        return [int(x.get("id")) for x in rows if x.get("enable",True) and x.get("id") is not None and (not kind or indexer_supports_kind(x,kind))]
    except Exception:
        return []

async def prowlarr_search(q,kind,indexer_ids=None,limit=100):
    """Query indexers independently. A slow/broken indexer can no longer hold
    the whole search hostage. Successful partial results are returned."""
    if not PROWLARR_KEY:
        return []

    ids=list(indexer_ids or [])
    if not ids:
        ids=bound_indexers(kind)
    if not ids and bindings_configured(kind):
        return []
    if not ids:
        ids=await enabled_indexer_ids(kind)
    if not ids:
        return []

    sem=asyncio.Semaphore(4)
    per_indexer=max(20,min(60,limit))

    async def one(idx):
        async with sem:
            async with httpx.AsyncClient(timeout=httpx.Timeout(14.0,connect=5.0),trust_env=False) as c:
                async def req(with_category=True):
                    params=[
                        ("query",q),("type","search"),
                        ("indexerIds",str(idx)),
                        ("limit",str(per_indexer)),("offset","0")
                    ]
                    if with_category:
                        params.append(("categories",str(CATEGORIES.get(kind,2000))))
                    r=await c.get(
                        PROWLARR_URL+"/api/v1/search",
                        headers={"X-Api-Key":PROWLARR_KEY},
                        params=params
                    )
                    r.raise_for_status()
                    return r.json()
                try:
                    rows=await req(True)
                    if not rows and q.strip():
                        rows=await req(False)
                    return rows
                except Exception:
                    return []

    groups=await asyncio.gather(*(one(i) for i in ids),return_exceptions=False)
    merged=[]
    seen=set()
    for rows in groups:
        for x in rows:
            if not release_matches_kind(x,kind):
                continue
            key=str(x.get("guid") or x.get("downloadUrl") or x.get("title") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(x)

    def score(x):
        return (
            int(x.get("seeders") or 0),
            str(x.get("publishDate") or "")
        )
    merged.sort(key=score,reverse=True)
    return merged[:limit]

ANILIST_URL="https://graphql.anilist.co"

def strip_html(text):
    return re.sub(r"<[^>]+>"," ",text or "").replace("&quot;",'"').replace("&#039;","'").strip()

async def anilist_search(q):
    query=r"""
    query ($search:String!) {
      Page(page:1,perPage:30) {
        media(search:$search,type:ANIME,isAdult:false,sort:SEARCH_MATCH) {
          id idMal
          title { romaji english native }
          description
          seasonYear
          format
          status
          episodes
          duration
          averageScore
          popularity
          genres
          coverImage { large extraLarge }
          bannerImage
          studios(isMain:true) { nodes { name } }
        }
      }
    }
    """
    try:
        async with external_async_client(22) as c:
            r=await external_request(
                c,"POST",ANILIST_URL,
                json_body={"query":query,"variables":{"search":q}},
                headers={"Accept":"application/json","Content-Type":"application/json"},retries=3
            )
            rows=((r.json().get("data") or {}).get("Page") or {}).get("media") or []
    except Exception:
        return []
    out=[]
    for x in rows:
        title=(x.get("title") or {})
        studio=((x.get("studios") or {}).get("nodes") or [{}])[0].get("name","")
        out.append({
            "title":title.get("english") or title.get("romaji") or title.get("native"),
            "originalTitle":title.get("romaji") or title.get("native") or "",
            "year":x.get("seasonYear"),
            "overview":strip_html(x.get("description")),
            "poster":((x.get("coverImage") or {}).get("extraLarge")
                      or (x.get("coverImage") or {}).get("large")),
            "externalId":x.get("id"),
            "catalog":"anilist",
            "genres":x.get("genres") or [],
            "runtime":x.get("duration") or 0,
            "rating":(float(x.get("averageScore") or 0)/10.0) if x.get("averageScore") else None,
            "status":x.get("status") or "",
            "studio":studio,
            "network":"",
            "episodes":x.get("episodes"),
            "format":x.get("format"),
            "popularity":x.get("popularity"),
            "idMal":x.get("idMal"),
        })
    return out

async def anilist_details(external_id):
    if not external_id:
        return {}
    query=r"""
    query ($id:Int!) {
      Media(id:$id,type:ANIME) {
        id idMal
        title { romaji english native }
        description
        seasonYear
        format
        status
        episodes
        duration
        averageScore
        popularity
        genres
        coverImage { large extraLarge }
        bannerImage
        studios(isMain:true) { nodes { name } }
        nextAiringEpisode { episode timeUntilAiring }
      }
    }
    """
    try:
        async with external_async_client(22) as c:
            r=await external_request(
                c,"POST",ANILIST_URL,
                json_body={"query":query,"variables":{"id":int(external_id)}},
                headers={"Accept":"application/json","Content-Type":"application/json"},retries=3
            )
            x=(r.json().get("data") or {}).get("Media") or {}
    except Exception:
        return {}
    title=x.get("title") or {}
    studio=((x.get("studios") or {}).get("nodes") or [{}])[0].get("name","")
    return {
        "title":title.get("english") or title.get("romaji") or title.get("native"),
        "originalTitle":title.get("romaji") or title.get("native") or "",
        "year":x.get("seasonYear"),
        "overview":strip_html(x.get("description")),
        "poster":((x.get("coverImage") or {}).get("extraLarge")
                  or (x.get("coverImage") or {}).get("large")),
        "externalId":x.get("id"),
        "catalog":"anilist",
        "genres":x.get("genres") or [],
        "runtime":x.get("duration") or 0,
        "rating":(float(x.get("averageScore") or 0)/10.0) if x.get("averageScore") else None,
        "status":x.get("status") or "",
        "studio":studio,
        "network":"",
        "episodes":x.get("episodes"),
        "format":x.get("format"),
        "popularity":x.get("popularity"),
        "idMal":x.get("idMal"),
        "nextAiringEpisode":x.get("nextAiringEpisode"),
    }

def release_quality(title):
    t=(title or "").upper()
    if "2160P" in t or "4K" in t:
        return "2160p"
    if "1080P" in t:
        return "1080p"
    if "720P" in t:
        return "720p"
    if "480P" in t:
        return "480p"
    return ""

# Раздачи, которые скачаются, но не заиграют: образ диска нужно распаковывать
# вручную, а RMVB Плеер MediaHUB не открывает. «BDRemux» и «BDRip» — обычные mkv,
# поэтому проверяются только явные признаки образа.
RELEASE_UNPLAYABLE_RULES=[
    (r"(?<![a-z0-9])iso(?![a-z0-9])","ISO","образ диска"),
    (r"(?<![a-z0-9])bdmv(?![a-z0-9])","BDMV","структура Blu-ray диска"),
    (r"(?<![a-z0-9])video_ts(?![a-z0-9])","VIDEO_TS","структура DVD диска"),
    (r"(?<![a-z0-9])avchd(?![a-z0-9])","AVCHD","структура AVCHD диска"),
    # Цифра не должна быть началом «5.1»: DVD 5.1 — это звук, а не образ диска.
    (r"(?<![a-z0-9])dvd\s?[-_]?\s?[59](?![a-z0-9])(?!\.\d)","DVD9/DVD5","образ DVD"),
    (r"(?<![a-z0-9])bd\s?[-_]?\s?(25|50|66|100)(?![a-z0-9])(?!\.\d)","BD50","образ Blu-ray"),
    (r"blu\s?[-_]?\s?ray\s+disc","Blu-ray Disc","образ Blu-ray"),
    (r"(?<![a-z0-9])rmvb(?![a-z0-9])","RMVB","контейнер RMVB"),
]

def release_playback(title):
    """Можно ли смотреть такую раздачу без ручной возни.

    Формат берётся из названия — это всё, что отдают индексеры. Возвращает
    {format, playable, reason}: reason объясняет отказ по-русски.
    """
    t=(title or "").lower()
    for pattern,label,reason in RELEASE_UNPLAYABLE_RULES:
        if re.search(pattern,t):
            return {"format":label,"playable":False,"reason":f"{label}: {reason}"}
    fmt=""
    # «TS» в названии релиза — это TeleSync, а не контейнер, поэтому его здесь нет.
    m=re.search(r"(?<![a-z0-9])(mkv|mp4|avi|m2ts|webm|mov|wmv|mpg|mpeg|vob|flv|divx)(?![a-z0-9])",t)
    if m:
        label,support=format_support(m.group(1))
        fmt=label
        if support=="transcode":
            return {"format":fmt,"playable":True,
                    "reason":f"{fmt}: {FORMAT_SUPPORT_NOTE['transcode']}"}
    return {"format":fmt,"playable":True,"reason":""}

def skip_unplayable_releases():
    return get_setting("skip_unplayable","1")=="1"

def release_smart_score(title, seeders=0, size=0, kind="movies"):
    """Heuristic score for choosing a useful release, not a claim about media quality.
    The title is all Prowlarr consistently exposes across indexers, so every signal
    is deliberately explainable in the UI.
    """
    raw=title or ""; t=raw.upper(); score=20; reasons=[]; warnings=[]; tags=[]
    quality=release_quality(raw)
    if quality=="2160p": score+=30; reasons.append("4K / 2160p"); tags.append("4K")
    elif quality=="1080p": score+=22; reasons.append("1080p"); tags.append("1080p")
    elif quality=="720p": score+=8; reasons.append("720p"); tags.append("720p")
    elif quality=="480p": score-=8; warnings.append("низкое разрешение"); tags.append("480p")

    if re.search(r"\b(HDCAM|CAMRIP|CAM|TELESYNC|HDTS|TS|TELECINE|TC)\b",t):
        score-=75; warnings.append("CAM/TS источник"); tags.append("CAM/TS")
    elif re.search(r"\b(SCR|DVDSCR|WEBSCREENER)\b",t):
        score-=30; warnings.append("screening-копия")

    if re.search(r"\b(REMUX|BLU[ ._-]?RAY|BDREMUX|BDRIP)\b",t):
        score+=14; reasons.append("Blu-ray / Remux"); tags.append("Blu-ray")
    elif re.search(r"\b(WEB[ ._-]?DL|WEB-DL)\b",t):
        score+=12; reasons.append("WEB-DL"); tags.append("WEB-DL")
    elif re.search(r"\bWEBRIP\b",t):
        score+=7; reasons.append("WEBRip"); tags.append("WEBRip")

    if re.search(r"\b(HEVC|H[ .]?265|X265)\b",t):
        score+=10; reasons.append("HEVC / x265"); tags.append("HEVC")
    elif re.search(r"\bAV1\b",t):
        score+=9; reasons.append("AV1"); tags.append("AV1")
    elif re.search(r"\b(X264|H[ .]?264|AVC)\b",t):
        score+=4; reasons.append("H.264"); tags.append("H.264")

    russian = bool(re.search(r"(^|[ ._\-\[\(])(RUS|RU|RUSSIAN|DUB|DUBBED|DVO|MVO|AVO|ДБ|ДУБ|ПРОФ)([ ._\-\]\)]|$)", t))
    explicit_english = bool(re.search(r"(^|[ ._\-\[\(])(ENG|ENGLISH)([ ._\-\]\)]|$)", t))
    if russian:
        score+=25; reasons.append("русская дорожка/озвучка"); tags.append("RU")
    elif explicit_english:
        score-=10; warnings.append("указана только ENG дорожка")

    seeds=max(0,int(seeders or 0))
    if seeds>=100: score+=15; reasons.append(f"{seeds} сидов")
    elif seeds>=30: score+=11; reasons.append(f"{seeds} сидов")
    elif seeds>=10: score+=7; reasons.append(f"{seeds} сидов")
    elif seeds>=3: score+=3
    elif seeds==0: score-=12; warnings.append("нет сидов")
    else: score-=5; warnings.append("мало сидов")

    n=int(size or 0)
    gb=n/(1024**3) if n else 0
    # Reward sane sizes only gently. Packs and remuxes can legitimately be large.
    if gb:
        if quality=="2160p" and 6<=gb<=65: score+=5; reasons.append("разумный размер для 4K")
        elif quality=="1080p" and 2<=gb<=30: score+=5; reasons.append("разумный размер для 1080p")
        elif quality=="720p" and .7<=gb<=14: score+=4
        if kind=="movies" and gb>120: score-=10; warnings.append("очень большой размер")
        if gb<.35 and kind=="movies": score-=12; warnings.append("подозрительно маленький файл")

    if re.search(r"\b(PROPER|REPACK|REAL)\b",t): score+=3; reasons.append("исправленный релиз")

    play=release_playback(raw)
    if not play["playable"]:
        # Образ диска качать бессмысленно: Jellyfin его не откроет.
        score-=60; warnings.append(play["reason"]); tags.append(play["format"])
    elif play["reason"]:
        score-=4; warnings.append(play["reason"])

    score=max(0,min(100,int(score)))
    label="Лучший кандидат" if score>=82 else "Хороший" if score>=65 else "Средний" if score>=45 else "Не рекомендуется"
    return {"score":score,"label":label,"reasons":reasons[:6],"warnings":warnings[:4],"tags":tags[:6],"russian":russian}

def store_release(kind, rel):
    raw=json.dumps(rel,ensure_ascii=False,sort_keys=True,default=str)
    basis="|".join([
        kind, str(rel.get("indexerId") or ""),
        str(rel.get("guid") or rel.get("downloadUrl") or ""),
        str(rel.get("title") or "")
    ])
    token=hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]
    with cache_db() as con:
        con.execute("""insert or replace into search_release
            (token,kind,title,payload,created_at) values(?,?,?,?,?)""",
            (token,kind,rel.get("title") or "",raw,
             datetime.now(timezone.utc).isoformat()))
        con.commit()
    return token

def load_release(token):
    with cache_db() as con:
        row=con.execute("select payload,kind,title from search_release where token=?",(token,)).fetchone()
    if not row:
        return None
    return {"payload":json.loads(row["payload"]),"kind":row["kind"],"title":row["title"]}

async def tmdb_search(q,kind):
    if not TMDB_KEY or kind=="games":return []
    media="movie" if kind=="movies" else "tv"
    try:
        async with external_async_client(22) as c:
            r=await external_request(c,"GET",f"https://api.themoviedb.org/3/search/{media}",
                params=tmdb_auth_params({"language":"ru-RU","query":q,"include_adult":"false"}),
                headers=tmdb_auth_headers(),retries=3)
            rows=(r.json() or {}).get("results",[])
    except Exception:
        return []
    out=[]
    for x in rows[:40]:
        out.append({
            "title":x.get("title") or x.get("name"),
            "year":(x.get("release_date") or x.get("first_air_date") or "")[:4],
            "overview":x.get("overview") or "",
            "poster":"https://image.tmdb.org/t/p/w500"+x["poster_path"] if x.get("poster_path") else None,
            "externalId":x.get("id"),
            "catalog":"tmdb",
            "genres":[],
            "rating":x.get("vote_average"),
            "runtime":0,
            "status":"",
            "studio":"",
            "network":"",
        })
    return out

def _is_under(path:Path, root:Path):
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False

def already_in_library(path:Path):
    return any(_is_under(path,r) for r in (MOVIES_ROOT,TV_ROOT,ANIME_ROOT))

def in_inbox(path:Path):
    return _is_under(path,INBOX_ROOT)

def _season_root_source(src:Path):
    """True when selected folder already contains Season 01 / S01 folders."""
    try:
        for x in src.iterdir():
            if x.is_dir() and re.match(r'^(?:season\s*\d+|s\d{1,2})\b',x.name,re.I):
                return True
    except Exception:
        pass
    return False

def movie_library_folder(src:Path,title:str):
    """Папка фильма в медиатеке.

    У фильма всегда своя папка «Название (Год)»: и одиночный файл, и папка
    раздачи приходят в неё, поэтому в корне movies больше не остаётся голых
    mkv. Год берётся из имени раздачи, если пользователь не указал свой.
    """
    # У файла в запасе только имя без расширения: папка «Фильм.mkv» не нужна.
    fallback=clean_title(src.name) or (src.stem if src.is_file() else src.name)
    title=(title or "").strip() or fallback
    year=_fs_library_year(title) or _fs_library_year(src.name)
    existing=existing_movie_folder(MOVIES_ROOT,title,year)
    return existing or MOVIES_ROOT/(movie_folder_name(title,year) or title)

def library_destination(src:Path,kind:str,title:str,season:int):
    title=(title or clean_title(src.name) or src.name).strip()
    if kind=="movies":
        folder=movie_library_folder(src,title)
        # Одиночный файл кладём внутрь папки фильма, а не переименовываем в неё.
        return folder/src.name if src.is_file() else folder
    root=ANIME_ROOT if kind=="anime" else TV_ROOT
    if src.is_dir() and _season_root_source(src):
        return root/title
    return root/title/f"Season {max(1,int(season or 1)):02d}"

def _season_folder_number(name):
    """Season number when `name` itself looks like a season folder (Season 02, S2, ...)."""
    if re.match(r'^(?:season\s*\d+|s\d{1,2})\b',name,re.I):
        return season_number(name)
    return None

def _remap_season_relpath(rel:Path):
    """Merge mismatched season-folder names onto the canonical "Season NN".

    A duplicate/second-season download rarely spells its season folder the
    same way MediaHub does ("S02" vs "Season 02"). Left alone, merging it
    into an existing title folder creates a sibling folder instead of really
    merging — the same kind of stray duplicate that made the old "uni Kage
    no Jitsuryokusha ni Naritakute!" folder linger. Only the first path
    component is remapped, and only when it actually looks like a season
    folder, so plain files being merged in are never touched.
    """
    parts=rel.parts
    if len(parts)>1:
        n=_season_folder_number(parts[0])
        if n:
            return Path(f"Season {n:02d}",*parts[1:])
    return rel

def _preflight_merge(src:Path,dst:Path):
    """Refuse destructive overwrites. Identical files are safe duplicates."""
    conflicts=[]
    if src.is_file():
        if dst.exists() and dst.is_file():
            try:
                if src.stat().st_size!=dst.stat().st_size:
                    conflicts.append(str(dst))
            except OSError:
                conflicts.append(str(dst))
        elif dst.exists():
            conflicts.append(str(dst))
        return conflicts
    if not dst.exists():
        return conflicts
    if not dst.is_dir():
        return [str(dst)]
    for f in src.rglob('*'):
        if not f.is_file():
            continue
        target=dst/_remap_season_relpath(f.relative_to(src))
        if target.exists():
            if not target.is_file():
                conflicts.append(str(target)); continue
            try:
                if f.stat().st_size!=target.stat().st_size:
                    conflicts.append(str(target))
            except OSError:
                conflicts.append(str(target))
    return conflicts

def move_merge(src:Path,dst:Path):
    """Move, never copy. Merge into an existing destination safely."""
    src=src.resolve(); dst=dst.resolve()
    if src==dst:
        return {"moved":0,"deduped":0,"mode":"already"}
    conflicts=_preflight_merge(src,dst)
    if conflicts:
        raise RuntimeError("Конфликт файлов: "+", ".join(conflicts[:4]))
    moved=deduped=0
    dst.parent.mkdir(parents=True,exist_ok=True)
    if src.is_file():
        if dst.exists():
            src.unlink(); deduped=1
        else:
            shutil.move(str(src),str(dst)); moved=1
        return {"moved":moved,"deduped":deduped,"mode":"move"}
    if not dst.exists():
        shutil.move(str(src),str(dst))
        try:
            moved=sum(1 for f in dst.rglob('*') if f.is_file())
        except Exception:
            moved=1
        return {"moved":moved,"deduped":0,"mode":"move"}
    # Destination exists: merge recursively, remove identical duplicates.
    for f in sorted(src.rglob('*'), key=lambda p: len(p.parts), reverse=False):
        if not f.is_file():
            continue
        target=dst/_remap_season_relpath(f.relative_to(src))
        target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists():
            f.unlink(); deduped+=1
        else:
            shutil.move(str(f),str(target)); moved+=1
    # remove now-empty source tree
    for d in sorted([p for p in src.rglob('*') if p.is_dir()],key=lambda p:len(p.parts),reverse=True):
        try:d.rmdir()
        except OSError:pass
    try:src.rmdir()
    except OSError:pass
    return {"moved":moved,"deduped":deduped,"mode":"move"}

def cleanup_empty_inbox_parents(path:Path):
    p=path.resolve()
    stop=INBOX_ROOT.resolve()
    # Папки категорий (inbox/movies, inbox/tv…) остаются: в них смотрят qBittorrent и проводник,
    # а после их удаления проводник показывал «Папка не найдена» поверх сообщения об успехе.
    while p!=stop and p.parent!=stop and _is_under(p,stop):
        try:
            p.rmdir()
        except OSError:
            break
        p=p.parent

def is_media_root(path:Path):
    try:
        return path.resolve() in {MOVIES_ROOT.resolve(),TV_ROOT.resolve(),ANIME_ROOT.resolve()}
    except Exception:
        return False

def protected_media_dirs():
    """Каркас медиатеки: эти папки удалять нельзя, всё остальное — можно."""
    out=set()
    for p in (MEDIA_ROOT,MEDIA_ROOT/".mediahub",MOVIES_ROOT,TV_ROOT,ANIME_ROOT,INBOX_ROOT,
              INBOX_ROOT/"movies",INBOX_ROOT/"tv",INBOX_ROOT/"anime",INBOX_ROOT/"manual"):
        try:out.add(p.resolve())
        except Exception:pass
    return out

def can_delete_path(path:Path,protected=None):
    """Удалить можно любой файл и любую папку внутри /mnt/media, кроме каркаса."""
    try:
        target=path.resolve()
    except Exception:
        return False
    if not safe_media_path(path):
        return False
    if path.is_file():
        return True
    return target not in (protected if protected is not None else protected_media_dirs())


async def qbit_drop_tasks_under(target:Path):
    """Снять с qBittorrent задачи, чьи файлы лежат внутри пути.

    Сами файлы qBit не трогает — их удаляет MediaHub. Иначе клиент продолжил бы
    раздавать уже удалённое и мог бы скачать его заново.
    """
    removed=0
    c=await qbit_login()
    if not c:
        return 0
    try:
        r=await c.get(QBIT_URL+"/api/v2/torrents/info")
        for t in r.json():
            cp=t.get("content_path") or ""
            if not cp:
                continue
            try:
                inside=_is_under(Path(cp),target) or Path(cp).resolve()==target.resolve()
            except Exception:
                inside=False
            if inside:
                await c.post(QBIT_URL+"/api/v2/torrents/delete",data={"hashes":t.get("hash"),"deleteFiles":"false"})
                removed+=1
    except Exception:
        pass
    finally:
        await c.aclose()
    return removed


def path_size(target:Path):
    """Сколько места освободится: размер файла или всей папки."""
    try:
        if target.is_file():
            return target.stat().st_size
    except OSError:
        return 0
    return folder_stats(target)[0]


def delete_media_tree(target:Path):
    """Удалить файл или папку и одинаково объяснить отказ во всех эндпоинтах."""
    is_file=target.is_file() or target.is_symlink()
    try:
        if is_file:
            target.unlink()
        else:
            shutil.rmtree(target)
    except PermissionError:
        raise HTTPException(403,"Нет прав на удаление — проверь владельца папки")
    except OSError as e:
        raise HTTPException(500,f"Не удалось удалить: {e}")
    return is_file


def forget_media_path(path_key:str):
    """Забыть путь вместе со всем, что лежало внутри него."""
    key=str(path_key or "").rstrip("/")
    if not key:
        return
    try:
        with cache_db() as con:
            # Вместе с папкой уходят и карточки всего, что лежало внутри неё.
            con.execute("delete from library_cache where rtrim(path,'/')=? or path like ?",(key,key+"/%"))
            con.execute("delete from manual_meta where path=? or path like ?",(key,key+"/%"))
            con.commit()
    except Exception:
        pass


def reset_fs_scan_cache():
    """Скан медиатеки закэширован — сбрасываем, чтобы списки обновились сразу."""
    for kind in ("movies","tv","anime"):
        try:set_setting(f"fs_scan_{kind}","")
        except Exception:pass


_ORGANIZER_MODULE=None

def download_organizer_module():
    """Функции download_organizer.py (раскладка коллекций) без запуска самого организатора."""
    global _ORGANIZER_MODULE
    if _ORGANIZER_MODULE is None:
        try:
            import importlib.util
            spec=importlib.util.spec_from_file_location("mediahub_download_organizer",BASE_DIR/"download_organizer.py")
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            _ORGANIZER_MODULE=module
        except Exception as e:
            print("download_organizer import failed:",e)
            _ORGANIZER_MODULE=False
    return _ORGANIZER_MODULE or None

def save_split_collection(organizer,parts,source_name,folders):
    """Фильмы разложенной раздачи сразу собрать в коллекцию библиотеки."""
    try:
        with cache_db() as con:
            organizer.save_collection(con,"movies",organizer.collection_name([p["title"] for p in parts],source_name),folders)
            con.commit()
    except Exception as e:
        print("collection save failed:",e)

def import_folder_to_library(src:Path,kind:str,title:str,season:int):
    # Папка, лежащая прямо в корне раздела, формально «уже в библиотеке», но
    # её всё равно нужно упорядочить — перенести в папку тайтла.
    if already_in_library(src) and not is_media_root(src.parent):
        return src,0,{"move":0,"deduped":0,"already":1}
    parent=src.parent
    # Коллекция фильмов — каждому фильму своя папка; та же раскладка, что у организатора загрузок.
    parts=[]
    if kind=="movies":
        organizer=download_organizer_module()
        if organizer:
            try:parts=organizer.collection_parts(src)
            except Exception:parts=[]
    if parts:
        folders=organizer.organize_collection(src,parts,src.name)
        save_split_collection(organizer,parts,src.name,folders)
        if in_inbox(parent):
            cleanup_empty_inbox_parents(parent)
        return folders[0],len(parts),{"move":len(parts),"deduped":0,"already":0,"collection":[str(f) for f in folders]}
    dest=library_destination(src,kind,title,season)
    result=move_merge(src,dest)
    if in_inbox(parent):
        cleanup_empty_inbox_parents(parent)
    return dest,result.get("moved",0),{
        "move":result.get("moved",0),
        "deduped":result.get("deduped",0),
        "already":0,
    }

@app.get("/",response_class=HTMLResponse)
async def home(request:Request):
    # Never let a browser/proxy pin an old MediaHub shell after an upgrade.
    return templates.TemplateResponse(
        "index.html",
        {"request":request,"app_version":APP_VERSION},
        headers={
            "Cache-Control":"no-store, no-cache, must-revalidate, max-age=0",
            "Pragma":"no-cache",
            "Expires":"0",
        },
    )

@app.get("/api/version")
async def api_version(request:Request):
    # Маршрут открыт без входа: снаружи не раскрываем устройство сервера.
    if auth_external(request):return {"name":"MediaHub","version":APP_VERSION}
    return {
        "name":"MediaHub",
        "version":APP_VERSION,
        "appDir":str(BASE_DIR),
        "python":sys.version.split()[0],
    }


# --- v21.23 загрузка установочных образов Linux ------------------------------
# MediaHub не может переустановить систему, на которой сам работает. Зато он
# может скачать установочный образ на хранилище: с него ставят систему на другую
# машину или на этот же сервер после перезагрузки с флешки.

ISO_SOURCES=[
    {"id":"debian","title":"Debian stable · netinst",
     "index":"https://cdimage.debian.org/debian-cd/current/amd64/iso-cd/",
     "pattern":r"debian-[\d.]+-amd64-netinst\.iso",
     "note":"Минимальный установщик, ~700 МБ. Рекомендуется для домашнего сервера."},
    {"id":"ubuntu","title":"Ubuntu Server 24.04 LTS",
     "index":"https://releases.ubuntu.com/24.04/",
     "pattern":r"ubuntu-24\.04[\d.]*-live-server-amd64\.iso",
     "note":"Серверная LTS-сборка, ~2,5 ГБ."},
    {"id":"proxmox","title":"Proxmox VE",
     "index":"https://enterprise.proxmox.com/iso/",
     "pattern":r"proxmox-ve_[\d.\-]+\.iso",
     "note":"Гипервизор: MediaHub и сервисы живут в отдельных контейнерах."},
]

ISO_DIR_NAME="iso"
_ISO_TASKS={}


def iso_dir():
    path=Path(MEDIA_ROOT)/ISO_DIR_NAME
    try:path.mkdir(parents=True,exist_ok=True)
    except Exception:pass
    return path


async def _iso_probe(source):
    """Найти в каталоге дистрибутива актуальный файл образа и его контрольную сумму."""
    from urllib.parse import urljoin
    out={**source,"file":"","url":"","sha256":"","size":0,"error":""}
    try:
        async with httpx.AsyncClient(timeout=20,trust_env=False,follow_redirects=True) as c:
            r=await c.get(source["index"],headers={"User-Agent":"MediaHub"})
            if r.status_code>=400:
                out["error"]=f"каталог ответил HTTP {r.status_code}"; return out
            names=re.findall(source["pattern"],r.text)
            if not names:
                out["error"]="в каталоге нет подходящего образа"; return out
            name=sorted(set(names))[-1]
            out["file"]=name
            out["url"]=urljoin(source["index"],name)
            try:
                s=await c.get(urljoin(source["index"],"SHA256SUMS"),headers={"User-Agent":"MediaHub"})
                if s.status_code<300:
                    for line in (s.text or "").splitlines():
                        parts=line.split()
                        if len(parts)==2 and parts[1].lstrip("*")==name:
                            out["sha256"]=parts[0]; break
            except Exception:
                pass
            try:
                h=await c.head(out["url"],headers={"User-Agent":"MediaHub"})
                out["size"]=int(h.headers.get("content-length") or 0)
            except Exception:
                pass
    except Exception as e:
        out["error"]=str(e)[:150]
    return out


@app.get("/api/setup/iso/catalog")
async def iso_catalog():
    results=await asyncio.gather(*[_iso_probe(s) for s in ISO_SOURCES],return_exceptions=True)
    items=[]
    for src,res in zip(ISO_SOURCES,results):
        if isinstance(res,Exception):
            items.append({**src,"error":str(res)[:120],"file":"","url":"","size":0,"sha256":""})
        else:
            items.append(res)
    for x in items:
        x["sizeHuman"]=human(x.get("size") or 0) if x.get("size") else ""
    return {"dir":str(iso_dir()),"items":items,"downloaded":_iso_local_files()}


def _iso_local_files():
    out=[]
    try:
        for f in sorted(iso_dir().glob("*.iso")):
            try:st=f.stat()
            except Exception:continue
            out.append({"name":f.name,"path":str(f),"size":st.st_size,"sizeHuman":human(st.st_size),
                        "modified":datetime.fromtimestamp(st.st_mtime,tz=timezone.utc).isoformat()})
    except Exception:
        pass
    return out


def _iso_state(name=None):
    raw=get_setting("iso_download_state","")
    try:state=json.loads(raw) if raw else {}
    except Exception:state={}
    return state.get(name) if name else state


def _iso_set_state(name,patch):
    state=_iso_state() or {}
    cur=state.get(name) or {}
    cur.update(patch); cur["updatedAt"]=datetime.now(timezone.utc).isoformat()
    state[name]=cur
    set_setting("iso_download_state",json.dumps(state,ensure_ascii=False))


async def _iso_download_worker(url,name,sha256=""):
    target=iso_dir()/name
    tmp=target.with_suffix(target.suffix+".part")
    _iso_set_state(name,{"status":"downloading","downloaded":0,"total":0,"error":"","path":str(target)})
    try:
        async with httpx.AsyncClient(timeout=None,trust_env=False,follow_redirects=True) as c:
            async with c.stream("GET",url,headers={"User-Agent":"MediaHub"}) as r:
                if r.status_code>=400:
                    raise RuntimeError(f"источник ответил HTTP {r.status_code}")
                total=int(r.headers.get("content-length") or 0)
                got=0; last=0
                digest=hashlib.sha256()
                with open(tmp,"wb") as fh:
                    async for chunk in r.aiter_bytes(1024*512):
                        fh.write(chunk); digest.update(chunk); got+=len(chunk)
                        if got-last>8*1024*1024:
                            last=got
                            _iso_set_state(name,{"status":"downloading","downloaded":got,"total":total})
        if sha256 and digest.hexdigest().lower()!=sha256.lower():
            tmp.unlink(missing_ok=True)
            raise RuntimeError("контрольная сумма не совпала, файл удалён")
        tmp.replace(target)
        _iso_set_state(name,{"status":"ready","downloaded":target.stat().st_size,
                             "total":target.stat().st_size,"verified":bool(sha256),"error":""})
        log_activity("iso-download",name,f"скачан в {target}",True)
    except asyncio.CancelledError:
        tmp.unlink(missing_ok=True)
        _iso_set_state(name,{"status":"cancelled","error":"отменено"})
        raise
    except Exception as e:
        tmp.unlink(missing_ok=True)
        _iso_set_state(name,{"status":"error","error":str(e)[:200]})
        log_activity("iso-download",name,str(e)[:200],False)


@app.post("/api/setup/iso/download")
async def iso_download(url:str=Form(...),name:str=Form(""),sha256:str=Form("")):
    url=(url or "").strip()
    if not url.lower().startswith("https://"):
        raise HTTPException(400,"Образ скачивается только по https")
    name=(name or url.rsplit("/",1)[-1]).strip()
    if not re.fullmatch(r"[A-Za-z0-9._\-+]{4,120}\.iso",name):
        raise HTTPException(400,"Недопустимое имя файла образа")
    task=_ISO_TASKS.get(name)
    if task and not task.done():
        return {"ok":True,"message":"Этот образ уже скачивается","name":name}
    free=shutil.disk_usage(iso_dir()).free
    if free<3*1024**3:
        raise HTTPException(400,f"На хранилище мало места: свободно {human(free)}")
    task=asyncio.create_task(_iso_download_worker(url,name,sha256))
    _ISO_TASKS[name]=task
    task.add_done_callback(lambda _:_ISO_TASKS.pop(name,None))
    return {"ok":True,"message":f"Качаю {name} в {iso_dir()}","name":name}


@app.get("/api/setup/iso/status")
async def iso_status():
    state=_iso_state() or {}
    for name,item in state.items():
        total=int(item.get("total") or 0); got=int(item.get("downloaded") or 0)
        item["percent"]=round(got/total*100,1) if total else 0
        item["downloadedHuman"]=human(got); item["totalHuman"]=human(total)
    try:
        free=shutil.disk_usage(iso_dir()).free
    except Exception:
        free=0
    return {"dir":str(iso_dir()),"free":free,"freeHuman":human(free),
            "jobs":state,"files":_iso_local_files()}


@app.post("/api/setup/iso/cancel")
async def iso_cancel(name:str=Form(...)):
    task=_ISO_TASKS.get(name)
    if task and not task.done():
        task.cancel()
        return {"ok":True,"message":"Загрузка отменена"}
    return {"ok":False,"message":"Такая загрузка не идёт"}


@app.delete("/api/setup/iso/{name}")
async def iso_delete(name:str):
    if not re.fullmatch(r"[A-Za-z0-9._\-+]{4,120}\.iso",name):
        raise HTTPException(400,"Недопустимое имя файла")
    target=iso_dir()/name
    if not target.exists():
        raise HTTPException(404,"Образ не найден")
    try:
        target.unlink()
    except Exception as e:
        raise HTTPException(500,f"Не удалось удалить: {str(e)[:120]}")
    state=_iso_state() or {}
    state.pop(name,None)
    set_setting("iso_download_state",json.dumps(state,ensure_ascii=False))
    log_activity("iso-delete",name,"образ удалён",True)
    return {"ok":True,"message":"Образ удалён"}


@app.get("/api/setup/status")
async def setup_status(request:Request):
    # Проверки запускают systemctl и сервисы с --version: в потоке, чтобы плеер и пульт не ждали.
    return {
        "host": await asyncio.to_thread(setup_host_info),
        "mediaRoot": str(Path(os.getenv("MEDIA_ROOT","/mnt/media"))),
        "configuredMediaRoot": str(__import__("system_setup").media_root()),
        "components": await asyncio.to_thread(setup_component_status),
        "recommended": SETUP_RECOMMENDED,
        "job": setup_read_state(),
        "localInstallAllowed": setup_request_allowed(request),
        "noPassword": True,
    }

SETUP_WARM_TASKS=set()

@app.on_event("startup")
async def _warm_setup_versions():
    """Прогреть версии сервисов в фоне: первое открытие «Настроек» не ждёт запуска .NET."""
    task=asyncio.create_task(asyncio.to_thread(__import__("system_setup").warm_versions))
    SETUP_WARM_TASKS.add(task);task.add_done_callback(SETUP_WARM_TASKS.discard)

UPDATE_REPO="viendhyra/MediaHUB"
UPDATE_CACHE={}
UPDATE_CHECK_LOCK=asyncio.Lock()

def update_version_tuple(value):
    """Сравнить числовые версии без лексикографических ошибок."""
    if not re.fullmatch(r'[0-9]+(?:\.[0-9]+){1,3}',str(value)):raise ValueError('Неверный формат версии')
    return tuple(int(x) for x in str(value).split('.'))

async def github_update_info(force=False):
    """Прочитать версию и новости одного коммита GitHub с кэшем и таймаутом."""
    async with UPDATE_CHECK_LOCK:
        if not force and UPDATE_CACHE.get('expires',0)>time.time():return dict(UPDATE_CACHE['data'])
        result={'currentVersion':APP_VERSION,'available':False,'repository':f'https://github.com/{UPDATE_REPO}'}
        # Битый IPv6 отказывает только по таймауту подключения: сначала IPv4, затем как настроено в системе.
        for local_address in ('0.0.0.0',None):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(12,connect=5),trust_env=False,follow_redirects=False,transport=httpx.AsyncHTTPTransport(local_address=local_address)) as client:
                    response=await client.get(f'https://api.github.com/repos/{UPDATE_REPO}/commits/main',headers={'Accept':'application/vnd.github+json','User-Agent':f'MediaHUB/{APP_VERSION}'})
                    response.raise_for_status();commit=response.json()['sha']
                    if not re.fullmatch('[0-9a-f]{40}',commit):raise ValueError('Некорректный ответ GitHub')
                    base=f'https://raw.githubusercontent.com/{UPDATE_REPO}/{commit}'
                    version_response=await client.get(base+'/VERSION.txt');version_response.raise_for_status()
                    version=version_response.text.strip()
                    available=update_version_tuple(version)>update_version_tuple(APP_VERSION)
                    notes_response=await client.get(base+'/CHANGELOG.md');notes_response.raise_for_status()
                    notes=notes_response.text.split('\n## ',1)[0].strip()
                    # CHANGELOG начинается с заголовка документа; показываем первую запись.
                    if '\n## ' in notes_response.text:notes='## '+notes_response.text.split('\n## ',1)[1].split('\n## ',1)[0]
                    result.update(latestVersion=version,available=available,commit=commit,notes=notes[:6000],checkedAt=time.time())
                result.pop('error',None);break
            except Exception:
                result['error']='Не удалось проверить GitHub. Проверьте интернет и повторите позже.'
        UPDATE_CACHE.update(data=result,expires=time.time()+(60 if result.get('error') else 3600))
        return dict(result)

@app.get('/api/updates')
async def updates_check(request:Request,force:bool=False):
    """Проверить обновления с доступом только для администратора локальной сети."""
    require_setup_access(request)
    return await github_update_info(force)

@app.post('/api/updates/install')
async def updates_install(request:Request,commit:str=Form(...)):
    """Запустить только предложенный коммит через отдельную systemd-службу."""
    require_setup_access(request)
    info=await github_update_info()
    if not info.get('available') or commit!=info.get('commit'):raise HTTPException(409,'Версия изменилась. Проверьте обновления ещё раз.')
    if setup_read_state().get('running'):raise HTTPException(409,'Дождитесь установки компонентов')
    if not Path('/usr/local/sbin/mediahub-update').is_file():raise HTTPException(409,'Сначала установите эту версию через install.sh — команда обновления ещё не зарегистрирована')
    process=await asyncio.create_subprocess_exec('systemd-run','--unit=mediahub-update','--collect','/usr/local/sbin/mediahub-update',commit,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    try:output,error=await asyncio.wait_for(process.communicate(),timeout=10)
    except asyncio.TimeoutError:raise HTTPException(504,'Не удалось получить ответ systemd')
    if process.returncode:raise HTTPException(409,'Обновление уже выполняется или systemd не смог его запустить')
    return {'ok':True,'message':'Обновление запущено. Портал ненадолго перезапустится.'}

@app.get('/api/updates/status')
def updates_status(request:Request):
    """Вернуть состояние и ограниченный хвост журнала без секретов настроек."""
    require_setup_access(request)
    try:state=json.loads(Path('/var/lib/mediahub/update.json').read_text())
    except Exception:state={'status':'idle','message':'Обновления ещё не запускались'}
    try:
        with Path('/var/lib/mediahub/update.log').open('rb') as file:
            file.seek(0,2);file.seek(max(0,file.tell()-12000));state['log']=file.read().decode('utf-8',errors='replace')
    except Exception:state['log']=''
    return state

@app.get('/api/apps')
def apps_info():
    """Показать только действительно присутствующий проверенный APK."""
    path=BASE_DIR/'static'/'android.json'
    try:manifest=json.loads(path.read_text(encoding='utf-8'))
    except Exception:return {'available':False}
    apk=BASE_DIR/'static'/str(manifest.get('filename',''))
    if apk.parent!=BASE_DIR/'static' or not apk.is_file():return {'available':False}
    return dict(manifest,available=True,url=f'/api/apps/android/download?version={manifest.get("version","")}')

@app.get('/api/apps/android/download')
def apps_download():
    """Отдать APK вложением, без долгого кэша для изменяемой ссылки."""
    manifest=apps_info()
    if not manifest.get('available'):raise HTTPException(404,'APK пока не размещён на сервере')
    return FileResponse(BASE_DIR/'static'/manifest['filename'],media_type='application/vnd.android.package-archive',filename=manifest['filename'],headers={'Cache-Control':'no-store'})

TV_INSTALL={'status':'idle','message':'Установка на ТВ ещё не запускалась','log':[]}
TV_INSTALL_TASKS=set()
# Ключ adb хранится постоянно: «Всегда разрешать» на ТВ запоминает именно его.
TV_ADB_HOME=Path(os.getenv('MEDIAHUB_ADB_HOME','/var/lib/mediahub/adb'))
TV_INSTALL_ERRORS={
    'INSTALL_FAILED_UPDATE_INCOMPATIBLE':'На ТВ стоит MediaHUB с другой подписью: удалите его на ТВ и повторите.',
    'INSTALL_FAILED_VERSION_DOWNGRADE':'На ТВ уже установлена более новая версия MediaHUB.',
    'INSUFFICIENT_STORAGE':'На ТВ не хватает места для приложения.',
    'INSTALL_FAILED_OLDER_SDK':'Телевизору нужен Android 7.0 или новее.',
    'INSTALL_FAILED_USER_RESTRICTED':'ТВ запретил установку через отладку: разрешите её в «Для разработчиков».',
}

def tv_install_target(address,default_port=5555):
    """Проверить адрес ТВ: IPv4 локальной сети и порт ADB."""
    m=re.fullmatch(r'\s*(\d{1,3}(?:\.\d{1,3}){3})(?::(\d{1,5}))?\s*',str(address or ''))
    if not m or (default_port is None and not m.group(2)):raise ValueError('Укажите адрес как 192.168.1.50 или 192.168.1.50:5555')
    try:ip=ipaddress.ip_address(m.group(1))
    except ValueError:raise ValueError('Неверный IP-адрес')
    port=int(m.group(2) or default_port)
    if not ip.is_private or ip.is_loopback or ip.is_link_local or not 1<=port<=65535:raise ValueError('Телевизор должен быть в локальной сети')
    return f'{ip}:{port}'

async def tv_run(*command,timeout=30,env=None):
    """Выполнить команду с ограничением времени и вернуть код и вывод."""
    process=await asyncio.create_subprocess_exec(*command,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT,env=env)
    try:output,_=await asyncio.wait_for(process.communicate(),timeout)
    except asyncio.TimeoutError:
        process.kill();await process.wait()
        raise RuntimeError(f'{Path(command[0]).name} не ответил за {timeout} с')
    return process.returncode,(output or b'').decode('utf-8','replace').strip()

def tv_step(status,message):
    """Записать шаг установки, который видит страница настроек."""
    TV_INSTALL.update(status=status,message=message,updatedAt=time.time())
    TV_INSTALL['log']=(TV_INSTALL.get('log') or [])[-30:]+[message]

async def tv_install_job(target,pair_target,pair_code):
    """Поставить APK с сервера на ТВ так же, как INSTALL_ON_DEVICE.bat: connect → разрешение → install → запуск."""
    adb=shutil.which('adb');env=None
    try:
        apk=apps_info()
        if not apk.get('available'):raise RuntimeError('На сервере нет APK. Обновите MediaHUB.')
        if not adb:
            tv_step('running','Устанавливаю adb на сервер…')
            code,output=await tv_run('apt-get','install','-y','-qq','adb',timeout=600,env=dict(os.environ,DEBIAN_FRONTEND='noninteractive'))
            adb=shutil.which('adb')
            if code or not adb:raise RuntimeError('Не удалось установить adb: '+output[-300:])
        TV_ADB_HOME.mkdir(parents=True,exist_ok=True);env=dict(os.environ,HOME=str(TV_ADB_HOME))
        # Сервер adb уходит в фон и держит канал вывода: ждём только выхода клиента.
        process=await asyncio.create_subprocess_exec(adb,'start-server',stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL,env=env)
        await asyncio.wait_for(process.wait(),30)
        if pair_target:
            tv_step('running',f'Сопряжение с {pair_target}…')
            code,output=await tv_run(adb,'pair',pair_target,pair_code,timeout=30,env=env)
            if 'successfully paired' not in output.lower():raise RuntimeError('Сопряжение не удалось. Проверьте порт и код на экране «Подключение с кодом»: '+output[-200:])
        tv_step('running',f'Подключаюсь к {target}…')
        code,output=await tv_run(adb,'connect',target,timeout=20,env=env)
        if not re.search(r'connected to|failed to authenticate',output,re.I):raise RuntimeError(f'ТВ {target} не принимает подключение отладки. Проверьте IP и включённую отладку по сети. Ответ: {output[-160:]}')
        for attempt in range(60):
            code,output=await tv_run(adb,'-s',target,'get-state',timeout=10,env=env)
            if output.strip()=='device':break
            if attempt==0:tv_step('waiting','На экране ТВ появится «Разрешить отладку?». Отметьте «Всегда разрешать с этого компьютера» и нажмите OK.')
            # После нажатия OK на ТВ соединение нужно открыть заново.
            await tv_run(adb,'connect',target,timeout=10,env=env);await asyncio.sleep(1)
        else:raise RuntimeError('ТВ не дал разрешение на отладку за минуту. Подтвердите окно на экране ТВ и повторите.')
        tv_step('running',f'Устанавливаю MediaHUB {apk["version"]} на ТВ, это займёт до минуты…')
        code,output=await tv_run(adb,'-s',target,'install','-r',str(BASE_DIR/'static'/apk['filename']),timeout=300,env=env)
        if 'Success' not in output:raise RuntimeError(next((text for key,text in TV_INSTALL_ERRORS.items() if key in output),'Установка не удалась: '+output[-240:]))
        code,output=await tv_run(adb,'-s',target,'shell','monkey','-p','ru.mediahub.app','-c','android.intent.category.LEANBACK_LAUNCHER','1',timeout=20,env=env)
        # На телефоне и планшете нет LEANBACK_LAUNCHER: запускаем обычную иконку.
        if 'No activities found' in output:await tv_run(adb,'-s',target,'shell','monkey','-p','ru.mediahub.app','-c','android.intent.category.LAUNCHER','1',timeout=20,env=env)
        tv_step('done',f'Готово: MediaHUB {apk["version"]} установлен и запущен на {target}. Отладку на ТВ теперь можно выключить.')
    except Exception as error:tv_step('error',str(error) or 'Установка на ТВ не удалась')
    finally:
        if adb and env:
            try:await tv_run(adb,'disconnect',target,timeout=10,env=env);await tv_run(adb,'kill-server',timeout=10,env=env)
            except Exception:pass

@app.post('/api/apps/tv-install')
async def apps_tv_install(request:Request,address:str=Form(...),pair_address:str=Form(''),pair_code:str=Form('')):
    """Запустить установку APK на ТВ по сети; результат — в GET того же адреса."""
    require_setup_access(request)
    if TV_INSTALL.get('status') in {'running','waiting'}:raise HTTPException(409,'Установка на ТВ уже идёт')
    try:
        target=tv_install_target(address);pair_target=tv_install_target(pair_address,None) if pair_address.strip() else ''
        if pair_target and not re.fullmatch(r'\d{6}',pair_code.strip()):raise ValueError('Код сопряжения — 6 цифр с экрана ТВ')
    except ValueError as error:raise HTTPException(422,str(error))
    TV_INSTALL.update(status='running',message='Начинаю установку…',log=[],target=target,startedAt=time.time())
    task=asyncio.create_task(tv_install_job(target,pair_target,pair_code.strip()))
    TV_INSTALL_TASKS.add(task);task.add_done_callback(TV_INSTALL_TASKS.discard)
    return {'ok':True,'target':target}

@app.get('/api/apps/tv-install')
def apps_tv_install_status(request:Request):
    """Состояние последней установки на ТВ."""
    require_setup_access(request)
    return {k:TV_INSTALL.get(k) for k in ('status','message','log','target','updatedAt')}

@app.post('/api/setup/connect')
async def setup_connect(request:Request):
    """Связать службы вне процесса портала и перезапустить его после настройки."""
    require_setup_access(request)
    if setup_read_state().get('running'):raise HTTPException(409,'Дождитесь установки компонентов')
    process=await asyncio.create_subprocess_exec('systemd-run','--unit=mediahub-connect','--collect','--property=ExecStartPost=/usr/bin/systemctl restart mediahub.service',sys.executable,str(SETUP_SCRIPT),'connect',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    await asyncio.wait_for(process.communicate(),timeout=10)
    if process.returncode:raise HTTPException(409,'Подключение уже выполняется или недоступно')
    return {'ok':True,'message':'Подключаю сервисы. Портал перезапустится после завершения. Результат: journalctl -u mediahub-connect.'}

@app.get("/api/setup/job")
async def setup_job():
    st=setup_read_state()
    st["log"]=setup_log_tail(220)
    return st

@app.get("/api/setup/storage/disks")
async def setup_storage_disks(request:Request):
    data=setup_storage_overview()
    data["localInstallAllowed"]=setup_request_allowed(request)
    return data

@app.post("/api/setup/storage/pool")
async def setup_storage_pool(
    request:Request,
    disks:str=Form(...),
    mountpoint:str=Form("/mnt/media"),
    pool_name:str=Form("MediaPool"),
    reserve_gb:int=Form(20),
    confirm:str=Form(""),
):
    require_setup_access(request)
    state=setup_read_state()
    if state.get("running"):
        raise HTTPException(409,"Другая установка уже выполняется")
    try:
        selected=json.loads(disks)
    except Exception:
        raise HTTPException(400,"Некорректный список дисков")
    if not isinstance(selected,list) or not selected:
        raise HTTPException(400,"Выбери хотя бы один диск")
    selected=[str(x) for x in selected if str(x).startswith("/dev/")]
    if not selected:
        raise HTTPException(400,"Не выбраны допустимые диски")
    if confirm!="ERASE":
        raise HTTPException(400,"Нужно подтвердить удаление данных")
    overview=setup_storage_overview()
    allowed={x.get("path") for x in overview.get("disks",[]) if x.get("eligible")}
    forbidden=[x for x in selected if x not in allowed]
    if forbidden:
        raise HTTPException(409,"Один из дисков уже используется или защищён: "+", ".join(forbidden))
    queue_storage_request({
        "disks":selected,
        "mountpoint":mountpoint.strip() or "/mnt/media",
        "pool_name":pool_name.strip() or "MediaPool",
        "reserve_gb":max(1,min(10000,int(reserve_gb))),
        "confirm":"ERASE",
    })
    subprocess.Popen([sys.executable,str(SETUP_SCRIPT),"storage-pool"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    log_activity("setup","MediaPool",("Добавление дисков: " if overview.get("configured") else "Создание пула: ")+", ".join(selected),True)
    return {"ok":True,"message":"Мастер хранилища запущен"}

@app.post("/api/setup/install")
async def setup_install(request:Request,component:str=Form(...)):
    require_setup_access(request)
    allowed=set(SETUP_RECOMMENDED)|{"recommended"}
    if component not in allowed:
        raise HTTPException(400,"Неизвестный компонент")
    state=setup_read_state()
    if state.get("running"):
        raise HTTPException(409,"Установка уже выполняется")
    subprocess.Popen([sys.executable,str(SETUP_SCRIPT),"install",component],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    log_activity("setup",f"Установка: {component}","Запущено из MediaHub",True)
    return {"ok":True,"message":"Установка запущена"}

@app.post("/api/setup/storage")
async def setup_storage(request:Request,root:str=Form(...)):
    require_setup_access(request)
    raw=(root or "").strip()
    if not raw.startswith("/"):
        raise HTTPException(400,"Путь должен быть абсолютным")
    target=Path(raw).resolve()
    forbidden={Path("/"),Path("/etc"),Path("/usr"),Path("/var"),Path("/opt"),Path("/boot"),Path("/proc"),Path("/sys"),Path("/dev")}
    if target in forbidden or target == BASE_DIR or BASE_DIR in target.parents:
        raise HTTPException(400,"Этот путь нельзя использовать как медиатеку")
    vals={
        "MEDIA_ROOT":str(target),
        "MOVIES_ROOT":str(target/"movies"),
        "TV_ROOT":str(target/"tv"),
        "ANIME_ROOT":str(target/"anime"),
        "INBOX_ROOT":str(target/"inbox"),
    }
    update_env_values(vals)
    state=setup_read_state()
    if state.get("running"):
        return {"ok":True,"message":"Путь сохранён. Дождись текущей установки и нажми Создать структуру.","restartRequired":True}
    subprocess.Popen([sys.executable,str(SETUP_SCRIPT),"install","storage"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    log_activity("setup","Хранилище",f"MEDIA_ROOT={target}",True)
    return {"ok":True,"message":"Путь сохранён, создаю структуру папок","restartRequired":True}

@app.post("/api/setup/restart")
async def setup_restart(request:Request):
    require_setup_access(request)
    subprocess.Popen(["bash","-lc","sleep 1; systemctl restart mediahub.service"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    return {"ok":True,"message":"MediaHub перезапускается"}

@app.get("/api/setup/config")
async def setup_config_get():
    return {
        "qbitUser":QBIT_USER,
        "qbitPasswordConfigured":bool(QBIT_PASS),
        "jellyfinApiConfigured":bool(JELLYFIN_KEY),
        "tmdbConfigured":bool(_persistent_value("TMDB_API_KEY")),
        "tmdbCredentialType":tmdb_credential_type(_persistent_value("TMDB_API_KEY")),
        "tmdbStorage":"secure+env" if _persistent_value("TMDB_API_KEY") else "",
        "outboundProxyConfigured":bool(_persistent_value("MEDIAHUB_OUTBOUND_PROXY")),
        "kinopoiskConfigured":bool(_persistent_value("KINOPOISK_API_KEY")),
        "jellyfinPublicUrl":JELLYFIN_PUBLIC_URL,
        "radarrDetected":bool(RADARR_KEY),
        "sonarrDetected":bool(SONARR_KEY),
        "prowlarrDetected":bool(PROWLARR_KEY),
    }

@app.post("/api/setup/config")
async def setup_config_save(
    request:Request,
    qbit_user:str=Form(""),
    qbit_pass:str=Form(""),
    jellyfin_api_key:str=Form(""),
    tmdb_api_key:str=Form(""),
    kinopoisk_api_key:str=Form(""),
    jellyfin_public_url:str=Form(""),
    outbound_proxy:str=Form(""),
):
    require_setup_access(request)
    values={}
    if qbit_user.strip(): values["QBIT_USER"]=qbit_user.strip()
    if qbit_pass: values["QBIT_PASS"]=qbit_pass
    if jellyfin_api_key.strip(): values["JELLYFIN_API_KEY"]=jellyfin_api_key.strip()
    if tmdb_api_key.strip(): values["TMDB_API_KEY"]=tmdb_api_key.strip()
    if kinopoisk_api_key.strip(): values["KINOPOISK_API_KEY"]=kinopoisk_api_key.strip()
    if jellyfin_public_url.strip(): values["JELLYFIN_PUBLIC_URL"]=jellyfin_public_url.strip().rstrip("/")
    if outbound_proxy.strip(): values["MEDIAHUB_OUTBOUND_PROXY"]=outbound_proxy.strip()
    if values:
        update_env_values(values)
        log_activity("setup","Интеграции",f"Обновлено полей: {', '.join(values.keys())}",True)
    tmdb_result=None
    if "TMDB_API_KEY" in values:
        tmdb_result=await refresh_tmdb_live_cache()
    return {"ok":True,"message":"Настройки сохранены постоянно",
            "tmdbConfigured":bool(_persistent_value("TMDB_API_KEY")),
            "tmdb":tmdb_result}

@app.post("/api/setup/test-tmdb")
async def setup_test_tmdb(request:Request, credential:str=Form("")):
    require_setup_access(request)
    cred=(credential or _persistent_value("TMDB_API_KEY") or TMDB_KEY).strip()
    if not cred:
        raise HTTPException(400,"Вставь TMDB API Key или API Read Access Token")
    try:
        async with external_async_client(20) as c:
            r=await external_request(
                c,"GET","https://api.themoviedb.org/3/movie/popular",
                params=tmdb_auth_params({"language":"ru-RU","page":1,"region":"RU"},cred),
                headers=tmdb_auth_headers(cred),retries=3
            )
            rows=(r.json() or {}).get("results") or []
        ru=sum(1 for x in rows if re.search(r"[А-Яа-яЁё]",x.get("title") or ""))
        saved=False
        if credential.strip():
            update_env_values({"TMDB_API_KEY":cred})
            saved=True
        # Keep the successful test response instead of throwing it away. Even
        # if the network drops during the following multi-shelf refresh, the
        # Popular Movies shelf is already usable and remains cached.
        test_items=[]
        for x in rows:
            title=(x.get("title") or "").strip(); mid=x.get("id")
            if not title or not mid:continue
            test_items.append({
                "title":title,"localizedTitle":title,"year":str(x.get("release_date") or "")[:4],
                "overview":x.get("overview") or "",
                "poster":"https://image.tmdb.org/t/p/w500"+x["poster_path"] if x.get("poster_path") else None,
                "externalId":mid,"catalog":"tmdb","catalog_source":"tmdb","locale":"ru-RU",
                "rating":x.get("vote_average"),"originalTitle":x.get("original_title") or "",
            })
        if test_items:
            with cache_db() as con:
                _store_live_tmdb_shelf(con,"movies","popular",test_items)
        live=await refresh_tmdb_live_cache()
        return {"ok":True,"saved":saved,"configured":bool(_persistent_value("TMDB_API_KEY")),
                "message":f"TMDB подключён и ключ сохранён: {tmdb_credential_type(cred)} · тест {len(rows)} карточек · витрина {live.get('total',0)}",
                "tmdb":live}
    except Exception as e:
        raise HTTPException(400,f"TMDB не принял ключ: {str(e)[:180]}")

@app.get("/api/preferences")
async def preferences():
    return {
        "upgradeMode":get_setting("upgrade_mode","notify"),
        "autoSearchUpgrades":get_setting("auto_search_upgrades","0")=="1",
        "replaceAfterImport":get_setting("replace_after_import","1")=="1",
        "keepOldCopy":get_setting("keep_old_copy","0")=="1",
        # v21.1 extended settings
        "browseTarget":browse_target(),
        "defaultKind":get_setting("default_kind","movies"),
        "grabCategory":get_setting("grab_category","movies"),
        "freeSpaceWarnGb":int(get_setting("free_space_warn_gb","50") or 50),
        "homeShelfSize":int(get_setting("home_shelf_size","18") or 18),
        "releasePrefetch":get_setting("release_prefetch","1")=="1",
        "notesAutoSearch":get_setting("notes_auto_search","1")=="1",
        "strictReleaseMatch":get_setting("strict_release_match","1")=="1",
        "skipUnplayable":skip_unplayable_releases(),
        "cacheLimitGb":round(cache_limit_bytes()/1024**3,1),
    }

@app.post("/api/preferences")
async def preferences_save(
    upgrade_mode:str=Form("notify"),
    auto_search_upgrades:str=Form("0"),
    replace_after_import:str=Form("1"),
    keep_old_copy:str=Form("0"),
    browse_target_value:str=Form(""),
    default_kind:str=Form(""),
    grab_category:str=Form(""),
    free_space_warn_gb:str=Form(""),
    home_shelf_size:str=Form(""),
    release_prefetch:str=Form(""),
    notes_auto_search:str=Form(""),
    strict_release_match:str=Form(""),
    skip_unplayable:str=Form(""),
    cache_limit_gb:str=Form(""),
):
    if upgrade_mode not in {"notify","auto","manual"}:
        raise HTTPException(400,"Неизвестный режим обновления")
    set_setting("upgrade_mode",upgrade_mode)
    set_setting("auto_search_upgrades","1" if auto_search_upgrades=="1" else "0")
    set_setting("replace_after_import","1" if replace_after_import=="1" else "0")
    set_setting("keep_old_copy","1" if keep_old_copy=="1" else "0")
    if browse_target_value.strip():
        try:set_setting("browse_target",max(100,min(5000,int(browse_target_value))))
        except Exception:raise HTTPException(400,"Размер локальной базы должен быть числом")
    if default_kind in {"movies","tv","anime"}:
        set_setting("default_kind",default_kind)
    if grab_category in {"movies","tv","anime","manual"}:
        set_setting("grab_category",grab_category)
    if free_space_warn_gb.strip():
        try:set_setting("free_space_warn_gb",max(1,min(10000,int(free_space_warn_gb))))
        except Exception:raise HTTPException(400,"Порог свободного места должен быть числом")
    if home_shelf_size.strip():
        try:set_setting("home_shelf_size",max(6,min(60,int(home_shelf_size))))
        except Exception:raise HTTPException(400,"Размер полки должен быть числом")
    if cache_limit_gb.strip():
        try:set_setting("cache_limit_gb",max(1,min(2000,int(float(cache_limit_gb)))))
        except Exception:raise HTTPException(400,"Лимит кэша должен быть числом")
    for key,value in (("release_prefetch",release_prefetch),("notes_auto_search",notes_auto_search),
                      ("strict_release_match",strict_release_match),("skip_unplayable",skip_unplayable)):
        if value!="":
            set_setting(key,"1" if value=="1" else "0")
    return {"ok":True,"message":"Настройки сохранены"}

@app.get("/api/status")
async def status():
    svcs=[{"key":"mediahub","service":"mediahub.service","installed":True,"active":systemctl_active("mediahub.service"),"enabled":systemctl_enabled("mediahub.service")}]
    for key,svc in managed_services().items():
        svcs.append({"key":key,"service":svc,"installed":unit_available(svc),"active":systemctl_active(svc),"enabled":systemctl_enabled(svc)})
    stamp=None
    with cache_db() as con:
        row=con.execute("select value from meta where key='last_refresh'").fetchone()
        stamp=row["value"] if row else None
    return {"services":svcs,"cacheLastRefresh":stamp,"configured":{
        "tmdb":bool(TMDB_KEY),"prowlarr":bool(PROWLARR_KEY),
        "qbit":bool(QBIT_USER and QBIT_PASS),"jellyfin":bool(JELLYFIN_KEY)}}

@app.get("/api/system-health")
async def system_health():
    try:
        du=shutil.disk_usage(MEDIA_ROOT)
        disk={"total":du.total,"used":du.used,"free":du.free,"percent":round((du.used/du.total*100) if du.total else 0,1)}
    except Exception:
        disk={"total":0,"used":0,"free":0,"percent":0}
    mt,mu=_meminfo()
    services=[]
    for key,svc in {"mediahub":"mediahub.service",**managed_services()}.items():
        services.append({"key":key,"active":systemctl_active(svc)})
    try:db_size=CACHE_DB.stat().st_size
    except Exception:db_size=0
    return {"disk":disk,"memory":{"total":mt,"used":mu,"percent":round((mu/mt*100) if mt else 0,1)},
            "uptime":_uptime_seconds(),"load":list(os.getloadavg()) if hasattr(os,"getloadavg") else [],
            "cacheDbSize":db_size,"cacheDbPath":str(CACHE_DB),"services":{"total":len(services),"active":sum(1 for x in services if x["active"]),"items":services}}

# --- v21.1 storage map -------------------------------------------------------
# "Какой диск выбран и что на нём лежит": named disks plus a per-folder size
# breakdown. `du` on a media pool is slow, so results are cached in SQLite.

STORAGE_USAGE_TTL=timedelta(minutes=20)

def _dir_usage(path:Path,timeout=25):
    """Return (bytes, files) for one media folder."""
    if not path.exists():
        return 0,0
    try:
        p=subprocess.run(["du","-sb",str(path)],capture_output=True,text=True,timeout=timeout)
        if p.returncode==0 and p.stdout.strip():
            size=int(p.stdout.split()[0])
            try:
                c=subprocess.run(["bash","-lc",f"find {json.dumps(str(path))} -type f | wc -l"],
                                 capture_output=True,text=True,timeout=timeout)
                files=int((c.stdout or "0").strip() or 0)
            except Exception:
                files=0
            return size,files
    except Exception:
        pass
    size=files=0
    try:
        for root,_dirs,names in os.walk(path):
            for n in names:
                try:
                    size+=(Path(root)/n).stat().st_size; files+=1
                except Exception:
                    pass
    except Exception:
        pass
    return size,files

def _disk_label_rows():
    out={}
    try:
        with cache_db() as con:
            for r in con.execute("select * from disk_labels").fetchall():
                out[r["device"]]=dict(r)
    except Exception:
        pass
    return out

def _storage_usage_payload():
    folders=[
        ("movies","Фильмы",MOVIES_ROOT),
        ("tv","Сериалы",TV_ROOT),
        ("anime","Аниме",ANIME_ROOT),
        ("inbox","Загрузки / инбокс",INBOX_ROOT),
    ]
    items=[]; counted=0
    for key,title,path in folders:
        size,files=_dir_usage(Path(path))
        counted+=size
        items.append({"key":key,"title":title,"path":str(path),"size":size,
                      "sizeHuman":human(size),"files":files,"exists":Path(path).exists()})
    try:
        du=shutil.disk_usage(MEDIA_ROOT)
        total,used,free=du.total,du.used,du.free
    except Exception:
        total=used=free=0
    other=max(0,used-counted)
    items.append({"key":"other","title":"Прочие данные тома","path":str(MEDIA_ROOT),
                  "size":other,"sizeHuman":human(other),"files":0,"exists":True})
    for x in items:
        x["percent"]=round((x["size"]/total*100),1) if total else 0.0
    try:
        overview=setup_storage_overview()
    except Exception:
        overview={}
    labels=_disk_label_rows()
    disks=[]
    for d in (overview.get("disks") or []):
        dev=str(d.get("path") or "")
        lab=labels.get(dev) or {}
        disks.append({**d,"label":lab.get("label") or d.get("model") or d.get("name") or dev,
                      "note":lab.get("note") or "","custom":bool(lab.get("label"))})
    return {
        "mediaRoot":str(MEDIA_ROOT),
        "pool":{"name":overview.get("name") or "","mountpoint":overview.get("mountpoint") or str(MEDIA_ROOT),
                "mounted":bool(overview.get("mounted")),"configured":bool(overview.get("configured")),
                "type":overview.get("type") or "","branches":overview.get("branches") or []},
        "volume":{"total":total,"used":used,"free":free,"totalHuman":human(total),
                  "usedHuman":human(used),"freeHuman":human(free),
                  "percent":round((used/total*100),1) if total else 0.0},
        "breakdown":items,"disks":disks,
        "databasePath":overview.get("databasePath") or str(CACHE_DB),
        "updatedAt":datetime.now(timezone.utc).isoformat(),
    }

@app.get("/api/storage/usage")
async def storage_usage(refresh:bool=Query(False)):
    cached=get_setting("storage_usage_cache","")
    if cached and not refresh:
        try:
            data=json.loads(cached)
            stamp=datetime.fromisoformat(data.get("updatedAt"))
            if stamp.tzinfo is None: stamp=stamp.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc)-stamp<STORAGE_USAGE_TTL:
                data["cached"]=True
                return data
        except Exception:
            pass
    data=await asyncio.get_running_loop().run_in_executor(None,_storage_usage_payload)
    set_setting("storage_usage_cache",json.dumps(data,ensure_ascii=False))
    data["cached"]=False
    return data

@app.post("/api/storage/label")
async def storage_label(device:str=Form(...),label:str=Form(""),note:str=Form("")):
    device=(device or "").strip()
    if not device:
        raise HTTPException(400,"Не указан диск")
    with cache_db() as con:
        if label.strip():
            con.execute("""insert or replace into disk_labels(device,label,note,updated_at)
                           values(?,?,?,?)""",(device,label.strip(),note.strip(),
                           datetime.now(timezone.utc).isoformat()))
        else:
            con.execute("delete from disk_labels where device=?",(device,))
        con.commit()
    set_setting("storage_usage_cache","")
    return {"ok":True,"message":"Подпись диска сохранена" if label.strip() else "Подпись удалена"}

# --- v21.21 контроль размера кэша -------------------------------------------
# Кэш не должен расти вечно. При превышении лимита самые старые и наименее
# ценные записи удаляются, пока размер не вернётся в норму.

CACHE_LIMIT_DEFAULT_GB=50
CACHE_CLEAN_STEPS=[
    # (описание, SQL, параметры) — от наименее ценного к более ценному.
    ("токены релизов старше 3 дней",
     "delete from search_release where created_at < ?",lambda: [_iso_days_ago(3)]),
    ("ответы внешних API старше 30 дней",
     "delete from api_cache where updated_at < ?",lambda: [_iso_days_ago(30)]),
    ("неудачные поиски релизов старше 7 дней",
     "delete from release_prefetch where status in ('error','queued') and updated_at < ?",
     lambda: [_iso_days_ago(7)]),
    ("найденные релизы старше 30 дней",
     "delete from release_prefetch where updated_at < ?",lambda: [_iso_days_ago(30)]),
    ("карточки каталога старше 30 дней",
     "delete from browse_cache where updated_at < ?",lambda: [_iso_days_ago(30)]),
    ("метаданные не из библиотеки старше 30 дней",
     "delete from tmdb_detail_cache where in_library=0 and updated_at < ?",lambda: [_iso_days_ago(30)]),
    ("внешние новинки старше 30 дней",
     "delete from external_discovery where added_at < ?",lambda: [_iso_days_ago(30)]),
    ("историю действий сверх 500 записей",
     "delete from activity_log where id not in (select id from activity_log order by id desc limit 500)",
     lambda: []),
    ("самые старые токены релизов",
     "delete from search_release where token in (select token from search_release order by created_at limit 2000)",
     lambda: []),
    ("самые старые поиски релизов",
     "delete from release_prefetch where cache_key in (select cache_key from release_prefetch order by updated_at limit 500)",
     lambda: []),
    ("самые старые карточки каталога",
     "delete from browse_cache where rowid in (select rowid from browse_cache order by updated_at limit 5000)",
     lambda: []),
]


def _iso_days_ago(days):
    return (datetime.now(timezone.utc)-timedelta(days=days)).isoformat()


def cache_limit_bytes():
    try:
        gb=float(get_setting("cache_limit_gb",str(CACHE_LIMIT_DEFAULT_GB)))
    except Exception:
        gb=CACHE_LIMIT_DEFAULT_GB
    return int(max(1,min(2000,gb))*1024**3)


def cache_db_size():
    total=0
    for suffix in ("","-wal","-shm"):
        try:
            total+=Path(str(CACHE_DB)+suffix).stat().st_size
        except Exception:
            pass
    return total


def cache_table_report():
    tables=["catalog","browse_cache","release_prefetch","search_release","api_cache",
            "tmdb_detail_cache","library_cache","external_discovery","activity_log",
            "provider_feed","favorites","download_jobs","page_watch","notes"]
    out=[]
    with cache_db() as con:
        for t in tables:
            try:
                n=con.execute(f"select count(*) as n from {t}").fetchone()["n"]
            except Exception:
                continue
            out.append({"table":t,"rows":int(n or 0)})
    out.sort(key=lambda x:x["rows"],reverse=True)
    return out


def enforce_cache_limit(force=False):
    """Удалять старые записи, пока кэш не уложится в лимит."""
    limit=cache_limit_bytes()
    size=cache_db_size()
    if size<=limit and not force:
        return {"cleaned":False,"size":size,"limit":limit,"steps":[]}
    done=[]
    with cache_db() as con:
        for label,sql,params in CACHE_CLEAN_STEPS:
            if cache_db_size()<=limit*0.9 and not force:
                break
            try:
                cur=con.execute(sql,params())
                if cur.rowcount and cur.rowcount>0:
                    done.append(f"{label}: {cur.rowcount}")
                con.commit()
            except Exception:
                continue
    try:
        with cache_db() as con:
            con.execute("vacuum")
    except Exception:
        pass
    after=cache_db_size()
    if done:
        log_activity("cache-cleanup","Очистка кэша",
                     f"было {human(size)}, стало {human(after)}; "+"; ".join(done[:4]),True)
    return {"cleaned":bool(done),"size":size,"sizeAfter":after,"limit":limit,"steps":done}


@app.get("/api/cache/status")
async def cache_status():
    size=cache_db_size(); limit=cache_limit_bytes()
    return {
        "path":str(CACHE_DB),
        "size":size,"sizeHuman":human(size),
        "limit":limit,"limitHuman":human(limit),
        "limitGb":round(limit/1024**3,1),
        "percent":round(size/limit*100,1) if limit else 0,
        "tables":cache_table_report(),
    }


@app.post("/api/cache/cleanup")
async def cache_cleanup(force:bool=Query(True)):
    result=await asyncio.get_running_loop().run_in_executor(None,enforce_cache_limit,force)
    return {"ok":True,"message":(f"Кэш уменьшен: {human(result['size'])} → {human(result.get('sizeAfter') or result['size'])}"
                                 if result["cleaned"] else "Кэш и так в пределах лимита"),**result}


@app.get("/api/activity")
async def activity(limit:int=Query(100,ge=1,le=500)):
    with cache_db() as con:
        rows=con.execute("select * from activity_log order by id desc limit ?",(limit,)).fetchall()
    return [dict(r) for r in rows]

@app.get("/api/jellyfin/resume")
async def jellyfin_resume(request:Request,limit:int=Query(18,ge=1,le=50)):
    return await jellyfin_resume_items(request,limit)

@app.post("/api/service")
async def service_action(key:str=Form(...),action:str=Form(...)):
    if key=="mediahub": raise HTTPException(400,"MediaHub нельзя остановить из собственного интерфейса")
    svc=managed_services().get(key)
    if not svc: raise HTTPException(404,"Неизвестный сервис")
    if not unit_available(svc): raise HTTPException(409,"Сервис ещё не установлен. Открой раздел Установка системы")
    p=run_systemctl(action,svc)
    log_activity("service",f"{action}: {key}",(p.stderr or p.stdout or "").strip(),p.returncode==0)
    return {"ok":p.returncode==0,"message":(p.stderr or p.stdout or "").strip()}

@app.get("/api/service/logs")
async def service_logs(key:str=Query(...),lines:int=Query(80,ge=10,le=400)):
    """Last journal lines of one managed unit, so a failure can be read from
    the MediaHub page instead of an SSH session."""
    svc="mediahub.service" if key=="mediahub" else managed_services().get(key)
    if not svc:
        raise HTTPException(404,"Неизвестный сервис")
    try:
        p=subprocess.run(["journalctl","-u",svc,"-n",str(lines),"--no-pager","--output","short-iso"],
                         capture_output=True,text=True,timeout=12)
        text=(p.stdout or p.stderr or "").strip()
    except Exception as e:
        text=f"journalctl недоступен: {e}"
    detail={"key":key,"service":svc,"installed":unit_available(svc),
            "active":systemctl_active(svc),"enabled":systemctl_enabled(svc)}
    return {**detail,"log":text[-20000:]}

@app.post("/api/start-stack")
async def start_stack():
    out=[]
    for svc in startup_services():
        if not unit_available(svc):
            out.append({"service":svc,"ok":True,"skipped":True})
            continue
        p=subprocess.run(["systemctl","enable","--now",svc],capture_output=True,text=True,timeout=20)
        out.append({"service":svc,"ok":p.returncode==0,"skipped":False})
    present=[x for x in out if not x.get("skipped")]
    ok=all(x["ok"] for x in present); log_activity("stack","Запуск установленного стека",f"{sum(1 for x in present if x['ok'])}/{len(present)} сервисов",ok)
    return {"ok":ok,"results":out}

@app.post("/api/scan")
async def scan():
    out=[]
    for svc in ["mediahub-local-cache.service","media-auto-library.service","media-smart-search.service"]:
        if not unit_available(svc):
            continue
        p=subprocess.run(["systemctl","start",svc],capture_output=True,text=True,timeout=12)
        out.append({"service":svc,"ok":p.returncode==0})
    jf_ok=await jellyfin_refresh() if JELLYFIN_KEY else True
    ok=all(x["ok"] for x in out) and jf_ok
    log_activity("scan","Сканирование",f"{sum(1 for x in out if x['ok'])}/{len(out)} локальных задач",ok)
    return {"ok":ok,"results":out,"jellyfin":jf_ok}

@app.post("/api/refresh-jellyfin")
async def refresh_jf():
    ok=await jellyfin_refresh(); log_activity("jellyfin","Обновление Jellyfin","",ok)
    return {"ok":ok,"message":"Jellyfin обновляется" if ok else "Jellyfin API key не настроен"}



def row_media(r,kind=None):
    d=dict(r)
    if d.get("extra_json"):
        try:
            extra=json.loads(d["extra_json"])
            for k,v in extra.items():
                if k not in d or d.get(k) in (None,""):
                    d[k]=v
        except Exception:
            pass
    if kind:
        d["kind"]=kind
    if "external_id" in d and "externalId" not in d:
        d["externalId"]=d.get("external_id")
    d["catalog"]=d.get("catalog_source") or d.get("catalog") or ""
    return d


def prepare_home_recommendation(item):
    """Return a recommendation safe for the Russian home feed.

    TMDB rows requested with language=ru-RU are considered localized even
    when the official Russian title itself is written in Latin characters
    (for example F1 or TRON). Legacy AniList/unknown English cache rows still
    require a Cyrillic localized title and are hidden otherwise.
    """
    d=dict(item or {})
    localized=(d.get("localizedTitle") or "").strip()
    if localized:
        d["title"]=localized
    title=(d.get("title") or "").strip()
    source=(d.get("catalog_source") or d.get("catalog") or "").strip().lower()
    locale=(d.get("locale") or "").strip().lower()
    tmdb_localized=(source=="tmdb" or locale=="ru-ru")
    if not title:
        return None
    if not tmdb_localized and not is_cyrillic(title):
        return None
    overview=(d.get("overview") or "").strip()
    # Keep TMDB ru-RU descriptions as returned. For legacy/unknown sources,
    # avoid mixing an English synopsis into an otherwise Russian shelf.
    if overview and not tmdb_localized and not is_cyrillic(overview):
        d["overview"]=""
    d["locale"]="ru-RU"
    return d

def home_catalog_items(con,kind,mode,limit=20):
    rows=con.execute(
        "select * from catalog where kind=? and mode=? order by rank limit ?",
        (kind,mode,max(limit*3,limit))
    ).fetchall()
    out=[]
    for r in rows:
        x=prepare_home_recommendation(row_media(r,kind))
        if x:
            out.append(x)
        if len(out)>=limit:
            break
    return out

def home_external_items(con,source,kind,limit=20):
    rows=con.execute(
        "select * from external_discovery where source=? order by added_at desc limit ?",
        (source,max(limit*3,limit))
    ).fetchall()
    out=[]
    for r in rows:
        x=dict(r)
        x["kind"]=kind
        x["catalog"]="external"
        x["externalId"]=""
        try:
            ex=json.loads(x.get("extra_json") or "{}")
            x.update({k:v for k,v in ex.items() if k not in x or not x.get(k)})
        except Exception:
            pass
        x=prepare_home_recommendation(x)
        if x:
            out.append(x)
        if len(out)>=limit:
            break
    return out

TMDB_LIVE_ERRORS={}

async def _tmdb_live_shelf(kind,mode,limit=24,client=None):
    """Fetch one resilient discovery shelf from TMDB using a shared client."""
    if not TMDB_KEY or kind not in {"movies","tv","anime"}:
        return []

    today=datetime.now(timezone.utc).date()
    if kind=="movies" and mode=="new":
        attempts=[
            ("/discover/movie",{
                "region":"RU","include_video":"false","sort_by":"primary_release_date.desc",
                "primary_release_date.gte":(today-timedelta(days=120)).isoformat(),
                "primary_release_date.lte":today.isoformat(),"vote_count.gte":"1"
            }),
            ("/movie/now_playing",{"region":"RU"}),
            ("/movie/now_playing",{}),
        ]
    elif kind=="movies":
        attempts=[("/movie/popular",{"region":"RU"}),("/movie/popular",{})]
    elif kind=="tv" and mode=="new":
        attempts=[
            ("/discover/tv",{
                "sort_by":"first_air_date.desc",
                "first_air_date.gte":(today-timedelta(days=150)).isoformat(),
                "first_air_date.lte":today.isoformat(),"vote_count.gte":"1"
            }),
            ("/tv/on_the_air",{}),
        ]
    elif kind=="tv":
        attempts=[("/tv/popular",{})]
    elif kind=="anime" and mode=="new":
        attempts=[
            ("/discover/tv",{
                "with_genres":"16","with_original_language":"ja",
                "sort_by":"first_air_date.desc",
                "first_air_date.gte":(today-timedelta(days=210)).isoformat(),
                "first_air_date.lte":today.isoformat(),"vote_count.gte":"1"
            }),
            ("/discover/tv",{
                "with_genres":"16","with_original_language":"ja","sort_by":"popularity.desc"
            }),
            ("/discover/tv",{"with_genres":"16","sort_by":"first_air_date.desc"}),
        ]
    else:
        attempts=[
            ("/discover/tv",{
                "with_genres":"16","with_original_language":"ja","sort_by":"popularity.desc"
            }),
            ("/discover/tv",{"with_genres":"16","sort_by":"popularity.desc"}),
        ]

    out=[]; seen=set(); errors=[]
    TMDB_LIVE_ERRORS.pop(f"{kind}:{mode}",None)
    owns_client=client is None
    c=client or external_async_client(22)
    try:
        if owns_client:
            await c.__aenter__()
        for path,base_params in attempts:
            before=len(out)
            for page in range(1,4):
                params={"language":"ru-RU","page":page,"include_adult":"false"}
                params.update(base_params)
                try:
                    r=await external_request(
                        c,"GET","https://api.themoviedb.org/3"+path,
                        params=tmdb_auth_params(params),headers=tmdb_auth_headers(),retries=3
                    )
                except Exception as e:
                    errors.append(f"{path}: {e}")
                    break
                for x in (r.json() or {}).get("results") or []:
                    mid=x.get("id")
                    if not mid or mid in seen:
                        continue
                    title=(x.get("title") or x.get("name") or "").strip()
                    if not title:
                        continue
                    seen.add(mid)
                    out.append({
                        "kind":kind,"mode":mode,"rank":len(out)+1,
                        "title":title,"localizedTitle":title,
                        "year":((x.get("release_date") or x.get("first_air_date") or "")[:4]),
                        "overview":x.get("overview") or "",
                        "poster":"https://image.tmdb.org/t/p/w500"+x["poster_path"] if x.get("poster_path") else None,
                        "externalId":mid,"catalog":"tmdb","catalog_source":"tmdb",
                        "locale":"ru-RU","rating":x.get("vote_average"),
                        "originalTitle":x.get("original_title") or x.get("original_name") or "",
                    })
                    if len(out)>=limit:
                        return out[:limit]
                if len(out)>=min(limit,12):
                    return out[:limit]
            if len(out)>before:
                return out[:limit]
    except Exception as e:
        errors.append(str(e))
    finally:
        if owns_client:
            try:
                await c.__aexit__(None,None,None)
            except Exception:
                pass

    if not out:
        TMDB_LIVE_ERRORS[f"{kind}:{mode}"]="; ".join(errors)[-500:] if errors else "TMDB вернул 0 карточек"
    return out[:limit]

def _store_live_tmdb_shelf(con,kind,mode,items):
    if not items:
        return 0
    con.execute("delete from catalog where kind=? and mode=?",(kind,mode))
    rows=[]
    for i,x in enumerate(items,1):
        extra={
            "rating":x.get("rating"),"genres":[],"catalog":"tmdb",
            "localizedTitle":x.get("localizedTitle") or x.get("title") or "",
            "originalTitle":x.get("originalTitle") or "",
            "locale":"ru-RU",
            "feed":mode,
        }
        rows.append((
            kind,mode,i,x.get("title") or "",x.get("year") or "",
            x.get("overview") or "",x.get("poster"),x.get("externalId"),
            "tmdb",json.dumps(extra,ensure_ascii=False)
        ))
    con.executemany(
        """insert or replace into catalog
           (kind,mode,rank,title,year,overview,poster,external_id,catalog_source,extra_json)
           values(?,?,?,?,?,?,?,?,?,?)""",rows
    )
    con.commit()
    return len(rows)

async def refresh_tmdb_live_cache():
    """Fast foreground refresh used by the UI and first-load fallback."""
    global TMDB_KEY
    TMDB_KEY=_persistent_value("TMDB_API_KEY")
    if not TMDB_KEY:
        return {"ok":False,"configured":False,"counts":{},"message":"TMDB не настроен"}
    pairs=[("movies","new"),("movies","popular"),("tv","new"),("tv","popular"),("anime","new"),("anime","popular")]
    # One shared connection pool, sequential shelves. Some home routers/VPNs
    # reject the previous six-connection burst even though a single TMDB test
    # succeeds. Reusing one pool is slower by a few seconds but far more stable.
    results=[]
    async with external_async_client(22) as client:
        for k,m in pairs:
            results.append(await _tmdb_live_shelf(k,m,24,client))
    counts={}
    with cache_db() as con:
        for (kind,mode),items in zip(pairs,results):
            n=_store_live_tmdb_shelf(con,kind,mode,items)
            counts[f"{kind}:{mode}"]=n
            source=f"TMDB {kind} {mode}"
            old=con.execute("select last_success from source_state where source=?",(source,)).fetchone()
            err=TMDB_LIVE_ERRORS.get(f"{kind}:{mode}","")
            con.execute(
                """insert or replace into source_state
                   (source,ok,item_count,last_success,last_error,duration_ms) values(?,?,?,?,?,?)""",
                (source,1 if n else 0,n,datetime.now(timezone.utc).isoformat() if n else (old["last_success"] if old else None),
                 "" if n else (err or "TMDB вернул 0 карточек"),0)
            )
        con.execute("insert or replace into meta(key,value) values('last_refresh',?)",(datetime.now(timezone.utc).isoformat(),))
        con.commit()
    total=sum(counts.values())
    return {"ok":total>0,"configured":True,"counts":counts,"total":total,
            "message":f"TMDB: загружено {total} карточек" if total else "TMDB подключён, но карточки не получены"}


def _browse_genre_map(kind):
    return {int(i):name for i,name in BROWSE_GENRES.get(kind,[])}

def _browse_kind_ok(kind):
    if kind not in {"movies","tv","anime"}:
        raise HTTPException(400,"Каталог доступен для фильмов, сериалов и аниме")
    return kind

def _browse_mood_params(kind,mood):
    # Genre combinations are intentionally broad. Mood is a discovery aid, not
    # a claim that every returned title has an objective emotional property.
    by_kind={
        "movies":{
            "light":([35,10751,10749],{"vote_average.gte":"5.5"}),
            "funny":([35],{}),"tense":([53,80,9648],{}),"dark":([27,53,80,9648],{}),
            "family":([10751],{}),"romantic":([10749],{}),"adventure":([12,28],{}),
            "epic":([14,878,12,28],{"vote_average.gte":"6"}),"smart":([9648,878,53],{"vote_average.gte":"6"}),
            "cozy":([35,10751,10749],{"with_runtime.lte":"130","vote_average.gte":"6"}),
            "highrated":([],{"vote_average.gte":"7.3","vote_count.gte":"250"}),
        },
        "tv":{
            "light":([35,10751,18],{"vote_average.gte":"5.5"}),"funny":([35],{}),
            "tense":([10759,80,9648],{}),"dark":([80,9648,10765],{}),"family":([10751],{}),
            "romantic":([18,35],{}),"adventure":([10759],{}),"epic":([10759,10765],{"vote_average.gte":"6"}),
            "smart":([9648,10765],{"vote_average.gte":"6"}),"cozy":([35,10751,18],{"vote_average.gte":"6"}),
            "highrated":([],{"vote_average.gte":"7.3","vote_count.gte":"100"}),
        },
        "anime":{
            "light":([35,10751,18],{"vote_average.gte":"5.5"}),"funny":([35],{}),
            "tense":([10759,9648],{}),"dark":([9648,10765,18],{}),"family":([10751],{}),
            "romantic":([18,35],{}),"adventure":([10759],{}),"epic":([10759,10765],{"vote_average.gte":"6"}),
            "smart":([9648,10765],{"vote_average.gte":"6"}),"cozy":([35,10751,18],{"vote_average.gte":"6"}),
            "highrated":([],{"vote_average.gte":"7.3","vote_count.gte":"80"}),
        },
    }
    genres,extra=by_kind.get(kind,{}).get(mood,([],{}))
    return list(genres),dict(extra)

def _browse_interpret(q,kind):
    raw=(q or "").strip(); low=raw.casefold().replace("ё","е")
    matched_mood=""; understood=[]; genre_ids=[]
    for mid,aliases in MOOD_ALIASES.items():
        if any(a in low for a in aliases):
            matched_mood=mid
            label=next((x["label"] for x in BROWSE_MOODS if x["id"]==mid),mid)
            understood.append(label)
            break
    canon=GENRE_CANON_IDS.get(kind,{})
    for fragment,cname in GENRE_ALIASES.items():
        if fragment in low and cname in canon:
            gid=canon[cname]
            if gid not in genre_ids:
                genre_ids.append(gid)
                understood.append(_browse_genre_map(kind).get(gid,cname))
    year=""
    m=re.search(r"\b(19\d{2}|20\d{2})\b",low)
    if m:
        year=m.group(1); understood.append(year)
    # If any intent words were understood, common filler words should not turn
    # this into a title search. A real title query with no intent is preserved.
    intent=bool(matched_mood or genre_ids or year)
    return {"mood":matched_mood,"genres":genre_ids,"year":year,"intent":intent,"understood":understood,"raw":raw}

def _browse_bucket(kind,genres,mood,sort,year):
    key={"kind":kind,"genres":sorted({int(x) for x in genres}),"mood":mood or "","sort":sort or "popular","year":year or ""}
    digest=hashlib.sha1(json.dumps(key,sort_keys=True,separators=(",",":")).encode()).hexdigest()[:18]
    return f"browse:{kind}:{digest}"

def _browse_sort_params(kind,sort):
    if sort=="rating": return {"sort_by":"vote_average.desc","vote_count.gte":"100" if kind=="movies" else "50"}
    if sort=="new": return {"sort_by":"primary_release_date.desc" if kind=="movies" else "first_air_date.desc"}
    if sort=="old": return {"sort_by":"primary_release_date.asc" if kind=="movies" else "first_air_date.asc"}
    return {"sort_by":"popularity.desc"}

def _browse_item_from_tmdb(x,kind):
    gid_map=_browse_genre_map(kind)
    gids=[int(v) for v in (x.get("genre_ids") or []) if str(v).isdigit()]
    genres=[gid_map[g] for g in gids if g in gid_map]
    title=(x.get("title") or x.get("name") or "").strip()
    return {
        "kind":kind,"title":title,"localizedTitle":title,
        "originalTitle":x.get("original_title") or x.get("original_name") or "",
        "year":((x.get("release_date") or x.get("first_air_date") or "")[:4]),
        "releaseDate":x.get("release_date") or x.get("first_air_date") or "",
        "overview":x.get("overview") or "",
        "poster":"https://image.tmdb.org/t/p/w500"+x["poster_path"] if x.get("poster_path") else None,
        "externalId":x.get("id"),"catalog":"tmdb","catalog_source":"tmdb","locale":"ru-RU",
        "genres":genres,"genreIds":gids,"rating":x.get("vote_average"),"voteCount":x.get("vote_count"),
        "popularity":x.get("popularity"),"runtime":0,"status":"","studio":"","network":"",
    }

def _browse_state(bucket):
    with cache_db() as con:
        st=con.execute("select * from browse_state where bucket=?",(bucket,)).fetchone()
        n=con.execute("select count(*) as n from browse_cache where bucket=?",(bucket,)).fetchone()["n"]
    return (dict(st) if st else {"next_page":1,"loaded_pages":0,"total_pages":0,"exhausted":0,"updated_at":None,"last_error":""}),int(n or 0)

# v21.1: local catalogue target raised from 100 to 1000 cards per shelf.
# TMDB returns 20 rows per page, so 10 pages = 200 rows per batch and five
# expands reach the target without hammering the API on first open.
BROWSE_TARGET_DEFAULT=1000
BROWSE_FIRST_PAGES=10
BROWSE_EXPAND_PAGES=15

def browse_target():
    try:
        value=int(get_setting("browse_target",str(BROWSE_TARGET_DEFAULT)))
    except Exception:
        value=BROWSE_TARGET_DEFAULT
    return max(100,min(5000,value))

async def _browse_fetch_batch(kind,genres,mood,sort,year,bucket,pages=10,reset=False):
    global TMDB_KEY
    TMDB_KEY=_persistent_value("TMDB_API_KEY")
    if not TMDB_KEY:
        raise HTTPException(400,"Сначала подключи TMDB в Установка → Подключения")
    st,count=_browse_state(bucket)
    start=1 if reset else max(1,int(st.get("next_page") or 1))
    if not reset and int(st.get("exhausted") or 0):
        return count
    if reset:
        with cache_db() as con:
            con.execute("delete from browse_cache where bucket=?",(bucket,))
            con.execute("delete from browse_state where bucket=?",(bucket,)); con.commit()
        count=0; start=1
    media="movie" if kind=="movies" else "tv"
    params={"language":"ru-RU","include_adult":"false"}
    if kind=="movies": params["region"]="RU"
    params.update(_browse_sort_params(kind,sort))
    chosen=[int(x) for x in genres if str(x).isdigit()]
    mood_genres,mood_extra=_browse_mood_params(kind,mood)
    params.update(mood_extra)
    if year:
        if kind=="movies": params["primary_release_year"]=year
        else: params["first_air_date_year"]=year
    # Explicit genres are ANDed. Mood genres are ORed with each other. Anime
    # always requires animation and Japanese original language.
    genre_parts=[]
    if kind=="anime":
        genre_parts.append("16")
        params["with_original_language"]="ja"
    genre_parts.extend(str(x) for x in chosen if not (kind=="anime" and x==16))
    if mood_genres:
        genre_parts.append("|".join(str(x) for x in mood_genres))
    if genre_parts: params["with_genres"]=",".join(genre_parts)

    rows=[]; last_error=""; total_pages=int(st.get("total_pages") or 0); fetched=0
    async with external_async_client(24) as client:
        for page in range(start,start+max(1,pages)):
            if total_pages and page>total_pages: break
            pp=dict(params);pp["page"]=page
            try:
                r=await external_request(client,"GET",f"https://api.themoviedb.org/3/discover/{media}",params=tmdb_auth_params(pp),headers=tmdb_auth_headers(),retries=3)
                data=r.json() or {}; total_pages=min(500,int(data.get("total_pages") or 0))
                page_rows=data.get("results") or []
                fetched+=1
                if not page_rows: break
                rows.extend(_browse_item_from_tmdb(x,kind) for x in page_rows if x.get("id") and (x.get("title") or x.get("name")))
            except Exception as e:
                last_error=str(e)[:500]; break
    now=datetime.now(timezone.utc).isoformat()
    with cache_db() as con:
        maxrank=con.execute("select coalesce(max(rank),0) as n from browse_cache where bucket=?",(bucket,)).fetchone()["n"]
        rank=int(maxrank or 0)
        for item in rows:
            rank+=1
            raw=json.dumps(item,ensure_ascii=False)
            con.execute("""insert or ignore into browse_cache
                (bucket,kind,external_id,rank,title,original_title,year,overview,poster,genres_json,rating,popularity,vote_count,raw_json,updated_at)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (bucket,kind,str(item.get("externalId") or ""),rank,item.get("title") or "",item.get("originalTitle") or "",item.get("year") or "",item.get("overview") or "",item.get("poster"),json.dumps(item.get("genres") or [],ensure_ascii=False),item.get("rating"),item.get("popularity"),item.get("voteCount"),raw,now))
        next_page=start+fetched
        exhausted=1 if (total_pages and next_page>total_pages) or fetched<max(1,pages) and not last_error else 0
        con.execute("""insert or replace into browse_state(bucket,kind,next_page,loaded_pages,total_pages,exhausted,updated_at,last_error)
            values(?,?,?,?,?,?,?,?)""",(bucket,kind,next_page,max(0,next_page-1),total_pages,exhausted,now,last_error))
        con.commit()
    _,count=_browse_state(bucket)
    return count

def _browse_rows(bucket,offset,limit):
    with cache_db() as con:
        rows=con.execute("select raw_json from browse_cache where bucket=? order by rank limit ? offset ?",(bucket,limit,offset)).fetchall()
    out=[]
    for r in rows:
        try: out.append(json.loads(r["raw_json"]))
        except Exception: pass
    return out

def _browse_local_text_search(q,kind,limit=80):
    qn=normalize_search_text(q); tokens=[x for x in qn.split() if len(x)>1]
    if not qn:return []
    with cache_db() as con:
        rows=con.execute("select raw_json,title,original_title,overview,popularity from browse_cache where kind=?",(kind,)).fetchall()
    scored={}
    for r in rows:
        title=normalize_search_text(r["title"] or ""); orig=normalize_search_text(r["original_title"] or ""); overview=normalize_search_text(r["overview"] or "")
        score=0
        if qn in title or qn in orig: score=120
        elif tokens and all(t in title or t in orig for t in tokens): score=90
        elif tokens and all(t in (title+" "+orig+" "+overview) for t in tokens): score=60
        else:
            ratio=max(SequenceMatcher(None,qn,title).ratio(),SequenceMatcher(None,qn,orig).ratio() if orig else 0)
            if ratio>=0.52: score=int(ratio*70)
        if not score:continue
        try:item=json.loads(r["raw_json"])
        except Exception:continue
        key=str(item.get("externalId") or item.get("title"));score+=min(20,int(float(r["popularity"] or 0)/10))
        if key not in scored or score>scored[key][0]:scored[key]=(score,item)
    return [v[1] for v in sorted(scored.values(),key=lambda x:x[0],reverse=True)[:limit]]

async def _browse_tmdb_text_search(q,kind,limit=60):
    if not TMDB_KEY:return []
    media="movie" if kind=="movies" else "tv";out=[];seen=set()
    async with external_async_client(22) as client:
        for page in range(1,4):
            try:
                r=await external_request(client,"GET",f"https://api.themoviedb.org/3/search/{media}",params=tmdb_auth_params({"language":"ru-RU","query":q,"include_adult":"false","page":page}),headers=tmdb_auth_headers(),retries=3)
            except Exception:break
            for x in (r.json() or {}).get("results") or []:
                if not x.get("id") or x.get("id") in seen:continue
                if kind=="anime" and not (16 in (x.get("genre_ids") or []) and (x.get("original_language") or "")=='ja'):
                    continue
                seen.add(x.get("id"));out.append(_browse_item_from_tmdb(x,kind))
                if len(out)>=limit:return out
    return out

@app.get("/api/browse/options")
async def browse_options(kind:str=Query("movies")):
    _browse_kind_ok(kind)
    return {"kind":kind,"genres":[{"id":i,"name":name} for i,name in BROWSE_GENRES[kind]],"moods":BROWSE_MOODS,
            "sorts":[{"id":"popular","name":"По популярности"},{"id":"rating","name":"По рейтингу"},{"id":"new","name":"Сначала новые"},{"id":"old","name":"Сначала старые"}]}

@app.get("/api/browse")
async def browse_catalog(kind:str=Query("movies"),genres:str=Query(""),mood:str=Query(""),sort:str=Query("popular"),q:str=Query(""),offset:int=Query(0,ge=0),limit:int=Query(40,ge=1,le=100),expand:bool=Query(False)):
    _browse_kind_ok(kind)
    if sort not in {"popular","rating","new","old"}:sort="popular"
    explicit=[]
    for v in (genres or "").split(","):
        try:explicit.append(int(v))
        except Exception:pass
    interp=_browse_interpret(q,kind)
    selected=list(dict.fromkeys(explicit+interp["genres"]))
    chosen_mood=mood or interp["mood"]
    year=interp["year"]
    understood=list(interp["understood"])
    if mood and not interp["mood"]:
        understood.append(next((x["label"] for x in BROWSE_MOODS if x["id"]==mood),mood))
    # A free-text query with no detected genre/mood is a forgiving title search.
    # Explicit chip filters still apply, so "Менталист" + "Детектив" works.
    if q.strip() and not interp["intent"]:
        local=_browse_local_text_search(q,kind,100)
        if len(local)<30:
            live=await _browse_tmdb_text_search(q,kind,80)
            seen={str(x.get("externalId")) for x in local}; local.extend(x for x in live if str(x.get("externalId")) not in seen)
        mood_ids,_=_browse_mood_params(kind,mood) if mood else ([],{})
        if explicit or mood_ids:
            filtered=[]
            for item in local:
                gids=set(int(x) for x in (item.get("genreIds") or []) if str(x).isdigit())
                if explicit and not all(int(x) in gids for x in explicit):continue
                if mood_ids and gids and not any(int(x) in gids for x in mood_ids):continue
                filtered.append(item)
            local=filtered
        labels=[f'Название ≈ «{q.strip()}»']
        gmap=_browse_genre_map(kind);labels.extend(gmap[x] for x in explicit if x in gmap)
        if mood:labels.append(next((x["label"] for x in BROWSE_MOODS if x["id"]==mood),mood))
        page_items=local[offset:offset+limit]
        annotate_library_items(page_items,kind)
        annotate_cards_release_prefetch(page_items,kind)
        schedule_cards_release_prefetch(page_items,kind,limit=5)
        return {"items":page_items,"offset":offset,"nextOffset":offset+min(limit,max(0,len(local)-offset)),"cached":len(local),"hasMore":offset+limit<len(local),"understood":labels,"mode":"search","kind":kind}

    bucket=_browse_bucket(kind,selected,chosen_mood,sort,year)
    st,count=_browse_state(bucket)
    stale=False
    if st.get("updated_at"):
        try:stale=(datetime.now(timezone.utc)-datetime.fromisoformat(st["updated_at"])).total_seconds()>7*86400
        except Exception:pass
    target=browse_target()
    if count==0 or (stale and offset==0):
        count=await _browse_fetch_batch(kind,selected,chosen_mood,sort,year,bucket,pages=BROWSE_FIRST_PAGES,reset=stale)
    elif expand or offset>=count:
        # Keep pulling until the local shelf reaches the configured target
        # (1000 by default) or TMDB runs out of pages.
        rounds=0
        while rounds<4:
            count=await _browse_fetch_batch(kind,selected,chosen_mood,sort,year,bucket,pages=BROWSE_EXPAND_PAGES,reset=False)
            rounds+=1
            st_now,_=_browse_state(bucket)
            if count>=target or int(st_now.get("exhausted") or 0) or (not expand and count>offset+limit):
                break
    items=_browse_rows(bucket,offset,limit)
    st,count=_browse_state(bucket)
    has_more=(offset+len(items)<count) or not bool(st.get("exhausted"))
    labels=[];gmap=_browse_genre_map(kind)
    for gid in selected:
        if gid in gmap: labels.append(gmap[gid])
    if chosen_mood: labels.append(next((x["label"] for x in BROWSE_MOODS if x["id"]==chosen_mood),chosen_mood))
    if year:labels.append(year)
    understood=list(dict.fromkeys(understood+labels))
    annotate_library_items(items,kind)
    annotate_cards_release_prefetch(items,kind)
    schedule_cards_release_prefetch(items,kind,limit=6)
    return {"items":items,"offset":offset,"nextOffset":offset+len(items),"cached":count,"target":target,"hasMore":has_more,"understood":understood,"mode":"discover","kind":kind,"bucket":bucket,"state":{"loadedPages":st.get("loaded_pages",0),"exhausted":bool(st.get("exhausted")),"error":st.get("last_error") or ""}}

@app.get("/api/jellyfin-image/{item_id}")
async def jellyfin_image(item_id:str,alt:str=Query("")):
    """Картинка для «Продолжить просмотр».

    У эпизода часто нет Primary, зато есть Thumb, а у сериала — постер.
    Поэтому пробуем несколько вариантов, прежде чем отдать 404 и позволить
    интерфейсу нарисовать заглушку.
    """
    if not JELLYFIN_KEY:
        raise HTTPException(404,"Jellyfin image unavailable")
    attempts=[(item_id,"Primary"),(item_id,"Thumb")]
    if alt and alt!=item_id:
        attempts+= [(alt,"Primary"),(alt,"Thumb")]
    attempts.append((item_id,"Backdrop"))
    try:
        async with httpx.AsyncClient(timeout=15,trust_env=False) as c:
            for iid,img_type in attempts:
                try:
                    r=await c.get(
                        JELLYFIN_URL+f"/Items/{iid}/Images/{img_type}",
                        headers={"X-Emby-Token":JELLYFIN_KEY},
                        params={"maxWidth":600,"quality":90}
                    )
                except Exception:
                    continue
                if r.status_code<300 and r.content:
                    return Response(content=r.content,
                                    media_type=r.headers.get("content-type","image/jpeg"),
                                    headers={"Cache-Control":"private, max-age=21600"})
    except Exception:
        pass
    raise HTTPException(404,"Jellyfin image unavailable")

@app.get("/api/home")
async def home_data(request:Request):
    sections=[]
    own_resume=await asyncio.to_thread(player_resume_items)
    if own_resume:
        sections.append({"id":"web-resume","title":"Продолжить просмотр","type":"resume","sourceLabel":"MediaHub","items":own_resume})

    # v20.5 checks EVERY required shelf. Previously one working shelf (for
    # example TV popular) made MediaHub believe all discovery data existed, so
    # missing movie/anime/new shelves were never repaired. Retry is throttled
    # to avoid hitting TMDB on every page load while a source is unavailable.
    if TMDB_KEY:
        try:
            required=(("movies","new"),("movies","popular"),("tv","new"),("tv","popular"),("anime","new"),("anime","popular"))
            with cache_db() as _pre:
                missing=[]
                for k,m in required:
                    n=_pre.execute("select count(*) as n from catalog where kind=? and mode=?",(k,m)).fetchone()["n"]
                    if not n: missing.append(f"{k}:{m}")
                last_row=_pre.execute("select value from meta where key='last_live_tmdb_attempt'").fetchone()
            due=True
            if last_row and last_row["value"]:
                try: due=(datetime.now(timezone.utc)-datetime.fromisoformat(last_row["value"])).total_seconds()>=120
                except Exception: pass
            if missing and due:
                with cache_db() as _mark:
                    _mark.execute("insert or replace into meta(key,value) values('last_live_tmdb_attempt',?)",(datetime.now(timezone.utc).isoformat(),));_mark.commit()
                await refresh_tmdb_live_cache()
        except Exception:
            pass

    with cache_db() as con:
        # Home recommendation order is intentional: MOVIES -> TV -> ANIME.
        # Only Russian-localized cards are admitted to these shelves.
        groups=[
            (
                "movies",
                [
                    ("new","Новинки фильмов"),
                    ("popular","Популярные фильмы"),
                ],
                [("KinopoiskMovies","Премьеры фильмов","КиноПоиск","https://www.kinopoisk.ru/")],
            ),
            (
                "tv",
                [
                    ("new","Новинки сериалов"),
                    ("popular","Популярные сериалы"),
                ],
                [("TVmazeSeries","Новые сериалы из эфира","TVmaze","https://www.tvmaze.com/")],
            ),
            (
                "anime",
                [
                    ("new","Новинки аниме"),
                    ("popular","Популярное аниме"),
                ],
                # v21.3: полка AniLiberty убрана с главной. Парсер публичного
                # каталога тянул служебные страницы («Жанры», «Расписание
                # релизов», «Боевые искусства 22 релиза») вместо тайтлов.
                # Источник остаётся резервом для поиска, но витрину не засоряет.
                [],
            ),
        ]

        recommendation_counts={"movies":0,"tv":0,"anime":0}
        for kind,catalog_shelves,external_shelves in groups:
            for mode,title in catalog_shelves:
                items=home_catalog_items(con,kind,mode,20)
                if not items:
                    continue
                source_code=(items[0].get("catalog_source") or items[0].get("catalog") or "")
                source_label={"tmdb":"TMDB","anilist":"AniList"}.get(source_code,source_code.upper() if source_code else "")
                source_url={"tmdb":"https://www.themoviedb.org/","anilist":"https://anilist.co/"}.get(source_code,"")
                sections.append({
                    "id":f"{kind}-{mode}","title":title,"type":"media",
                    "sourceLabel":source_label,"sourceUrl":source_url,
                    "items":items,"locale":"ru-RU"
                })
                recommendation_counts[kind]+=len(items)

            for source,title,label,url in external_shelves:
                items=home_external_items(con,source,kind,20 if kind!="anime" else 18)
                if not items:
                    continue
                sections.append({
                    "id":source.lower(),"title":title,"type":"media",
                    "sourceLabel":label,"sourceUrl":url,"items":items,"locale":"ru-RU"
                })
                recommendation_counts[kind]+=len(items)

        # Personal library follows discovery shelves, so the first recommendation
        # users see is always movies rather than an old anime-only cache.
        library=con.execute(
            "select * from library_cache order by added_at desc limit 24"
        ).fetchall()
        if library:
            sections.append({
                "id":"library","title":"Недавно в моей библиотеке","type":"media",
                "items":[row_media(r,r["kind"]) for r in library]
            })

        raw_feeds=con.execute(
            """select * from provider_feed
               order by published_at desc, seeders desc limit 80"""
        ).fetchall()
        feeds=[]; _seen_feed=set()
        for r in raw_feeds:
            key=((r["guid"] or "").strip() or (r["title"] or "").strip().casefold())
            if key in _seen_feed:
                continue
            _seen_feed.add(key); feeds.append(r)
            if len(feeds)>=18:
                break
        if feeds:
            items=[]
            for r in feeds:
                x=dict(r)
                x["sizeHuman"]=human(x.get("size"))
                x["quality"]=release_quality(x.get("title"))
                items.append(x)
            sections.append({
                "id":"providers","title":"Новое у моих поставщиков",
                "type":"releases","sourceLabel":"Prowlarr","sourceUrl":"","items":items
            })

        states=[dict(r) for r in con.execute(
            "select * from source_state where source not in ('AniList new','AniList popular','AniList upcoming','TMDB movies trending','TMDB tv trending','TMDB anime trending','TMDB anime upcoming') order by source"
        ).fetchall()]
        full=con.execute("select value from meta where key='last_refresh'").fetchone()
        local=con.execute("select value from meta where key='last_local_refresh'").fetchone()

    missing=[]
    if recommendation_counts["movies"]==0:
        missing.append("movies")
    if recommendation_counts["tv"]==0:
        missing.append("tv")
    if recommendation_counts["anime"]==0:
        missing.append("anime")

    # Mark cards already owned before rendering the home shelves.
    try:
        annotate_home_library(sections)
    except Exception:
        pass

    # Expose already warmed cards, then continue warming the rest without
    # delaying the home response.
    try:
        annotate_home_release_prefetch(sections)
        schedule_home_release_prefetch(sections)
    except Exception:
        pass

    return {
        "sections":sections,
        "sourceStates":states,
        "lastRefresh":full["value"] if full else None,
        "lastLocalRefresh":local["value"] if local else None,
        "recommendationLocale":"ru-RU",
        "missingRecommendationKinds":missing,
    }


@app.get("/api/library-cached")
async def library_cached(kind:str=Query("movies")):
    if kind not in {"movies","tv","anime"}:
        raise HTTPException(400,"Неизвестный раздел библиотеки")
    with cache_db() as con:
        rows=con.execute(
            """select * from library_cache where kind=?
               order by case when added_at is null or added_at='' then 1 else 0 end,
                        added_at desc, title collate nocase""",(kind,)
        ).fetchall()
    # Файлы на диске учитываются, даже если Sonarr/Radarr их ещё не
    # импортировали: иначе скачанный сериал показывался как «файлов нет».
    live=live_filesystem_items_cached(kind)
    disk_by_path={str(x.get("path") or "").rstrip("/"):int(x.get("videoCount") or x.get("video_count") or 0) or 1
                  for x in live if x.get("path")}
    manual=manual_meta_index()
    cached=[]
    for r in rows:
        item=row_media(r,kind)
        p=str(item.get("path") or "").rstrip("/")
        # Папку перенесли, объединили или удалили — строка из базы уйдёт при
        # следующей синхронизации, а показывать её уже сейчас незачем.
        if (r["catalog_source"] or "")=="filesystem" and p and p not in disk_by_path and not Path(p).exists():
            continue
        disk=disk_by_path.get(p,0)
        if disk and str(item.get("overview") or "").startswith("Локальная медиатека"):
            item["overview"]=f"Локальная медиатека · {disk} видео"
        item.update(library_item_state(r,disk))
        apply_manual_meta(item,manual)
        cached.append(item)

    # Critical stability rule: the UI must reflect real media folders even if
    # the systemd cache timer is late, failed, or Sonarr/Radarr do not know them.
    by_path={str(x.get("path") or "").rstrip("/"):x for x in cached if x.get("path")}
    for x in live:
        p=str(x.get("path") or "").rstrip("/")
        if p not in by_path:
            x.update({"inLibrary":True,"hasFile":True,"tracked":True,"awaitingFiles":False,
                      "libraryStatus":"complete","libraryLabel":"✓ В библиотеке","monitored":False})
            apply_manual_meta(x,manual)
            cached.append(x)
            # persist asynchronously for the next page load
            try:upsert_live_library_item(kind,p,x.get("title"))
            except Exception:pass
    # Папку сначала нашёл обход диска, затем её импортировал Radarr/Sonarr: в кэше две
    # строки одного проекта. Оставляем каталожную — у неё внешние данные и ID. Дубли
    # ломали приложение: одинаковые ключи в прокручиваемом ряду роняют интерфейс.
    unique={}
    for x in cached:
        p=str(x.get("path") or "").rstrip("/") or id(x);old=unique.get(p)
        if old is None or ((old.get("catalog") or old.get("catalog_source"))=="filesystem" and (x.get("catalog") or x.get("catalog_source"))!="filesystem"):unique[p]=x
    cached=list(unique.values())
    cached.sort(key=lambda x:(str(x.get("added_at") or ""),str(x.get("title") or "").casefold()),reverse=True)
    schedule_library_detail_warm(kind,cached)
    return cached


def schedule_library_detail_warm(kind,items,limit=6):
    """Догреть кэш метаданных для библиотеки в фоне.

    Со временем все карточки медиатеки открываются мгновенно и без интернета.
    За один заход берём не больше шести тайтлов, чтобы не долбить TMDB.
    """
    if not TMDB_KEY:
        return
    queued=0
    for x in items or []:
        if queued>=limit:
            break
        ext=x.get("providerIds",{}).get("Tmdb") if isinstance(x.get("providerIds"),dict) else None
        if not ext and kind=="movies":
            ext=x.get("externalId") or x.get("external_id")
        ext=str(ext or "").strip()
        if not ext.isdigit():
            continue
        cached,_meta=_detail_cache_read(kind,ext)
        if cached:
            continue
        try:
            asyncio.create_task(_refresh_detail_cache(kind,ext,True))
            queued+=1
        except RuntimeError:
            break

def find_library_row(kind:str,item_key:str="",path:str="",title:str=""):
    """Найти строку библиотеки по ключу ARR, пути или названию.

    Один поиск на все операции с карточкой — переименование, данные по ссылке,
    удаление. Название сверяется и точно, и по нормализованному виду: в разных
    местах интерфейса у одного тайтла отличаются регистр и пунктуация.
    """
    with cache_db() as con:
        row=None
        if item_key:
            row=con.execute("select * from library_cache where kind=? and item_key=?",(kind,item_key)).fetchone()
        if not row and path:
            row=con.execute("select * from library_cache where kind=? and rtrim(path,'/')=?",
                            (kind,str(path).rstrip("/"))).fetchone()
        if not row and title:
            row=con.execute("select * from library_cache where kind=? and title=? limit 1",(kind,title)).fetchone()
        if not row and title:
            needle=normalize_search_text(title)
            for candidate in con.execute("select * from library_cache where kind=?",(kind,)).fetchall():
                if normalize_search_text(candidate["title"])==needle:
                    row=candidate; break
    return dict(row) if row else None


async def arr_forget_title(source:str,item_key:str,delete_files:bool=False):
    """Убрать тайтл из Radarr или Sonarr. Возвращает (убрано, ошибка)."""
    source=(source or "").lower()
    if source not in {"radarr","sonarr"} or not str(item_key).isdigit():
        return False,""
    base,key=(RADARR_URL,RADARR_KEY) if source=="radarr" else (SONARR_URL,SONARR_KEY)
    endpoint="/api/v3/movie/" if source=="radarr" else "/api/v3/series/"
    if not key:
        return False,f"{source.capitalize()} не подключён"
    try:
        async with httpx.AsyncClient(timeout=20,trust_env=False) as c:
            r=await c.delete(base+endpoint+str(item_key),headers={"X-Api-Key":key},
                             params={"deleteFiles":"true" if delete_files else "false",
                                     "addImportExclusion":"false"})
        return (r.status_code<300),("" if r.status_code<300 else f"HTTP {r.status_code}")
    except Exception as e:
        return False,str(e)[:200]


def store_page_card(kind,item_key,path,title,url,meta):
    """Сохранить карточку проекта целиком из данных страницы (или введённых вручную).

    Страница — единственный источник карточки. Чего на ней нет, то остаётся
    пустым: раньше недостающее добиралось из старой строки, то есть из TMDB или
    Jellyfin, и у тайтла оставался чужой постер, год и жанры.
    """
    row=find_library_row(kind,item_key,path,title)
    if not row:
        raise HTTPException(404,"Эта карточка ещё не в библиотеке — данные по ссылке можно привязать к тому, что уже скачано")
    path=str(row.get("path") or "").rstrip("/")
    with cache_db() as con:
        if path:
            # manual_meta переживает пересоздание library_cache синхронизацией с ARR.
            save_manual_meta(con,path,kind,meta.get("title") or "",meta.get("year") or "",
                             meta.get("poster") or "",meta.get("overview") or "",url,
                             int(meta.get("episodes") or 0),meta.get("genres"),meta.get("rating"))
        page_item=apply_manual_meta({"path":path,"title":row["title"]},
                                    {path:{"title":meta.get("title") or "","year":meta.get("year") or "",
                                           "poster":meta.get("poster") or "","overview":meta.get("overview") or "",
                                           "sourceUrl":url,"episodes":int(meta.get("episodes") or 0),
                                           "genres":meta.get("genres") or [],"rating":meta.get("rating")}})
        extra=_json_dict(row["extra_json"])
        for k in ("tmdbId","jellyfinId","providerIds","originalTitle"):
            extra.pop(k,None)
        extra.update(FOREIGN_META_FIELDS)
        extra.update({"sourceUrl":url,"fromPage":True,"needsMetadata":False,"manualMetadata":True,
                      "genres":page_item["genres"],"rating":page_item["rating"],
                      "catalog":extra.get("catalog") or "filesystem"})
        if int(meta.get("episodes") or 0)>0:
            extra["episodesTotal"]=int(meta["episodes"])
        con.execute("""update library_cache set title=?,year=?,overview=?,poster=?,extra_json=?
                       where kind=? and item_key=?""",
                    (page_item["title"],page_item["year"],page_item["overview"],page_item["poster"],
                     json.dumps(extra,ensure_ascii=False),kind,row["item_key"]))
        con.commit()
        fresh=con.execute("select * from library_cache where kind=? and item_key=?",(kind,row["item_key"])).fetchone()
    item=row_media(fresh,kind)
    item.update(library_item_state(fresh))
    apply_manual_meta(item)
    item["libraryItemKey"]=row["item_key"]
    return item


@app.post("/api/library/attach-page")
async def library_attach_page(url:str=Form(...),kind:str=Form("movies"),
                              item_key:str=Form(""),path:str=Form(""),title:str=Form("")):
    """Прикрепить к карточке данные с произвольной страницы.

    Полезно, когда TMDB не знает тайтл: пользователь даёт ссылку на страницу
    сайта, а MediaHub забирает оттуда название, год, постер и описание.
    """
    url=(url or "").strip()
    if not url.lower().startswith(("http://","https://")):
        raise HTTPException(400,"Нужна ссылка http или https")
    if kind not in {"movies","tv","anime"}:
        raise HTTPException(400,"Неизвестный раздел")
    _links,meta=await torrent_links_from_page(url,return_meta=True)
    meta=meta or {}
    if meta.get("error") and not (meta.get("title") or meta.get("poster")):
        # Сервер часто не достаёт до сайта, который открывается в браузере, —
        # тогда предлагаем заполнить карточку вручную.
        return JSONResponse({"detail":f"Страницу не удалось разобрать: {meta['error']}","canFillManually":True},status_code=400)
    if not (meta.get("title") or meta.get("poster") or meta.get("overview")):
        raise HTTPException(400,"На странице не нашлось названия, постера и описания")

    item=store_page_card(kind,item_key,path,title,url,meta)
    log_activity("library-page",item.get("title") or "",f"данные взяты со страницы {url}",True)
    # Сразу отдаём карточку в Jellyfin, чтобы не нажимать кнопку отдельно.
    if JELLYFIN_KEY:
        try:asyncio.create_task(jellyfin_push_metadata(limit=20))
        except Exception:pass
    return {"ok":True,"message":"Данные со страницы подставлены, отправляю в Jellyfin","item":item,"meta":meta}


@app.post("/api/library/manual-meta")
async def library_manual_meta(kind:str=Form("movies"),item_key:str=Form(""),path:str=Form(""),
                              title:str=Form(""),url:str=Form(""),new_title:str=Form(""),
                              year:str=Form(""),poster:str=Form(""),overview:str=Form(""),
                              genres:str=Form(""),episodes:int=Form(0)):
    """Заполнить карточку проекта вручную — когда сервер не может открыть страницу.

    Сохраняется так же, как данные со страницы: только введённое, без TMDB.
    """
    if kind not in {"movies","tv","anime"}:
        raise HTTPException(400,"Неизвестный раздел")
    new_title=" ".join(str(new_title or "").split())
    if not new_title:
        raise HTTPException(400,"Нужно хотя бы название")
    url=(url or "").strip()
    if url and not url.lower().startswith(("http://","https://")):
        raise HTTPException(400,"Ссылка должна начинаться с http или https")
    poster=(poster or "").strip()
    if poster and not poster.lower().startswith(("http://","https://")):
        raise HTTPException(400,"Постер — ссылка на картинку http или https")
    year=str(year or "").strip()
    if year and not re.fullmatch(r"(19|20)\d{2}",year):
        raise HTTPException(400,"Год — четыре цифры")
    meta={"title":new_title[:200],"year":year,"poster":poster,"overview":" ".join((overview or "").split())[:2000],
          "episodes":max(0,min(int(episodes or 0),5000)),
          "genres":[g.strip() for g in re.split(r"[,;]",genres or "") if g.strip()][:6],"rating":None}
    item=store_page_card(kind,item_key,path,title,url or "manual",meta)
    log_activity("library-page",item.get("title") or "","данные введены вручную"+(f" для {url}" if url else ""),True)
    if JELLYFIN_KEY:
        try:asyncio.create_task(jellyfin_push_metadata(limit=20))
        except Exception:pass
    return {"ok":True,"message":"Карточка сохранена","item":item}


@app.post("/api/library/rename")
async def library_rename(new_title:str=Form(...),kind:str=Form("movies"),
                         item_key:str=Form(""),path:str=Form(""),
                         rename_folder:str=Form("1")):
    """Переименовать скачанный тайтл: карточку и, при желании, папку на диске.

    Папку у тайтлов, которыми управляют Radarr или Sonarr, не трогаем — иначе
    ARR потеряет связь со своей библиотекой и начнёт качать заново.
    """
    new_title=" ".join(str(new_title or "").split())
    if not new_title:
        raise HTTPException(400,"Пустое название")
    if len(new_title)>200:
        raise HTTPException(400,"Слишком длинное название")
    if re.search(r"[\\/:*?\"<>|]",new_title):
        raise HTTPException(400,"В названии нельзя использовать символы \\ / : * ? \" < > |")
    if kind not in {"movies","tv","anime"}:
        raise HTTPException(400,"Неизвестный раздел")

    row=find_library_row(kind,item_key,path)
    if not row:
        raise HTTPException(404,"Тайтл не найден в библиотеке")

    source=(row.get("catalog_source") or "").lower()
    old_path=str(row.get("path") or "").rstrip("/")
    new_path=old_path
    folder_renamed=False
    warning=""

    if rename_folder=="1" and old_path:
        if source in {"radarr","sonarr"}:
            warning=f"Папку не трогаю: тайтлом управляет {source.capitalize()}, переименование разорвёт связь."
        else:
            src=Path(old_path)
            dst=src.parent/new_title
            if not src.exists():
                warning="Папка на диске не найдена, переименовано только название карточки."
            elif dst.exists() and str(dst)!=str(src):
                warning="Папка с таким именем уже есть, переименовано только название карточки."
            else:
                try:
                    if str(dst)!=str(src):
                        shutil.move(str(src),str(dst))
                    new_path=str(dst); folder_renamed=True
                except Exception as e:
                    warning=f"Папку переименовать не удалось: {str(e)[:120]}"

    extra=_json_dict(row.get("extra_json"))
    extra["manualMetadata"]=True
    extra["needsMetadata"]=False
    # Ручные данные привязаны к пути — переносим их на новый, сохраняя то, что
    # пришло со страницы-источника: строка library_cache могла быть уже
    # перезаписана синхронизацией с ARR и хранить чужой постер.
    prev=manual_meta_index().get(old_path) if old_path else None
    prev=prev or {"year":row.get("year") or "","poster":row.get("poster") or "",
                  "overview":row.get("overview") or "","sourceUrl":extra.get("sourceUrl") or "",
                  "episodes":int(extra.get("episodesTotal") or 0),
                  "genres":extra.get("genres") if extra.get("fromPage") else [],
                  "rating":extra.get("rating") if extra.get("fromPage") else None}
    with cache_db() as con:
        con.execute("""update library_cache set title=?,path=?,extra_json=? where kind=? and item_key=?""",
                    (new_title,new_path,json.dumps(extra,ensure_ascii=False),kind,row["item_key"]))
        save_manual_meta(con,new_path,kind,new_title,prev.get("year"),prev.get("poster"),
                         prev.get("overview"),prev.get("sourceUrl"),prev.get("episodes"),
                         prev.get("genres"),prev.get("rating"))
        if old_path and old_path!=new_path:
            con.execute("delete from manual_meta where path=?",(old_path,))
            con.execute("update download_jobs set final_path=? where final_path=?",(new_path,old_path))
            move_collection_item(con,old_path,new_path)
        con.commit()
    # Скан медиатеки закэширован — сбрасываем, чтобы список обновился сразу.
    set_setting(f"fs_scan_{kind}","")
    log_activity("library-rename",new_title,
                 f"{old_path or 'без пути'} → {new_path or 'без пути'}"+(f"; {warning}" if warning else ""),True)
    if JELLYFIN_KEY:
        try:asyncio.create_task(jellyfin_push_metadata(limit=20))
        except Exception:pass
    return {"ok":True,"title":new_title,"path":new_path,"folderRenamed":folder_renamed,
            "warning":warning,
            "message":("Переименовано" if not warning else "Переименовано с оговоркой")}


@app.post("/api/library/delete")
async def library_delete(kind:str=Form("movies"),item_key:str=Form(""),path:str=Form(""),
                         title:str=Form(""),delete_files:str=Form("1"),
                         stop_tracking:str=Form("1")):
    """Убрать тайтл из библиотеки: файлы с диска, тайтл из ARR и карточку из базы.

    Отслеживание снимаем вместе с файлами: иначе Radarr или Sonarr увидят
    пропажу и скачают удалённое заново.
    """
    if kind not in {"movies","tv","anime"}:
        raise HTTPException(400,"Неизвестный раздел")
    wipe=str(delete_files)=="1"
    untrack=str(stop_tracking)=="1"
    row=find_library_row(kind,item_key,path,title) or {}
    if not row and not path:
        raise HTTPException(404,"Тайтл не найден в библиотеке")
    source=(row.get("catalog_source") or "").lower()
    name=row.get("title") or title or "Тайтл"
    key=str(row.get("item_key") or item_key or "")
    target_path=str(row.get("path") or path or "").rstrip("/")

    warnings=[]
    arr_removed=False
    if untrack and source in {"radarr","sonarr"}:
        # Файлы удаляет MediaHub: путь на диске он знает точнее, чем ARR.
        arr_removed,arr_error=await arr_forget_title(source,key,False)
        if arr_error:
            warnings.append(f"{source.capitalize()}: {arr_error}")
    elif wipe and source in {"radarr","sonarr"}:
        warnings.append(f"Тайтл остался в {source.capitalize()} — он может скачать его заново")

    freed=0; removed_torrents=0; wiped=False
    if wipe and target_path:
        target=Path(target_path)
        if not target.exists() and not target.is_symlink():
            warnings.append("Файлов на диске уже не было")
        elif not safe_media_path(target):
            raise HTTPException(400,f"Путь вне медиатеки, удалять нельзя: {target_path}")
        elif not can_delete_path(target):
            raise HTTPException(400,"Это корневая папка раздела — её удалять нельзя")
        else:
            freed=path_size(target)
            removed_torrents=await qbit_drop_tasks_under(target)
            delete_media_tree(target)
            wiped=True
    elif wipe:
        warnings.append("У карточки нет пути на диске — удалять было нечего")
    elif target_path:
        warnings.append("Файлы остались на диске, карточка вернётся при следующем сканировании")

    with cache_db() as con:
        if key:
            con.execute("delete from library_cache where kind=? and item_key=?",(kind,key))
        if untrack:
            # Пользовательская метка живёт отдельно от ARR, иначе тайтл вернётся в список.
            con.execute("delete from user_tracking where kind=? and (item_key=? or track_key=?)",
                        (kind,key,f"{kind}|{normalize_search_text(name)}"))
        if wiped and target_path:
            move_collection_item(con,target_path,None)
        con.commit()
    if wiped:
        forget_media_path(target_path)
    reset_fs_scan_cache()
    await jellyfin_refresh()
    subprocess.Popen(["systemctl","start","mediahub-local-cache.service"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

    done=[]
    if wiped:
        done.append(f"файлы удалены ({human(freed)})" if freed else "файлы удалены")
    if arr_removed:
        done.append(f"снято с отслеживания в {source.capitalize()}")
    if removed_torrents:
        done.append(f"задач в qBittorrent убрано: {removed_torrents}")
    warning="; ".join(warnings)
    log_activity("library-delete",name,
                 f"{target_path or 'без пути'} · {', '.join(done) or 'только карточка'}"+(f"; {warning}" if warning else ""),
                 True)
    return {"ok":True,"title":name,"path":target_path,"deletedFiles":wiped,
            "freed":freed,"freedHuman":human(freed) if freed else "",
            "removedFromArr":arr_removed,"removedTorrents":removed_torrents,
            "warning":warning,
            "message":f"«{name}» убран из библиотеки"+(" · "+", ".join(done) if done else "")}


@app.get("/api/library/titles")
async def library_titles(kind:str=Query("anime")):
    """Список уже скачанных проектов — чтобы доложить новый сезон к существующему."""
    if kind not in {"movies","tv","anime"}:
        raise HTTPException(400,"Неизвестный раздел")
    root={"movies":MOVIES_ROOT,"tv":TV_ROOT,"anime":ANIME_ROOT}[kind]
    out=[]
    with cache_db() as con:
        rows=con.execute("""select title,path,poster from library_cache
                            where kind=? and path is not null and path!=''
                            order by title collate nocase""",(kind,)).fetchall()
    seen=set()
    manual=manual_meta_index()
    for r in rows:
        folder=Path(str(r["path"]))
        name=folder.name
        if name in seen:
            continue
        seen.add(name)
        seasons=project_season_numbers(folder)
        item=apply_manual_meta({"title":r["title"] or name,"poster":r["poster"] or "","path":str(folder)},manual)
        out.append({"title":item.get("title") or name,"folder":name,"path":str(folder),
                    "poster":item.get("poster") or "","seasons":seasons,
                    "isDir":folder.is_dir(),
                    "nextSeason":(max(seasons)+1) if seasons else 2})
    return {"root":str(root),"items":out}


def project_season_numbers(folder:Path):
    """Номера сезонов по подпапкам «Season NN» / «S2» внутри проекта."""
    seasons=set()
    try:
        for child in Path(folder).iterdir():
            m=re.match(r"^(?:season\s*|s)(\d{1,3})$",child.name,re.I) if child.is_dir() else None
            if m:
                seasons.add(int(m.group(1)))
    except Exception:
        pass
    return sorted(seasons)


# Файлы оформления Jellyfin в корне сериала: они описывают весь проект и при
# раскладке по сезонам должны остаться на месте.
PROJECT_ROOT_KEEP_EXTS={".nfo",".jpg",".jpeg",".png",".webp",".tbn"}


def guess_merge_season(name,existing):
    """Номер сезона для присоединяемой части: из имени папки или следующий свободный.

    Кроме обычных «S2», «[ТВ-2]», «2 сезон» понимает хвост вида «Kanojo 3 RUS» —
    так часто называют раздачи следующих сезонов аниме.
    """
    n=season_number(name)
    if n<=1:
        m=re.search(r"(?<![\d.])(\d{1,2})(?:\s+(?:rus|eng|jap|sub|subs|dub|tv|bd|web|hd))*\s*$",
                    _fs_library_title(name) or name,re.I)
        n=int(m.group(1)) if m else 1
    if n>1 and n not in existing:
        return n
    return (max(existing)+1) if existing else 2


@app.post("/api/library/merge")
async def library_merge(kind:str=Form("anime"),
                        source_key:str=Form(""),source_path:str=Form(""),source_title:str=Form(""),
                        target_key:str=Form(""),target_path:str=Form(""),
                        season:int=Form(0)):
    """Объединить два проекта библиотеки: файлы второго переезжают в папку первого.

    Типичный случай — следующий сезон аниме скачан отдельной раздачей и лёг
    рядом самостоятельной папкой, поэтому MediaHub и Jellyfin видели два тайтла.
    Сериал получает подпапку «Season NN», фильм — просто дополнительные файлы.
    Карточка остаётся у основного проекта, вторая убирается.
    """
    if kind not in {"movies","tv","anime"}:
        raise HTTPException(400,"Неизвестный раздел")
    src_row=find_library_row(kind,source_key,source_path,source_title) or {}
    dst_row=find_library_row(kind,target_key,target_path) or {}
    src_path=str(src_row.get("path") or source_path or "").rstrip("/")
    dst_path=str(dst_row.get("path") or target_path or "").rstrip("/")
    if not src_path or not dst_path:
        raise HTTPException(404,"Не найдены папки обоих проектов")
    src=Path(src_path); dst=Path(dst_path)
    for p,label in ((src,"присоединяемого"),(dst,"основного")):
        if not p.exists():
            raise HTTPException(404,f"Папка {label} проекта не найдена на диске: {p}")
        if not safe_media_path(p) or is_media_root(p):
            raise HTTPException(400,f"Папка {label} проекта вне медиатеки: {p}")
    if src.resolve()==dst.resolve():
        raise HTTPException(400,"Это один и тот же проект")
    if _is_under(dst.resolve(),src.resolve()) or _is_under(src.resolve(),dst.resolve()):
        raise HTTPException(400,"Один проект лежит внутри другого — объединять нечего")
    if not dst.is_dir():
        raise HTTPException(400,"Основной проект — одиночный файл. Выбери основным проект с папкой")

    src_source=(src_row.get("catalog_source") or "").lower()
    dst_source=(dst_row.get("catalog_source") or "").lower()
    src_name=src_row.get("title") or source_title or src.name
    dst_name=dst_row.get("title") or dst.name
    manual=manual_meta_index()
    src_name=(manual.get(src_path) or {}).get("title") or src_name
    dst_name=(manual.get(dst_path) or {}).get("title") or dst_name
    warnings=[]; normalized=0; season_used=0

    if kind in {"tv","anime"}:
        existing=project_season_numbers(dst)
        if not existing and dst_source!="sonarr":
            # Серии, лежащие прямо в папке проекта, — это первый сезон. Рядом с
            # «Season 02» Jellyfin иначе путал бы, где чей эпизод.
            loose=[c for c in dst.iterdir()
                   if not c.name.startswith(".")
                   and not (c.is_file() and c.suffix.lower() in PROJECT_ROOT_KEEP_EXTS)]
            if any((c.is_file() and c.suffix.lower() in VIDEO_EXTS) or (c.is_dir() and _video_count(c)[0]) for c in loose):
                for c in loose:
                    try:
                        normalized+=move_merge(c,dst/"Season 01"/c.name).get("moved",0)
                    except RuntimeError as e:
                        raise HTTPException(409,str(e))
                existing=[1]
        if src.is_dir() and _season_root_source(src):
            # Внутри уже разложено по сезонам — переносим как есть.
            dest=dst
        else:
            season_used=int(season or 0) or guess_merge_season(src.stem if src.is_file() else src.name,existing)
            if not 0<season_used<=200:
                raise HTTPException(400,"Номер сезона должен быть от 1 до 200")
            dest=dst/f"Season {season_used:02d}"
            if src.is_file():
                dest=dest/src.name
    else:
        dest=dst/src.name if src.is_file() else dst

    removed_torrents=await qbit_drop_tasks_under(src)
    try:
        result=await asyncio.get_running_loop().run_in_executor(None,move_merge,src,dest)
    except RuntimeError as e:
        raise HTTPException(409,str(e))

    arr_removed=False
    if src_source in {"radarr","sonarr"} and src_row.get("item_key"):
        # Папки больше нет — ARR без этого начал бы качать «пропажу» заново.
        arr_removed,arr_error=await arr_forget_title(src_source,str(src_row["item_key"]),False)
        if arr_error:
            warnings.append(f"{src_source.capitalize()}: {arr_error}")
    if dst_source in {"radarr","sonarr"}:
        warnings.append(f"Основной проект ведёт {dst_source.capitalize()} — новые файлы он увидит после пересканирования")

    with cache_db() as con:
        if src_row.get("item_key"):
            con.execute("delete from library_cache where kind=? and item_key=?",(kind,str(src_row["item_key"])))
        con.execute("delete from manual_meta where path=?",(src_path,))
        con.execute("update download_jobs set final_path=? where final_path=?",(str(dest),src_path))
        # Присоединённого проекта больше нет — в коллекции остаётся основной.
        move_collection_item(con,src_path,None)
        con.commit()
    forget_media_path(src_path)
    reset_fs_scan_cache()
    await jellyfin_refresh()
    subprocess.Popen(["systemctl","start","mediahub-local-cache.service"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

    moved=int(result.get("moved") or 0)
    where=f"Season {season_used:02d}" if season_used else ("со своими сезонами" if kind!="movies" else "в папку фильма")
    done=[f"перенесено файлов: {moved}"]
    if result.get("deduped"):
        done.append(f"дубликатов убрано: {result['deduped']}")
    if normalized:
        done.append(f"серии основного проекта сложены в Season 01: {normalized}")
    if removed_torrents:
        done.append(f"задач в qBittorrent снято: {removed_torrents}")
    if arr_removed:
        done.append(f"снят с отслеживания в {src_source.capitalize()}")
    warning="; ".join(warnings)
    log_activity("library-merge",dst_name,
                 f"{src_path} → {dest} · {', '.join(done)}"+(f"; {warning}" if warning else ""),True)
    return {"ok":True,"target":dst_name,"source":src_name,"dest":str(dest),"season":season_used,
            "moved":moved,"normalized":normalized,"removedTorrents":removed_torrents,
            "warning":warning,
            "message":f"«{src_name}» присоединён к «{dst_name}» ({where}) · "+", ".join(done)}


PROJECT_FILES_LIMIT=3000


def _project_root_for(path:Path):
    """Папка проекта для файла: первый уровень внутри корня раздела."""
    for root in (MOVIES_ROOT,TV_ROOT,ANIME_ROOT):
        try:
            rel=path.resolve().relative_to(Path(root).resolve())
        except Exception:
            continue
        if rel.parts:
            return Path(root).resolve()/rel.parts[0]
    return None


def _file_companions(f:Path):
    """Субтитры и дорожки с тем же именем, что у видео: «Серия 01.rus.srt»."""
    out=[]
    try:
        for x in f.parent.iterdir():
            if x!=f and x.is_file() and x.name.startswith(f.stem+"."):
                out.append(x)
    except Exception:
        pass
    return out


def _check_project_file(path:str):
    f=Path(path or "")
    if not f.exists() or not f.is_file():
        raise HTTPException(404,"Файл не найден")
    if not safe_media_path(f) or not _project_root_for(f):
        raise HTTPException(400,"Файл вне папок медиатеки")
    return f


@app.get("/api/library/files")
async def library_files(path:str=Query(...)):
    """Список файлов проекта для карточки: что лежит, в каком сезоне, играет ли."""
    root=Path(path or "")
    if not root.exists():
        raise HTTPException(404,"Папка проекта не найдена на диске")
    if not safe_media_path(root) or is_media_root(root):
        raise HTTPException(400,"Это не папка проекта")
    def collect():
        files=[root] if root.is_file() else sorted(
            (f for f in root.rglob("*") if f.is_file() and not f.name.startswith(".")),
            key=lambda f:[p.casefold() for p in f.relative_to(root).parts])
        out=[];total=0
        for f in files[:PROJECT_FILES_LIMIT]:
            try:size=f.stat().st_size
            except OSError:size=0
            total+=size
            ext=f.suffix.lower()
            label,support=format_support(ext)
            if ext in SUB_EXTS:support="sub"
            elif ext not in VIDEO_EXTS:support="other"
            rel=f.name if root.is_file() else str(f.relative_to(root))
            folder="" if root.is_file() or f.parent==root else str(f.parent.relative_to(root))
            out.append({"name":f.name,"path":str(f),"rel":rel,"folder":folder,"size":size,"sizeHuman":human(size),
                        "video":ext in VIDEO_EXTS,"format":label,"support":support,
                        "supportNote":FORMAT_SUPPORT_NOTE.get(support,""),
                        "season":_season_folder_number(f.parent.name) if f.parent!=root else None})
        return out,total,len(files)
    items,total,count=await asyncio.get_running_loop().run_in_executor(None,collect)
    return {"path":str(root),"items":items,"count":count,"truncated":count>len(items),
            "totalSize":total,"totalHuman":human(total),
            "videos":sum(1 for x in items if x["video"]),
            "seasons":project_season_numbers(root) if root.is_dir() else []}


PLAYER_PROBE_CACHE={}
PLAYER_TRANSCODES=set()

ARTWORK_ROOT=CACHE_DB.parent/'artwork'
ARTWORK_SLOTS=asyncio.Semaphore(3)

@app.get('/api/artwork')
async def artwork(url: str = Query(...,max_length=4096)):
    """Кэшировать публичные обложки через настроенный канал хаба с ограничением нагрузки."""
    from urllib.parse import urlparse
    parsed=urlparse(url)
    if parsed.scheme not in ('http','https') or not parsed.hostname or _blocked_fetch_host(url):
        raise HTTPException(400,'Недопустимый адрес обложки')
    key=hashlib.sha256(url.encode()).hexdigest()
    target=ARTWORK_ROOT/f'{key}.img'
    if not target.is_file():
        async with ARTWORK_SLOTS:
            if not target.is_file():
                try:
                    response,magnet=await asyncio.wait_for(fetch_page_resource(url,referer=f'{parsed.scheme}://{parsed.netloc}/',limit=8*1024*1024),timeout=28)
                    data=response.content if response is not None and not magnet else b''
                    valid=data.startswith(b'\xff\xd8\xff') or data.startswith(b'\x89PNG\r\n\x1a\n') or (data[:4]==b'RIFF' and data[8:12]==b'WEBP') or data[:6] in (b'GIF87a',b'GIF89a')
                    if not valid:raise ValueError('Источник не вернул изображение')
                    ARTWORK_ROOT.mkdir(parents=True,exist_ok=True)
                    temporary=target.with_suffix('.tmp')
                    try:
                        temporary.write_bytes(data);temporary.replace(target)
                    finally:temporary.unlink(missing_ok=True)
                    cached=list(ARTWORK_ROOT.glob('*.img'))
                    if len(cached)>2048:
                        for old in sorted(cached,key=lambda p:p.stat().st_mtime)[:256]:
                            if old!=target:old.unlink(missing_ok=True)
                except (ValueError,httpx.HTTPError,asyncio.TimeoutError,OSError) as error:
                    raise HTTPException(502,'Обложка пока недоступна') from error
    signature=target.open('rb')
    with signature:head=signature.read(12)
    mime='image/jpeg' if head[:3]==b'\xff\xd8\xff' else 'image/png' if head[:4]==b'\x89PNG' else 'image/webp' if head[:4]==b'RIFF' else 'image/gif'
    return FileResponse(target,media_type=mime,headers={'Cache-Control':'private, max-age=86400'})

PLAYER_THUMB_LOCK=threading.Lock()
PLAYER_THUMB_ROOT=CACHE_DB.parent/'player-previews'


def player_thumb_args(f,output,second):
    """Получить один небольшой кадр без звука и полной конвертации видео."""
    return [player_binary('ffmpeg'),'-hide_banner','-loglevel','error','-nostdin','-y',
            '-ss',str(second),'-threads','1','-i',str(f),'-map','0:v:0','-an','-sn',
            '-frames:v','1','-vf','scale=480:270:force_original_aspect_ratio=decrease,pad=480:270:(ow-iw)/2:(oh-ih)/2',
            '-threads','1','-q:v','4',str(output)]


@app.get('/api/player/thumbnail')
def player_thumbnail(path:str=Query(...),frame:int=Query(0,ge=0,le=2)):
    """Лениво создать и закэшировать кадр серии; одновременно работает только один FFmpeg."""
    f=player_file(path);key=hashlib.sha256(f'{f}|{player_signature(f)}|{frame}'.encode()).hexdigest()
    target=PLAYER_THUMB_ROOT/f'{key}.jpg'
    if not target.exists():
        if not player_binary('ffmpeg'):raise HTTPException(503,'FFmpeg недоступен для превью')
        if not PLAYER_THUMB_LOCK.acquire(timeout=20):raise HTTPException(503,'Превью ещё готовится, повторите позже')
        try:
            if not target.exists():
                PLAYER_THUMB_ROOT.mkdir(parents=True,exist_ok=True)
                probe=player_probe(f);duration=float(probe.get('duration') or 0)
                second=max(0,min(duration-.5,duration*(.12,.38,.65)[frame])) if duration>0 else 0
                temporary=target.with_suffix('.tmp.jpg')
                try:
                    subprocess.run(player_thumb_args(f,temporary,second),capture_output=True,check=True,timeout=15)
                    if not temporary.exists() or not temporary.stat().st_size:raise HTTPException(503,'Не удалось получить кадр')
                    temporary.replace(target)
                except (OSError,subprocess.SubprocessError):raise HTTPException(503,'Не удалось получить кадр видео')
                finally:temporary.unlink(missing_ok=True)
                cached=list(PLAYER_THUMB_ROOT.glob('*.jpg'))
                if len(cached)>2048:
                    for old in sorted(cached,key=lambda p:p.stat().st_mtime)[:256]:
                        if old!=target:old.unlink(missing_ok=True)
        finally:PLAYER_THUMB_LOCK.release()
    return FileResponse(target,media_type='image/jpeg',headers={'Cache-Control':'private, max-age=86400'})


def player_binary(name):
    """Найти самостоятельный FFmpeg либо установленный комплект кодеков."""
    return shutil.which(name) or (str(Path('/usr/lib/jellyfin-ffmpeg')/name) if (Path('/usr/lib/jellyfin-ffmpeg')/name).is_file() else '')


def player_file(path):
    """Разрешить просмотр только видео из папки проекта медиатеки."""
    f=_check_project_file(path).resolve()
    if f.suffix.lower() not in VIDEO_EXTS: raise HTTPException(400,"Это не видеофайл")
    return f


def player_signature(f):
    """Отличить заменённый файл от прежней записи просмотра."""
    s=f.stat();return f'{s.st_size}:{s.st_mtime_ns}'


def player_probe(f):
    """Определить дорожки и длительность с таймаутом и кэшем."""
    key=(str(f),player_signature(f));cached=PLAYER_PROBE_CACHE.get(key)
    if cached and time.time()-cached[0]<600:return cached[1]
    result={"duration":0,"audio":[],"chapters":[],"direct":f.suffix.lower() in {'.mp4','.m4v','.webm'},"probeAvailable":False,"remux":False,"streamCodec":"avc1.42E01F","defaultQuality":"720"}
    binary=player_binary('ffprobe')
    if binary:
        try:
            p=subprocess.run([binary,'-v','error','-show_format','-show_streams','-show_chapters','-of','json',str(f)],capture_output=True,timeout=12,check=True)
            data=json.loads(p.stdout);streams=data.get('streams',[])
            # Главы файла (опенинг, эпизод, эндинг) — для перемотки по таймкодам в плеерах.
            for i,c in enumerate(data.get('chapters',[])[:200]):
                try:start=float(c.get('start_time') or 0);end=float(c.get('end_time') or 0)
                except (TypeError,ValueError):continue
                if end>start:result['chapters'].append({"start":round(start,3),"end":round(end,3),"title":(c.get('tags') or {}).get('title') or f'Глава {i+1}'})
            result['duration']=max(0,float(data.get('format',{}).get('duration',0) or 0));result['probeAvailable']=True
            videos=[s for s in streams if s.get('codec_type')=='video'];audios=[s for s in streams if s.get('codec_type')=='audio']
            for i,s in enumerate(audios):
                tags=s.get('tags',{});result['audio'].append({"index":i,"label":tags.get('title') or tags.get('language') or f'Дорожка {i+1}',"codec":s.get('codec_name','')})
            v=videos[0] if videos else {};a=audios[0] if audios else {}
            result['remux']=v.get('codec_name')=='h264' and v.get('pix_fmt') in {'yuv420p','yuvj420p'}
            if result['remux']:
                profile={'High':'6400','Main':'4D40','Baseline':'42E0','Constrained Baseline':'42E0'}.get(v.get('profile'),'6400')
                result['streamCodec']='avc1.'+profile+format(int(v.get('level') or 40),'02X')
            if v.get('codec_name') in {'hevc','av1'} and (os.cpu_count() or 2)<=4:result['defaultQuality']='480'
            result['direct']=bool(videos) and ((f.suffix.lower() in {'.mp4','.m4v'} and v.get('codec_name')=='h264' and v.get('pix_fmt') in {'yuv420p','yuvj420p'} and a.get('codec_name','aac') in {'aac','mp3'}) or (f.suffix.lower()=='.webm' and v.get('codec_name') in {'vp8','vp9','av1'} and a.get('codec_name','opus') in {'opus','vorbis'}))
        except (OSError,ValueError,subprocess.SubprocessError): pass
    if len(PLAYER_PROBE_CACHE)>=128:PLAYER_PROBE_CACHE.pop(next(iter(PLAYER_PROBE_CACHE)))
    PLAYER_PROBE_CACHE[key]=(time.time(),result);return result


def player_normalize_history():
    """Учесть титры в ранее сохранённой истории, не меняя позицию просмотра."""
    with cache_db() as con:
        con.execute('update account_playback_history set completed=1 where user_id=? and completed=0 and duration>0 and position>=max(duration*.85,duration-180)',(auth_user_id(),));con.commit()


def player_saved(f):
    """Вернуть позицию только для неизменённого видеофайла."""
    player_normalize_history()
    with cache_db() as con:row=con.execute('select * from account_playback_history where user_id=? and path=?',(auth_user_id(),str(f))).fetchone()
    return dict(row) if row and row['signature']==player_signature(f) else {}


def player_resume_items():
    """Собрать полку незаконченных просмотров встроенного плеера."""
    player_normalize_history()
    with cache_db() as con:
        rows=con.execute('select * from account_playback_history where user_id=? and completed=0 and position>=5 order by updated_at desc limit 80',(auth_user_id(),)).fetchall()
        cards={r['path']:dict(r) for r in con.execute('select path,title,poster,kind from library_cache where has_file=1')}
    out=[];seen=set()
    for row in rows:
        try:
            f=player_file(row['path'])
            if row['signature']!=player_signature(f) or row['project'] in seen:continue
        except (HTTPException,OSError):continue
        card=cards.get(row['project'],{});seen.add(row['project'])
        out.append({"title":card.get('title') or Path(row['project']).stem,"poster":card.get('poster') or '',"kind":card.get('kind') or 'tv',"subtitle":f.name,"projectPath":row['project'],"filePath":str(f),"progress":min(100,round(100*row['position']/max(1,row['duration']))),"position":row['position'],"duration":row['duration']})
        if len(out)>=18:break
    return out


@app.get('/api/player/resume')
def player_resume():
    """Полка «Продолжить просмотр» отдельно от главной: приложению на ТВ и телефоне
    не нужно тянуть весь каталог /api/home ради нескольких карточек. Обычная
    функция — FastAPI выполнит её в пуле потоков, не задерживая другие запросы."""
    return {"items":player_resume_items()}


@app.get('/api/player/session')
async def player_session(path:str=Query(...),file:str=''):
    """Подготовить плейлист, дорожки, субтитры и сохранённую позицию."""
    player_normalize_history()
    listing=await library_files(path)
    items=[x for x in listing['items'] if x['video'] and safe_media_path(Path(x['path']))]
    for item in items:item['path']=str(Path(item['path']).resolve())
    items.sort(key=lambda x:re.sub(r'\d+',lambda m:m.group().zfill(12),x['rel'].casefold()))
    if not items:raise HTTPException(404,'В проекте нет видео для просмотра')
    if file:
        chosen=player_file(file)
        if not any(Path(x['path']).resolve()==chosen for x in items):raise HTTPException(400,'Файл не принадлежит проекту')
    else:
        chosen=player_file(items[0]['path'])
        with cache_db() as con:rows=con.execute('select * from account_playback_history where user_id=? and project=? order by updated_at desc',(auth_user_id(),str(Path(path).resolve()))).fetchall()
        for r in rows:
            index=next((i for i,x in enumerate(items) if Path(x['path']).resolve()==Path(r['path'])),None)
            if index is None or r['signature']!=player_signature(player_file(r['path'])):continue
            chosen=player_file(items[index+1]['path'] if r['completed'] and index+1<len(items) else r['path']);break
    probe=await asyncio.to_thread(player_probe,chosen);saved=await asyncio.to_thread(player_saved,chosen)
    subtitles=[]
    for s in chosen.parent.iterdir():
        if s.is_file() and safe_media_path(s) and s.suffix.lower() in {'.srt','.vtt'} and (s.stem==chosen.stem or s.name.startswith(chosen.stem+'.')):
            subtitles.append({"path":str(s),"label":s.name})
    return {"project":str(Path(path)),"file":str(chosen),"items":items,"subtitles":subtitles,"position":0 if saved.get('completed') else saved.get('position',0),"transcodeAvailable":bool(player_binary('ffmpeg')) and probe['probeAvailable'],**probe}


@app.get('/api/player/project')
async def player_project(path:str=Query(...)):
    """Вернуть серии с историей без анализа кодеков каждого большого файла."""
    player_normalize_history()
    listing=await library_files(path);items=[];latest=None
    with cache_db() as con:history={r['path']:dict(r) for r in con.execute('select * from account_playback_history where user_id=? and project=?',(auth_user_id(),str(Path(path).resolve())))}
    for item in listing['items']:
        if not item['video'] or not safe_media_path(Path(item['path'])):continue
        f=Path(item['path']).resolve();item['path']=str(f);saved=history.get(str(f),{})
        try:
            if saved and saved['signature']!=player_signature(f):saved={}
        except OSError:continue
        item.update(position=saved.get('position',0),duration=saved.get('duration',0),completed=bool(saved.get('completed')),progress=round(min(100,100*saved.get('position',0)/max(1,saved.get('duration',0)))),updatedAt=saved.get('updated_at',0))
        version=hashlib.sha256(player_signature(f).encode()).hexdigest()[:12]
        item['previewFrames']=[f'/api/player/thumbnail?path={quote(str(f),safe="")}&frame={n}&v={version}' for n in range(3)]
        item['thumb']=item['previewFrames'][0]
        if saved and (not latest or saved['updated_at']>latest['updatedAt']):latest=item
        items.append(item)
    items.sort(key=lambda x:re.sub(r'\d+',lambda m:m.group().zfill(12),x['rel'].casefold()))
    resume=latest
    if latest and latest['completed']:
        index=items.index(latest);resume=items[index+1] if index+1<len(items) else None
    return {'project':str(Path(path)),'items':items,'last':latest,'resume':resume,'count':len(items),'watched':sum(x['completed'] for x in items),'truncated':listing.get('truncated',False)}


@app.api_route('/api/player/file',methods=['GET','HEAD'])
async def player_direct(path:str=Query(...)):
    """Отдать видео с HTTP Range для перемотки без полной загрузки."""
    f=player_file(path);mime='video/webm' if f.suffix.lower()=='.webm' else 'video/mp4'
    return FileResponse(f,media_type=mime,headers={'Content-Encoding':'identity','Cache-Control':'private, no-cache'})


@app.get('/api/player/subtitle')
async def player_subtitle(path:str=Query(...)):
    """Преобразовать внешние SRT в браузерные WebVTT."""
    f=_check_project_file(path)
    if f.suffix.lower() not in {'.srt','.vtt'} or f.stat().st_size>2*1024*1024:raise HTTPException(400,'Поддерживаются SRT/VTT до 2 МБ')
    raw=await asyncio.to_thread(f.read_bytes)
    try:body=raw.decode('utf-8-sig')
    except UnicodeDecodeError:body=raw.decode('cp1251')
    body=body.replace('\r\n','\n').replace('\r','\n')
    if f.suffix.lower()=='.srt':body='WEBVTT\n\n'+re.sub(r'(\d{2}:\d{2}:\d{2}),(\d{3})',r'\1.\2',body)
    return Response(body,media_type='text/vtt',headers={'Cache-Control':'private, max-age=60'})


@app.post('/api/player/progress')
async def player_progress(path:str=Form(...),position:float=Form(...),duration:float=Form(...),ended:int=Form(0)):
    """Сохранить личную историю просмотра и отметку завершения."""
    import math
    f=player_file(path)
    if not math.isfinite(position) or not math.isfinite(duration) or position<0 or duration<=0 or duration>7*86400:raise HTTPException(400,'Некорректная позиция просмотра')
    position=min(position,duration);completed=int(bool(ended) or position>=max(duration*.85,duration-180))
    with cache_db() as con:
        con.execute('insert or replace into account_playback_history values(?,?,?,?,?,?,?,?)',(auth_user_id(),str(f),str(_project_root_for(f)),position,duration,completed,player_signature(f),time.time()));con.commit()
    return {'ok':True,'completed':bool(completed)}


# Пульт: телефон выбирает тайтл, телевизор с открытым приложением его запускает.
# Всё живёт в памяти процесса: после перезапуска ТВ просто зарегистрируется заново
# при следующем опросе, а старые команды не должны срабатывать спустя часы.
REMOTE_DEVICES={}
REMOTE_ONLINE_SECONDS=70
REMOTE_COMMAND_TTL=90
REMOTE_ACTIONS={'play','toggle','pause','resume','back','forward','seek','next','stop'}
REMOTE_SEQ=[int(time.time()*1000)]


def remote_device(device,name='',kind='tv'):
    """Найти или создать запись устройства; имя обновляется при каждом опросе."""
    d=REMOTE_DEVICES.get((auth_user_id(),device))
    if d is None:
        if len(REMOTE_DEVICES)>=32:
            stale=min(REMOTE_DEVICES.values(),key=lambda x:x['seen']);REMOTE_DEVICES.pop((stale['user_id'],stale['id']),None)
        d=REMOTE_DEVICES[(auth_user_id(),device)]={'user_id':auth_user_id(),'id':device,'name':'','kind':kind,'seen':0,'queue':[],'state':{},'event':None,'polls':0,'away':False}
    if name:d['name']=name[:60]
    if kind:d['kind']=kind[:16]
    return d


def remote_public(d):
    """Описание устройства для телефона, без служебных полей."""
    online=not d['away'] and (d['polls']>0 or time.time()-d['seen']<REMOTE_ONLINE_SECONDS)
    return {'id':d['id'],'name':d['name'] or 'Телевизор','kind':d['kind'],'online':online,'state':d['state']}


@app.get('/api/remote/poll')
async def remote_poll(device:str=Query(...,pattern=r'^[A-Za-z0-9_-]{6,64}$'),name:str='',kind:str=Query('tv',pattern='^(tv|phone|tablet)$'),ack:int=0,wait:float=Query(25,ge=0,le=30)):
    """Долгий опрос устройства с открытым приложением. Команда снимается с очереди только после подтверждения
    следующим опросом (ack), поэтому оборванное соединение её не теряет."""
    d=remote_device(device,name,kind);d['seen']=time.time();d['away']=False
    d['queue']=[c for c in d['queue'] if c['id']>ack and time.time()-c['at']<REMOTE_COMMAND_TTL]
    if not d['queue'] and wait>0:
        d['event']=d['event'] or asyncio.Event();d['event'].clear();d['polls']+=1
        try:await asyncio.wait_for(d['event'].wait(),wait)
        except asyncio.TimeoutError:pass
        finally:d['polls']-=1;d['seen']=time.time()
    return {'commands':[{k:v for k,v in c.items() if k!='at'} for c in d['queue']]}


@app.post('/api/remote/state')
async def remote_state(device:str=Form(...),title:str=Form(''),subtitle:str=Form(''),poster:str=Form(''),project:str=Form(''),file:str=Form(''),section:str=Form(''),playing:int=Form(0),position:float=Form(0),duration:float=Form(0),active:int=Form(1),away:int=Form(0)):
    """Что сейчас идёт на телевизоре — для экрана пульта на телефоне. away=1 — приложение
    на ТВ свёрнуто: команды до возвращения не выполнятся, поэтому ТВ сразу пропадает из списка."""
    d=REMOTE_DEVICES.get((auth_user_id(),device))
    if d is None:raise HTTPException(404,'Устройство не зарегистрировано')
    d['seen']=time.time();d['away']=bool(away)
    d['state']={'title':title[:200],'subtitle':subtitle[:200],'poster':poster[:2000],'project':project,'file':file,'section':section[:16],'playing':bool(playing),'position':max(0.0,position),'duration':max(0.0,duration),'updatedAt':time.time()} if active else {}
    return {'ok':True}


@app.get('/api/remote/devices')
async def remote_devices():
    """Устройства, на которые можно отправить просмотр, и что на каждом идёт."""
    devices=[remote_public(d) for d in REMOTE_DEVICES.values() if d['user_id']==auth_user_id()]
    return {'devices':sorted([d for d in devices if d['online']],key=lambda d:d['name'].casefold())}


@app.post('/api/remote/send')
async def remote_send(device:str=Form(...),action:str=Form(...),path:str=Form(''),file:str=Form(''),restart:int=Form(0),seconds:float=Form(0),start:float=Form(-1),title:str=Form(''),poster:str=Form(''),kind:str=Form('')):
    """Поставить команду в очередь телевизора и сразу разбудить его опрос."""
    import math
    d=REMOTE_DEVICES.get((auth_user_id(),device))
    if d is None or not remote_public(d)['online']:raise HTTPException(404,'Телевизор не в сети. Откройте на нём MediaHub.')
    if action not in REMOTE_ACTIONS:raise HTTPException(400,'Неизвестная команда пульта')
    if not math.isfinite(seconds) or not math.isfinite(start):raise HTTPException(400,'Некорректная позиция')
    command={'id':0,'action':action,'at':time.time()}
    if action=='play':
        project=Path(path or '')
        if not path or not project.exists() or not safe_media_path(project) or is_media_root(project):raise HTTPException(400,'Это не папка проекта')
        chosen=player_file(file) if file else None
        if chosen and _project_root_for(chosen)!=_project_root_for(project):raise HTTPException(400,'Файл не принадлежит проекту')
        command.update(path=str(project),file=str(chosen or ''),restart=bool(restart),title=title[:200],poster=poster[:2000],kind=kind[:16])
        # Перенос просмотра: продолжить ровно с той секунды, где остановилось другое устройство.
        if chosen and start>=0:command['start']=start
    elif action=='seek':command['seconds']=max(0.0,seconds)
    REMOTE_SEQ[0]=max(REMOTE_SEQ[0]+1,int(time.time()*1000));command['id']=REMOTE_SEQ[0]
    # Новый запуск отменяет ещё не выполненные команды: ТВ должен играть последнее выбранное.
    if action=='play':d['queue']=[c for c in d['queue'] if c['action']!='play']
    d['queue']=(d['queue']+[command])[-20:]
    if d['event']:d['event'].set()
    return {'ok':True,'id':command['id'],'device':remote_public(d)}


async def player_stop_process(proc):
    """Освободить кодировщик после закрытия плеера или обрыва сети."""
    if proc.returncode is not None:return
    try:proc.terminate()
    except ProcessLookupError:return
    try:await asyncio.wait_for(proc.wait(),3)
    except asyncio.TimeoutError:
        try:proc.kill()
        except ProcessLookupError:pass
        await proc.wait()


@app.get('/api/player/stream')
async def player_stream(path:str=Query(...),start:float=Query(0,ge=0,le=604800),audio:int=Query(0,ge=0,le=100),quality:str=Query('auto',pattern='^(auto|480|720|original)$'),bitrate:int=Query(0,ge=0,le=20000),timeline:str=Query('accurate',pattern='^(accurate|keyframe)$')):
    """Передавать совместимый H.264/AAC поток; максимум два кодировщика."""
    import math
    f=player_file(path);probe=await asyncio.to_thread(player_probe,f);binary=player_binary('ffmpeg')
    if not math.isfinite(start):raise HTTPException(400,'Некорректная позиция')
    if not binary or not probe['probeAvailable']:raise HTTPException(503,'Для этого формата нужен FFmpeg и FFprobe на сервере')
    if audio>=max(1,len(probe['audio'])):raise HTTPException(400,'Звуковая дорожка не найдена')
    if probe['duration'] and start>=probe['duration']:raise HTTPException(400,'Позиция за концом видео')
    if len(PLAYER_TRANSCODES)>=2:raise HTTPException(429,'Два просмотра уже используют конвертацию. Повтори позже')
    if quality=='original' and not probe.get('remux'):raise HTTPException(400,'Исходный формат требует конвертации')
    if bitrate and bitrate not in {1000,2000,4000,8000,12000,20000}:raise HTTPException(400,'Неизвестная скорость потока')
    if bitrate:probe=dict(probe,remux=False)
    anchor=start
    if probe.get('remux') and quality in {'auto','original'} and start>0:
        anchor=await asyncio.to_thread(player_seek_anchor,f,start) if timeline=='keyframe' else None
        if anchor is None:
            probe=dict(probe,remux=False);anchor=start
            if quality=='original':quality='720'
    token=object();PLAYER_TRANSCODES.add(token);proc=None
    args=player_stream_args(binary,f,probe,anchor,audio,quality,bitrate)
    try:
        proc=await asyncio.create_subprocess_exec(*args,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
        first=await asyncio.wait_for(proc.stdout.read(65536),20)
        if not first:raise HTTPException(422,'FFmpeg не смог открыть видео или выбранную дорожку')
    except asyncio.TimeoutError:
        PLAYER_TRANSCODES.discard(token)
        if proc:await asyncio.shield(player_stop_process(proc))
        raise HTTPException(504,'FFmpeg не успел подготовить поток')
    except BaseException:
        PLAYER_TRANSCODES.discard(token)
        if proc:await asyncio.shield(player_stop_process(proc))
        raise
    async def chunks():
        """Завершить дочерний процесс также при отмене HTTP-запроса."""
        try:
            yield first
            while True:
                data=await proc.stdout.read(65536)
                if not data:break
                yield data
        finally:
            try:await asyncio.shield(player_stop_process(proc))
            finally:PLAYER_TRANSCODES.discard(token)
    return StreamingResponse(chunks(),media_type='video/mp4',headers={'Content-Encoding':'identity','Cache-Control':'no-store','X-Player-Start':str(anchor)})


def player_seek_anchor(f,start):
    """Найти ключевой кадр перед перемоткой; при ошибке нужен точный режим кодирования."""
    binary=player_binary('ffprobe')
    if not binary:return None
    try:
        raw=subprocess.run([binary,'-v','error','-skip_frame','nokey','-read_intervals',f'{start}%+1','-select_streams','v:0','-show_frames','-show_entries','frame=best_effort_timestamp_time','-of','json',str(f)],capture_output=True,timeout=5,check=True)
        frames=json.loads(raw.stdout).get('frames',[])
        points=[float(x['best_effort_timestamp_time']) for x in frames if 'best_effort_timestamp_time' in x]
        points=[x for x in points if 0<=x<=start+0.001]
        return max(points) if points else None
    except (OSError,ValueError,KeyError,subprocess.SubprocessError):return None


def player_stream_args(binary,f,probe,start,audio,quality,bitrate=0):
    """Сохранить H.264 без перекодирования; сложные кодеки конвертировать быстрее."""
    copy=not bitrate and probe.get('remux') and quality in {'auto','original'}
    size=quality if quality in {'480','720'} else probe.get('defaultQuality','720')
    if bitrate and quality=='auto':size='480' if bitrate<=2000 else '720'
    rate=str(max(256,bitrate-128))+'k' if bitrate else ('2500k' if size=='480' else '4500k')
    buffer=str(max(512,(bitrate-128)*2))+'k' if bitrate else ('5000k' if size=='480' else '9000k')
    width,height=(854,480) if size=='480' else (1280,720)
    threads=str(max(1,min(4,(os.cpu_count() or 2)-1)))
    args=[binary,'-nostdin','-hide_banner','-loglevel','error','-ss',str(start),'-threads',threads,*(['-noaccurate_seek'] if copy else []),'-i',str(f),'-map','0:v:0','-map',f'0:a:{audio}?','-sn','-dn']
    if copy:args+=['-c:v','copy']
    else:args+=['-vf',f"scale=w='min({width},iw)':h='min({height},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",'-c:v','libx264','-profile:v','baseline','-level','3.1','-pix_fmt','yuv420p','-preset','ultrafast','-crf','24','-fps_mode','passthrough','-g','50','-threads',threads,'-maxrate',rate,'-bufsize',buffer]
    args+=['-af','aresample=async=1:first_pts=0' if not copy else 'aresample=async=1','-c:a','aac','-ac','2','-b:a','128k','-avoid_negative_ts','make_zero','-movflags','frag_keyframe+empty_moov+default_base_moof','-frag_duration','1000000','-f','mp4','pipe:1']
    return args


@app.post("/api/library/file-rename")
async def library_file_rename(path:str=Form(...),new_name:str=Form(...)):
    """Переименовать файл проекта вместе с одноимёнными субтитрами и дорожками."""
    f=_check_project_file(path)
    name=" ".join(str(new_name or "").split())
    if not name:
        raise HTTPException(400,"Пустое имя")
    if re.search(r'[\\/:*?"<>|]',name) or name in {".",".."}:
        raise HTTPException(400,'В имени нельзя использовать символы \\ / : * ? " < > |')
    # Расширение не забываем: без него Jellyfin перестанет видеть файл.
    if Path(name).suffix.lower()!=f.suffix.lower():
        name+=f.suffix
    dst=f.with_name(name)
    if dst==f:
        return {"ok":True,"message":"Имя не изменилось","path":str(f)}
    if dst.exists():
        raise HTTPException(409,"Файл с таким именем уже есть")
    new_stem=Path(name).stem
    moves=[(f,dst)]+[(c,c.with_name(new_stem+c.name[len(f.stem):])) for c in _file_companions(f)]
    if any(d.exists() for _s,d in moves[1:]):
        raise HTTPException(409,"Одноимённые субтитры с новым именем уже есть")
    await qbit_drop_tasks_under(f)
    for a,b in moves:
        a.rename(b)
    reset_fs_scan_cache()
    await jellyfin_refresh()
    log_activity("file-rename",f.name,f"{f} → {dst}"+(f" (+{len(moves)-1} доп.)" if len(moves)>1 else ""),True)
    return {"ok":True,"path":str(dst),"renamed":len(moves),
            "message":f"Переименовано: {dst.name}"+(f" и ещё {len(moves)-1} одноимённых" if len(moves)>1 else "")}


@app.post("/api/library/file-move")
async def library_file_move(path:str=Form(...),season:int=Form(...)):
    """Переложить серию в «Season NN» своего проекта — если она попала не в тот сезон."""
    f=_check_project_file(path)
    if not 0<=int(season)<=200:
        raise HTTPException(400,"Номер сезона — от 0 до 200")
    project=_project_root_for(f)
    if not project or not project.is_dir():
        raise HTTPException(400,"Файл лежит прямо в корне раздела — сначала объедините его с проектом")
    target_dir=project/f"Season {int(season):02d}"
    if f.parent.resolve()==target_dir.resolve():
        return {"ok":True,"message":"Файл уже в этом сезоне","path":str(f)}
    files=[f]+_file_companions(f)
    for x in files:
        if (target_dir/x.name).exists():
            raise HTTPException(409,f"В {target_dir.name} уже есть {x.name}")
    await qbit_drop_tasks_under(f)
    old_parent=f.parent
    for x in files:
        move_merge(x,target_dir/x.name)
    # Опустевшую сезонную папку не оставляем.
    try:
        if old_parent!=project and not any(old_parent.iterdir()):
            old_parent.rmdir()
    except OSError:
        pass
    reset_fs_scan_cache()
    await jellyfin_refresh()
    log_activity("file-move",f.name,f"{f} → {target_dir}",True)
    return {"ok":True,"path":str(target_dir/f.name),"moved":len(files),
            "message":f"Перенесено в {target_dir.name}"+(f" вместе с {len(files)-1} одноимёнными" if len(files)>1 else "")}


@app.get("/api/library-pending")
async def library_pending():
    """Titles ARR monitors while no file exists yet ("ждём файлы")."""
    out=[]
    with cache_db() as con:
        rows=con.execute(
            """select * from library_cache where has_file=0
               order by added_at desc, title collate nocase limit 400"""
        ).fetchall()
    for r in rows:
        item=row_media(r,r["kind"])
        item.update(library_item_state(r))
        out.append({
            "kind":r["kind"],"title":item.get("title"),"year":item.get("year"),
            "poster":item.get("poster"),"path":item.get("path") or "",
            "addedAt":item.get("added_at") or "","source":item.get("catalog_source") or "",
            "externalId":item.get("externalId") or "","itemKey":item.get("item_key") or "",
            "libraryLabel":item.get("libraryLabel"),"libraryStatus":item.get("libraryStatus"),
        })
    return {"count":len(out),"items":out}


# --- v21.4 отслеживание ------------------------------------------------------
# «Избранное → Отслеживаемые»: что ARR мониторит, но чего ещё нет целиком.
# Отсюда можно проверить релизы или снять тайтл с отслеживания.

def _tracking_release_counts():
    """Сколько релизов уже найдено фоновым поиском по каждому тайтлу."""
    out={}
    try:
        with cache_db() as con:
            rows=con.execute(
                """select kind,title,result_count,status,updated_at from release_prefetch
                   where result_count>0"""
            ).fetchall()
    except Exception:
        return out
    for r in rows:
        key=(r["kind"],normalize_search_text(r["title"]))
        prev=out.get(key) or {}
        if int(r["result_count"] or 0)>=int(prev.get("count") or 0):
            out[key]={"count":int(r["result_count"] or 0),"updatedAt":r["updated_at"],"status":r["status"]}
    return out


def _tracking_rows(only_incomplete=True,only_user=True):
    with cache_db() as con:
        rows=con.execute(
            """select * from library_cache
               order by added_at desc, title collate nocase limit 600"""
        ).fetchall()
    counts=_tracking_release_counts()
    mine=user_tracking_index() if only_user else None
    items=[]
    for r in rows:
        item=row_media(r,r["kind"])
        state=library_item_state(r)
        if only_incomplete and state["libraryStatus"]=="complete":
            continue
        if (r["catalog_source"] or "")=="filesystem":
            continue
        # В «Отслеживаемые» попадает только то, что пользователь отправил сам:
        # раньше сюда сваливалось всё неполное, что ведут Radarr и Sonarr.
        if mine is not None and not is_user_tracked(mine,r["kind"],r["item_key"],
                                                   item.get("externalId"),item.get("title")):
            continue
        found=counts.get((r["kind"],normalize_search_text(item.get("title") or ""))) or {}
        items.append({
            "kind":r["kind"],"itemKey":item.get("item_key") or "","title":item.get("title") or "",
            "year":item.get("year") or "","poster":item.get("poster") or "",
            "path":item.get("path") or "","source":item.get("catalog_source") or "",
            "externalId":item.get("externalId") or "","addedAt":item.get("added_at") or "",
            "libraryStatus":state["libraryStatus"],"libraryLabel":state["libraryLabel"],
            "seasons":state.get("librarySeasons",0),"seasonsComplete":state.get("librarySeasonsComplete",0),
            "episodes":state.get("libraryFiles",0),"episodesTotal":state.get("libraryTotal",0),
            "releaseCount":int(found.get("count") or 0),
            "releaseCheckedAt":found.get("updatedAt") or "",
        })
    return items


@app.get("/api/tracking")
async def tracking_list(include_complete:bool=Query(False),all_titles:bool=Query(False)):
    items=_tracking_rows(only_incomplete=not include_complete,only_user=not all_titles)
    return {
        "onlyMine":not all_titles,
        "count":len(items),
        "withReleases":sum(1 for x in items if x["releaseCount"]),
        "awaiting":sum(1 for x in items if x["libraryStatus"]=="tracked"),
        "partial":sum(1 for x in items if x["libraryStatus"]=="partial"),
        "items":items,
    }


@app.get("/api/tracking/summary")
async def tracking_summary():
    """Короткая сводка для значка в боковой панели."""
    items=_tracking_rows(only_incomplete=True)
    with_releases=[x for x in items if x["releaseCount"]]
    return {
        "tracked":len(items),
        "awaiting":sum(1 for x in items if x["libraryStatus"]=="tracked"),
        "partial":sum(1 for x in items if x["libraryStatus"]=="partial"),
        "withReleases":len(with_releases),
        "titles":[x["title"] for x in with_releases[:5]],
    }


@app.post("/api/tracking/check")
async def tracking_check(kind:str=Form(...),title:str=Form(...),external_id:str=Form(""),
                         catalog:str=Form(""),year:str=Form("")):
    if kind not in {"movies","tv","anime"}:
        raise HTTPException(400,"Неизвестный раздел")
    card={"kind":kind,"catalog":catalog or "","externalId":external_id or "",
          "title":title,"year":year or ""}
    key,row=_release_prefetch_row(kind,catalog or "",external_id or "",title)
    schedule_release_prefetch(card,force=True)
    items=_prefetch_items(row)
    return {"ok":True,"queued":True,"count":len(items),
            "message":(f"Уже найдено релизов: {len(items)}. Обновляю поиск…" if items
                       else "Поиск релизов запущен, результат появится в карточке")}


@app.delete("/api/tracking/{kind}/{item_key}")
async def tracking_remove(kind:str,item_key:str,delete_files:bool=Query(False)):
    """Снять тайтл с отслеживания: убрать из Radarr/Sonarr и из локальной базы."""
    if kind not in {"movies","tv","anime"}:
        raise HTTPException(400,"Неизвестный раздел")
    with cache_db() as con:
        row=con.execute("select * from library_cache where kind=? and item_key=?",(kind,item_key)).fetchone()
    if not row:
        raise HTTPException(404,"Тайтл не найден в локальной базе")
    source=(row["catalog_source"] or "").lower()
    title=row["title"] or ""
    removed,error=await arr_forget_title(source,item_key,delete_files)
    with cache_db() as con:
        con.execute("delete from library_cache where kind=? and item_key=?",(kind,item_key))
        # Снимаем и пользовательскую метку, иначе тайтл вернётся в список.
        con.execute("""delete from user_tracking where kind=? and (item_key=? or track_key=?)""",
                    (kind,str(item_key),f"{kind}|{normalize_search_text(title)}"))
        con.commit()
    log_activity("tracking-remove",title,f"{source or 'локально'} · {'удалено' if removed else error or 'только из базы MediaHub'}",not error)
    return {"ok":not error,"removed":removed,"error":error,
            "message":("Снято с отслеживания" if not error else f"Убрано из MediaHub, но ARR ответил: {error}")}


@app.get("/api/library-search")
async def library_search(q:str=Query(""),kind:str=Query("movies")):
    if kind not in {"movies","tv","anime"}:
        raise HTTPException(400,"Неизвестный раздел библиотеки")
    items=await library_cached(kind)
    needle=normalize_search_text(q)
    if not needle:
        return items
    out=[]
    for x in items:
        hay=normalize_search_text(" ".join([
            str(x.get("title") or ""),str(x.get("year") or ""),
            str(x.get("overview") or ""),str(x.get("path") or "")
        ]))
        if needle in hay or SequenceMatcher(None,needle,normalize_search_text(x.get("title") or "")).ratio()>=0.72:
            out.append(x)
    return out


@app.get("/api/discovery-sources")
async def discovery_sources():
    states={}
    with cache_db() as con:
        for r in con.execute("select * from source_state").fetchall():states[r["source"]]=dict(r)
    return [
        {"name":"TMDB","url":"https://www.themoviedb.org/","enabled":bool(_persistent_value("TMDB_API_KEY")),"purpose":"Русские названия, новинки и популярное фильмов/сериалов"},
        {"name":"TVmaze","url":"https://www.tvmaze.com/","enabled":True,"purpose":"Свежие и выходящие веб-сериалы; русское название сверяется через TMDB"},
        {"name":"КиноПоиск","url":"https://www.kinopoisk.ru/","enabled":bool(_persistent_value("KINOPOISK_API_KEY")),"purpose":"Премьеры фильмов; нужен KINOPOISK_API_KEY"},
        {"name":"AniLiberty","url":"https://aniliberty.top/","enabled":True,"purpose":"Discovery новых аниме"},
    ]

def source_state_view(row):
    d=dict(row)
    src=str(d.get("source") or "")
    err=str(d.get("last_error") or "")
    if d.get("ok"):
        d["health"]="ok"
    elif any(x in err.lower() for x in ("not configured","не настроен","disabled","необязательно","api key not found")):
        d["health"]="off"
    elif "лента без поискового запроса" in err.lower() or "unsupported feed" in err.lower():
        d["health"]="skip"
    elif src.startswith("TVmaze") or src.startswith("AniList") or src.startswith("AniLiberty"):
        d["health"]="warn"
    else:
        d["health"]="bad"
    return d

@app.get("/api/source-states")
async def source_states():
    with cache_db() as con:
        return [source_state_view(r) for r in con.execute(
            "select * from source_state where source not in ('AniList new','AniList popular','AniList upcoming','TMDB movies trending','TMDB tv trending','TMDB anime trending','TMDB anime upcoming') order by source"
        ).fetchall()]

def _start_cache_refresh(mode):
    unit="mediahub-local-cache.service" if mode=="local" else "mediahub-cache.service"
    arg="--local" if mode=="local" else "--full"
    try:
        chk=subprocess.run(["systemctl","cat",unit],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=3)
        if chk.returncode==0:
            subprocess.Popen(["systemctl","start",unit],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
            return
    except Exception:
        pass
    py=BASE_DIR/"venv"/"bin"/"python"
    exe=str(py if py.exists() else Path(sys.executable))
    subprocess.Popen([exe,str(BASE_DIR/"cache_refresh.py"),arg],cwd=str(BASE_DIR),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)

@app.post("/api/local-cache-refresh")
async def local_cache_refresh():
    _start_cache_refresh("local"); log_activity("cache","Локальная библиотека","Обновление запущено",True)
    return {"ok":True,"message":"Обновление локальной библиотеки запущено"}

@app.post("/api/cache-refresh")
async def cache_refresh():
    live=await refresh_tmdb_live_cache() if _persistent_value("TMDB_API_KEY") else {"ok":False,"configured":False,"total":0,"counts":{}}
    _start_cache_refresh("full")
    log_activity("cache","Внешний кэш",live.get("message") or "Фоновое обновление запущено",True)
    return {"ok":True,"message":live.get("message") or "Фоновое обновление запущено",
            "tmdb":live,"background":True}

@app.get("/api/network-health")
async def network_health():
    targets=[
        ("TMDB","https://api.themoviedb.org/3/configuration",tmdb_auth_params({}),tmdb_auth_headers()),
        ("AniList","https://graphql.anilist.co",None,{"Accept":"application/json"}),
        ("TVmaze","https://api.tvmaze.com/shows/1",None,{"Accept":"application/json"}),
        ("AniLiberty","https://aniliberty.top/",None,{"Accept":"text/html"}),
    ]
    out=[]
    async with external_async_client(12) as c:
        for name,url,params,headers in targets:
            started=datetime.now(timezone.utc)
            try:
                # AniList rejects GET at the GraphQL root with 405 on some
                # deployments; any HTTP response proves network reachability.
                r=await c.get(url,params=params,headers=headers)
                reachable=r.status_code<500
                out.append({"name":name,"ok":reachable,"status":r.status_code,
                            "ms":int((datetime.now(timezone.utc)-started).total_seconds()*1000),"error":""})
            except Exception as e:
                out.append({"name":name,"ok":False,"status":0,
                            "ms":int((datetime.now(timezone.utc)-started).total_seconds()*1000),"error":str(e)[:180]})
    return {"proxyConfigured":bool(outbound_proxy()),"targets":out}

@app.get("/api/discovery-health")
async def discovery_health():
    with cache_db() as con:
        counts={}
        for kind in ("movies","tv","anime"):
            for mode in ("new","popular"):
                row=con.execute("select count(*) as n from catalog where kind=? and mode=?",(kind,mode)).fetchone()
                counts[f"{kind}:{mode}"]=int(row["n"] or 0)
        states=[dict(r) for r in con.execute(
            "select * from source_state where source in ('TMDB movies new','TMDB movies popular','TMDB tv new','TMDB tv popular','TMDB anime new','TMDB anime popular') order by source"
        ).fetchall()]
        last=con.execute("select value from meta where key='last_refresh'").fetchone()
    return {"tmdbConfigured":bool(_persistent_value("TMDB_API_KEY")),"credentialType":tmdb_credential_type(_persistent_value("TMDB_API_KEY")),
            "counts":counts,"states":states,"lastRefresh":last["value"] if last else None}

@app.get("/api/feed")
async def feed(kind:str=Query("movies"),mode:str=Query("new"),source:str=Query("catalog"),limit:int=Query(60,ge=1,le=200)):
    with cache_db() as con:
        if source=="providers":
            order="published_at desc" if mode=="new" else "seeders desc,published_at desc"
            rows=con.execute(f"select * from provider_feed where kind=? order by {order} limit ?",(kind,limit)).fetchall()
        else:
            rows=con.execute("select * from catalog where kind=? and mode=? order by rank asc limit ?",(kind,mode,limit)).fetchall()
    if source=="providers":
        items=[]
        for row in rows:
            item=dict(row)
            payload={"title":item["title"],"guid":item["guid"],"indexerId":item["indexer_id"],"indexer":item["indexer"],
                     "downloadUrl":item.get("download_url") or "","size":item.get("size") or 0,
                     "seeders":item.get("seeders") or 0,"peers":item.get("peers") or 0,"publishDate":item.get("published_at") or "","protocol":"torrent"}
            if str(payload["downloadUrl"]).lower().startswith("magnet:"):payload["magnetUrl"]=payload["downloadUrl"]
            item["token"]=store_release(kind,payload)
            items.append(item)
        return items
    # Новинки / популярное / в тренде: разворачиваем метаданные, помечаем
    # библиотечное состояние и подтягиваем заранее найденные релизы.
    items=[row_media(r,kind) for r in rows]
    annotate_library_items(items,kind)
    annotate_cards_release_prefetch(items,kind)
    schedule_cards_release_prefetch(items,kind)
    return items

@app.get("/api/providers")
async def providers():
    if not PROWLARR_KEY:return []
    try:
        items=await get_json(PROWLARR_URL,PROWLARR_KEY,"/api/v1/indexer")
    except Exception:
        return []
    return [{"id":x.get("id"),"name":x.get("name"),"enable":x.get("enable",True),
             "protocol":x.get("protocol"),"priority":x.get("priority"),
             "supportedKinds":[k for k in CATEGORIES if indexer_supports_kind(x,k)]} for x in items]

@app.get("/api/provider-bindings")
async def provider_bindings():
    ps=await providers()
    with cache_db() as con:
        rows=con.execute("select kind,indexer_id,enabled from bindings").fetchall()
    sel={(r["kind"],int(r["indexer_id"])):bool(r["enabled"]) for r in rows}
    configured={k:any(r["kind"]==k for r in rows) for k in CATEGORIES}
    out=[]
    for x in ps:
        xid=int(x["id"]); bindings={}
        for k in CATEGORIES:
            bindings[k]=sel.get((k,xid),False) if configured[k] else (k in (x.get("supportedKinds") or []))
        out.append({**x,"bindings":bindings,"bindingMode":{k:("manual" if configured[k] else "auto") for k in CATEGORIES}})
    return out

@app.post("/api/provider-bindings")
async def save_provider_bindings(kind:str=Form(...),indexer_ids:str=Form("")):
    if kind not in CATEGORIES:raise HTTPException(400,"Неизвестная категория")
    ids={int(x) for x in indexer_ids.split(",") if x.strip().isdigit()}
    ps=await providers()
    with cache_db() as con:
        con.execute("delete from bindings where kind=?",(kind,))
        # Persist both checked and unchecked rows, so an intentionally empty
        # category does not silently fall back to every indexer.
        for x in ps:
            idx=int(x["id"]); enabled=1 if idx in ids else 0
            con.execute("insert or replace into bindings(kind,indexer_id,enabled) values(?,?,?)",(kind,idx,enabled))
        con.commit()
    return {"ok":True,"kind":kind,"indexerIds":sorted(ids)}

@app.post("/api/provider-test")
async def provider_test():
    if not PROWLARR_KEY:return {"ok":False,"message":"Prowlarr API key не найден"}
    async with httpx.AsyncClient(timeout=60,trust_env=False) as c:
        r=await c.post(PROWLARR_URL+"/api/v1/indexer/testall",headers={"X-Api-Key":PROWLARR_KEY})
    return {"ok":r.status_code<300,"status":r.status_code}


def normalize_search_text(value):
    s=(value or "").casefold().replace("ё","е")
    s=re.sub(r"[^0-9a-zа-я]+"," ",s,flags=re.I)
    return re.sub(r"\s+"," ",s).strip()

def is_cyrillic(value):
    return bool(re.search(r"[а-яё]",value or "",re.I))

def local_title_candidates(kind):
    values=[]
    with cache_db() as con:
        for table,where in [
            ("catalog","kind=?"),
            ("library_cache","kind=?"),
        ]:
            try:
                rows=con.execute(
                    f"select title from {table} where {where} and title is not null limit 1500",
                    (kind,)
                ).fetchall()
                values.extend([str(r["title"]) for r in rows if r["title"]])
            except Exception:
                pass
        if kind=="anime":
            try:
                rows=con.execute(
                    "select title from external_discovery where title is not null limit 500"
                ).fetchall()
                values.extend([str(r["title"]) for r in rows if r["title"]])
            except Exception:
                pass
    # stable unique
    seen=set(); out=[]
    for x in values:
        n=normalize_search_text(x)
        if n and n not in seen:
            seen.add(n); out.append(x)
    return out

def fuzzy_correct_local(q,kind):
    qn=normalize_search_text(q)
    if len(qn)<4:
        return None,0.0
    best=None; score=0.0
    q_tokens=set(qn.split())
    for title in local_title_candidates(kind):
        tn=normalize_search_text(title)
        if not tn:
            continue
        ratio=SequenceMatcher(None,qn,tn).ratio()
        # Word-level bonus makes "гари потер" -> "гарри поттер" much stronger.
        t_tokens=set(tn.split())
        overlap=len(q_tokens & t_tokens)/max(1,len(q_tokens|t_tokens))
        combined=max(ratio, ratio*0.86+overlap*0.14)
        if combined>score:
            best,score=title,combined
    # Avoid surprising rewrites. Short queries require higher confidence.
    threshold=0.78 if len(qn)<8 else 0.70
    if best and score>=threshold and normalize_search_text(best)!=qn:
        return best,round(score,3)
    return None,round(score,3)

def unique_terms(values):
    seen=set(); out=[]
    for v in values:
        v=(v or "").strip()
        n=normalize_search_text(v)
        if v and n and n not in seen:
            seen.add(n); out.append(v)
    return out

async def smart_search_plan(q,kind,catalog_hint=None):
    """Build a small alias set for Prowlarr without doing another catalog round-trip.

    v20.7 accidentally called this helper without defining it.  Keeping the
    plan deliberately small is also important: one title with eight aliases can
    multiply slow-indexer timeouts into many minutes.
    """
    q=(q or "").strip()
    corrected,confidence=fuzzy_correct_local(q,kind) if q else (None,0.0)
    hint=catalog_hint or {}
    terms=unique_terms([
        hint.get("localizedTitle"), hint.get("title"), hint.get("originalTitle"),
        corrected, q,
    ])
    return {"query":q,"corrected":corrected,"confidence":confidence,"terms":terms}

# --- v21.1 release relevance -------------------------------------------------
# Prowlarr returns whatever an indexer considers a text match, so a query like
# "Мятеж" (2026) used to return "Мятеж на Баунти / Mutiny on the Bounty (1962)".
# Every release is now compared against the card's titles and year before it is
# shown, and an obviously different film is dropped instead of being ranked.

RELEASE_NOISE_TOKENS={
    "bdrip","bdremux","bluray","blu","ray","brrip","webrip","web","dl","dlrip","webdl",
    "hdrip","dvdrip","dvdscr","dvd","remux","avc","hevc","x264","x265","h264","h265",
    "xvid","divx","1080p","1080i","720p","2160p","480p","576p","4k","uhd","hdr","hdr10",
    "dv","sdr","10bit","8bit","proper","repack","rerip","extended","unrated","imax",
    "ac3","eac3","dts","dtshd","hd","ma","aac","mp3","flac","opus","atmos","truehd",
    "rus","eng","ukr","jap","multi","dub","dubbed","dubbing","sub","subs","subbed",
    "voice","licence","license","lic","p","d","l","hdtv","hdtvrip","satrip","tvrip",
    "season","seasons","complete","full","pack","сезон","сезоны","сезона","серия",
    "серии","серий","эпизод","эпизоды","весь","все","озвучка","дубляж","многоголосый",
    "многоголосая","профессиональный","любительский","авторский","лицензия",
}

def _release_title_chunks(title):
    """Split a release title into candidate media names.

    Trackers write "Русское имя / Original Name (Год) BDRip 1080p", sometimes
    with several dubbing groups in brackets. Each slash-separated part is a
    candidate name; technical noise, brackets and years are removed.
    """
    raw=str(title or "")
    raw=re.sub(r"[\[\(\{][^\]\)\}]*[\]\)\}]"," ",raw)
    raw=re.sub(r"\b(19|20)\d{2}\b"," ",raw)
    raw=re.sub(r"\bs\d{1,3}(\s*-\s*s?\d{1,3})?(\s*e\d{1,4})?\b"," ",raw,flags=re.I)
    raw=re.sub(r"\bсезон[ыа]?\s*\d{1,3}(\s*-\s*\d{1,3})?\b"," ",raw,flags=re.I)
    raw=re.sub(r"\b\d{1,3}\s*сезон[ыа]?\b"," ",raw,flags=re.I)
    chunks=[]
    for part in re.split(r"[/|]",raw):
        tokens=[t for t in normalize_search_text(part).split() if t and t not in RELEASE_NOISE_TOKENS]
        if tokens:
            chunks.append(set(tokens))
    if not chunks:
        tokens=[t for t in normalize_search_text(raw).split() if t and t not in RELEASE_NOISE_TOKENS]
        if tokens: chunks.append(set(tokens))
    return chunks

def _release_years(title):
    return [int(y) for y in re.findall(r"\b((?:19|20)\d{2})\b",str(title or ""))]

def _expected_title_sets(card):
    out=[]
    for key in ("localizedTitle","title","originalTitle","libraryTitle","name","originalName"):
        value=(card or {}).get(key)
        tokens={t for t in normalize_search_text(value).split() if t and t not in RELEASE_NOISE_TOKENS}
        if tokens and tokens not in out:
            out.append(tokens)
    return out

def _name_similarity(expected,chunk):
    if not expected or not chunk:
        return 0.0
    inter=expected & chunk
    coverage=len(inter)/len(expected)
    precision=len(inter)/len(chunk)
    ratio=SequenceMatcher(None," ".join(sorted(expected))," ".join(sorted(chunk))).ratio()
    return round(coverage*0.6+precision*0.25+ratio*0.15,4)

def release_relevance(release_title,expected_sets,expected_year,kind="movies"):
    """Return {ok, score, precision, reason} for one release title."""
    if not expected_sets:
        return {"ok":True,"score":0.5,"reason":""}
    chunks=_release_title_chunks(release_title)
    if not chunks:
        return {"ok":False,"score":0.0,"reason":"пустое название релиза"}

    best=0.0; best_precision=0.0
    for expected in expected_sets:
        for chunk in chunks:
            score=_name_similarity(expected,chunk)
            if score>best:
                best=score
                best_precision=len(expected & chunk)/len(chunk)

    # A one or two word title ("Мятеж") matches inside many longer names, so it
    # additionally has to cover most of the release name itself.
    shortest=min(len(x) for x in expected_sets)
    min_precision=0.4 if shortest<=2 else 0.25
    if best_precision<min_precision:
        return {"ok":False,"score":best,"reason":"название релиза шире искомого"}
    if best<0.62:
        return {"ok":False,"score":best,"reason":"название не совпадает"}

    years=_release_years(release_title)
    try: want=int(str(expected_year)[:4])
    except Exception: want=0
    if want and years:
        tolerance=1 if kind=="movies" else 3
        if min(abs(y-want) for y in years)>tolerance:
            return {"ok":False,"score":best,"reason":f"год релиза {years[0]} вместо {want}"}
    return {"ok":True,"score":best,"reason":""}

RELEASE_PREFETCH_TASKS={}
RELEASE_PREFETCH_TTL=timedelta(hours=12)
RELEASE_PREFETCH_RETRY=timedelta(minutes=20)
_RELEASE_PREFETCH_SEM=None

def _release_prefetch_sem():
    global _RELEASE_PREFETCH_SEM
    if _RELEASE_PREFETCH_SEM is None:
        # Открытая карточка не должна ждать освобождения слота: фоновые задачи
        # отменяются при переключении, поэтому трёх слотов достаточно.
        _RELEASE_PREFETCH_SEM=asyncio.Semaphore(3)
    return _RELEASE_PREFETCH_SEM

def release_prefetch_key(kind,catalog,external_id,title):
    stable=f"{kind}|{catalog or ''}|{external_id or ''}|{normalize_search_text(title or '')}"
    return hashlib.sha1(stable.encode('utf-8',errors='ignore')).hexdigest()

def _release_prefetch_row(kind,catalog,external_id,title):
    key=release_prefetch_key(kind,catalog,external_id,title)
    with cache_db() as con:
        row=con.execute("select * from release_prefetch where cache_key=?",(key,)).fetchone()
        if not row and title:
            # Карточка могла открываться с другим catalog/externalId (с полки —
            # tmdb, из библиотеки — radarr). Ищем прошлый поиск по названию,
            # чтобы не искать заново то, что уже найдено.
            needle=normalize_search_text(title)
            for candidate in con.execute(
                """select * from release_prefetch where kind=? and result_count>0
                   order by updated_at desc limit 40""",(kind,)).fetchall():
                if normalize_search_text(candidate["title"])==needle:
                    row=candidate; break
    return key,(dict(row) if row else None)

def _prefetch_age(row):
    if not row or not row.get("updated_at"):
        return None
    try:
        dt=datetime.fromisoformat(row["updated_at"])
        if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc)-dt
    except Exception:
        return None

def _prefetch_items(row):
    if not row or not row.get("result_json"):
        return []
    try:
        data=json.loads(row["result_json"])
        return data if isinstance(data,list) else []
    except Exception:
        return []

def _save_prefetch_state(key,card,status,items=None,error=""):
    items=items if isinstance(items,list) else []
    with cache_db() as con:
        con.execute(
            """insert or replace into release_prefetch
               (cache_key,kind,catalog,external_id,title,status,result_json,result_count,updated_at,last_error)
               values(?,?,?,?,?,?,?,?,?,?)""",
            (key,card.get("kind") or "movies",card.get("catalog") or card.get("catalog_source") or "",
             str(card.get("externalId") or card.get("external_id") or ""),card.get("title") or "",
             status,json.dumps(items,ensure_ascii=False),len(items),datetime.now(timezone.utc).isoformat(),error or "")
        )
        con.commit()

async def _release_prefetch_worker(card,force=False):
    kind=card.get("kind") or "movies"
    catalog=card.get("catalog") or card.get("catalog_source") or ""
    ext=str(card.get("externalId") or card.get("external_id") or "")
    title=(card.get("title") or "").strip()
    if not title or kind not in {"movies","tv","anime"}:
        return
    key,row=_release_prefetch_row(kind,catalog,ext,title)
    age=_prefetch_age(row)
    if not force and row:
        if row.get("status")=="ready" and age is not None and age<RELEASE_PREFETCH_TTL:
            return
        if row.get("status")=="searching" and age is not None and age<timedelta(minutes=3):
            return
        if row.get("status")=="error" and age is not None and age<RELEASE_PREFETCH_RETRY:
            return
    _save_prefetch_state(key,card,"searching",_prefetch_items(row),"")
    async with _release_prefetch_sem():
        def on_partial(partial):
            # Первые найденные релизы сразу уходят в кэш: карточка показывает их,
            # не дожидаясь самого медленного индексера.
            if partial:
                _save_prefetch_state(key,card,"searching",partial,"")
        try:
            # Background work favours speed over exhaustive alias hunting. Two
            # aliases are normally enough for RU + original title and keep a
            # dead tracker from monopolising a worker for minutes.
            items=await asyncio.wait_for(
                provider_search_live(title,kind,catalog_hint=card,max_terms=2,max_seconds=24.0,
                                     on_partial=on_partial),
                timeout=28.0
            )
            _save_prefetch_state(key,card,"ready",items,"")
        except asyncio.CancelledError:
            # Карточку закрыли и открыли другую: сохраняем, что успели найти,
            # и возвращаем задачу в очередь на простой.
            _save_prefetch_state(key,card,"queued",_prefetch_items(_release_prefetch_row(
                card.get("kind") or "movies",card.get("catalog") or "",
                str(card.get("externalId") or ""),title)[1]),"")
            raise
        except Exception as e:
            # Preserve previous useful results even when a refresh fails.
            old=_prefetch_items(row)
            _save_prefetch_state(key,card,"ready" if old else "error",old,str(e)[-500:])

# --- v21.18 приоритет открытой карточки -------------------------------------
# Индексеров мало, и они медленные. Пока карточка открыта, весь поиск идёт по
# ней; поиск по другим карточкам ставится в очередь и разбирается в простое,
# когда пользователь ничего не смотрит.

ACTIVE_RELEASE_KEY=None
ACTIVE_RELEASE_AT=None
RELEASE_IDLE_AFTER=timedelta(seconds=25)
_PREFETCH_IDLE_TASK=None


def set_active_release(key):
    """Отметить карточку, которую пользователь смотрит прямо сейчас."""
    global ACTIVE_RELEASE_KEY,ACTIVE_RELEASE_AT
    switched=bool(ACTIVE_RELEASE_KEY and key and ACTIVE_RELEASE_KEY!=key)
    ACTIVE_RELEASE_KEY=key
    ACTIVE_RELEASE_AT=datetime.now(timezone.utc)
    if switched:
        cancel_foreign_prefetch(key)


def release_idle():
    """Нет открытой карточки дольше RELEASE_IDLE_AFTER."""
    if not ACTIVE_RELEASE_AT:
        return True
    return (datetime.now(timezone.utc)-ACTIVE_RELEASE_AT)>RELEASE_IDLE_AFTER


def cancel_foreign_prefetch(keep_key):
    """Освободить индексеры для новой карточки: чужие поиски отменяем.

    Отменённая работа не теряется — строка остаётся в release_prefetch со
    статусом queued и будет доделана в простое.
    """
    for key,task in list(RELEASE_PREFETCH_TASKS.items()):
        if key==keep_key or task.done():
            continue
        task.cancel()
        RELEASE_PREFETCH_TASKS.pop(key,None)
        try:
            with cache_db() as con:
                con.execute("""update release_prefetch set status='queued'
                               where cache_key=? and status='searching'""",(key,))
                con.commit()
        except Exception:
            pass


def schedule_release_prefetch(card,force=False,priority=False):
    if not PROWLARR_KEY:
        return None
    kind=card.get("kind") or "movies"
    catalog=card.get("catalog") or card.get("catalog_source") or ""
    ext=str(card.get("externalId") or card.get("external_id") or "")
    title=(card.get("title") or "").strip()
    if not title or kind not in {"movies","tv","anime"}:
        return None
    key=release_prefetch_key(kind,catalog,ext,title)
    task=RELEASE_PREFETCH_TASKS.get(key)
    if task and not task.done():
        return task
    if not priority and not release_idle() and key!=ACTIVE_RELEASE_KEY:
        # Пользователь смотрит другую карточку — ждём простоя.
        _queue_prefetch(key,card)
        return None
    task=asyncio.create_task(_release_prefetch_worker(dict(card),force=force))
    RELEASE_PREFETCH_TASKS[key]=task
    def _done(_): RELEASE_PREFETCH_TASKS.pop(key,None)
    task.add_done_callback(_done)
    return task


def _queue_prefetch(key,card):
    try:
        with cache_db() as con:
            row=con.execute("select status from release_prefetch where cache_key=?",(key,)).fetchone()
            if row and row["status"] in {"ready","searching"}:
                return
            con.execute("""insert or replace into release_prefetch
                (cache_key,kind,catalog,external_id,title,status,result_json,result_count,updated_at,last_error)
                values(?,?,?,?,?,'queued',coalesce((select result_json from release_prefetch where cache_key=?),'[]'),
                       coalesce((select result_count from release_prefetch where cache_key=?),0),?,'')""",
                (key,card.get("kind") or "movies",card.get("catalog") or card.get("catalog_source") or "",
                 str(card.get("externalId") or card.get("external_id") or ""),card.get("title") or "",
                 key,key,datetime.now(timezone.utc).isoformat()))
            con.commit()
    except Exception:
        pass


async def _prefetch_idle_loop():
    """В простое доделываем отложенные поиски по одному."""
    await asyncio.sleep(60)
    last_cache_check=datetime.now(timezone.utc)
    while True:
        try:
            # Раз в шесть часов проверяем, что кэш не перерос лимит.
            if (datetime.now(timezone.utc)-last_cache_check)>timedelta(hours=6):
                last_cache_check=datetime.now(timezone.utc)
                await asyncio.get_running_loop().run_in_executor(None,enforce_cache_limit,False)
            # В простое проверяем карточки лент: есть ли на странице торрент.
            if release_idle():
                await feed_verify_batch(5)
        except Exception:
            pass
        try:
            if release_idle() and PROWLARR_KEY:
                with cache_db() as con:
                    rows=con.execute("""select * from release_prefetch
                                        where status='queued' order by updated_at limit 2""").fetchall()
                for row in rows:
                    if not release_idle():
                        break
                    card={"kind":row["kind"],"catalog":row["catalog"],
                          "externalId":row["external_id"],"title":row["title"]}
                    task=schedule_release_prefetch(card,priority=False)
                    if task:
                        try:
                            await asyncio.wait_for(asyncio.shield(task),timeout=90)
                        except Exception:
                            pass
        except Exception:
            pass
        await asyncio.sleep(30)


@app.on_event("startup")
async def _start_prefetch_idle_loop():
    global _PREFETCH_IDLE_TASK
    if _PREFETCH_IDLE_TASK is None or _PREFETCH_IDLE_TASK.done():
        _PREFETCH_IDLE_TASK=asyncio.create_task(_prefetch_idle_loop())

def schedule_home_release_prefetch(sections):
    """Queue New/Popular home cards without delaying /api/home.

    Queue order mirrors the screen, so the first visible cards are searched
    first.  SQLite status prevents every browser refresh from duplicating work.
    """
    queued=0
    for section in sections or []:
        sid=str(section.get("id") or "")
        if sid not in {"movies-new","movies-popular","tv-new","tv-popular","anime-new","anime-popular"}:
            continue
        for card in (section.get("items") or [])[:20]:
            c=dict(card); c["kind"]=c.get("kind") or sid.split("-",1)[0]
            schedule_release_prefetch(c)
            queued+=1
    return queued

def annotate_cards_release_prefetch(cards,kind):
    """Показать на карточках, сколько релизов уже найдено заранее."""
    if not cards:
        return cards
    try:
        with cache_db() as con:
            for card in cards:
                k=card.get("kind") or kind
                key=release_prefetch_key(k,card.get("catalog") or card.get("catalog_source") or "",
                                         card.get("externalId") or card.get("external_id") or "",
                                         card.get("title") or "")
                row=con.execute("select status,result_count from release_prefetch where cache_key=?",(key,)).fetchone()
                if not row:
                    # Тот же тайтл мог искаться из другого раздела: ключ включает
                    # источник карточки, поэтому пробуем ещё и по названию.
                    needle=normalize_search_text(card.get("title") or "")
                    if needle:
                        for cand in con.execute(
                            """select title,status,result_count from release_prefetch
                               where kind=? and result_count>0 order by updated_at desc limit 30""",(k,)).fetchall():
                            if normalize_search_text(cand["title"])==needle:
                                row=cand; break
                if row:
                    card["releasePrefetchStatus"]=row["status"]
                    card["releasePrefetchCount"]=int(row["result_count"] or 0)
    except Exception:
        pass
    return cards


def schedule_cards_release_prefetch(cards,kind,limit=8):
    """Поставить поиск релизов для показанных карточек в фоновую очередь.

    Приоритет не повышаем: очередь разбирается в простое, поэтому открытая
    карточка всегда ищется первой.
    """
    if not PROWLARR_KEY:
        return 0
    queued=0
    for card in cards or []:
        if queued>=limit:
            break
        title=(card.get("title") or "").strip()
        if not title:
            continue
        if int(card.get("releasePrefetchCount") or 0)>0:
            continue
        payload={"kind":card.get("kind") or kind,
                 "catalog":card.get("catalog") or card.get("catalog_source") or "",
                 "externalId":card.get("externalId") or card.get("external_id") or "",
                 "title":title,"year":card.get("year") or "",
                 "originalTitle":card.get("originalTitle") or ""}
        try:
            schedule_release_prefetch(payload)
            queued+=1
        except Exception:
            break
    return queued


def annotate_home_release_prefetch(sections):
    targets={"movies-new","movies-popular","tv-new","tv-popular","anime-new","anime-popular"}
    try:
        with cache_db() as con:
            for section in sections or []:
                sid=str(section.get("id") or "")
                if sid not in targets: continue
                default_kind=sid.split("-",1)[0]
                for card in section.get("items") or []:
                    kind=card.get("kind") or default_kind
                    key=release_prefetch_key(kind,card.get("catalog") or card.get("catalog_source") or "",
                                             card.get("externalId") or card.get("external_id") or "",card.get("title") or "")
                    row=con.execute("select status,result_count,updated_at from release_prefetch where cache_key=?",(key,)).fetchone()
                    if row:
                        card["releasePrefetchStatus"]=row["status"]
                        card["releasePrefetchCount"]=int(row["result_count"] or 0)
    except Exception:
        pass

async def tmdb_search_bilingual(q,kind):
    """TMDB search in Russian and English through the resilient external client."""
    if not TMDB_KEY or kind=="games":
        return []
    media="movie" if kind=="movies" else "tv"
    results={}
    async with external_async_client(22) as c:
        for language in ("ru-RU","en-US"):
            try:
                r=await external_request(
                    c,"GET",f"https://api.themoviedb.org/3/search/{media}",
                    params=tmdb_auth_params({"language":language,"query":q,"include_adult":"false","page":1}),
                    headers=tmdb_auth_headers(),retries=3
                )
                results[language]=(r.json() or {}).get("results",[])
            except Exception:
                results[language]=[]
    ru=results.get("ru-RU",[]); en=results.get("en-US",[])
    merged=[]; by_id={}
    for source_lang,rows in (("ru-RU",ru),("en-US",en)):
        for x in rows:
            mid=x.get("id")
            if not mid:continue
            title=x.get("title") or x.get("name") or ""
            original=x.get("original_title") or x.get("original_name") or ""
            if mid not in by_id:
                item={
                    "title":title,"localizedTitle":title if source_lang=="ru-RU" else "",
                    "originalTitle":original,
                    "year":(x.get("release_date") or x.get("first_air_date") or "")[:4],
                    "overview":x.get("overview") or "",
                    "poster":"https://image.tmdb.org/t/p/w500"+x["poster_path"] if x.get("poster_path") else None,
                    "externalId":mid,"catalog":"tmdb","genres":[],"rating":x.get("vote_average"),
                    "runtime":0,"status":"","studio":"","network":"",
                }
                by_id[mid]=item;merged.append(item)
            elif source_lang=="ru-RU":
                item=by_id[mid]
                if title:item["title"]=title;item["localizedTitle"]=title
                if x.get("overview"):item["overview"]=x.get("overview")
    return merged[:40]

async def catalog_search_live(q,kind):
    q=(q or "").strip()
    if not q:
        return []

    corrected,_=fuzzy_correct_local(q,kind)
    queries=unique_terms([q,corrected])

    for query in queries:
        # Anime: AniList gives rich anime metadata, TMDB gives Russian
        # localization. Run both and put localized cards first for Cyrillic
        # user queries.
        if kind=="anime":
            ani_task=asyncio.create_task(anilist_search(query))
            tmdb_task=asyncio.create_task(tmdb_search_bilingual(query,"anime"))
            ani,tm=await asyncio.gather(ani_task,tmdb_task)
            if tm or ani:
                rows=(tm+ani) if is_cyrillic(query) else (ani+tm)
                seen=set(); out=[]
                for x in rows:
                    key=(normalize_search_text(x.get("title") or ""),
                         str(x.get("year") or ""))
                    if key in seen:
                        continue
                    seen.add(key); out.append(x)
                return out[:40]

        if kind=="movies":
            rad=[]
            try:
                items=await get_json(RADARR_URL,RADARR_KEY,"/api/v3/movie/lookup",{"term":query})
                rad=[{
                    "title":x.get("title"),
                    "localizedTitle":x.get("title") or "",
                    "originalTitle":x.get("originalTitle") or "",
                    "year":x.get("year"),
                    "overview":x.get("overview") or "",
                    "poster":next((i.get("remoteUrl") for i in x.get("images",[]) if i.get("coverType")=="poster"),None),
                    "externalId":x.get("tmdbId"),"catalog":"radarr",
                    "genres":x.get("genres") or [],"runtime":x.get("runtime") or 0,
                    "rating":((x.get("ratings") or {}).get("value")
                              or ((x.get("ratings") or {}).get("imdb") or {}).get("value")
                              or ((x.get("ratings") or {}).get("tmdb") or {}).get("value")),
                    "status":x.get("status") or "","studio":x.get("studio") or "",
                    "network":"","certification":x.get("certification") or "",
                } for x in items[:40]]
            except Exception:
                pass
            tm=await tmdb_search_bilingual(query,"movies")
            if rad or tm:
                # Cyrillic search should show localized TMDB names first.
                return (tm+rad)[:40] if is_cyrillic(query) else (rad+tm)[:40]

        elif kind=="tv":
            son=[]
            try:
                items=await get_json(SONARR_URL,SONARR_KEY,"/api/v3/series/lookup",{"term":query})
                son=[{
                    "title":x.get("title"),
                    "localizedTitle":x.get("title") or "",
                    "originalTitle":x.get("originalTitle") or "",
                    "year":x.get("year"),
                    "overview":x.get("overview") or "",
                    "poster":next((i.get("remoteUrl") for i in x.get("images",[]) if i.get("coverType")=="poster"),None),
                    "externalId":x.get("tvdbId"),"catalog":"sonarr",
                    "genres":x.get("genres") or [],"runtime":x.get("runtime") or 0,
                    "rating":((x.get("ratings") or {}).get("value")
                              or ((x.get("ratings") or {}).get("imdb") or {}).get("value")
                              or ((x.get("ratings") or {}).get("tvdb") or {}).get("value")),
                    "status":x.get("status") or "","network":x.get("network") or "",
                    "studio":x.get("network") or "",
                    "seasonCount":len(x.get("seasons") or []),
                } for x in items[:40]]
            except Exception:
                pass
            tm=await tmdb_search_bilingual(query,"tv")
            if son or tm:
                return (tm+son)[:40] if is_cyrillic(query) else (son+tm)[:40]

    return []

def _format_release_items(all_rels,kind,catalog_hint,q,used_terms):
    """Отфильтровать по релевантности, оценить и превратить в карточки релизов."""
    hint=catalog_hint or {}
    expected_sets=_expected_title_sets(hint)
    if not expected_sets:
        tokens={t for t in normalize_search_text(q).split() if t and t not in RELEASE_NOISE_TOKENS}
        if tokens: expected_sets=[tokens]
    expected_year=hint.get("year") or hint.get("releaseYear") or ""
    strict=get_setting("strict_release_match","1")=="1"
    skip_unplayable=skip_unplayable_releases()
    kept=[]; rejected=[]; unplayable=[]
    for x in all_rels:
        rel=release_relevance(x.get("title"),expected_sets,expected_year,kind)
        if skip_unplayable and not release_playback(x.get("title"))["playable"]:
            # Такое скачается, но не заиграет — в списке ему не место.
            unplayable.append((rel,x)); continue
        if strict and not rel["ok"]:
            rejected.append((rel,x))
        else:
            kept.append((rel,x))
    fallback=False
    if not kept and unplayable:
        # Совсем пустой список хуже честного предупреждения: показываем образы
        # дисков с пометкой, скачать их можно только осознанно. Подходящий
        # тайтл в неудобном формате полезнее, чем чужой фильм в удобном.
        good=[pair for pair in unplayable if pair[0].get("ok")]
        kept=good or ([] if rejected else unplayable)
    if not kept and rejected:
        # Never show an empty list when something plausible exists: show the
        # closest matches, flagged, so the user can decide.
        rejected.sort(key=lambda pair:pair[0]["score"],reverse=True)
        kept=[({"ok":False,"score":r["score"],"reason":r["reason"]},x) for r,x in rejected[:12]]
        fallback=True

    scored=[]
    for rel,x in kept:
        smart=release_smart_score(x.get("title"),x.get("seeders"),x.get("size"),kind)
        scored.append((smart,x,rel))
    scored.sort(key=lambda t:(t[2]["ok"],t[0]["score"],int(t[1].get("seeders") or 0),str(t[1].get("publishDate") or "")),reverse=True)

    out=[]
    for smart,x,rel in scored[:120]:
        play=release_playback(x.get("title"))
        out.append({
            "token":store_release(kind,x),
            "title":x.get("title"),
            "format":play["format"],
            "playable":play["playable"],
            "playWarning":play["reason"],
            "indexer":x.get("indexer"),
            "indexerId":x.get("indexerId"),
            "size":x.get("size",0),
            "sizeHuman":human(x.get("size",0)),
            "seeders":x.get("seeders"),"peers":x.get("peers"),
            "publishDate":x.get("publishDate"),"protocol":x.get("protocol"),
            "quality":release_quality(x.get("title")),
            "smartScore":smart["score"],"smartLabel":smart["label"],
            "smartReasons":smart["reasons"],"smartWarnings":smart["warnings"],
            "smartTags":smart["tags"],"russianAudio":smart["russian"],
            "matchedBy":used_terms[0] if used_terms else "",
            "searchTerms":used_terms,
            "relevance":round(float(rel.get("score") or 0),3),
            "relevanceOk":bool(rel.get("ok")),
            "relevanceReason":rel.get("reason") or "",
            "uncertainMatch":bool(fallback or not rel.get("ok")),
            **_release_structure_fields(x.get("title")),
        })
    return out


async def provider_search_live(q,kind,provider=None,catalog_hint=None,max_terms=4,max_seconds=35.0,on_partial=None):
    ids=[provider] if provider else bound_indexers(kind)
    plan=await smart_search_plan(q,kind,catalog_hint)
    terms=plan["terms"]

    # If caller did not provide a media card, cheaply resolve one so provider
    # search still gets Russian + original aliases.
    if not catalog_hint:
        try:
            cards=await catalog_search_live(plan["corrected"] or q,kind)
        except Exception:
            cards=[]
        if cards:
            hint=cards[0]
            for k in ("localizedTitle","title","originalTitle"):
                terms.append(hint.get(k))
            # Anime AniList usually supplies English/Romaji while TMDB often
            # supplies the Russian localized name. Try both cards.
            for x in cards[:4]:
                for k in ("localizedTitle","title","originalTitle"):
                    terms.append(x.get(k))

    terms=unique_terms(terms)[:max(1,int(max_terms))]

    all_rels=[]; seen=set(); used_terms=[]
    loop=asyncio.get_running_loop(); deadline=loop.time()+max(5.0,float(max_seconds))

    def absorb(term,rels):
        added=0
        if rels:
            used_terms.append(term)
        for x in rels or []:
            rkey=str(x.get("guid") or x.get("downloadUrl") or x.get("title") or "")
            if not rkey or rkey in seen:
                continue
            seen.add(rkey); all_rels.append(x); added+=1
        return added

    # Первые два алиаса ищем параллельно: результат появляется примерно за время
    # одного запроса, а не двух подряд.
    head=terms[:2]; tail=terms[2:]
    if head:
        remaining=max(1.0,deadline-loop.time())
        tasks=[asyncio.create_task(prowlarr_search(t,kind,ids,70)) for t in head]
        done,pending=await asyncio.wait(tasks,timeout=min(14.0,remaining))
        for t,task in zip(head,tasks):
            if task in done:
                try:absorb(t,task.result())
                except Exception:pass
            else:
                task.cancel()
        if all_rels and callable(on_partial):
            # Отдаём то, что уже нашли, не дожидаясь остальных алиасов.
            try:on_partial(_format_release_items(all_rels,kind,catalog_hint,q,used_terms))
            except Exception:pass

    for term in tail:
        remaining=deadline-loop.time()
        if remaining<=0.2 or len(all_rels)>=80:
            break
        try:
            rels=await asyncio.wait_for(prowlarr_search(term,kind,ids,70),timeout=min(14.0,remaining))
        except Exception:
            rels=[]
        absorb(term,rels)

    return _format_release_items(all_rels,kind,catalog_hint,q,used_terms)


@app.post("/api/releases/prefetch-home")
async def prefetch_home_releases():
    cards=[]
    with cache_db() as con:
        for kind,mode in (("movies","new"),("movies","popular"),("tv","new"),("tv","popular"),("anime","new"),("anime","popular")):
            rows=con.execute(
                "select * from catalog where kind=? and mode=? order by rank limit 20",(kind,mode)
            ).fetchall()
            for r in rows:
                x=row_media(r,kind); x["kind"]=kind; cards.append(x)
    for card in cards:
        schedule_release_prefetch(card)
    return {"ok":True,"queued":len(cards),"running":sum(1 for t in RELEASE_PREFETCH_TASKS.values() if not t.done())}

@app.get("/api/releases/availability")
async def release_availability(
    kind:str=Query("movies"), catalog:str=Query(""), external_id:str=Query(""),
    title:str=Query(""), refresh:bool=Query(False),
    year:str=Query(""), original_title:str=Query("")
):
    if kind not in {"movies","tv","anime"}:
        raise HTTPException(400,"Неизвестный тип медиа")
    title=(title or "").strip()
    if not title:
        return {"status":"error","items":[],"message":"Нет названия для поиска"}
    key,row=_release_prefetch_row(kind,catalog,external_id,title)
    age=_prefetch_age(row)
    items=_prefetch_items(row)
    fresh=bool(row and row.get("status")=="ready" and age is not None and age<RELEASE_PREFETCH_TTL)
    card={"kind":kind,"catalog":catalog,"externalId":external_id,"title":title,
          "year":(year or "").strip(),"originalTitle":(original_title or "").strip()}
    # Эта карточка сейчас открыта: её поиск получает приоритет, чужие фоновые
    # поиски отменяются и доделываются позже, в простое.
    set_active_release(key)
    if refresh or not fresh:
        schedule_release_prefetch(card,force=bool(refresh),priority=True)
    if items:
        # Пока поиск идёт, отдаём частичный список и честный статус, чтобы
        # интерфейс показал первые релизы и продолжил ждать остальные.
        row_status=(row or {}).get("status") or "ready"
        searching=row_status in {"searching","queued"}
        return {
            "status":"searching" if searching else "ready",
            "items":items,"count":len(items),"cached":True,
            "stale":not fresh,"updatedAt":row.get("updated_at") if row else None,
            "message":("Показываем первые находки, ищем дальше" if searching
                       else ("Релизы найдены заранее" if fresh else "Показываем кэш, обновляем в фоне"))
        }
    status=(row or {}).get("status") or "queued"
    return {
        "status":status if status in {"queued","searching","error"} else "queued",
        "items":[],"count":0,"cached":False,
        "updatedAt":(row or {}).get("updated_at"),
        "message":"Фоновый поиск уже идёт" if status=="searching" else "Поставлено в фоновый поиск",
        "error":(row or {}).get("last_error") or ""
    }

@app.get("/api/releases/prefetch-status")
async def release_prefetch_status():
    with cache_db() as con:
        rows=con.execute(
            """select status,count(*) as n from release_prefetch group by status order by status"""
        ).fetchall()
        recent=con.execute(
            """select kind,title,status,result_count,updated_at,last_error from release_prefetch
               order by updated_at desc limit 20"""
        ).fetchall()
    return {"counts":{r["status"]:r["n"] for r in rows},"recent":[dict(r) for r in recent],
            "running":sum(1 for t in RELEASE_PREFETCH_TASKS.values() if not t.done())}

@app.get("/api/search/info")
async def search_info(q:str,kind:str=Query("movies")):
    corrected,confidence=fuzzy_correct_local(q,kind)
    return {
        "query":q,
        "corrected":corrected,
        "confidence":confidence,
        "message":f"Исправили запрос: {corrected}" if corrected else ""
    }

@app.get("/api/search/catalog")
async def search_catalog(q:str,kind:str=Query("movies")):
    items=await catalog_search_live(q,kind)
    annotate_library_items(items,kind)
    annotate_cards_release_prefetch(items,kind)
    schedule_cards_release_prefetch(items,kind,limit=5)
    return items

@app.get("/api/search/providers")
async def search_providers(q:str,kind:str=Query("movies"),provider:int|None=None):
    return await provider_search_live(q,kind,provider)

@app.get("/api/unified-search")
async def unified_search(q:str,kind:str=Query("movies"),provider:int|None=None):
    q=q.strip()
    if not q:
        return {"catalog":[],"providers":[]}

    catalog_task=asyncio.create_task(catalog_search_live(q,kind))
    provider_task=asyncio.create_task(provider_search_live(q,kind,provider))
    catalog,providers=await asyncio.gather(catalog_task,provider_task)
    annotate_library_items(catalog,kind)
    if not providers and catalog:
        providers=await provider_search_live(q,kind,provider,catalog[0])
    return {"catalog":catalog,"providers":providers}


async def qbit_set_category_by_title(title, category):
    """Prowlarr performs the grab. Afterwards, if qBittorrent credentials are
    configured, move the newly-added torrent into the MediaHub category."""
    if not QBIT_USER or not QBIT_PASS or category not in {"movies","tv","anime","manual"}:
        return
    target=(title or "").strip().lower()
    if not target:
        return
    for _ in range(12):
        await asyncio.sleep(1)
        c=await qbit_login()
        if not c:
            return
        try:
            r=await c.get(QBIT_URL+"/api/v2/torrents/info")
            items=r.json()
            best=None
            for x in items:
                name=(x.get("name") or "").lower()
                if name==target or target in name or name in target:
                    best=x
                    break
            if best:
                await c.post(QBIT_URL+"/api/v2/torrents/setCategory",
                             data={"hashes":best.get("hash"),"category":category})
                return
        finally:
            await c.aclose()




def _json_dict(raw):
    try:
        data=json.loads(raw or "{}")
        return data if isinstance(data,dict) else {}
    except Exception:
        return {}


def _library_rows(kind):
    try:
        with cache_db() as con:
            return [dict(r) for r in con.execute("select * from library_cache where kind=?",(kind,)).fetchall()]
    except Exception:
        return []


def _library_provider_ids(row):
    extra=_json_dict((row or {}).get("extra_json"))
    ids=extra.get("providerIds") or {}
    return {str(k).casefold():str(v) for k,v in ids.items() if v not in (None,"")}


def _library_match_from_rows(kind, rows, external_id="", catalog="", title="", year=""):
    ext=str(external_id or "").strip(); cat=(catalog or "").strip().casefold()
    want_title=normalize_search_text(title or ""); want_year=str(year or "").strip()
    for row in rows or []:
        rid=str(row.get("external_id") or "").strip(); ids=_library_provider_ids(row)
        if ext:
            if kind=="movies" and cat in {"tmdb","radarr"} and (rid==ext or ids.get("tmdb")==ext): return row
            if kind in {"tv","anime"}:
                if cat=="sonarr" and (rid==ext or ids.get("tvdb")==ext): return row
                if cat=="tmdb" and ids.get("tmdb")==ext: return row
                if cat=="anilist" and ids.get("anilist")==ext: return row
    if want_title:
        best=None; best_score=0.0
        for row in rows or []:
            rt=normalize_search_text(row.get("title") or "")
            if not rt: continue
            score=1.0 if rt==want_title else SequenceMatcher(None,rt,want_title).ratio()
            ry=str(row.get("year") or "")
            if want_year and ry and want_year!=ry: score-=0.12
            if score>best_score: best,best_score=row,score
        if best and best_score>=0.83:return best
    return None


def _library_match(kind, external_id="", catalog="", title="", year=""):
    return _library_match_from_rows(kind,_library_rows(kind),external_id,catalog,title,year)


def _library_state_from_rows(kind,card,rows):
    row=_library_match_from_rows(kind,rows,card.get("externalId") or card.get("external_id"),card.get("catalog") or card.get("catalog_source"),card.get("title"),card.get("year"))
    if not row:return {"inLibrary":False,"tracked":False,"hasFile":False}
    extra=_json_dict(row.get("extra_json"));has_file=bool(row.get("has_file"))
    progress=season_progress(extra) if kind in {"tv","anime"} else {"episodes":0,"episodesTotal":0,"seasons":0,"seasonsComplete":0,"seasonsPartial":0}
    status,label=library_status_label(kind,has_file,progress,0,True)
    return {"inLibrary":status in {"complete","partial"},"tracked":True,"hasFile":has_file,
            "libraryStatus":status,"libraryLabel":label,
            "libraryFiles":progress.get("episodes",0),"libraryTotal":progress.get("episodesTotal",0),
            "librarySeasons":progress.get("seasons",0),
            "librarySeasonsComplete":progress.get("seasonsComplete",0),
            "path":row.get("path") or "","librarySource":row.get("catalog_source") or extra.get("catalog") or "filesystem",
            "libraryTitle":row.get("title") or card.get("title") or "","libraryItemKey":row.get("item_key") or ""}


def library_state_for_card(kind, card):
    return _library_state_from_rows(kind,card,_library_rows(kind))


def annotate_library_items(items,kind):
    rows=_library_rows(kind)
    for x in items or []:
        try:x.update(_library_state_from_rows(kind,x,rows))
        except Exception:pass
    return items


def annotate_home_library(sections):
    rows_by={k:_library_rows(k) for k in ("movies","tv","anime")}
    for section in sections or []:
        if section.get("type") not in {"media",None}:continue
        sid=str(section.get("id") or "")
        default="movies" if sid.startswith("movies") else "anime" if sid.startswith("anime") else "tv" if sid.startswith("tv") else ""
        for x in section.get("items") or []:
            k=x.get("kind") or default
            if k in rows_by:
                try:x.update(_library_state_from_rows(k,x,rows_by[k]))
                except Exception:pass


def _local_season_files(path):
    out={}
    if not path:return out
    root=Path(path)
    if not root.exists():return out
    try:
        for f in root.rglob("*"):
            if not f.is_file() or f.suffix.lower() not in VIDEO_EXTS:continue
            n=None
            for token in [f.name,*[p.name for p in f.parents if p!=root.parent][:3]]:
                m=re.search(r"(?:^|\b)(?:Season\s*|S)(\d{1,3})(?:\b|[^0-9])",token,re.I)
                if m:
                    n=int(m.group(1));break
            if n is None:n=1
            out[n]=out.get(n,0)+1
    except Exception:pass
    return out


async def _sonarr_existing_for_card(kind,title,external_id,catalog):
    if not SONARR_KEY:return None,None
    try: existing=await get_json(SONARR_URL,SONARR_KEY,"/api/v3/series")
    except Exception:return None,None
    ext=str(external_id or ""); cat=(catalog or "").casefold(); lookup=[]
    terms=[]
    if ext and cat=="sonarr":terms.append(f"tvdb:{ext}")
    if ext and cat=="tmdb":terms.append(f"tmdb:{ext}")
    if title:terms.append(title)
    for term in unique_terms(terms):
        try:
            lookup=await get_json(SONARR_URL,SONARR_KEY,"/api/v3/series/lookup",{"term":term})
        except Exception:lookup=[]
        if lookup:break
    tvdb=str((lookup[0] if lookup else {}).get("tvdbId") or "")
    chosen=None
    for x in existing:
        if tvdb and str(x.get("tvdbId") or "")==tvdb:
            chosen=x;break
    if not chosen and title:
        nt=normalize_search_text(title)
        chosen=next((x for x in existing if normalize_search_text(x.get("title") or "")==nt),None)
    return chosen,(lookup[0] if lookup else None)


async def _series_structure(kind,title,external_id,catalog,tmdb_seasons=None):
    local_row=_library_match(kind,external_id,catalog,title,"")
    local_counts=_local_season_files((local_row or {}).get("path"))
    # Сколько серий всего — по данным страницы-источника, если ARR и TMDB молчат.
    page_total=int(_json_dict((local_row or {}).get("extra_json")).get("episodesTotal") or 0)
    existing,lookup=await _sonarr_existing_for_card(kind,title,external_id,catalog)
    sonarr_by={}
    if existing:
        for s in existing.get("seasons") or []:
            try:n=int(s.get("seasonNumber"))
            except Exception:continue
            st=s.get("statistics") or {}
            sonarr_by[n]={"downloaded":int(st.get("episodeFileCount") or 0),"episodes":int(st.get("episodeCount") or 0),"monitored":bool(s.get("monitored",True))}
    meta={}
    for s in tmdb_seasons or []:
        try:n=int(s.get("season_number"))
        except Exception:continue
        if n<=0:continue
        meta[n]={"season":n,"name":s.get("name") or f"Сезон {n}","episodes":int(s.get("episode_count") or 0),"airDate":s.get("air_date") or "","poster":"https://image.tmdb.org/t/p/w300"+s["poster_path"] if s.get("poster_path") else None}
    all_nums=sorted(set(meta)|set(local_counts)|set(sonarr_by))
    out=[]
    for n in all_nums:
        item=dict(meta.get(n) or {"season":n,"name":f"Сезон {n}","episodes":0,"airDate":"","poster":None})
        ss=sonarr_by.get(n,{})
        downloaded=max(int(local_counts.get(n,0)),int(ss.get("downloaded") or 0))
        total=max(int(item.get("episodes") or 0),int(ss.get("episodes") or 0))
        # У однесезонного тайтла со страницы известно общее число серий.
        if not total and page_total and len(all_nums)==1:
            total=page_total
            item["episodes"]=page_total
            item["episodesSource"]="page"
        item.update({"downloaded":downloaded,"inLibrary":downloaded>0,"complete":bool(total and downloaded>=total),"missing":max(0,total-downloaded) if total else None,"monitored":ss.get("monitored",False)})
        out.append(item)
    # Снимок сезонов в библиотеке мог устареть (Sonarr ещё не импортировал
    # свежие серии). Сохраняем точную сводку — по ней считаются бейджи.
    if local_row and out:
        try:
            _store_season_summary(kind,local_row,out)
        except Exception:
            pass
    return {"items":out,"seriesId":(existing or {}).get("id"),"inSonarr":bool(existing),"localPath":(local_row or {}).get("path") or "","missingSeasons":[x["season"] for x in out if not x.get("complete")]}


def _store_season_summary(kind,row,items):
    summary=[{"season":int(x.get("season") or 0),
              "episodes":int(x.get("episodes") or 0),
              "downloaded":int(x.get("downloaded") or 0)} for x in items if x.get("season")]
    if not summary:
        return
    extra=_json_dict(row.get("extra_json"))
    if extra.get("seasonsSummary")==summary:
        return
    extra["seasonsSummary"]=summary
    with cache_db() as con:
        con.execute("update library_cache set extra_json=? where kind=? and item_key=?",
                    (json.dumps(extra,ensure_ascii=False),kind,row.get("item_key")))
        con.commit()


def _release_structure_fields(title):
    """Поля о сезонах/сериях для одного релиза, как их ждёт интерфейс."""
    nums,pack,ep=release_season_info(title)
    label=""
    if ep["episodeFrom"] and ep["episodeTo"]>ep["episodeFrom"]:
        label=f"E{ep['episodeFrom']:02d}–E{ep['episodeTo']:02d}"
        if ep["episodeTotal"]:
            label+=f" из {ep['episodeTotal']}"
    elif ep["episodeFrom"]:
        label=f"E{ep['episodeFrom']:02d}"
    return {
        "seasonNumbers":nums,"packType":pack,
        "episodeFrom":ep["episodeFrom"],"episodeTo":ep["episodeTo"],
        "episodeTotal":ep["episodeTotal"],"episodeLabel":label,
        "seasonComplete":ep["seasonComplete"],
    }


def release_episode_info(title):
    """Диапазон серий в названии релиза: «E1-24 of 24», «1-16 из 16», «E05»."""
    text=" "+(title or "")+" "
    first=last=total=0
    # Буква E может стоять сразу после номера сезона («S04E1-24»), поэтому
    # цифры перед ней допустимы, а буквы (как в «WEB») — нет.
    m=re.search(r"(?<![a-zа-я])e\s*(\d{1,4})\s*[-–—]\s*(?:e\s*)?(\d{1,4})",text,re.I)
    if m:
        first,last=int(m.group(1)),int(m.group(2))
    else:
        m=re.search(r"(?<![a-zа-я0-9])(\d{1,3})\s*[-–—]\s*(\d{1,3})\s*(?:сери[ияй]|эпизод)",text,re.I)
        if m:
            first,last=int(m.group(1)),int(m.group(2))
        else:
            m=re.search(r"(?<![a-zа-я])e\s*(\d{1,4})(?![-–—0-9])",text,re.I)
            if m:
                first=last=int(m.group(1))
    m=re.search(r"(?:of|из)\s*(\d{1,4})",text,re.I)
    if m:
        total=int(m.group(1))
    if last and last<first:
        first,last=last,first
    if total and last>total:
        total=0
    return {"episodeFrom":first,"episodeTo":last,"episodeTotal":total,
            "seasonComplete":bool(total and last>=total and first<=1)}


def release_season_info(title):
    """Определение сезонов в названии релиза.

    Разбираются варианты, которые реально встречаются у трекеров:
    `S04E1-24 of 24`, `S01-S05`, `Сезон 4`, `4 сезон`, `сезоны 1-3`, `4x01`,
    `Season 2`, `Complete`. Возвращает (список сезонов, тип пака, детали серий).
    """
    text=" "+(title or "")+" "
    nums=set()
    complete=bool(re.search(r"(complete|full\s*series|all\s*seasons|все\s*сезон|полный\s*сериал|весь\s*сериал)",text,re.I))

    def add_range(a,b):
        aa,bb=int(a),int(b)
        if 0<aa<=bb<=200 and bb-aa<=50:
            nums.update(range(aa,bb+1))

    # S01-S05, S1 - 5, S01–S05
    for a,b in re.findall(r"(?<![a-zа-я0-9])s\s*(\d{1,3})\s*[-–—_]\s*s\s*(\d{1,3})(?![0-9])",text,re.I):
        add_range(a,b)
    # S1-3E1-24: диапазон сезонов без второй буквы S, дальше идут серии.
    for a,b in re.findall(r"(?<![a-zа-я0-9])s\s*(\d{1,3})\s*[-–—]\s*(\d{1,3})(?=\s*(?:e\s*\d|x\s*\d|[^0-9a-zа-я]|$))",text,re.I):
        add_range(a,b)
    # Season 1-3, Сезоны 1-3, сезон 1-3
    for a,b in re.findall(r"(?:seasons?|сезон[ыа]?)\s*(\d{1,3})\s*[-–—]\s*(\d{1,3})(?![0-9])",text,re.I):
        add_range(a,b)
    # 1-3 сезоны
    for a,b in re.findall(r"(?<![a-zа-я0-9])(\d{1,3})\s*[-–—]\s*(\d{1,3})\s*(?:-?[йая]?\s*)?сезон",text,re.I):
        add_range(a,b)
    # S04, S04E01, S04E1-24, S4 E12
    for n in re.findall(r"(?<![a-zа-я0-9])s\s*(\d{1,3})(?=\s*(?:e\s*\d|[^0-9a-zа-я]|$))",text,re.I):
        nums.add(int(n))
    # Season 4, Сезон 4
    for n in re.findall(r"(?:seasons?|сезон)\s*(\d{1,3})(?![0-9])",text,re.I):
        nums.add(int(n))
    # 4 сезон, 4-й сезон
    for n in re.findall(r"(?<![a-zа-я0-9])(\d{1,3})\s*(?:-?[йяе]\w{0,2}\s*)?сезон",text,re.I):
        nums.add(int(n))
    # 4x01
    for n in re.findall(r"(?<![a-zа-я0-9])(\d{1,2})x\d{1,3}(?![0-9])",text,re.I):
        nums.add(int(n))
    # Аниме-раздачи пишут номер сезона как «[ТВ-2]», «TV-3», «ТВ 2».
    for n in re.findall(r"(?<![a-zа-я0-9])(?:тв|tv)\s*[-–—]?\s*(\d{1,2})(?![0-9])",text,re.I):
        nums.add(int(n))
    # «второй сезон», «2nd season»
    for word,value in SEASON_WORDS.items():
        if re.search(rf"(?<![a-zа-я]){word}\w*\s+сезон",text,re.I):
            nums.add(value)
    for n in re.findall(r"(?<![a-zа-я0-9])(\d{1,2})\s*(?:-?(?:st|nd|rd|th))?\s+season",text,re.I):
        nums.add(int(n))

    nums={n for n in nums if 0<n<=200}
    episodes=release_episode_info(title)

    if complete:
        return sorted(nums),"series-pack",episodes
    if len(nums)>1:
        return sorted(nums),"multi-season",episodes
    if len(nums)==1:
        return sorted(nums),"season",episodes
    if episodes["episodeFrom"] and episodes["episodeTo"]>episodes["episodeFrom"]:
        # «E1-16 of 16» без номера сезона — пак серий одного сезона.
        return [],"episodes",episodes
    return [],"unknown",episodes


async def _tmdb_collection(collection_id):
    if not TMDB_KEY or not collection_id:return None
    try:
        async with external_async_client(22) as c:
            r=await external_request(c,"GET",f"https://api.themoviedb.org/3/collection/{collection_id}",params=tmdb_auth_params({"language":"ru-RU"}),headers=tmdb_auth_headers(),retries=3)
            x=r.json()
    except Exception:return None
    parts=[]
    for p in sorted(x.get("parts") or [],key=lambda z:z.get("release_date") or "9999"):
        card={"kind":"movies","title":p.get("title") or p.get("original_title") or "","originalTitle":p.get("original_title") or "","year":str(p.get("release_date") or "")[:4],"overview":p.get("overview") or "","poster":"https://image.tmdb.org/t/p/w300"+p["poster_path"] if p.get("poster_path") else None,"externalId":p.get("id"),"catalog":"tmdb","rating":p.get("vote_average")}
        card.update(library_state_for_card("movies",card));parts.append(card)
    return {"id":x.get("id"),"name":x.get("name") or "Коллекция","poster":"https://image.tmdb.org/t/p/w500"+x["poster_path"] if x.get("poster_path") else None,"parts":parts,"owned":sum(1 for z in parts if z.get("inLibrary")),"total":len(parts)}


async def enrich_media_structure(kind,data,catalog="",original_external_id="",title=""):
    card=dict(data or {})
    ext=card.get("externalId") or original_external_id or ""
    card["library"]=library_state_for_card(kind,{**card,"externalId":ext,"catalog":card.get("catalog") or catalog,"title":card.get("title") or title})
    card.update(card["library"])
    if kind in {"tv","anime"}:
        card["seasonMap"]=await _series_structure(kind,card.get("title") or title,ext,card.get("catalog") or catalog,card.pop("_tmdb_seasons",[]))
    elif kind=="movies" and card.get("_collection_id"):
        card["collection"]=await _tmdb_collection(card.pop("_collection_id"))
    else:
        card.pop("_tmdb_seasons",None);card.pop("_collection_id",None)
    return card

def pick_best_lookup(rows,title,year="",kind="movies"):
    """Выбрать из ответа Radarr/Sonarr/каталога карточку, а не просто первую.

    Поиск по названию «Одиссея» возвращал «Peter's Odyssey (2018)», потому что
    бралась первая строка. Теперь названия и год сравниваются, а кандидат с
    сильно другим годом отбрасывается.
    """
    rows=[r for r in (rows or []) if isinstance(r,dict)]
    if not rows:
        return None
    want=normalize_search_text(title)
    try: want_year=int(str(year)[:4])
    except Exception: want_year=0
    best=None; best_score=-99.0
    for r in rows:
        names=[r.get("title"),r.get("name"),r.get("originalTitle"),r.get("original_title"),
               r.get("localizedTitle"),r.get("sortTitle")]
        names=[normalize_search_text(n) for n in names if n]
        score=0.0
        if want and names:
            score=max(SequenceMatcher(None,want,n).ratio() for n in names)
            if any(n==want for n in names):
                score=1.0
        ry=str(r.get("year") or r.get("releaseDate") or r.get("firstAired") or "")[:4]
        try: ry=int(ry)
        except Exception: ry=0
        if want_year and ry:
            diff=abs(ry-want_year)
            if diff==0: score+=0.45
            elif diff==1: score+=0.15
            else: score-=0.6
        if score>best_score:
            best_score=score; best=r
    # Совсем непохожий результат лучше не подставлять молча.
    if best_score<0.35 and want:
        return None
    return best


# --- v21.6 общий кэш внешних ответов -----------------------------------------
# Radarr/Sonarr lookup и поиск по TMDB ходят в интернет и занимают секунды.
# Ответы складываются в SQLite: повторное открытие карточки читает их локально,
# а если внешний сервис недоступен, отдаётся последний известный результат.

API_CACHE_TTL=timedelta(days=7)

def _api_cache_read(key):
    try:
        with cache_db() as con:
            row=con.execute("select payload,updated_at from api_cache where cache_key=?",(key,)).fetchone()
    except Exception:
        return None,None
    if not row:
        return None,None
    try:
        payload=json.loads(row["payload"])
    except Exception:
        return None,None
    age=None
    try:
        dt=datetime.fromisoformat(row["updated_at"])
        if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
        age=datetime.now(timezone.utc)-dt
    except Exception:
        pass
    return payload,age

def _api_cache_write(key,payload):
    try:
        with cache_db() as con:
            con.execute("insert or replace into api_cache(cache_key,payload,updated_at) values(?,?,?)",
                        (key,json.dumps(payload,ensure_ascii=False),datetime.now(timezone.utc).isoformat()))
            # Кэш не должен расти бесконечно на долгоживущей установке.
            con.execute("""delete from api_cache where cache_key not in
                           (select cache_key from api_cache order by updated_at desc limit 4000)""")
            con.commit()
    except Exception:
        pass

async def cached_api_call(key,factory,ttl=API_CACHE_TTL):
    """Вернуть закэшированный ответ; при протухании обновить, при ошибке — отдать старое."""
    payload,age=_api_cache_read(key)
    if payload is not None and age is not None and age<ttl:
        return payload
    try:
        fresh=await factory()
    except Exception:
        fresh=None
    if fresh:
        _api_cache_write(key,fresh)
        return fresh
    return payload if payload is not None else fresh

# --- v21.6 постоянный кэш метаданных ----------------------------------------
# Всё, что уже есть в библиотеке, должно открываться мгновенно и без интернета.
# Ответы TMDB по карточкам складываем в SQLite: для библиотечных тайтлов кэш
# считается свежим очень долго, для остальных — неделю.

TMDB_DETAIL_TTL=timedelta(days=7)
TMDB_DETAIL_TTL_LIBRARY=timedelta(days=90)

def _detail_cache_key(kind,external_id):
    return f"{kind}|tmdb|{external_id}"

def _detail_cache_read(kind,external_id):
    try:
        with cache_db() as con:
            row=con.execute("select payload,updated_at,in_library from tmdb_detail_cache where cache_key=?",
                            (_detail_cache_key(kind,external_id),)).fetchone()
    except Exception:
        return None,None
    if not row:
        return None,None
    try:
        data=json.loads(row["payload"])
    except Exception:
        return None,None
    age=None
    try:
        dt=datetime.fromisoformat(row["updated_at"])
        if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
        age=datetime.now(timezone.utc)-dt
    except Exception:
        pass
    return data,{"age":age,"inLibrary":bool(row["in_library"])}

def _detail_cache_write(kind,external_id,data,in_library=False):
    if not data:
        return
    try:
        with cache_db() as con:
            con.execute("""insert or replace into tmdb_detail_cache
                (cache_key,kind,external_id,title,payload,in_library,updated_at)
                values(?,?,?,?,?,?,?)""",
                (_detail_cache_key(kind,external_id),kind,str(external_id),data.get("title") or "",
                 json.dumps(data,ensure_ascii=False),1 if in_library else 0,
                 datetime.now(timezone.utc).isoformat()))
            con.commit()
    except Exception:
        pass

def _library_has_external(kind,external_id):
    """Есть ли тайтл в library_cache: такие карточки кэшируем надолго."""
    if not external_id:
        return False
    try:
        with cache_db() as con:
            row=con.execute("""select 1 from library_cache
                               where kind=? and external_id=? limit 1""",
                            (kind,str(external_id))).fetchone()
        return bool(row)
    except Exception:
        return False

async def detail_from_tmdb(kind, external_id, allow_cache=True):
    if not external_id or kind=="games":
        return {}
    in_library=_library_has_external(kind,external_id)
    if allow_cache:
        cached,meta=_detail_cache_read(kind,external_id)
        if cached:
            ttl=TMDB_DETAIL_TTL_LIBRARY if (in_library or (meta or {}).get("inLibrary")) else TMDB_DETAIL_TTL
            age=(meta or {}).get("age")
            if age is None or age<ttl:
                return cached
            # Кэш устарел, но он есть: отдаём его и обновляем в фоне, чтобы
            # карточка открылась сразу.
            asyncio.create_task(_refresh_detail_cache(kind,external_id,in_library))
            return cached
    data=await _fetch_detail_from_tmdb(kind,external_id)
    if data:
        _detail_cache_write(kind,external_id,data,in_library)
    return data

async def _refresh_detail_cache(kind,external_id,in_library=False):
    try:
        data=await _fetch_detail_from_tmdb(kind,external_id)
        if data:
            _detail_cache_write(kind,external_id,data,in_library)
    except Exception:
        pass

async def _fetch_detail_from_tmdb(kind, external_id):
    if not TMDB_KEY or not external_id or kind=="games":
        return {}
    media="movie" if kind=="movies" else "tv"
    try:
        async with external_async_client(22) as c:
            r=await external_request(c,"GET",f"https://api.themoviedb.org/3/{media}/{external_id}",
                params=tmdb_auth_params({"language":"ru-RU"}),headers=tmdb_auth_headers(),retries=3)
            x=r.json()
    except Exception:
        return {}
    genres=[g.get("name") for g in (x.get("genres") or []) if g.get("name")]
    runtime=x.get("runtime") or ((x.get("episode_run_time") or [0])[0] if x.get("episode_run_time") else 0)
    studio=""
    network=""
    if x.get("production_companies"):
        studio=(x["production_companies"][0] or {}).get("name") or ""
    if x.get("networks"):
        network=(x["networks"][0] or {}).get("name") or ""
    return {
        "title":x.get("title") or x.get("name"),
        "year":(x.get("release_date") or x.get("first_air_date") or "")[:4],
        "overview":x.get("overview") or "",
        "poster":"https://image.tmdb.org/t/p/w500"+x["poster_path"] if x.get("poster_path") else None,
        "genres":genres,
        "rating":x.get("vote_average"),
        "runtime":runtime or 0,
        "status":x.get("status") or "",
        "studio":studio,
        "network":network,
        "externalId":x.get("id"),
        "catalog":"tmdb",
        "_tmdb_seasons":x.get("seasons") or [],
        "_collection_id":((x.get("belongs_to_collection") or {}).get("id") if kind=="movies" else None),
    }

def page_series_structure(kind,row,path,page_total=0):
    """Сезоны проекта со страницы-источника — только по файлам на диске.

    Sonarr и TMDB сюда не зовём: по названию они как раз и находили чужой
    сериал. Общее число серий известно только для однесезонного тайтла.
    """
    counts=_local_season_files(path)
    out=[]
    for n in sorted(counts):
        have=int(counts[n])
        total=page_total if page_total and len(counts)==1 else 0
        out.append({"season":n,"name":f"Сезон {n}","episodes":total,"airDate":"","poster":None,
                    "episodesSource":"page" if total else "","downloaded":have,"inLibrary":have>0,
                    "complete":bool(total and have>=total),"missing":max(0,total-have) if total else None,
                    "monitored":False})
    if row and out:
        try:_store_season_summary(kind,row,out)
        except Exception:pass
    return {"items":out,"seriesId":None,"inSonarr":False,"localPath":path,
            "missingSeasons":[x["season"] for x in out if not x.get("complete")]}


def library_page_card(kind,path):
    """Карточка проекта, к которому привязана страница-источник.

    Всё берётся со страницы и с диска. Внешние каталоги не опрашиваются:
    поиск по названию подставлял постер и описание другого тайтла.
    """
    path=str(path or "").rstrip("/")
    if not path or kind not in {"movies","tv","anime"}:
        return None
    meta=manual_meta_index().get(path)
    if not meta or not meta.get("sourceUrl"):
        return None
    row=find_library_row(kind,"",path)
    card=row_media(row,kind) if row else {"kind":kind,"path":path}
    if row:
        card.update(library_item_state(row))
    apply_manual_meta(card,{path:meta})
    card.update({"catalog":"page","catalog_source":"page","externalId":"","external_id":"",
                 "path":path,"libraryItemKey":(row or {}).get("item_key") or "",
                 "libraryTitle":card.get("title") or ""})
    card["library"]={"inLibrary":bool(card.get("inLibrary",True)),"tracked":True,"path":path,
                     "sourceUrl":meta["sourceUrl"],"libraryItemKey":card["libraryItemKey"]}
    if kind in {"tv","anime"}:
        total=int(meta.get("episodes") or _json_dict((row or {}).get("extra_json")).get("episodesTotal") or 0)
        card["seasonMap"]=page_series_structure(kind,row,path,total)
    return card


@app.get("/api/media-details")
async def media_details(
    kind:str=Query("movies"),
    external_id:str=Query(""),
    catalog:str=Query(""),
    title:str=Query(""),
    year:str=Query(""),
    path:str=Query(""),
    refresh:bool=Query(False)
):
    # К проекту привязана страница — карточка целиком с неё, без TMDB/ARR.
    page_card=library_page_card(kind,path)
    if page_card:
        return page_card
    if refresh:
        # Ручное обновление карточки: чистим кэш метаданных именно по ней.
        needle=normalize_search_text(title)
        try:
            with cache_db() as con:
                if external_id:
                    con.execute("delete from tmdb_detail_cache where cache_key=?",
                                (_detail_cache_key(kind,external_id),))
                if needle:
                    con.execute("delete from api_cache where cache_key like ?",(f"%{needle}%",))
                con.commit()
        except Exception:
            pass
    data={}
    if catalog=="anilist" and external_id:
        data=await cached_api_call(f"anilist-detail|{external_id}",lambda: anilist_details(external_id)) or {}
        # AniList does not carry TMDB season metadata. Try TMDB by title for
        # the season map while keeping the richer anime card when possible.
        if data and kind=="anime":
            try:
                aq=data.get("title") or title
                tm=await cached_api_call(f"tmdb-search|anime|{normalize_search_text(aq)}",
                                         lambda: tmdb_search_bilingual(aq,"anime"))
                if tm:
                    td=await detail_from_tmdb("anime",tm[0].get("externalId"))
                    if td:
                        for k in ("_tmdb_seasons",): data[k]=td.get(k) or []
                        data.setdefault("tmdbId",td.get("externalId"))
            except Exception:pass
        if data:
            return await enrich_media_structure(kind,data,catalog,external_id,title)

    if catalog=="tmdb" and external_id:
        data=await detail_from_tmdb(kind,external_id)
        if data:
            return await enrich_media_structure(kind,data,catalog,external_id,title)

    if kind=="movies":
        term=f"tmdb:{external_id}" if external_id and catalog in {"radarr","tmdb"} else title
        rows=await cached_api_call(
            f"radarr-lookup|{normalize_search_text(term) or term}",
            lambda: get_json(RADARR_URL,RADARR_KEY,"/api/v3/movie/lookup",{"term":term})
        ) or []
        if rows:
            # По tmdb:<id> ответ однозначен, по названию — выбираем по году.
            x=rows[0] if term.startswith("tmdb:") else (pick_best_lookup(rows,title,year,kind) or rows[0])
            data={
                "title":x.get("title"),"year":x.get("year"),"overview":x.get("overview") or "",
                "poster":next((i.get("remoteUrl") for i in x.get("images",[]) if i.get("coverType")=="poster"),None),
                "externalId":x.get("tmdbId"),"catalog":"radarr","genres":x.get("genres") or [],"runtime":x.get("runtime") or 0,
                "rating":((x.get("ratings") or {}).get("value") or ((x.get("ratings") or {}).get("imdb") or {}).get("value") or ((x.get("ratings") or {}).get("tmdb") or {}).get("value")),
                "status":x.get("status") or "","studio":x.get("studio") or "","network":"","certification":x.get("certification") or "",
            }
            # TMDB detail adds belongs_to_collection for franchise cards.
            td=await detail_from_tmdb("movies",x.get("tmdbId")) if x.get("tmdbId") else {}
            if td:data.update({k:v for k,v in td.items() if k.startswith("_")})
            return await enrich_media_structure(kind,data,"radarr",x.get("tmdbId"),title)
    elif kind in {"tv","anime"}:
        term=f"tvdb:{external_id}" if external_id and catalog=="sonarr" else title
        rows=await cached_api_call(
            f"sonarr-lookup|{kind}|{normalize_search_text(term) or term}",
            lambda: get_json(SONARR_URL,SONARR_KEY,"/api/v3/series/lookup",{"term":term})
        ) or []
        if rows:
            x=rows[0] if term.startswith("tvdb:") else (pick_best_lookup(rows,title,year,kind) or rows[0])
            data={
                "title":x.get("title"),"year":x.get("year"),"overview":x.get("overview") or "",
                "poster":next((i.get("remoteUrl") for i in x.get("images",[]) if i.get("coverType")=="poster"),None),
                "externalId":x.get("tvdbId"),"catalog":"sonarr","genres":x.get("genres") or [],"runtime":x.get("runtime") or 0,
                "rating":((x.get("ratings") or {}).get("value") or ((x.get("ratings") or {}).get("imdb") or {}).get("value") or ((x.get("ratings") or {}).get("tvdb") or {}).get("value")),
                "status":x.get("status") or "","network":x.get("network") or "","studio":x.get("network") or "","seasonCount":len(x.get("seasons") or []),
            }
            # Sonarr gives season numbers/counts but TMDB gives localized season
            # names/posters. Search TMDB by title best-effort.
            try:
                query=x.get("title") or title
                tm=await cached_api_call(
                    f"tmdb-search|{kind}|{normalize_search_text(query)}",
                    lambda: tmdb_search_bilingual(query,"anime" if kind=="anime" else "tv")
                )
                if tm:
                    td=await detail_from_tmdb(kind,tm[0].get("externalId"))
                    if td:data["_tmdb_seasons"]=td.get("_tmdb_seasons") or []
            except Exception:pass
            return await enrich_media_structure(kind,data,"sonarr",x.get("tvdbId"),title)

    if catalog=="filesystem" and title:
        try: cards=await catalog_search_live(title,kind)
        except Exception: cards=[]
        if cards:
            data=pick_best_lookup(cards,title,year,kind) or cards[0]
            if data.get("catalog")=="tmdb" and data.get("externalId"):
                td=await detail_from_tmdb(kind,data.get("externalId"))
                if td:data.update(td)
            return await enrich_media_structure(kind,data,data.get("catalog") or catalog,data.get("externalId") or external_id,title)

    if kind=="anime" and title:
        rows=await anilist_search(title)
        if rows:
            best=pick_best_lookup(rows,title,year,kind) or rows[0]
            return await enrich_media_structure(kind,best,"anilist",best.get("externalId") or external_id,title)

    if external_id:
        data=await detail_from_tmdb(kind,external_id)
        if data:return await enrich_media_structure(kind,data,"tmdb",external_id,title)
    return await enrich_media_structure(kind,{"title":title,"genres":[],"runtime":0,"rating":None,"status":"","studio":"","network":""},catalog,external_id,title)

# --- v21.1 notes -------------------------------------------------------------
# A note is a plain "хочу посмотреть" line. MediaHub resolves it to real
# catalogue cards in the background so the user can send it to download or to
# tracking without searching manually again.

NOTE_KINDS={"movies","tv","anime"}

def _note_row(r):
    d=dict(r)
    for key,target in (("matched_json","matched"),("suggestions_json","suggestions")):
        raw=d.pop(key,None)
        try:d[target]=json.loads(raw) if raw else ([] if target=="suggestions" else None)
        except Exception:d[target]=[] if target=="suggestions" else None
    return d

def _note_suggestion(card,kind):
    return {
        "title":card.get("title") or "","year":str(card.get("year") or ""),
        "poster":card.get("poster") or "","overview":(card.get("overview") or "")[:220],
        "externalId":str(card.get("externalId") or card.get("external_id") or ""),
        "catalog":card.get("catalog") or card.get("catalog_source") or "tmdb",
        "kind":card.get("kind") or kind,"rating":card.get("rating"),
        "inLibrary":bool(card.get("inLibrary")),"tracked":bool(card.get("tracked")),
        "libraryLabel":card.get("libraryLabel") or "",
    }

async def _note_resolve(note_id,text,kind):
    """Search the catalogue for a note and store the top candidates."""
    try:
        cards=await catalog_search_live(text,kind)
    except Exception as e:
        with cache_db() as con:
            con.execute("update notes set status='error',updated_at=? where id=?",
                        (datetime.now(timezone.utc).isoformat(),note_id));con.commit()
        log_activity("note-search",text,str(e)[:200],False)
        return []
    annotate_library_items(cards,kind)
    top=[_note_suggestion(x,kind) for x in cards[:6]]
    with cache_db() as con:
        con.execute("update notes set suggestions_json=?,status=?,updated_at=? where id=?",
                    (json.dumps(top,ensure_ascii=False),"matched" if top else "empty",
                     datetime.now(timezone.utc).isoformat(),note_id))
        con.commit()
    return top

@app.get("/api/notes")
async def notes_list():
    with cache_db() as con:
        rows=con.execute("select * from notes order by id desc limit 300").fetchall()
    return [_note_row(r) for r in rows]

@app.post("/api/notes")
async def notes_create(text:str=Form(...),kind:str=Form("movies"),comment:str=Form("")):
    text=(text or "").strip()
    if not text:
        raise HTTPException(400,"Пустая заметка")
    if kind not in NOTE_KINDS:
        kind="movies"
    now=datetime.now(timezone.utc).isoformat()
    with cache_db() as con:
        cur=con.execute("""insert into notes(text,kind,status,comment,created_at,updated_at)
                           values(?,?,?,?,?,?)""",(text,kind,"new",comment or "",now,now))
        note_id=cur.lastrowid; con.commit()
    log_activity("note",text,f"Заметка добавлена ({kind})")
    suggestions=await _note_resolve(note_id,text,kind)
    with cache_db() as con:
        row=con.execute("select * from notes where id=?",(note_id,)).fetchone()
    return {"ok":True,"note":_note_row(row),"suggestions":suggestions}

@app.post("/api/notes/{note_id}/refresh")
async def notes_refresh(note_id:int):
    with cache_db() as con:
        row=con.execute("select * from notes where id=?",(note_id,)).fetchone()
    if not row:
        raise HTTPException(404,"Заметка не найдена")
    suggestions=await _note_resolve(note_id,row["text"],row["kind"])
    return {"ok":True,"suggestions":suggestions,
            "message":"Совпадений не нашлось" if not suggestions else f"Найдено {len(suggestions)}"}

@app.post("/api/notes/{note_id}/pick")
async def notes_pick(note_id:int,external_id:str=Form(""),catalog:str=Form("tmdb"),
                     title:str=Form(""),track:str=Form("1")):
    with cache_db() as con:
        row=con.execute("select * from notes where id=?",(note_id,)).fetchone()
    if not row:
        raise HTTPException(404,"Заметка не найдена")
    kind=row["kind"] if row["kind"] in NOTE_KINDS else "movies"
    matched={"externalId":str(external_id or ""),"catalog":catalog or "tmdb",
             "title":title or row["text"],"kind":kind}
    result={"ok":True,"message":"Заметка связана с карточкой"}
    if track=="1" and external_id:
        try:
            result=await add(kind=kind,external_id=int(external_id),catalog=catalog or "tmdb",title=title or row["text"])
            if isinstance(result,JSONResponse):
                raise RuntimeError("Не удалось добавить в отслеживание")
        except Exception as e:
            with cache_db() as con:
                con.execute("update notes set matched_json=?,status='matched',updated_at=? where id=?",
                            (json.dumps(matched,ensure_ascii=False),datetime.now(timezone.utc).isoformat(),note_id));con.commit()
            return JSONResponse({"ok":False,"error":str(e)[:200]},400)
        matched["tracked"]=True
    with cache_db() as con:
        con.execute("update notes set matched_json=?,status=?,updated_at=? where id=?",
                    (json.dumps(matched,ensure_ascii=False),"tracked" if matched.get("tracked") else "matched",
                     datetime.now(timezone.utc).isoformat(),note_id))
        con.commit()
    log_activity("note-track",matched.get("title") or "",f"Заметка #{note_id} отправлена в отслеживание")
    return {"ok":True,"message":result.get("message") if isinstance(result,dict) else "Готово","matched":matched}

@app.delete("/api/notes/{note_id}")
async def notes_delete(note_id:int):
    with cache_db() as con:
        con.execute("delete from notes where id=?",(note_id,));con.commit()
    return {"ok":True}

@app.get("/api/favorites")
async def favorites(kind:str|None=None):
    with cache_db() as con:
        if kind:
            rows=con.execute("select * from favorites where kind=? order by created_at desc",(kind,)).fetchall()
        else:
            rows=con.execute("select * from favorites order by created_at desc").fetchall()
    out=[]
    for r in rows:
        d=dict(r)
        try:d["genres"]=json.loads(d.get("genres") or "[]")
        except Exception:d["genres"]=[]
        out.append(d)
    return out

@app.post("/api/favorites")
async def favorite_add(
    kind:str=Form(...),
    external_id:str=Form(""),
    title:str=Form(...),
    year:str=Form(""),
    overview:str=Form(""),
    poster:str=Form(""),
    genres:str=Form("[]"),
    rating:float|None=Form(None),
    runtime:int=Form(0),
    status:str=Form(""),
    studio:str=Form(""),
    network:str=Form(""),
    catalog:str=Form(""),
    raw_json:str=Form("{}"),
):
    fav_key=hashlib.sha256(f"{kind}|{external_id}|{title}".encode("utf-8")).hexdigest()[:32]
    with cache_db() as con:
        con.execute("""insert or replace into favorites(
            fav_key,kind,external_id,title,year,overview,poster,genres,rating,
            runtime,status,studio,network,catalog,raw_json,created_at
        ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (fav_key,kind,external_id,title,year,overview,poster,genres,rating,
         runtime,status,studio,network,catalog,raw_json,
         datetime.now(timezone.utc).isoformat()))
        con.commit()
    return {"ok":True,"favKey":fav_key,"message":"Добавлено в избранное"}

@app.delete("/api/favorites/{fav_key}")
async def favorite_delete(fav_key:str):
    with cache_db() as con:
        con.execute("delete from favorites where fav_key=?",(fav_key,))
        con.commit()
    return {"ok":True,"message":"Удалено из избранного"}


async def _ensure_sonarr_series(kind,external_id,catalog,title,year=""):
    if not SONARR_KEY:
        raise RuntimeError("Sonarr не настроен")
    term=(title or "").strip()
    if external_id and catalog=="sonarr": term=f"tvdb:{external_id}"
    elif external_id and catalog=="tmdb": term=f"tmdb:{external_id}"
    lookup=[]
    try:lookup=await get_json(SONARR_URL,SONARR_KEY,"/api/v3/series/lookup",{"term":term})
    except Exception:lookup=[]
    if not lookup and title and term!=title:
        lookup=await get_json(SONARR_URL,SONARR_KEY,"/api/v3/series/lookup",{"term":title})
    if not lookup: raise RuntimeError("Сериал не найден в Sonarr")
    # Sonarr возвращает список похожих названий. Раньше бралось первое — так в
    # библиотеку попадали чужие сериалы («A Teacher» вместо нужного).
    item=lookup[0] if term.startswith("tvdb:") else pick_best_lookup(lookup,title,year,kind)
    if not item:
        raise RuntimeError(f"В Sonarr нет точного совпадения для «{title}»")
    tvdb=str(item.get("tvdbId") or "")
    existing=await get_json(SONARR_URL,SONARR_KEY,"/api/v3/series")
    found=next((x for x in existing if tvdb and str(x.get("tvdbId") or "")==tvdb),None)
    if found:return found,False
    anime=kind=="anime"
    item.update({
        "qualityProfileId":await first_profile(SONARR_URL,SONARR_KEY),
        "rootFolderPath":str(ANIME_ROOT if anime else TV_ROOT),"monitored":True,"seasonFolder":True,
        "seriesType":"anime" if anime else "standard","addOptions":{"monitor":"all","searchForMissingEpisodes":False}
    })
    created=await post_json(SONARR_URL,SONARR_KEY,"/api/v3/series",item)
    return created,True


@app.post("/api/series/download")
async def series_download(
    kind:str=Form(...), external_id:str=Form(""), catalog:str=Form(""), title:str=Form(...),
    seasons:str=Form(""), all_missing:bool=Form(False), year:str=Form("")
):
    if kind not in {"tv","anime"}:raise HTTPException(400,"Только сериалы и аниме")
    try:series,created=await _ensure_sonarr_series(kind,external_id,catalog,title,year)
    except Exception as e:return JSONResponse({"ok":False,"error":str(e)},status_code=400)
    sid=series.get("id")
    if not sid:return JSONResponse({"ok":False,"error":"Sonarr не вернул id сериала"},status_code=502)
    if created:
        # Кнопка «Скачать сезоны» заводит сериал в Sonarr — фиксируем это в
        # журнале и помечаем как добавленный пользователем.
        log_activity("track-add",series.get("title") or title,
                     "заведён в Sonarr кнопкой «Скачать сезоны»",True)
        remember_user_tracking(kind,sid,series.get("tvdbId") or external_id,series.get("title") or title)
    nums=[]
    for x in (seasons or "").split(","):
        try:
            n=int(x)
            if n>0:nums.append(n)
        except Exception:pass
    commands=[]
    try:
        if all_missing or not nums:
            commands.append(await post_json(SONARR_URL,SONARR_KEY,"/api/v3/command",{"name":"SeriesSearch","seriesId":sid}))
            msg="Sonarr запустил поиск всех отсутствующих эпизодов"
        else:
            for n in sorted(set(nums)):
                commands.append(await post_json(SONARR_URL,SONARR_KEY,"/api/v3/command",{"name":"SeasonSearch","seriesId":sid,"seasonNumber":n}))
            msg="Запущен поиск сезонов: "+", ".join(f"S{n:02d}" for n in sorted(set(nums)))
        log_activity("series-bulk",title,msg,True)
        return {"ok":True,"message":msg,"created":created,"seriesId":sid,"commands":[x.get("id") for x in commands if isinstance(x,dict)]}
    except Exception as e:
        return JSONResponse({"ok":False,"error":str(e)},status_code=502)


@app.post("/api/movie/collection-download")
async def movie_collection_download(collection_id:int=Form(...)):
    if not RADARR_KEY:return JSONResponse({"ok":False,"error":"Radarr не настроен"},status_code=400)
    collection=await _tmdb_collection(collection_id)
    if not collection:return JSONResponse({"ok":False,"error":"Коллекция TMDB не найдена"},status_code=404)
    try:existing=await get_json(RADARR_URL,RADARR_KEY,"/api/v3/movie")
    except Exception:existing=[]
    have={int(x.get("tmdbId")) for x in existing if x.get("tmdbId")}
    profile=await first_profile(RADARR_URL,RADARR_KEY)
    added=[];skipped=[];errors=[]
    for part in collection.get("parts") or []:
        mid=part.get("externalId")
        if not mid:continue
        if int(mid) in have:
            skipped.append(part.get("title"));continue
        try:
            lookup=await get_json(RADARR_URL,RADARR_KEY,"/api/v3/movie/lookup",{"term":f"tmdb:{mid}"})
            if not lookup:raise RuntimeError("не найден в Radarr")
            item=lookup[0]
            item.update({"qualityProfileId":profile,"rootFolderPath":str(MOVIES_ROOT),"monitored":True,"minimumAvailability":"released","addOptions":{"searchForMovie":True}})
            await post_json(RADARR_URL,RADARR_KEY,"/api/v3/movie",item);added.append(part.get("title") or str(mid))
        except Exception as e:errors.append({"title":part.get("title") or str(mid),"error":str(e)})
    log_activity("collection",collection.get("name") or "Коллекция",f"Добавлено {len(added)}, уже было {len(skipped)}",not errors)
    return {"ok":not bool(errors),"message":f"Коллекция: добавлено {len(added)}, уже есть {len(skipped)}","added":added,"skipped":skipped,"errors":errors}

async def _find_new_torrent(before,want_hash="",tries=6,delay=1.0):
    """Появился ли в qBittorrent торрент, которого не было до запроса."""
    want_hash=(want_hash or "").lower()
    for _ in range(max(1,int(tries))):
        await asyncio.sleep(delay)
        c=await qbit_login()
        if not c:
            return None
        try:
            r=await c.get(QBIT_URL+"/api/v2/torrents/info")
            items=r.json()
        except Exception:
            items=[]
        finally:
            await c.aclose()
        for x in items:
            h=str(x.get("hash") or "").lower()
            if (want_hash and h==want_hash) or (h and h not in before):
                return x
    return None


async def _grab_release_direct(payload,category):
    """Запасной путь: качаем .torrent сами и отдаём его в qBittorrent.

    Возвращает (успех, пояснение). Используется, когда Prowlarr отвечает
    ошибкой на штатный grab.
    """
    magnet=str(payload.get("magnetUrl") or "").strip()
    url=str(payload.get("downloadUrl") or payload.get("link") or payload.get("guid") or "").strip()
    if url.lower().startswith("magnet:"):
        magnet,url=url,""
    content=b""; filename="release.torrent"; direct_error=""; page_error=""
    if not magnet and url:
        try:
            async with httpx.AsyncClient(timeout=60,trust_env=False,follow_redirects=False) as c:
                r=await c.get(url,headers={"User-Agent":"MediaHub"})
                hops=0
                while r.status_code in (301,302,303,307,308) and hops<5:
                    loc=r.headers.get("location") or ""
                    if loc.lower().startswith("magnet:"):
                        magnet=loc; break
                    if not loc:
                        break
                    r=await c.get(urljoin_safe(str(r.url),loc),headers={"User-Agent":"MediaHub"}); hops+=1
                if not magnet:
                    if r.status_code>=300:
                        direct_error=f"источник ответил HTTP {r.status_code}"
                    else:
                        content=r.content or b""
                        if not content.startswith(b"d") or content.startswith(b"doctype"):
                            direct_error="вместо торрента источник вернул страницу"
                            content=b""
                    name=(payload.get("title") or "release").strip().replace("/","-")[:120]
                    filename=f"{name}.torrent"
        except Exception as e:
            direct_error=f"скачать торрент не удалось: {page_fetch_error_text(e)}"
    if not magnet and not content:
        # Третья попытка: открыть страницу релиза и снять magnet/.torrent прямо
        # с неё. Так лечится ошибка Prowlarr «Download selectors didn't match»,
        # когда определение индексера устарело.
        page=str(payload.get("infoUrl") or payload.get("guid") or payload.get("details") or "").strip()
        if page.lower().startswith(("http://","https://")):
            try:
                links,_meta=await torrent_links_from_page(page,return_meta=True)
                page_error=_meta.get("error") or ""
            except Exception as e:
                links=[]; page_error=page_fetch_error_text(e)
            for link in (links or [])[:6]:
                if link.get("type")=="magnet":
                    magnet=link["url"]; break
                data,found_magnet,_err=await fetch_torrent_bytes(link["url"],referer=page)
                if _err:page_error=_err
                if found_magnet:
                    magnet=found_magnet; break
                if data:
                    content=data; break
    if not magnet and not content:
        return False,"; ".join(x for x in (direct_error,page_error,"индексер не отдал торрент и страница релиза не помогла") if x)

    c=await qbit_login()
    if not c:
        return False,"qBittorrent недоступен"
    try:
        data={"category":category,"paused":"false"}
        if content:
            r=await c.post(QBIT_URL+"/api/v2/torrents/add",data=data,
                           files={"torrents":(filename,content,"application/x-bittorrent")})
        else:
            r=await c.post(QBIT_URL+"/api/v2/torrents/add",data={**data,"urls":magnet})
        if r.status_code>=300 or (r.text or "").strip().lower().startswith("fails"):
            return False,f"qBittorrent отклонил торрент: HTTP {r.status_code}"
    except Exception as e:
        return False,f"qBittorrent не ответил: {str(e)[:120]}"
    finally:
        await c.aclose()
    return True,"добавлено напрямую"


@app.post("/api/provider-grab")
async def provider_grab(
    token:str=Form(...),
    category:str=Form("manual"),
    media_title:str=Form(""),
    season:int=Form(0),
    force:str=Form("0")
):
    row=load_release(token)
    if not row:
        raise HTTPException(404,"Релиз устарел. Повтори поиск.")
    if category not in {"movies","tv","anime","manual"}:
        raise HTTPException(400,"Неизвестная категория загрузки")
    play=release_playback(row.get("title") or "")
    if not play["playable"] and force!="1" and skip_unplayable_releases():
        # Последний рубеж: даже по прямой ссылке образ диска не уходит в очередь.
        log_activity("download-blocked",media_title or row.get("title") or "",play["reason"],False)
        raise HTTPException(409,f"Раздача не будет воспроизводиться — {play['reason']}. "
                                "Скачать всё равно можно в настройках, сняв запрет неподдерживаемых форматов.")

    before=set()
    qclient=await qbit_login()
    if qclient:
        try:
            r=await qclient.get(QBIT_URL+"/api/v2/torrents/info")
            before={x.get("hash") for x in r.json()}
        except Exception:
            before=set()
        finally:
            await qclient.aclose()

    # First use Prowlarr's normal grab path.
    prowlarr_error=""
    try:
        async with httpx.AsyncClient(timeout=45,trust_env=False) as c:
            r=await c.post(
                PROWLARR_URL+"/api/v1/search",
                headers={"X-Api-Key":PROWLARR_KEY,"Content-Type":"application/json"},
                json=row["payload"]
            )
        if r.status_code >= 300:
            prowlarr_error=f"HTTP {r.status_code} {(r.text or '').strip()[:200]}"
    except Exception as e:
        prowlarr_error=str(e)[:200]

    if prowlarr_error:
        # Prowlarr отдаёт 500, если индексер не смог отдать торрент по своей
        # ссылке (протухший guid, требуется авторизация, лимит трекера).
        # Но бывает и наоборот: торрент уже ушёл в qBittorrent, а ответ пришёл
        # с ошибкой или по таймауту. Прежде чем качать напрямую, проверяем
        # очередь — иначе один релиз добавлялся дважды.
        already=await _find_new_torrent(before,_magnet_hash(str((row.get("payload") or {}).get("magnetUrl") or "")),tries=4)
        if already:
            log_activity("download",media_title or already.get("name") or "",
                         f"Prowlarr ответил ошибкой ({prowlarr_error}), но задача уже в очереди",True)
            prowlarr_error=""
    if prowlarr_error:
        ok,detail=await _grab_release_direct(row.get("payload") or {},category)
        if not ok:
            return JSONResponse(
                {"ok":False,
                 "error":f"Prowlarr не принял релиз: {prowlarr_error}",
                 "details":f"Прямая загрузка тоже не удалась: {detail}"},status_code=502)
        log_activity("download-direct",media_title or row.get("title") or "",
                     f"Prowlarr: {prowlarr_error}; загружено напрямую",True)

    # Confirm that qBittorrent actually received something.
    detected=None
    for _ in range(10):
        await asyncio.sleep(1)
        qc=await qbit_login()
        if not qc:
            break
        try:
            rr=await qc.get(QBIT_URL+"/api/v2/torrents/info")
            items=rr.json()
            for x in items:
                if x.get("hash") not in before:
                    detected=x
                    break
            if detected:
                await qc.post(
                    QBIT_URL+"/api/v2/torrents/setCategory",
                    data={"hashes":detected.get("hash"),"category":category}
                )
                break
        finally:
            await qc.aclose()

    if detected:
        if category in {"movies","tv","anime"}:
            sn=int(season or 0) or season_number(row.get("title") or detected.get("name") or "")
            with cache_db() as con:
                con.execute("""insert or replace into download_jobs
                    (hash,kind,media_title,season,release_title,category,status,created_at,error)
                    values(?,?,?,?,?,?,?,?,?)""",
                    (detected.get("hash"),category,(media_title or "").strip(),sn,
                     row.get("title") or detected.get("name") or "",category,"downloading",
                     datetime.now(timezone.utc).isoformat(),""))
                con.commit()
        log_activity("download",media_title or detected.get("name") or "Загрузка",row.get("title") or "",True)
        return {
            "ok":True,
            "message":"Загрузка подтверждена. После завершения MediaHub сам перенесёт её в библиотеку и уберёт задачу из qBittorrent.",
            "torrent":detected.get("name"),
            "category":category
        }
    return {
        "ok":True,
        "message":"Prowlarr принял релиз. qBittorrent ещё не подтвердил его появление.",
        "pending":True
    }


@app.post("/api/jellyfin/push-metadata")
async def jellyfin_push_metadata(limit:int=Query(60,ge=1,le=300)):
    """Дослать в Jellyfin карточки, которые он сам не нашёл.

    Jellyfin ищет метаданные только у известных провайдеров, поэтому тайтлы,
    добытые со страницы-источника, остаются без постера и описания. Здесь мы
    сопоставляем элементы Jellyfin с нашей библиотекой по пути на диске и
    заливаем недостающий постер и описание.
    """
    if not JELLYFIN_KEY:
        return JSONResponse({"ok":False,"error":"Jellyfin API key не настроен"},status_code=400)
    headers={"X-Emby-Token":JELLYFIN_KEY}

    # Наши карточки: путь -> данные.
    by_path={}
    with cache_db() as con:
        for row in con.execute("select * from library_cache where path is not null and path!=''").fetchall():
            extra=_json_dict(row["extra_json"])
            poster=row["poster"] or ""
            overview=row["overview"] or ""
            if not poster and not overview:
                continue
            by_path[str(row["path"]).rstrip("/")]={
                "title":row["title"] or "","poster":poster,"overview":overview,
                "year":row["year"] or "","manual":bool(extra.get("manualMetadata") or extra.get("fromPage")),
            }
    # Ручные привязки живут отдельно и должны попасть в Jellyfin даже тогда,
    # когда строку библиотеки успела перезаписать синхронизация.
    for path,meta in manual_meta_index().items():
        if not (meta.get("poster") or meta.get("overview") or meta.get("title")):
            continue
        by_path[path]={"title":meta.get("title") or "","poster":meta.get("poster") or "",
                       "overview":meta.get("overview") or "","year":meta.get("year") or "",
                       "manual":True}
    if not by_path:
        return {"ok":True,"message":"В библиотеке нет карточек с постером или описанием","updated":0}

    updated=0; skipped=0; problems=[]
    try:
        async with httpx.AsyncClient(timeout=30,trust_env=False) as c:
            r=await c.get(JELLYFIN_URL+"/Items",headers=headers,
                          params={"Recursive":"true","IncludeItemTypes":"Series,Movie",
                                  "Fields":"Path,Overview","Limit":str(max(200,limit*4))})
            r.raise_for_status()
            items=(r.json() or {}).get("Items",[])
            for item in items:
                if updated>=limit:
                    break
                path=str(item.get("Path") or "").rstrip("/")
                mine=by_path.get(path)
                if not mine:
                    continue
                has_poster=bool((item.get("ImageTags") or {}).get("Primary"))
                has_overview=bool((item.get("Overview") or "").strip())
                if has_poster and has_overview and not mine["manual"]:
                    skipped+=1; continue
                item_id=item.get("Id")
                changed=False

                if mine["poster"] and (not has_poster or mine["manual"]):
                    try:
                        img=await c.get(mine["poster"],timeout=25,
                                        headers={"User-Agent":"Mozilla/5.0 (compatible; MediaHub)"})
                        if img.status_code<300 and img.content:
                            import base64
                            await c.post(JELLYFIN_URL+f"/Items/{item_id}/Images/Primary",
                                         headers={**headers,"Content-Type":img.headers.get("content-type","image/jpeg")},
                                         content=base64.b64encode(img.content))
                            changed=True
                    except Exception as e:
                        problems.append(f"{mine['title']}: постер — {str(e)[:60]}")

                # Ручную карточку переносим целиком: и название, и описание.
                need_name=bool(mine["manual"] and mine["title"] and mine["title"]!=(item.get("Name") or ""))
                need_overview=bool(mine["overview"] and (not has_overview or mine["manual"]))
                if need_name or need_overview:
                    try:
                        cur=await c.get(JELLYFIN_URL+f"/Items/{item_id}",headers=headers)
                        if cur.status_code<300:
                            payload=cur.json()
                            if need_overview:
                                payload["Overview"]=mine["overview"]
                            if need_name:
                                payload["Name"]=mine["title"]
                                if mine.get("year") and str(mine["year"]).isdigit():
                                    payload["ProductionYear"]=int(mine["year"])
                            await c.post(JELLYFIN_URL+f"/Items/{item_id}",headers={**headers,"Content-Type":"application/json"},
                                         json=payload)
                            changed=True
                    except Exception as e:
                        problems.append(f"{mine['title']}: описание — {str(e)[:60]}")

                if changed:
                    updated+=1
    except Exception as e:
        return JSONResponse({"ok":False,"error":f"Плеер MediaHUB не ответил: {str(e)[:150]}"},status_code=502)

    log_activity("jellyfin-metadata","Карточки в Jellyfin",
                 f"обновлено {updated}, пропущено {skipped}",True)
    return {"ok":True,"updated":updated,"skipped":skipped,"problems":problems[:5],
            "message":(f"Обновлено карточек в Jellyfin: {updated}" if updated
                       else "Все совпавшие карточки в Jellyfin уже заполнены")}


@app.post("/api/jellyfin-anime")
async def jellyfin_anime():
    ANIME_ROOT.mkdir(parents=True,exist_ok=True)
    if not JELLYFIN_KEY:
        return JSONResponse(
            {"ok":False,"error":"Jellyfin API key не настроен. Папка /mnt/media/anime создана."},
            status_code=400)
    headers={"X-Emby-Token":JELLYFIN_KEY}
    try:
        async with httpx.AsyncClient(timeout=20,trust_env=False) as c:
            current=await c.get(JELLYFIN_URL+"/Library/VirtualFolders",headers=headers)
            current.raise_for_status()
            folders=current.json()
            for f in folders:
                locs=f.get("Locations") or []
                if str(ANIME_ROOT) in locs or (f.get("Name") or "").lower() in {"anime","аниме"}:
                    await jellyfin_refresh()
                    return {"ok":True,"message":"Библиотека Anime уже есть в Jellyfin"}
            params=[
                ("name","Anime"),
                ("collectionType","tvshows"),
                ("paths",str(ANIME_ROOT)),
                ("refreshLibrary","true"),
            ]
            r=await c.post(JELLYFIN_URL+"/Library/VirtualFolders",
                           headers=headers,params=params)
            if r.status_code >= 300:
                return JSONResponse(
                    {"ok":False,"error":f"Jellyfin: HTTP {r.status_code}",
                     "details":r.text[:500]},status_code=502)
        return {"ok":True,"message":"Anime добавлена в Jellyfin: /mnt/media/anime"}
    except Exception as e:
        return JSONResponse({"ok":False,"error":str(e)},status_code=500)

@app.get("/api/file-browser")
async def file_browser(path:str=Query("/mnt/media")):
    p=Path(path)
    if not p.is_absolute(): p=MEDIA_ROOT/p
    if not safe_media_path(p): raise HTTPException(400,"Выход за /mnt/media запрещён")
    if not p.exists() or not p.is_dir(): raise HTTPException(404,"Папка не найдена")

    items=[]
    try:
        children=sorted(p.iterdir(),key=lambda x:(not x.is_dir(),x.name.lower()))
    except PermissionError:
        raise HTTPException(403,"Нет доступа к папке")

    protected=protected_media_dirs()

    def suggested_title(entry:Path):
        """Что подставить в поле «Название» при переносе.

        Фильму сразу предлагаем «Название (Год)» — ровно так будет называться
        созданная для него папка, и пользователь видит это до нажатия.
        """
        base=clean_title(entry.name) or entry.name
        if suggest_kind(entry)=="movies":
            return movie_folder_name(base,_fs_library_year(entry.name)) or base
        return base

    for x in children:
        if x.name==".mediahub":
            continue
        if x.is_dir():
            total,videos,files,formats=folder_stats(x)
            label,support,bad=formats_summary(formats)
            items.append({
                "type":"dir","name":x.name,"path":str(x),
                "size":total,"sizeHuman":human(total),
                "videos":videos,"files":files,
                # Формат виден сразу: внутри может лежать то, что Плеер MediaHUB не откроет.
                "format":label,"support":support,
                "supportNote":(f"Не воспроизводится: {', '.join(bad)}" if bad
                               else FORMAT_SUPPORT_NOTE.get(support,"")),
                "suggestedKind":suggest_kind(x),
                "suggestedTitle":suggested_title(x),
                "season":season_number(x.name),
                # Пак из нескольких фильмов: при переносе в «Фильмы» он разложится по карточкам.
                "collection":len(collection_preview_parts(x)) if videos>=2 and suggest_kind(x)=="movies" else 0,
                "inLibrary":already_in_library(x),
                "inInbox":in_inbox(x),
                "canDelete":can_delete_path(x,protected),
            })
        elif x.is_file():
            try:size=x.stat().st_size
            except OSError:size=0
            ext=x.suffix.lower()
            is_video=ext in VIDEO_EXTS
            label,support=format_support(ext)
            if not is_video:
                support="sub" if ext in SUB_EXTS else "other"
            items.append({
                "type":"file","name":x.name,"path":str(x),
                "size":size,"sizeHuman":human(size),
                "video":is_video,
                "format":label,"support":support,
                "supportNote":FORMAT_SUPPORT_NOTE.get(support,""),
                "suggestedKind":suggest_kind(x),
                "suggestedTitle":suggested_title(x),
                "season":season_number(x.name),
                "inLibrary":already_in_library(x),
                # Удалять можно любой файл внутри медиатеки, а не только папки.
                "canDelete":True,
            })

    parent=str(p.parent) if p.resolve()!=MEDIA_ROOT.resolve() else None
    # Папку можно удалить и находясь внутри неё — иначе пустой каталог без
    # строк в списке нечем было убрать.
    return {"current":str(p),"name":p.name,"parent":parent,"items":items,
            "canDelete":can_delete_path(p,protected)}

@app.post("/api/import-folder")
async def import_folder(path:str=Form(...),kind:str=Form(...),title:str=Form(...),season:int=Form(1)):
    src=Path(path)
    if not src.exists() or not src.is_dir(): raise HTTPException(404,"Папка не найдена")
    if not safe_media_path(src): raise HTTPException(400,"Разрешены только папки внутри /mnt/media")
    title=title.strip() or clean_title(src.name)
    if not title: raise HTTPException(400,"Укажи название")
    try:
        dest,imported,modes=import_folder_to_library(src,kind,title,season)
    except RuntimeError as e:
        raise HTTPException(409,str(e))
    await jellyfin_refresh()
    # У каждого фильма коллекции своё название — из имени его папки, а не введённое для всей раздачи.
    for folder,name in ([(Path(f),None) for f in modes["collection"]] if modes.get("collection") else [(dest,title)]):
        try:upsert_live_library_item(kind,folder,name)
        except Exception:pass
    subprocess.Popen(["systemctl","start","mediahub-local-cache.service"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    if modes.get("already"):
        return {"ok":True,"message":"Папка уже находится в медиатеке","dest":str(dest),"modes":modes}
    if modes.get("collection"):
        names=[Path(f).name for f in modes["collection"]]
        words=download_organizer_module().films_word(len(names))
        log_activity("import",title,f"Коллекция: {words} — "+", ".join(names),True)
        return {"ok":True,"message":f"Коллекция разложена: {words}, у каждого своя карточка.","dest":str(dest),"modes":modes}
    log_activity("import",title,str(dest),True)
    return {"ok":True,"message":f"Перемещено в медиатеку: {imported} файлов. Исходная папка удалена.","dest":str(dest),"modes":modes}

def collection_folder(path:str):
    """Папка фильма из медиатеки или загрузок, которую можно разложить как коллекцию."""
    src=Path(path)
    if not src.is_dir():raise HTTPException(404,"Папка не найдена")
    if not safe_media_path(src) or is_media_root(src):raise HTTPException(400,"Разрешены только папки внутри медиатеки")
    return src

def collection_preview_parts(src:Path):
    """Фильмы коллекции в папке, как их увидит раскладка, или []."""
    organizer=download_organizer_module()
    if not organizer:return []
    try:return organizer.collection_parts(src)
    except Exception:return []

@app.get("/api/library/collection-preview")
def library_collection_preview(path:str=Query(...)):
    """Какие фильмы получатся, если разложить папку как коллекцию."""
    parts=collection_preview_parts(collection_folder(path))
    return {"parts":[{"title":p["title"],"year":p["year"],"file":p["file"].name} for p in parts]}

@app.post("/api/library/split-collection")
async def library_split_collection(path:str=Form(...)):
    """Разложить уже скачанную коллекцию: каждому фильму своя папка и карточка."""
    src=collection_folder(path)
    parts=await asyncio.to_thread(collection_preview_parts,src)
    if not parts:raise HTTPException(400,"В папке не найдено нескольких разных фильмов — это не коллекция")
    try:folders=await asyncio.to_thread(download_organizer_module().organize_collection,src,parts,src.name)
    except RuntimeError as e:raise HTTPException(409,str(e))
    save_split_collection(download_organizer_module(),parts,src.name,folders)
    if in_inbox(src.parent):cleanup_empty_inbox_parents(src.parent)
    for f in folders:
        try:upsert_live_library_item("movies",f,None)
        except Exception:pass
    # Старая карточка всей коллекции указывает на папку, которой больше нет.
    with cache_db() as con:
        con.execute("delete from library_cache where kind='movies' and rtrim(path,'/')=? and catalog_source='filesystem'",(str(src).rstrip('/'),))
        con.commit()
    subprocess.Popen(["systemctl","start","mediahub-local-cache.service"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    names=[f.name for f in folders]
    words=download_organizer_module().films_word(len(names))
    log_activity("collection",src.name,f"Разложено: {words} — "+", ".join(names),True)
    return {"ok":True,"message":f"Коллекция разложена: {words}, у каждого своя карточка","folders":[str(f) for f in folders]}

def collection_key(path):
    """Путь проекта в коллекции — тот же вид, что у обхода медиатеки (resolve, без «/» в конце)."""
    try:return str(Path(path).resolve()).rstrip("/")
    except Exception:return str(path or "").rstrip("/")

def move_collection_item(con,old,new):
    """Проект переименовали (new — новый путь) или удалили (new=None): поправить коллекции."""
    old_raw=str(old or "").rstrip("/");old_key=collection_key(old)
    if new:
        con.execute("update or ignore library_collection_items set path=? where path in (?,?)",(collection_key(new),old_raw,old_key))
    con.execute("delete from library_collection_items where path in (?,?)",(old_raw,old_key))
    con.execute("delete from library_collections where id not in (select distinct collection_id from library_collection_items)")

def _collection_organizer():
    organizer=download_organizer_module()
    if not organizer:raise HTTPException(500,"Модуль коллекций не загрузился, подробности в журнале службы")
    return organizer

def _collection_paths(raw:str):
    """Список папок проектов из JSON формы: только существующие папки медиатеки."""
    try:paths=json.loads(raw or "[]")
    except Exception:raise HTTPException(400,"Неверный список проектов")
    if not isinstance(paths,list):raise HTTPException(400,"Неверный список проектов")
    out=[]
    for p in paths[:200]:
        f=Path(str(p or ""))
        if not str(p or "").strip() or not f.exists() or not safe_media_path(f) or is_media_root(f):
            raise HTTPException(400,f"Проект не найден в медиатеке: {p}")
        out.append(collection_key(f))
    return list(dict.fromkeys(out))

@app.get("/api/collections")
def library_collections(kind:str=Query("movies")):
    """Коллекции раздела: состав по порядку и прогресс просмотра каждого проекта."""
    with cache_db() as con:
        cols=con.execute("select * from library_collections where kind=? order by updated_at desc",(kind,)).fetchall()
        out=[]
        for c in cols:
            items=[]
            for r in con.execute("select path from library_collection_items where collection_id=? order by position",(c["id"],)).fetchall():
                hist=con.execute("select position,duration,completed from account_playback_history where user_id=? and project=? order by updated_at desc",(auth_user_id(),r["path"])).fetchall()
                last=hist[0] if hist else None
                items.append({"path":r["path"],"completed":bool(last and last["completed"]),
                              "position":last["position"] if last else 0,"duration":last["duration"] if last else 0,
                              "progress":round(min(100,100*last["position"]/max(1,last["duration"]))) if last else 0,
                              "exists":Path(r["path"]).exists()})
            out.append({"id":c["id"],"kind":c["kind"],"title":c["title"],"items":items,"count":len(items),
                        "watched":sum(1 for x in items if x["completed"]),"updatedAt":c["updated_at"]})
    return {"items":out}

@app.post("/api/collections")
def library_collection_create(kind:str=Form("movies"),title:str=Form(...),paths:str=Form(...)):
    """Собрать коллекцию из выбранных проектов (порядок — как передан)."""
    if kind not in {"movies","tv","anime"}:raise HTTPException(400,"Неизвестный раздел")
    title=" ".join(str(title or "").split())[:120]
    keys=_collection_paths(paths)
    if not title:raise HTTPException(400,"Укажи название коллекции")
    if len(keys)<2:raise HTTPException(400,"В коллекции должно быть хотя бы два проекта")
    with cache_db() as con:
        cid=_collection_organizer().save_collection(con,kind,title,keys);con.commit()
    log_activity("collection",title,f"Собрана коллекция: {len(keys)} проектов",True)
    return {"ok":True,"id":cid,"message":f"Коллекция «{title}» собрана"}

@app.post("/api/collections/{cid}")
def library_collection_update(cid:int,title:str=Form(""),paths:str=Form("")):
    """Переименовать коллекцию и/или задать новый состав и порядок."""
    with cache_db() as con:
        row=con.execute("select * from library_collections where id=?",(cid,)).fetchone()
        if not row:raise HTTPException(404,"Коллекция не найдена")
        title=" ".join(str(title or "").split())[:120]
        if title:
            con.execute("update library_collections set title=? where id=?",(title,cid))
        if paths:
            keys=_collection_paths(paths)
            if len(keys)<2:raise HTTPException(400,"В коллекции должно быть хотя бы два проекта — или разбери её")
            _collection_organizer().set_collection_items(con,cid,keys)
        con.execute("update library_collections set updated_at=? where id=?",(datetime.now(timezone.utc).isoformat(),cid))
        con.commit()
    return {"ok":True,"message":"Коллекция сохранена"}

@app.post("/api/collections/{cid}/delete")
def library_collection_delete(cid:int):
    """Разобрать коллекцию: проекты и файлы остаются в библиотеке как были."""
    with cache_db() as con:
        row=con.execute("select title from library_collections where id=?",(cid,)).fetchone()
        if not row:raise HTTPException(404,"Коллекция не найдена")
        con.execute("delete from library_collection_items where collection_id=?",(cid,))
        con.execute("delete from library_collections where id=?",(cid,))
        con.commit()
    return {"ok":True,"message":f"Коллекция «{row['title']}» разобрана, фильмы остались в библиотеке"}

@app.post("/api/import-file")
async def import_file(path:str=Form(...),kind:str=Form(...),title:str=Form(...),season:int=Form(1)):
    src=Path(path)
    if not src.exists() or not src.is_file(): raise HTTPException(404,"Файл не найден")
    if not safe_media_path(src): raise HTTPException(400,"Разрешены только файлы внутри /mnt/media")
    title=title.strip() or clean_title(src.name)
    if kind=="movies": dest=movie_library_folder(src,title)/src.name
    elif kind=="anime": dest=ANIME_ROOT/title/f"Season {season:02d}"/src.name
    else: dest=TV_ROOT/title/f"Season {season:02d}"/src.name
    # Файл в корне раздела нужно упорядочить, а не считать уже разложенным.
    if already_in_library(src) and not is_media_root(src.parent):
        return {"ok":True,"message":"Файл уже находится в медиатеке","dest":str(src)}
    if src.resolve()==dest.resolve():
        return {"ok":True,"message":"Файл уже лежит в папке тайтла","dest":str(src)}
    try:
        result=move_merge(src,dest)
    except RuntimeError as e:
        raise HTTPException(409,str(e))
    if in_inbox(src.parent):
        cleanup_empty_inbox_parents(src.parent)
    await jellyfin_refresh()
    try:upsert_live_library_item(kind,dest.parent,title)
    except Exception:pass
    subprocess.Popen(["systemctl","start","mediahub-local-cache.service"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    log_activity("import",title,str(dest),True)
    return {"ok":True,"message":"Файл перемещён в медиатеку. Исходник удалён.","dest":str(dest),"modes":result}


@app.post("/api/delete-folder")
async def delete_folder(path:str=Form(...)):
    target=Path(path)
    if not target.exists() and not target.is_symlink():
        raise HTTPException(404,"Файл или папка не найдены")
    if not safe_media_path(target):
        raise HTTPException(400,"Разрешено удаление только внутри /mnt/media")
    if not can_delete_path(target):
        raise HTTPException(400,"Системную папку MediaHub удалять нельзя")

    # Задачи qBittorrent снимаем до удаления файлов, сами файлы клиент не трогает.
    removed_torrents=await qbit_drop_tasks_under(target)

    target_resolved=str(target.resolve())
    parent=str(target.parent)
    is_file=delete_media_tree(target)
    forget_media_path(target_resolved)
    reset_fs_scan_cache()
    await jellyfin_refresh()
    subprocess.Popen(["systemctl","start","mediahub-local-cache.service"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    log_activity("delete",target.name,target_resolved,True)
    return {"ok":True,"message":"Файл удалён" if is_file else "Папка удалена",
            "removedTorrents":removed_torrents,"parent":parent}

@app.get("/api/quality/series")
async def quality_series():
    if not SONARR_KEY:
        return []
    try:
        series=await get_json(SONARR_URL,SONARR_KEY,"/api/v3/series")
    except Exception:
        return []

    out=[]
    for s in series:
        sid=s.get("id")
        if not sid:
            continue
        try:
            eps=await get_json(SONARR_URL,SONARR_KEY,"/api/v3/episode",{"seriesId":sid})
        except Exception:
            eps=[]
        try:
            files=await get_json(SONARR_URL,SONARR_KEY,"/api/v3/episodefile",{"seriesId":sid})
        except Exception:
            files=[]

        file_by_id={f.get("id"):f for f in files}
        seasons={}
        for ep in eps:
            sn=int(ep.get("seasonNumber") or 0)
            if sn<=0:
                continue
            row=seasons.setdefault(sn,{
                "seasonNumber":sn,"episodes":0,"downloaded":0,
                "qualities":[],"cutoffUnmet":0,"monitored":0
            })
            row["episodes"]+=1
            if ep.get("monitored"):
                row["monitored"]+=1
            fid=ep.get("episodeFileId")
            if fid:
                row["downloaded"]+=1
                f=file_by_id.get(fid,{})
                q=((f.get("quality") or {}).get("quality") or {}).get("name") or ""
                if q:
                    row["qualities"].append(q)
                # Sonarr exposes quality cutoff status in several shapes depending version.
                cutoff=bool(
                    f.get("qualityCutoffNotMet")
                    or f.get("cutoffNotMet")
                    or ((f.get("quality") or {}).get("cutoffNotMet"))
                )
                if cutoff:
                    row["cutoffUnmet"]+=1

        season_rows=[]
        for sn,row in sorted(seasons.items()):
            ranks=[quality_rank(q) for q in row["qualities"] if q]
            current=max(ranks) if ranks else 0
            # If Sonarr didn't mark cutoff, use a simple target hint:
            # anything below 1080p is treated as upgrade-eligible in MediaHub UI.
            needs_upgrade = row["cutoffUnmet"]>0 or (row["downloaded"]>0 and current<30)
            season_rows.append({
                **row,
                "currentQuality":quality_label(current),
                "needsUpgrade":needs_upgrade,
                "complete":row["downloaded"]>=row["episodes"] and row["episodes"]>0,
            })

        poster=next((i.get("remoteUrl") for i in s.get("images",[]) if i.get("coverType")=="poster"),None)
        out.append({
            "id":sid,
            "title":s.get("title"),
            "year":s.get("year"),
            "path":s.get("path"),
            "seriesType":s.get("seriesType"),
            "poster":poster,
            "seasons":season_rows,
        })
    return out

@app.post("/api/quality/search-upgrades")
async def quality_search_upgrades(series_id:int=Form(...),season_number:int=Form(...)):
    if not SONARR_KEY:
        raise HTTPException(400,"Sonarr API key не найден")
    # Sonarr's SeasonSearch command is the safest way to ask Sonarr to find
    # better/missing releases while keeping quality profiles/cutoffs in control.
    data={"name":"SeasonSearch","seriesId":series_id,"seasonNumber":season_number}
    try:
        result=await post_json(SONARR_URL,SONARR_KEY,"/api/v3/command",data)
        return {"ok":True,"message":"Sonarr начал поиск сезона","command":result}
    except Exception as e:
        return JSONResponse({"ok":False,"error":str(e)},status_code=500)

@app.get("/api/downloads")
async def downloads():
    c=await qbit_login()
    if not c:return {"configured":False,"items":[]}
    meta={}
    try:
        with cache_db() as con:
            for row in con.execute("select * from download_meta").fetchall():
                meta[str(row["hash"] or "").lower()]=dict(row)
    except Exception:
        meta={}
    try:
        r=await c.get(QBIT_URL+"/api/v2/torrents/info")
        return {"configured":True,"items":[{
            "meta":meta.get(str(x.get("hash") or "").lower()) or None,
            "hash":x.get("hash"),"name":x.get("name"),"state":x.get("state"),
            "progress":round((x.get("progress") or 0)*100,1),
            "dlspeed":x.get("dlspeed",0),"upspeed":x.get("upspeed",0),
            "size":x.get("size",0),"amount_left":x.get("amount_left",0),"eta":x.get("eta",0),
            "seeders":x.get("num_seeds",0),"peers":x.get("num_leechs",0),"ratio":round(float(x.get("ratio") or 0),2),
            "category":x.get("category",""),"save_path":x.get("save_path","")
        } for x in r.json()]}
    finally: await c.aclose()

# --- v21.3 свой торрент ------------------------------------------------------
# Пользователь может добавить собственный .torrent или magnet-ссылку: MediaHub
# кладёт задачу в qBittorrent с выбранной категорией и регистрирует её в
# download_jobs, поэтому после скачивания organizer переносит файлы в медиатеку
# так же, как для релизов, найденных через Prowlarr.

TORRENT_CATEGORIES={"movies","tv","anime","manual"}

def _blocked_fetch_host(url:str):
    """Не даём тянуть локальные адреса по ссылке из интерфейса."""
    try:
        from urllib.parse import urlparse
        host=(urlparse(url).hostname or "").strip()
    except Exception:
        return True
    if not host:
        return True
    if host.lower() in {"localhost","localhost.localdomain"}:
        return True
    try:
        addr=ipaddress.ip_address(host)
        return addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved
    except ValueError:
        return False


SITE_TITLE_NOISE=re.compile(
    r"(скачать|смотреть\s+онлайн|смотри\s+аниме|мобильное\s+аниме|торрент|torrent|"
    r"бесплатно|в\s+хорошем\s+качестве|hd\s*720|hd\s*1080|онлайн)",re.I)

def clean_page_title(raw):
    """Название тайтла из заголовка страницы.

    Сайты пишут в <title> всё сразу: оригинал, перевод, имя площадки и рекламу
    («… » AnimeMobi — мобильное аниме! Скачай и Смотри Аниме везде!»). Берём
    из этого только само название, предпочитая русскую часть.
    """
    title=" ".join(str(raw or "").split())
    if not title:
        return ""
    # Всё после названия площадки — мусор.
    title=re.split(r"\s*[»«|]\s*",title)[0]
    title=re.sub(r"\s*[-–—]\s*[^-–—]*(?:онлайн|аниме|сериал[ыа]?|фильм[ыы]?|портал|сайт)[^-–—]*$","",title,flags=re.I)
    # «Оригинал / Русское название (RUS)» — оставляем русскую часть.
    parts=[p.strip() for p in re.split(r"\s*/\s*",title) if p.strip()]
    if len(parts)>1:
        russian=[p for p in parts if re.search(r"[а-яё]",p,re.I)]
        title=(russian[0] if russian else parts[0])
    title=re.sub(r"\((?:rus|jap|eng|sub|dub)[^)]*\)","",title,flags=re.I)
    title=re.sub(r"\s*\((19|20)\d{2}\)\s*","",title)
    title=re.sub(r"\s*\[[^\]]*\]\s*"," ",title)
    # Рекламные хвосты после восклицательного знака или тире.
    for chunk in re.split(r"(?<=[!?.])\s+",title):
        if SITE_TITLE_NOISE.search(chunk) and len(chunk)<len(title):
            title=title.replace(chunk,"")
    title=SITE_TITLE_NOISE.sub("",title)
    title=" ".join(title.split()).strip(" -–—|:,.")
    return title[:160]


def page_media_metadata(soup,html,url):
    """Сопоставить JSON-LD, OpenGraph, подписи полей и содержимое страницы проекта."""
    from urllib.parse import urljoin,urlparse
    from html import unescape
    meta={"title":"","year":"","poster":"","overview":"","sourceUrl":url,"genres":[],"rating":None,"episodes":0,"season":0}
    def text(value):
        """Очистить значение поля от разметки и лишних пробелов."""
        return " ".join(unescape(re.sub(r"<[^>]+>"," ",str(value or ""))).split())
    def tag_value(prop):
        """Прочитать метатег по имени или свойству."""
        tag=soup.find("meta",attrs={"property":prop}) or soup.find("meta",attrs={"name":prop})
        return tag.get("content","").strip() if tag else ""
    meta.update(title=tag_value("og:title") or tag_value("twitter:title"),
                overview=tag_value("og:description") or tag_value("description") or tag_value("twitter:description"),
                poster=tag_value("og:image") or tag_value("twitter:image"))
    content=soup.select_one("#details, .fullstory, .full-story, [itemtype*='Movie'], [itemtype*='TVSeries'], article, main") or soup
    # Поля «Название», «Год выхода», «О фильме» точнее рекламных заголовков.
    lines=[]
    for line in content.get_text("\n",strip=True).splitlines():
        line=text(line)
        if line:lines.append(line)
    labels={"title":r"(?:название|русское название|name|title)","year":r"(?:год(?: выхода| выпуска)?|year|release year)",
            "overview":r"(?:о фильме|о сериале|об аниме|описание|сюжет|description|synopsis|plot)","genres":r"(?:жанр[ы]?|genres?)"}
    fields={}
    for i,line in enumerate(lines):
        for key,pattern in labels.items():
            match=re.fullmatch(pattern+r"\s*[:：]\s*(.*)",line,re.I)
            if not match:continue
            value=match.group(1).strip()
            if not value and i+1<len(lines):value=lines[i+1]
            if value and key not in fields:fields[key]=value
    # Предпочитаем объект произведения; Organization/WebSite не описывают фильм.
    media=[]
    for script in soup.find_all("script",attrs={"type":"application/ld+json"})[:12]:
        try:queue=[json.loads(script.string or script.get_text() or "{}")]
        except Exception:continue
        checked=0
        while queue and checked<100:
            obj=queue.pop(0);checked+=1
            if isinstance(obj,list):queue.extend(obj);continue
            if not isinstance(obj,dict):continue
            types=obj.get("@type") or [];types=[types] if isinstance(types,str) else types
            if any(t in {"Movie","TVSeries","TVSeason","TVEpisode","CreativeWork","VideoObject","Anime"} for t in types):media.append(obj)
            for key in ("@graph","mainEntity","itemListElement","item"):
                value=obj.get(key)
                if isinstance(value,(dict,list)):queue.append(value)
    for obj in media[:1]:
        if obj.get("name"):meta["title"]=text(obj["name"])
        if obj.get("description"):meta["overview"]=text(obj["description"])
        image=obj.get("image") or obj.get("thumbnailUrl")
        if isinstance(image,list):image=image[0] if image else ""
        if isinstance(image,dict):image=image.get("url") or image.get("contentUrl")
        if isinstance(image,str):meta["poster"]=image
        date=str(obj.get("datePublished") or obj.get("dateCreated") or "")
        if re.match(r"^(?:19|20)\d{2}",date):meta["year"]=date[:4]
        genre=obj.get("genre")
        if genre:meta["genres"]=[text(g) for g in (genre if isinstance(genre,list) else [genre])][:10]
        rating=obj.get("aggregateRating")
        if isinstance(rating,dict):
            try:
                value=float(str(rating.get("ratingValue")).replace(",","."));best=float(rating.get("bestRating") or 10)
                if 0<value<=best:meta["rating"]=round(value*10/best,1)
            except (ValueError,TypeError,ZeroDivisionError):pass
        for key,field in (("numberOfEpisodes","episodes"),("seasonNumber","season")):
            try:meta[field]=max(0,min(2000,int(obj.get(key) or 0)))
            except (ValueError,TypeError):pass
    if fields.get("title"):meta["title"]=fields["title"]
    if fields.get("year"):
        match=re.search(r"\b(?:19|20)\d{2}\b",fields["year"])
        if match:meta["year"]=match.group(0)
    if fields.get("overview") and (not meta["overview"] or len(fields["overview"])>len(meta["overview"])):meta["overview"]=fields["overview"]
    if fields.get("genres") and not meta["genres"]:
        genre_label=content.find(string=re.compile(r"^\s*(?:жанры?|genres?)\s*[:：]",re.I))
        genre_text=genre_label.parent.parent.get_text(" ",strip=True) if genre_label and genre_label.parent.parent else fields["genres"]
        genre_text=re.sub(r"^.*?(?:жанры?|genres?)\s*[:：]","",genre_text,flags=re.I)
        genre_text=re.split(r"(?:режисс[её]р|director|страна|country|о фильме)\s*:",genre_text,flags=re.I)[0]
        if len(genre_text)>200:genre_text=fields["genres"]
        meta["genres"]=[x.strip() for x in re.split(r"[,;/]",genre_text) if x.strip()][:10]
    heading=content.select_one("h1, #news-title, [itemprop='name'], h2") or soup.find("h1")
    heading_text=heading.get_text(" ",strip=True) if heading else ""
    if not meta["title"]:meta["title"]=heading_text or (soup.title.get_text() if soup.title else "")
    raw_title=re.sub(r"^\s*[a-z0-9.-]+\s*::\s*","",meta["title"],flags=re.I)
    if not meta["year"]:
        match=re.search(r"\b(?:19|20)\d{2}\b",raw_title) or re.search(r"\b(?:19|20)\d{2}\b",meta["overview"])
        if match:meta["year"]=match.group(0)
    meta["title"]=clean_page_title(raw_title)
    meta["title"]=re.sub(r"\s+(?:WEB[- ]?(?:DL|Rip)|BDRip|BluRay|HDTV|DVD[- ]?Rip|\d{3,4}p)\b.*$","",meta["title"],flags=re.I).strip()
    body=" ".join(lines)[:180000]
    if not meta["overview"]:
        candidates=content.select("[itemprop='description'], .description, .synopsis, .plot, .full-text, p")
        paragraphs=[text(x.get_text(" ",strip=True)) for x in candidates]
        paragraphs=[x for x in paragraphs if 100<=len(x)<=5000 and not re.search(r"(?:cookie|авторизац|зарегистр|права защищены)",x,re.I)]
        if paragraphs:meta["overview"]=max(paragraphs,key=len)
    meta["overview"]=text(meta["overview"])[:2000]
    if not meta["poster"]:
        images=[]
        for img in content.find_all("img"):
            src=img.get("data-src") or img.get("data-original") or img.get("src") or ""
            desc=" ".join([src,img.get("alt", "")," ".join(img.get("class") or [])]).lower()
            if not src or re.search(r"logo|banner|avatar|smil|icon|thumbs|screenshot|реклам",desc):continue
            score=10 if re.search(r"poster|cover|постер|обложк|itemprop",desc) or img.get("itemprop")=="image" else 0
            images.append((score,-len(images),src))
        if images:meta["poster"]=max(images)[2]
    if meta["poster"]:
        meta["poster"]=urljoin(url,meta["poster"])
        if urlparse(meta["poster"]).scheme not in {"http","https"}:meta["poster"]=""
    if not meta["episodes"]:
        match=re.search(r"(?:эпизод(?:ы|ов)?|серии|серий|episodes)\s*[:—-]?\s*(\d{1,4})|(?:\(|\b)(\d{1,4})\s*(?:эп\.|серий|episodes)",body,re.I)
        if match:meta["episodes"]=int(match.group(1) or match.group(2))
    if not meta["season"]:
        match=re.search(r"\bS(\d{1,2})\b|(?:сезон|season)\s*[:#]?\s*(\d{1,2})",heading_text or (soup.title.get_text() if soup.title else raw_title),re.I)
        if match:meta["season"]=int(match.group(1) or match.group(2))
    meta["suggestedCategory"]="anime" if re.search(r"(?:аниме|anime)",body[:30000],re.I) else "tv" if meta["season"] or re.search(r"(?:сериал|TVSeries)",body[:30000],re.I) else "movies"
    meta["missingFields"]=[key for key in ("title","year","poster","overview") if not meta.get(key)]
    return meta


PAGE_IMPORT_CACHE={}


def page_link_candidates(soup,html,url):
    """Найти раздачи в ссылках, атрибутах кнопок и строках скриптов без исполнения JS."""
    from urllib.parse import urljoin,urlparse,parse_qs,unquote
    from html import unescape
    found=[]; seen=set()
    def add(raw,label="",context=""):
        """Проверить кандидат и добавить уникальную ссылку с подписью."""
        raw=unescape(str(raw or "")).strip().replace("\\/","/")
        if raw.lower().startswith("magnet:?"):
            query=parse_qs(urlparse(raw).query)
            hashes=[v for v in query.get("xt",[]) if v.lower().startswith(("urn:btih:","urn:btmh:"))]
            if not hashes:return
            key="magnet:"+"|".join(hashes).lower()
            name=(query.get("dn") or [""])[0]
            if re.fullmatch(r"[\w.-]+\.(?:info|org|com|net|ru)",name,re.I):name=""
            full=raw;kind="magnet"
        else:
            full=urljoin(url,raw); parts=urlparse(full)
            if parts.scheme not in {"http","https"} or _blocked_fetch_host(full):return
            low=unquote(full).lower()
            explicit=bool(re.search(r"\.torrent(?:[?&#]|$)|/(?:download|dl|d)(?:/[\w.-]+|[?])|(?:download|dl|attach|attachment|get_torrent)\.php|[?&](?:do|action)=download(?:&|$)",low))
            labelled=bool(re.search(r"(?:скачать\s+торрент|download\s+torrent|\.torrent)",label,re.I))
            if not explicit and not labelled:return
            if re.search(r"(?:utorrent|bittorrent)\.com|play\.google\.com",parts.netloc,re.I):return
            key=full;name="";kind="torrent"
        if key in seen:return
        seen.add(key)
        fallback=parts.path.rsplit("/",1)[-1] if kind=="torrent" else "Magnet-ссылка"
        title=" ".join((name or label or context or fallback).split())[:200]
        info=" ".join(context.split())[:300]
        found.append({"type":kind,"url":full,"title":title,"context":info})
    for tag in soup.find_all(["a","button","input"]):
        label=tag.get_text(" ",strip=True) or tag.get("title") or tag.get("value") or ""
        parent=tag.find_parent(["tr","p","li"])
        context=parent.get_text(" ",strip=True) if parent else label
        for attr in ("href","data-href","data-url","data-download","data-magnet","data-clipboard-text","data-torrent","value"):
            if tag.get(attr):add(tag[attr],label,context)
        for raw in re.findall(r"[\"']((?:https?://|/|magnet:\?)[^\"']+)[\"']",tag.get("onclick") or ""):
            add(raw,label,context)
    # Строковые URL часто лежат в JSON/JS; код страницы не запускается.
    decoded=unescape(html).replace("\\/","/")
    decoded=re.sub(r"\\u([0-9a-fA-F]{4})",lambda m:chr(int(m.group(1),16)),decoded)
    for raw in re.findall(r"magnet:\?[^\s\"'<>]+",decoded):add(raw)
    for raw in re.findall(r"[\"']((?:https?://|//|/)[^\s\"'<>]+)[\"']",decoded):add(raw)
    return found[:80],len(found)>80


PAGE_DNS_CACHE={}


def page_dns_failure(error):
    """Отличить отказ DNS от ошибок соединения, сертификата и сервера."""
    import socket
    seen=set()
    while error and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error,socket.gaierror):return True
        if re.search(r"name or service not known|nodename nor servname|getaddrinfo failed|temporary failure in name resolution",str(error),re.I):return True
        error=error.__cause__ or error.__context__
    return False


async def resolve_page_dns(host):
    """Получить только публичные IPv4 через DNS-over-HTTPS, кэшируя ответ по TTL."""
    hit=PAGE_DNS_CACHE.get(host)
    if hit and hit[0]>time.monotonic():return hit[1]
    async with httpx.AsyncClient(timeout=8,trust_env=False,follow_redirects=False) as resolver:
        for endpoint in ("https://dns.google/resolve","https://cloudflare-dns.com/dns-query"):
            try:
                response=await resolver.get(endpoint,params={"name":host,"type":"A"},headers={"Accept":"application/dns-json"})
                response.raise_for_status();payload=response.json()
                if payload.get("Status")!=0:continue
                addresses=[];ttl=300
                for answer in payload.get("Answer",[]):
                    if answer.get("type")!=1:continue
                    address=ipaddress.ip_address(answer.get("data") or "")
                    if address.version!=4 or not address.is_global:continue
                    addresses.append(str(address));ttl=min(ttl,int(answer.get("TTL") or 60))
                if addresses:
                    if len(PAGE_DNS_CACHE)>=256:PAGE_DNS_CACHE.pop(next(iter(PAGE_DNS_CACHE)))
                    PAGE_DNS_CACHE[host]=(time.monotonic()+max(30,min(ttl,3600)),addresses[:4])
                    return addresses[:4]
            except Exception:continue
    raise ValueError(f"DNS сервера не определяет адрес {host}. Резервный DNS тоже недоступен. Проверьте сеть или прокси в настройках подключений.")


class PageDnsTransport(httpx.AsyncHTTPTransport):
    """При отказе системного DNS повторить запрос по DoH, сохранив Host и проверку TLS."""
    def __init__(self,local_address=None):
        """Создать отдельные пулы для разных имён сайтов и резервных адресов."""
        super().__init__(retries=0,local_address=local_address)
        self.local_address=local_address
        self.resolved_transports={}

    async def handle_async_request(self,request):
        """Сохранить исходный URL и SNI при подключении к резервному IP."""
        try:return await super().handle_async_request(request)
        except httpx.ConnectError as error:
            if not page_dns_failure(error):raise
        host=request.url.host
        addresses=await resolve_page_dns(host)
        last=None
        for address in addresses:
            key=(host,address,request.url.port)
            if key not in self.resolved_transports:self.resolved_transports[key]=httpx.AsyncHTTPTransport(retries=0,local_address=self.local_address)
            transport=self.resolved_transports[key]
            extensions=dict(request.extensions,sni_hostname=host)
            forwarded=httpx.Request(request.method,request.url.copy_with(host=address),
                                    headers=request.headers,stream=request.stream,extensions=extensions)
            try:return await transport.handle_async_request(forwarded)
            except (httpx.ConnectError,httpx.ConnectTimeout) as error:last=error
        if last:raise last
        raise ValueError(f"Не удалось подключиться к {host}")

    async def aclose(self):
        """Закрыть основной и все резервные пулы соединений."""
        await super().aclose()
        for transport in self.resolved_transports.values():await transport.aclose()


def page_outbound_proxy():
    """Отдельный канал для импорта страниц; настройки остальных каталогов не меняются."""
    return _persistent_value("MEDIAHUB_PAGE_PROXY").strip()


async def fetch_page_resource(url,referer="",limit=4*1024*1024):
    """Получить публичный ресурс с ограничением размера и проверкой каждого перенаправления.

    Если сайт не отвечает напрямую, пробуем ещё раз только по IPv4 (битый IPv6
    у провайдера даёт ровно такой зависший запрос) и через внешний прокси
    MediaHub, если он настроен.
    """
    attempts=[{"transport":PageDnsTransport()},{"transport":PageDnsTransport(local_address="0.0.0.0")}]
    proxy=outbound_proxy()
    if proxy:
        attempts.append({"proxy":proxy})
    page_proxy=page_outbound_proxy()
    if page_proxy:
        attempts.insert(0,{"proxy":page_proxy})
    last=None
    for extra in attempts:
        try:
            return await _fetch_page_once(url,referer,limit,extra)
        except httpx.TransportError as error:
            last=error
    raise last


def page_fetch_error_text(error):
    """Понятная причина, почему сервер не открыл страницу (у таймаутов str() пустой)."""
    if isinstance(error,httpx.TimeoutException):
        return "сайт не ответил серверу MediaHub вовремя — с сервера он недоступен, хотя в браузере может открываться"
    if isinstance(error,httpx.ConnectError):
        return "сервер MediaHub не смог подключиться к сайту"
    return str(error)[:180] or type(error).__name__


async def _fetch_page_once(url,referer,limit,client_extra):
    from urllib.parse import urljoin,urlparse
    headers={"User-Agent":"Mozilla/5.0 (compatible; MediaHub)","Accept-Language":"ru,en;q=0.8"}
    if referer:headers["Referer"]=referer
    async with httpx.AsyncClient(timeout=httpx.Timeout(20,connect=7),trust_env=False,follow_redirects=False,**client_extra) as client:
        for _ in range(6):
            if urlparse(url).scheme not in {"http","https"} or _blocked_fetch_host(url):
                raise ValueError("такой адрес открывать нельзя")
            async with client.stream("GET",url,headers=headers) as response:
                if response.status_code in {301,302,303,307,308}:
                    location=response.headers.get("location") or ""
                    if location.lower().startswith("magnet:"):
                        return None,location
                    if not location:raise ValueError("пустое перенаправление")
                    url=urljoin(str(response.url),location);continue
                if response.status_code>=400:raise ValueError(f"источник ответил HTTP {response.status_code}")
                data=bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data)>limit:raise ValueError("ресурс слишком большой")
                headers=dict(response.headers);headers.pop("content-encoding",None);headers.pop("content-length",None)
                return httpx.Response(response.status_code,headers=headers,content=bytes(data),request=response.request),""
    raise ValueError("слишком много перенаправлений")


def decode_page_html(response):
    """Учесть UTF-8 и старые кодировки трекеров, чтобы названия не превращались в мусор."""
    head=response.content[:8000]
    match=re.search(br"charset\s*=\s*[\"']?([a-zA-Z0-9_-]+)",head,re.I)
    encoding=match.group(1).decode("ascii") if match else response.encoding
    try:return response.content.decode(encoding or "utf-8")
    except (UnicodeDecodeError,LookupError):
        try:return response.content.decode("utf-8")
        except UnicodeDecodeError:return response.content.decode("cp1251",errors="replace")


async def torrent_links_from_page(url:str,return_meta=False):
    """Получить данные страницы и все доступные варианты торрентов и magnet-ссылок."""
    empty={"title":"","year":"","poster":"","overview":"","sourceUrl":url}
    def fail(reason):
        """Сохранить единый формат ответа при недоступности страницы."""
        return ([],dict(empty,error=reason)) if return_meta else ([],reason)
    try:
        from bs4 import BeautifulSoup
        response,magnet=await fetch_page_resource(url)
        if magnet:
            links=[{"type":"magnet","url":magnet,"title":_magnet_name(magnet)}]
            return (links,empty) if return_meta else (links,"")
        if "torrent" in response.headers.get("content-type","").lower() or response.content.startswith(b"d") and not response.content.startswith(b"doctype"):
            links=[{"type":"torrent","url":str(response.url),"title":"Файл .torrent"}]
            return (links,empty) if return_meta else (links,"")
        html=decode_page_html(response);soup=BeautifulSoup(html,"html.parser")
        meta=page_media_metadata(soup,html,str(response.url));meta["sourceUrl"]=url
        links,truncated=page_link_candidates(soup,html,str(response.url))
        if truncated:meta["warning"]="Показаны первые 80 ссылок; на странице есть ещё варианты"
        if not links:meta["error"]="На странице нет открытых ссылок на торрент. Возможно, требуется вход или ссылки появляются только после выполнения JavaScript. Можно приложить свой .torrent."
        return (links,meta) if return_meta else (links,meta.get("title") or meta.get("error") or "")
    except Exception as error:
        return fail(f"страница недоступна: {page_fetch_error_text(error)}")


async def scan_feed_cards(url:str):
    """Собрать со страницы карточки-новинки: ссылка, картинка, подпись.

    Парсер намеренно строгий: карточкой считается только ссылка с картинкой и
    внятной подписью. Пункты меню вроде «Жанры» или «Расписание» так отсеиваются
    — на этом в своё время сгорела полка AniLiberty.
    """
    from urllib.parse import urljoin,urlparse
    # Все ветки возвращают (карточки, метаданные страницы); ошибка лежит в meta.
    def fail(reason):
        return [],{"title":"","year":"","poster":"","overview":"","sourceUrl":url,"error":reason}
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return fail("на сервере нет библиотеки beautifulsoup4")
    if _blocked_fetch_host(url):
        return fail("такой адрес открывать нельзя")
    headers={"User-Agent":"Mozilla/5.0 (compatible; MediaHub)","Accept-Language":"ru,en;q=0.8"}
    try:
        async with httpx.AsyncClient(timeout=30,trust_env=False,follow_redirects=True) as c:
            r=await c.get(url,headers=headers)
        if r.status_code>=400:
            return fail(f"страница ответила HTTP {r.status_code}")
    except Exception as e:
        return fail(f"страница недоступна: {str(e)[:120]}")

    soup=BeautifulSoup(r.text,"html.parser")
    meta=page_media_metadata(soup,r.text,url)
    base_host=(urlparse(url).hostname or "").lower()
    cards=[]; seen=set()
    for a in soup.find_all("a",href=True):
        href=(a["href"] or "").strip()
        if not href or href.startswith(("#","javascript:","mailto:")):
            continue
        full=urljoin(url,href)
        if (urlparse(full).hostname or "").lower()!=base_host:
            continue
        img=a.find("img")
        if not img:
            continue
        poster=img.get("src") or img.get("data-src") or img.get("data-original") or ""
        if not poster:
            continue
        title=" ".join((a.get("title") or img.get("alt") or a.get_text() or "").split())
        if len(title)<8 or len(title)>200:
            continue
        # Служебные разделы обычно короткие и без года.
        if re.fullmatch(r"(жанры|расписание|новые эпизоды|франшизы|каталог|главная|аниме|фильмы|сериалы)",title,re.I):
            continue
        # Отсекаем всё, что не карточка тайтла: аватары, профили, теги, поиск,
        # комментарии, кнопки-действия. Раньше в ленту попадали ники людей.
        low_full=full.lower(); low_poster=(poster or "").lower()
        if re.search(r"/(user|users|profile|member|login|register|tag|tags|search|page|comment|comments|feedback|rules)(/|$|\?)",low_full):
            continue
        if re.search(r"(avatar|noavatar|no_avatar|userpic|profile|smile|emoji|icon|logo|banner)",low_poster):
            continue
        if re.fullmatch(r"[a-z0-9_.\-]{2,30}",title,re.I) and " " not in title:
            continue
        if re.fullmatch(r"(смотреть|подробнее|читать далее|скачать|комментарии|войти|регистрация|ещё|еще|далее)[\s.!]*",title,re.I):
            continue
        if full in seen:
            continue
        seen.add(full)
        year=""
        m=re.search(r"\b(19|20)\d{2}\b",title)
        if m: year=m.group(0)
        cards.append({"title":title[:160],"url":full,"poster":urljoin(url,poster),"year":year})
        if len(cards)>=60:
            break
    if not cards:
        return [],dict(meta,error="карточек с картинками на странице не нашлось")
    return cards,meta


async def fetch_torrent_bytes(url:str,referer=""):
    """Скачать выбранный торрент с Referer и ограничением размера, либо вернуть magnet."""
    try:
        response,magnet=await fetch_page_resource(url,referer=referer,limit=20*1024*1024)
        if magnet:return b"",magnet,""
        content=response.content
        if not content.startswith(b"d") or content.startswith(b"doctype"):
            page=decode_page_html(response)
            if re.search(r"(?:зарегистрирован|авторизац|войти на сайт)",page,re.I):
                return b"","","Сайт требует входа для скачивания. Приложите .torrent, скачанный после входа, вместе со ссылкой на страницу."
            return b"","","Вместо торрента сайт вернул страницу. Возможно, нужен вход, CAPTCHA или подтверждение скачивания."
        return content,"",""
    except Exception as error:return b"","",f"Не удалось скачать торрент: {str(error)[:180]}"


def urljoin_safe(base,link):
    try:
        from urllib.parse import urljoin
        return urljoin(base,link)
    except Exception:
        return link


def _magnet_hash(magnet:str):
    m=re.search(r"xt=urn:btih:([0-9a-zA-Z]{32,40})",magnet or "",re.I)
    return m.group(1).lower() if m else ""

def _magnet_name(magnet:str):
    m=re.search(r"[?&]dn=([^&]+)",magnet or "")
    if not m:
        return ""
    try:
        from urllib.parse import unquote_plus
        return unquote_plus(m.group(1))
    except Exception:
        return ""

@app.post("/api/torrent-page-preview")
async def torrent_page_preview(page_url:str=Form(...)):
    """Показать данные страницы и варианты раздач, сохранив их для выбранной загрузки."""
    import secrets
    url=page_url.strip()
    if not url.lower().startswith(("http://","https://")):
        raise HTTPException(400,"Нужна ссылка http или https на страницу проекта")
    links,meta=await torrent_links_from_page(url,return_meta=True)
    if not meta.get("title") and meta.get("error"):
        raise HTTPException(400,meta["error"])
    now=time.monotonic()
    for key in list(PAGE_IMPORT_CACHE):
        if now-PAGE_IMPORT_CACHE[key][0]>1800:PAGE_IMPORT_CACHE.pop(key,None)
    while len(PAGE_IMPORT_CACHE)>=128:PAGE_IMPORT_CACHE.pop(next(iter(PAGE_IMPORT_CACHE)))
    token=secrets.token_urlsafe(24);PAGE_IMPORT_CACHE[token]=(now,url,links,meta)
    return {"meta":meta,"links":links,"pageToken":token,"torrentCount":len(links),
            "message":meta.get("error") or meta.get("warning") or f"Найдено ссылок: {len(links)}. Выберите нужную раздачу."}


@app.post("/api/torrent-upload")
async def torrent_upload(
    file:UploadFile|None=File(None),
    magnet:str=Form(""),
    page_url:str=Form(""),
    category:str=Form("manual"),
    media_title:str=Form(""),
    season:int=Form(0),
    paused:str=Form("0"),
    selected_url:str=Form(""),
    page_token:str=Form(""),
):
    """Добавить торрент со страницы или файла с данными проекта для автоимпорта."""
    magnet=(magnet or "").strip()
    page_url=(page_url or "").strip()
    if category not in TORRENT_CATEGORIES:
        raise HTTPException(400,"Неизвестная категория загрузки")

    if not 0<=season<=99:
        raise HTTPException(400,"Сезон должен быть от 0 до 99")
    content=b""
    filename=""
    page_note=""
    page_meta={}
    # Ссылку можно указать и отдельным полем, и вместо magnet.
    if not page_url and magnet.lower().startswith(("http://","https://")):
        page_url=magnet;magnet=""
    if file is not None and getattr(file,"filename",""):
        filename=file.filename
        content=await file.read()
        if not content:
            raise HTTPException(400,"Файл пустой")
        if len(content)>20*1024*1024:
            raise HTTPException(400,"Файл больше 20 МБ — это не торрент-файл")
        if not content.startswith(b"d"):
            raise HTTPException(400,"Это не похоже на .torrent файл")
    elif page_url and not magnet.lower().startswith("magnet:"):
        snapshot=PAGE_IMPORT_CACHE.get(page_token) if page_token else None
        if page_token and (not snapshot or time.monotonic()-snapshot[0]>1800 or snapshot[1]!=page_url):
            raise HTTPException(409,"Предпросмотр устарел. Получите данные страницы снова.")
        if snapshot:links,page_meta=snapshot[2],snapshot[3]
        else:links,page_meta=await torrent_links_from_page(page_url,return_meta=True)
        if not links:
            raise HTTPException(400,page_meta.get("error") or "Не удалось найти торрент")
        chosen=next((link for link in links if link["url"]==selected_url),None) if selected_url else None
        if selected_url and not chosen:raise HTTPException(400,"Выбранная ссылка отсутствует в списке страницы")
        if not chosen and len(links)>1:
            return JSONResponse({"detail":"На странице несколько раздач. Получите данные страницы и выберите нужную ссылку.","links":links},status_code=409)
        chosen=chosen or links[0]
        if chosen["type"]=="magnet":magnet=chosen["url"]
        else:
            content,found_magnet,error=await fetch_torrent_bytes(chosen["url"],referer=page_url)
            if found_magnet:magnet=found_magnet
            if not content and not magnet:raise HTTPException(400,error or "Не удалось скачать выбранный торрент")
            filename=re.sub(r"[\\/:*?\"<>|]","-",chosen.get("title") or "release")[:100]
            if not filename.lower().endswith(".torrent"):filename+=".torrent"
    elif magnet:
        if not magnet.lower().startswith("magnet:"):
            raise HTTPException(400,"Ссылка должна начинаться с magnet: или http")
    else:
        raise HTTPException(400,"Нужен .torrent файл, magnet-ссылка или ссылка на страницу раздачи")

    # Метаданные страницы нужны и тогда, когда торрент пришёл файлом или
    # magnet-ссылкой, а страница указана только ради описания и постера.
    if page_url and not page_meta:
        try:
            _links,page_meta=await torrent_links_from_page(page_url,return_meta=True)
        except Exception:
            page_meta={}
    if isinstance(page_meta,dict) and page_meta.get("title"):
        page_note=page_meta["title"]
        if not media_title.strip():
            media_title=page_note

    c=await qbit_login()
    if not c:
        raise HTTPException(400,"qBittorrent не настроен или недоступен")
    try:
        try:
            r=await c.get(QBIT_URL+"/api/v2/torrents/info")
            before={x.get("hash") for x in r.json()}
        except Exception:
            before=set()

        # Путь не задаём: download_organizer держит у категорий movies/tv/anime/manual
        # свои savePath внутри inbox, и qBittorrent применит их сам.
        data={"category":category,"paused":"true" if paused=="1" else "false"}
        try:
            if content:
                r=await c.post(QBIT_URL+"/api/v2/torrents/add",data=data,
                               files={"torrents":(filename or "upload.torrent",content,"application/x-bittorrent")})
            else:
                r=await c.post(QBIT_URL+"/api/v2/torrents/add",data={**data,"urls":magnet})
        except Exception as e:
            raise HTTPException(502,f"qBittorrent не ответил: {e}")
        if r.status_code>=300 or (r.text or "").strip().lower().startswith("fails"):
            raise HTTPException(502,f"qBittorrent отклонил торрент: HTTP {r.status_code} {r.text[:200]}")

        detected=None
        want_hash=_magnet_hash(magnet)
        for _ in range(8):
            await asyncio.sleep(1)
            try:
                rr=await c.get(QBIT_URL+"/api/v2/torrents/info")
                items=rr.json()
            except Exception:
                continue
            for x in items:
                h=str(x.get("hash") or "").lower()
                if (want_hash and h==want_hash) or (h not in before):
                    detected=x; break
            if detected:
                break
    finally:
        await c.aclose()

    title=(media_title or "").strip() or (detected or {}).get("name") or _magnet_name(magnet) or filename or "Свой торрент"
    if detected and isinstance(page_meta,dict) and (page_meta.get("poster") or page_meta.get("overview") or page_url):
        # Сохраняем карточку со страницы: постер и описание пригодятся и в
        # очереди загрузок, и после переноса в медиатеку.
        with cache_db() as con:
            con.execute("""insert or replace into download_meta
                (hash,title,year,poster,overview,source_url,created_at)
                values(?,?,?,?,?,?,?)""",
                (detected.get("hash"),page_meta.get("title") or title,page_meta.get("year") or "",
                 page_meta.get("poster") or "",page_meta.get("overview") or "",page_url,
                 datetime.now(timezone.utc).isoformat()))
            con.commit()
    if detected and category in {"movies","tv","anime"}:
        sn=int(season or 0) or season_number((detected.get("name") or title))
        with cache_db() as con:
            con.execute("""insert or replace into download_jobs
                (hash,kind,media_title,season,release_title,category,status,created_at,error)
                values(?,?,?,?,?,?,?,?,?)""",
                (detected.get("hash"),category,title,sn,detected.get("name") or title,category,
                 "downloading",datetime.now(timezone.utc).isoformat(),""))
            con.commit()
    log_activity("torrent-upload",title,f"Категория: {category}"+("" if detected else " · задача пока не видна в очереди"),True)
    return {
        "ok":True,
        "message":("Торрент добавлен и скачивается" if detected else "Торрент отправлен в qBittorrent"),
        "hash":(detected or {}).get("hash",""),
        "name":(detected or {}).get("name") or title,
        "category":category,
        "autoImport":category in {"movies","tv","anime"},
        "meta":{k:v for k,v in (page_meta or {}).items() if k in {"title","year","poster","overview","sourceUrl"}},
    }

# --- v21.11 наблюдение за страницами ----------------------------------------
# Карточка добавляется ссылкой, MediaHub периодически перечитывает страницу и
# замечает новые серии или новые торрент-ссылки. Дальше — либо уведомление,
# либо автоматическая постановка на закачку.

WATCH_INTERVAL=timedelta(minutes=30)
_WATCH_TASK=None

def _watch_row(r):
    d=dict(r)
    try:d["items"]=json.loads(d.pop("last_items",None) or "[]")
    except Exception:d["items"]=[]
    d["autoDownload"]=bool(d.pop("auto_download",0))
    d["checkedAt"]=d.pop("checked_at",None)
    d["changedAt"]=d.pop("changed_at",None)
    d["createdAt"]=d.pop("created_at",None)
    d.pop("last_signature",None)
    return d


def _watch_signature(links,episodes,cards):
    basis="|".join(sorted(str(x.get("url") or "") for x in (links or [])))
    basis+="||"+str(episodes)+"||"+"|".join(sorted(str(x.get("url") or "") for x in (cards or [])))
    return hashlib.sha1(basis.encode("utf-8",errors="ignore")).hexdigest()


def _episodes_from(links,meta_title):
    """Максимальный номер серии, который упоминается на странице."""
    best=0
    for text in [meta_title or ""]+[str(x.get("title") or "") for x in (links or [])]:
        info=release_episode_info(text)
        best=max(best,int(info.get("episodeTo") or 0),int(info.get("episodeFrom") or 0),
                 int(info.get("episodeTotal") or 0))
    return best


async def watch_scan(url,mode="release"):
    """Прочитать страницу: метаданные, торрент-ссылки, карточки, число серий."""
    if mode=="feed":
        cards,meta=await scan_feed_cards(url)
        meta=meta or {}
        return {"meta":meta,"links":[],"cards":cards,"episodes":0,"error":meta.get("error") or ""}
    links,meta=await torrent_links_from_page(url,return_meta=True)
    error=(meta or {}).get("error") or ""
    if not links and error:
        # Страница могла открыться, но без торрентов — метаданные всё равно нужны.
        return {"meta":meta or {},"links":[],"cards":[],"episodes":0,"error":error}
    return {"meta":meta or {},"links":links,"cards":[],
            "episodes":_episodes_from(links,(meta or {}).get("title")),"error":""}


async def watch_check(row,force=False):
    """Проверить одну страницу и вернуть, что изменилось."""
    url=row["url"]; mode=row.get("mode") or "release"
    scan=await watch_scan(url,mode)
    now=datetime.now(timezone.utc).isoformat()
    if scan["error"] and not scan["links"] and not scan["cards"]:
        with cache_db() as con:
            con.execute("update page_watch set checked_at=?,status='error',error=? where id=?",
                        (now,scan["error"][:300],row["id"]));con.commit()
        return {"changed":False,"error":scan["error"]}

    signature=_watch_signature(scan["links"],scan["episodes"],scan["cards"])
    changed=bool(row.get("last_signature")) and signature!=row["last_signature"]
    first_run=not row.get("last_signature")
    meta=scan["meta"] or {}
    new_episodes=max(int(row.get("episodes") or 0),int(scan["episodes"] or 0))
    grew=int(scan["episodes"] or 0)>int(row.get("episodes") or 0)

    items=scan["cards"] if mode=="feed" else scan["links"]
    if mode=="feed" and items:
        # Карточки складываем в очередь на проверку: в ленту новинок попадут
        # только те, у которых на странице действительно есть торрент.
        try:_feed_remember(row["id"],items)
        except Exception:pass
    with cache_db() as con:
        con.execute("""update page_watch set title=?,poster=?,overview=?,last_signature=?,
                       last_items=?,episodes=?,status=?,error='',checked_at=?,
                       changed_at=case when ?=1 then ? else changed_at end,
                       unseen=case when ?=1 then unseen+1 else unseen end
                       where id=?""",
                    (row.get("title") or meta.get("title") or "",
                     row.get("poster") or meta.get("poster") or "",
                     row.get("overview") or meta.get("overview") or "",
                     signature,json.dumps(items[:60],ensure_ascii=False),new_episodes,
                     "update" if changed else "ok",now,
                     1 if changed else 0,now,1 if changed else 0,row["id"]))
        con.commit()

    if changed:
        detail=f"серий: {new_episodes}" if grew else f"новых ссылок: {len(items)}"
        log_activity("watch-update",row.get("title") or url,detail,True)
        if row.get("auto_download") and mode=="release" and scan["links"]:
            ok,note=await _watch_autograb(row,scan["links"])
            log_activity("watch-download",row.get("title") or url,note,ok)
    elif first_run:
        log_activity("watch-add",row.get("title") or meta.get("title") or url,
                     f"страница взята под наблюдение ({mode})",True)
    return {"changed":changed,"episodes":new_episodes,"items":len(items),"error":""}


async def _watch_autograb(row,links):
    """Поставить свежую раздачу на закачку, если включено автоскачивание."""
    category=row.get("category") or "manual"
    content=b""; magnet=""
    for link in links[:6]:
        if link.get("type")=="magnet":
            magnet=link["url"]; break
        data,found_magnet,error=await fetch_torrent_bytes(link["url"])
        if found_magnet:
            magnet=found_magnet; break
        if data:
            content=data; break
    if not magnet and not content:
        return False,"новую раздачу скачать не удалось"
    c=await qbit_login()
    if not c:
        return False,"qBittorrent недоступен"
    try:
        # Тот же торрент мог уже стоять в очереди — второй раз не добавляем.
        want=_magnet_hash(magnet)
        if want:
            try:
                r=await c.get(QBIT_URL+"/api/v2/torrents/info")
                if any(str(x.get("hash") or "").lower()==want for x in r.json()):
                    return True,"уже в очереди qBittorrent"
            except Exception:
                pass
        data={"category":category,"paused":"false"}
        if content:
            r=await c.post(QBIT_URL+"/api/v2/torrents/add",data=data,
                           files={"torrents":("watch.torrent",content,"application/x-bittorrent")})
        else:
            r=await c.post(QBIT_URL+"/api/v2/torrents/add",data={**data,"urls":magnet})
        if r.status_code>=300:
            return False,f"qBittorrent отклонил: HTTP {r.status_code}"
    except Exception as e:
        return False,f"qBittorrent не ответил: {str(e)[:100]}"
    finally:
        await c.aclose()
    return True,f"поставлено на закачку в «{category}»"


async def watch_check_all(force=False):
    with cache_db() as con:
        rows=[dict(r) for r in con.execute("select * from page_watch").fetchall()]
    checked=changed=0
    for row in rows:
        if not force and row.get("checked_at"):
            try:
                dt=datetime.fromisoformat(row["checked_at"])
                if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc)-dt<WATCH_INTERVAL:
                    continue
            except Exception:
                pass
        try:
            result=await watch_check(row)
            checked+=1; changed+=1 if result.get("changed") else 0
        except Exception as e:
            log_activity("watch-error",row.get("title") or row.get("url") or "",str(e)[:200],False)
    return {"checked":checked,"changed":changed,"total":len(rows)}


async def _watch_loop():
    await asyncio.sleep(45)
    while True:
        try:
            await watch_check_all()
        except Exception:
            pass
        await asyncio.sleep(WATCH_INTERVAL.total_seconds())


@app.on_event("startup")
async def _start_watch_loop():
    global _WATCH_TASK
    if _WATCH_TASK is None or _WATCH_TASK.done():
        _WATCH_TASK=asyncio.create_task(_watch_loop())


def _feed_remember(watch_id,cards):
    """Запомнить карточки ленты, чтобы проверить их по одной в фоне."""
    now=datetime.now(timezone.utc).isoformat()
    with cache_db() as con:
        for c in cards or []:
            url=str(c.get("url") or "").strip()
            if not url:
                continue
            con.execute("""insert or ignore into feed_items
                (url,watch_id,title,poster,year,overview,has_torrent,checked_at,first_seen,error)
                values(?,?,?,?,?,'',0,'',?,'')""",
                (url,watch_id,c.get("title") or "",c.get("poster") or "",c.get("year") or "",now))
            con.execute("""update feed_items set title=coalesce(nullif(title,''),?),
                           poster=coalesce(nullif(poster,''),?) where url=?""",
                        (c.get("title") or "",c.get("poster") or "",url))
        con.commit()


async def _feed_verify_one(row):
    """Открыть страницу карточки: есть ли торрент и нормальные метаданные."""
    url=row["url"]
    now=datetime.now(timezone.utc).isoformat()
    try:
        links,meta=await torrent_links_from_page(url,return_meta=True)
    except Exception as e:
        with cache_db() as con:
            con.execute("update feed_items set checked_at=?,error=? where url=?",
                        (now,str(e)[:200],url));con.commit()
        return False
    meta=meta or {}
    has=1 if links else 0
    with cache_db() as con:
        con.execute("""update feed_items set has_torrent=?,checked_at=?,
                       title=case when ?<>'' then ? else title end,
                       poster=case when ?<>'' then ? else poster end,
                       year=case when ?<>'' then ? else year end,
                       overview=?,error=? where url=?""",
                    (has,now,
                     meta.get("title") or "",meta.get("title") or "",
                     meta.get("poster") or "",meta.get("poster") or "",
                     meta.get("year") or "",meta.get("year") or "",
                     (meta.get("overview") or "")[:800],meta.get("error") or "",url))
        con.commit()
    return bool(has)


async def feed_verify_batch(limit=5):
    with cache_db() as con:
        rows=[dict(r) for r in con.execute(
            """select * from feed_items where checked_at='' or checked_at is null
               order by first_seen desc limit ?""",(limit,)).fetchall()]
    checked=0
    for row in rows:
        try:
            await _feed_verify_one(row); checked+=1
        except Exception:
            pass
    return checked


@app.post("/api/watch-feed/verify")
async def watch_feed_verify(limit:int=Query(12,ge=1,le=60)):
    checked=await feed_verify_batch(limit)
    with cache_db() as con:
        left=con.execute("select count(*) as n from feed_items where checked_at='' or checked_at is null").fetchone()["n"]
    return {"ok":True,"checked":checked,"pending":int(left or 0),
            "message":f"Проверено {checked}, осталось {left}"}


@app.get("/api/watch-feed")
async def watch_feed(limit:int=Query(200,ge=10,le=600)):
    """Новинки со всех отслеживаемых лент, сгруппированные по тайтлу.

    Группируем по названию, а не по сайту: один и тот же тайтл на трёх сайтах —
    это одна карточка с тремя источниками на выбор.
    """
    from urllib.parse import urlparse
    with cache_db() as con:
        rows=con.execute("select * from page_watch where mode='feed' order by id").fetchall()
        # В ленту попадают только проверенные карточки: страница открылась и на
        # ней действительно есть торрент или magnet.
        verified={r["url"]:dict(r) for r in con.execute(
            "select * from feed_items where has_torrent=1").fetchall()}
        pending=con.execute(
            "select count(*) as n from feed_items where checked_at='' or checked_at is null").fetchone()["n"]
    groups={}
    for r in rows:
        watcher=_watch_row(r)
        site=""
        try:
            site=(urlparse(watcher["url"]).hostname or "").replace("www.","")
        except Exception:
            site=watcher["url"]
        fresh=bool(watcher.get("unseen"))
        for item in watcher.get("items") or []:
            check=verified.get(str(item.get("url") or ""))
            if not check:
                continue
            item={**item,
                  "title":check.get("title") or item.get("title") or "",
                  "poster":check.get("poster") or item.get("poster") or "",
                  "year":check.get("year") or item.get("year") or ""}
            title=str(item.get("title") or "").strip()
            if not title:
                continue
            key=normalize_search_text(title)
            if not key:
                continue
            g=groups.setdefault(key,{"title":title,"year":item.get("year") or "","poster":"",
                                     "sources":[],"new":False,"seenAt":watcher.get("checkedAt") or ""})
            if len(title)<len(g["title"]):
                g["title"]=title
            if not g["poster"] and item.get("poster"):
                g["poster"]=item["poster"]
            if not g["year"] and item.get("year"):
                g["year"]=item["year"]
            if fresh:
                g["new"]=True
            if not any(s["url"]==item.get("url") for s in g["sources"]):
                g["sources"].append({"site":site,"url":item.get("url") or watcher["url"],
                                     "watchId":watcher["id"],"poster":item.get("poster") or ""})
    items=list(groups.values())
    items.sort(key=lambda g:(not g["new"],-len(g["sources"]),g["title"].casefold()))
    return {"count":len(items),
            "new":sum(1 for g in items if g["new"]),
            "pending":int(pending or 0),
            "sites":sorted({s["site"] for g in items for s in g["sources"]}),
            "items":items[:limit]}


@app.get("/api/watchers")
async def watchers_list():
    with cache_db() as con:
        rows=con.execute("select * from page_watch order by unseen desc, id desc").fetchall()
    items=[_watch_row(r) for r in rows]
    return {"count":len(items),"unseen":sum(1 for x in items if x.get("unseen")),"items":items}


@app.get("/api/watchers/summary")
async def watchers_summary():
    with cache_db() as con:
        row=con.execute("select count(*) as n, sum(case when unseen>0 then 1 else 0 end) as u from page_watch").fetchone()
    return {"total":int((row["n"] if row else 0) or 0),"unseen":int((row["u"] if row else 0) or 0)}


@app.post("/api/watchers")
async def watchers_create(url:str=Form(...),kind:str=Form("tv"),category:str=Form("manual"),
                          mode:str=Form("release"),auto_download:str=Form("0"),title:str=Form("")):
    url=(url or "").strip()
    if not url.lower().startswith(("http://","https://")):
        raise HTTPException(400,"Нужна ссылка на страницу (http или https)")
    if mode not in {"release","feed"}:
        mode="release"
    if category not in TORRENT_CATEGORIES:
        category="manual"
    if kind not in {"movies","tv","anime"}:
        kind="tv"
    scan=await watch_scan(url,mode)
    if scan["error"] and not scan["links"] and not scan["cards"]:
        raise HTTPException(400,f"Страницу не удалось разобрать: {scan['error']}")
    meta=scan["meta"] or {}
    if not meta.get("title"):
        # У ленты новинок осмысленного заголовка может не быть — берём домен.
        try:
            from urllib.parse import urlparse
            meta["title"]=(urlparse(url).hostname or url).replace("www.","")
        except Exception:
            meta["title"]=url
    now=datetime.now(timezone.utc).isoformat()
    with cache_db() as con:
        try:
            cur=con.execute("""insert into page_watch
                (url,title,poster,overview,kind,category,mode,auto_download,created_at,status)
                values(?,?,?,?,?,?,?,?,?,'new')""",
                (url,(title or meta.get("title") or url)[:300],meta.get("poster") or "",
                 meta.get("overview") or "",kind,category,mode,1 if auto_download=="1" else 0,now))
            watch_id=cur.lastrowid; con.commit()
        except sqlite3.IntegrityError:
            raise HTTPException(400,"Эта страница уже под наблюдением")
        row=dict(con.execute("select * from page_watch where id=?",(watch_id,)).fetchone())
    await watch_check(row)
    with cache_db() as con:
        fresh=con.execute("select * from page_watch where id=?",(watch_id,)).fetchone()
    return {"ok":True,"message":"Страница под наблюдением","watch":_watch_row(fresh)}


@app.post("/api/watchers/{watch_id}/check")
async def watchers_check(watch_id:int):
    with cache_db() as con:
        row=con.execute("select * from page_watch where id=?",(watch_id,)).fetchone()
    if not row:
        raise HTTPException(404,"Наблюдение не найдено")
    result=await watch_check(dict(row),force=True)
    with cache_db() as con:
        fresh=con.execute("select * from page_watch where id=?",(watch_id,)).fetchone()
    return {"ok":not result.get("error"),"changed":result.get("changed"),
            "message":("Появилось новое" if result.get("changed") else
                       (f"Ошибка: {result['error']}" if result.get("error") else "Изменений нет")),
            "watch":_watch_row(fresh)}


@app.post("/api/watchers/{watch_id}/seen")
async def watchers_seen(watch_id:int):
    with cache_db() as con:
        con.execute("update page_watch set unseen=0,status='ok' where id=?",(watch_id,));con.commit()
    return {"ok":True}


@app.post("/api/watchers/{watch_id}/settings")
async def watchers_settings(watch_id:int,auto_download:str=Form(""),category:str=Form(""),kind:str=Form("")):
    sets=[];vals=[]
    if auto_download in {"0","1"}:
        sets.append("auto_download=?");vals.append(1 if auto_download=="1" else 0)
    if category in TORRENT_CATEGORIES:
        sets.append("category=?");vals.append(category)
    if kind in {"movies","tv","anime"}:
        sets.append("kind=?");vals.append(kind)
    if not sets:
        return {"ok":True}
    vals.append(watch_id)
    with cache_db() as con:
        con.execute(f"update page_watch set {','.join(sets)} where id=?",vals);con.commit()
    return {"ok":True,"message":"Настройки наблюдения сохранены"}


@app.delete("/api/watchers/{watch_id}")
async def watchers_delete(watch_id:int):
    with cache_db() as con:
        con.execute("delete from page_watch where id=?",(watch_id,));con.commit()
    return {"ok":True,"message":"Наблюдение снято"}


@app.post("/api/watchers/check-all")
async def watchers_check_all():
    result=await watch_check_all(force=True)
    return {"ok":True,"message":f"Проверено {result['checked']} из {result['total']}, изменилось {result['changed']}",**result}


# --- v21.11 избранные сайты ---------------------------------------------------

@app.get("/api/sites")
async def sites_list():
    with cache_db() as con:
        rows=con.execute("select * from site_bookmarks order by id desc").fetchall()
    return [dict(r) for r in rows]


@app.post("/api/sites")
async def sites_create(url:str=Form(...),title:str=Form(""),note:str=Form("")):
    url=(url or "").strip()
    if not url.lower().startswith(("http://","https://")):
        raise HTTPException(400,"Нужна ссылка http или https")
    if not title.strip():
        try:
            from urllib.parse import urlparse
            title=(urlparse(url).hostname or url).replace("www.","")
        except Exception:
            title=url
    with cache_db() as con:
        con.execute("insert into site_bookmarks(title,url,note,created_at) values(?,?,?,?)",
                    (title.strip()[:200],url,note.strip()[:300],datetime.now(timezone.utc).isoformat()))
        con.commit()
    return {"ok":True,"message":"Сайт сохранён"}


@app.delete("/api/sites/{site_id}")
async def sites_delete(site_id:int):
    with cache_db() as con:
        con.execute("delete from site_bookmarks where id=?",(site_id,));con.commit()
    return {"ok":True}


@app.post("/api/download-action")
async def download_action(hashes:str=Form(...),action:str=Form(...)):
    c=await qbit_login()
    if not c: raise HTTPException(400,"qBittorrent не настроен")
    try:
        if action in {"delete","delete-files"}:
            r=await c.post(QBIT_URL+"/api/v2/torrents/delete",
                           data={"hashes":hashes,"deleteFiles":"true" if action=="delete-files" else "false"})
        elif action in {"pause","resume"}:
            # qBittorrent 5.x переименовал pause/resume в stop/start; старые имена — для 4.x.
            r=await c.post(QBIT_URL+"/api/v2/torrents/"+{"pause":"stop","resume":"start"}[action],data={"hashes":hashes})
            if r.status_code>=400:
                r=await c.post(QBIT_URL+f"/api/v2/torrents/{action}",data={"hashes":hashes})
        elif action=="recheck":
            r=await c.post(QBIT_URL+"/api/v2/torrents/recheck",data={"hashes":hashes})
        else: raise HTTPException(400,"Недоступное действие")
        ok=r.status_code<300; log_activity("download-action",action,hashes[:120],ok)
        return {"ok":ok}
    finally: await c.aclose()

@app.get("/api/library")
async def library(kind:str=Query("movies")):
    if kind=="games": return []
    if kind=="movies":
        items=await get_json(RADARR_URL,RADARR_KEY,"/api/v3/movie")
        return [{"title":x.get("title"),"year":x.get("year"),"path":x.get("path"),
                 "hasFile":x.get("hasFile",False),
                 "poster":next((i.get("remoteUrl") for i in x.get("images",[]) if i.get("coverType")=="poster"),None)}
                for x in items]
    items=await get_json(SONARR_URL,SONARR_KEY,"/api/v3/series")
    want=kind=="anime"; out=[]
    for x in items:
        ia=x.get("seriesType")=="anime" or str(x.get("path","")).startswith(str(ANIME_ROOT))
        if ia!=want: continue
        out.append({"title":x.get("title"),"year":x.get("year"),"path":x.get("path"),
                    "poster":next((i.get("remoteUrl") for i in x.get("images",[]) if i.get("coverType")=="poster"),None)})
    return out

@app.get("/api/search")
async def search(q:str,kind:str=Query("movies")):
    if kind=="movies":
        items=await get_json(RADARR_URL,RADARR_KEY,"/api/v3/movie/lookup",{"term":q})
        return [{"title":x.get("title"),"year":x.get("year"),"overview":x.get("overview"),
                 "tmdbId":x.get("tmdbId"),
                 "poster":next((i.get("remoteUrl") for i in x.get("images",[]) if i.get("coverType")=="poster"),None)}
                for x in items[:60]]
    items=await get_json(SONARR_URL,SONARR_KEY,"/api/v3/series/lookup",{"term":q})
    return [{"title":x.get("title"),"year":x.get("year"),"overview":x.get("overview"),
             "tvdbId":x.get("tvdbId"),
             "poster":next((i.get("remoteUrl") for i in x.get("images",[]) if i.get("coverType")=="poster"),None)}
            for x in items[:60]]

async def first_profile(base,key):
    p=await get_json(base,key,"/api/v3/qualityprofile")
    return p[0]["id"] if p else None

@app.post("/api/add")
async def add(
    kind:str=Form(...),
    external_id:int=Form(...),
    catalog:str=Form(""),
    title:str=Form(""),
    year:str=Form("")
):
    if kind=="games":
        raise HTTPException(400,"Игры не управляются Radarr/Sonarr")

    if kind=="movies":
        lookup=await get_json(
            RADARR_URL,RADARR_KEY,"/api/v3/movie/lookup",
            {"term":f"tmdb:{external_id}"}
        )
        if not lookup:
            return JSONResponse({"ok":False,"error":"Фильм не найден в Radarr"},404)
        existing=await get_json(RADARR_URL,RADARR_KEY,"/api/v3/movie")
        match=next((x for x in existing if x.get("tmdbId")==external_id),None)
        if match:
            has_file=bool(match.get("hasFile"))
            upsert_tracked_library_item("movies",str(match.get("id")),match.get("title") or title,
                                        match.get("year") or "",external_id=str(external_id),
                                        catalog_source="radarr",path=match.get("path") or "")
            remember_user_tracking("movies",match.get("id"),external_id,match.get("title") or title)
            return {"ok":True,"tracked":True,"hasFile":has_file,
                    "message":"Уже есть в библиотеке" if has_file else "Уже отслеживается, файлов ещё нет"}
        item=lookup[0]
        item.update({
            "qualityProfileId":await first_profile(RADARR_URL,RADARR_KEY),
            "rootFolderPath":str(MOVIES_ROOT),
            "monitored":True,
            "minimumAvailability":"released",
            "addOptions":{"searchForMovie":True}
        })
        created=await post_json(RADARR_URL,RADARR_KEY,"/api/v3/movie",item)
        new_id=(created or {}).get("id") if isinstance(created,dict) else None
        upsert_tracked_library_item("movies",str(new_id or f"tmdb-{external_id}"),
                                    item.get("title") or title,item.get("year") or "",
                                    poster=next((i.get("remoteUrl") for i in (item.get("images") or []) if i.get("coverType")=="poster"),""),
                                    overview=item.get("overview") or "",external_id=str(external_id),
                                    catalog_source="radarr",path=item.get("path") or "")
        remember_user_tracking("movies",new_id,external_id,item.get("title") or title)
        log_activity("track-add",item.get("title") or title,
                     f"добавлено в Radarr по кнопке · tmdb {external_id}",True)
        return {"ok":True,"tracked":True,"hasFile":False,
                "message":"Отслеживается в Radarr · ждём файлы"}

    if catalog=="tmdb":
        term=f"tmdb:{external_id}"
    elif catalog=="anilist":
        term=(title or "").strip()
    else:
        term=f"tvdb:{external_id}" if external_id else (title or "").strip()

    if not term:
        return JSONResponse({"ok":False,"error":"Не удалось определить название сериала"},400)

    try:
        lookup=await get_json(
            SONARR_URL,SONARR_KEY,"/api/v3/series/lookup",
            {"term":term}
        )
    except Exception:
        lookup=[]
    if not lookup and title and term != title:
        lookup=await get_json(
            SONARR_URL,SONARR_KEY,"/api/v3/series/lookup",
            {"term":title}
        )
    if not lookup:
        return JSONResponse({"ok":False,"error":"Сериал не найден в Sonarr"},404)

    # По точному tvdb/tmdb ответ однозначен, по названию — выбираем совпадение
    # по названию и году, иначе в Sonarr попадал первый похожий сериал.
    item=lookup[0] if term.startswith(("tvdb:","tmdb:")) else pick_best_lookup(lookup,title,year,kind)
    if not item:
        return JSONResponse({"ok":False,
                             "error":f"В Sonarr нет точного совпадения для «{title}». Уточни название или год."},404)
    resolved_tvdb=item.get("tvdbId")
    existing=await get_json(SONARR_URL,SONARR_KEY,"/api/v3/series")
    match=next((x for x in existing if resolved_tvdb and x.get("tvdbId")==resolved_tvdb),None)
    if match:
        stats=match.get("statistics") or {}
        has_file=int(stats.get("episodeFileCount") or 0)>0
        upsert_tracked_library_item(kind,str(match.get("id")),match.get("title") or title,
                                    match.get("year") or "",external_id=str(resolved_tvdb or ""),
                                    catalog_source="sonarr",path=match.get("path") or "")
        remember_user_tracking(kind,match.get("id"),resolved_tvdb,match.get("title") or title)
        return {"ok":True,"tracked":True,"hasFile":has_file,
                "message":"Уже есть в библиотеке" if has_file else "Уже отслеживается, файлов ещё нет"}

    anime=kind=="anime"
    item.update({
        "qualityProfileId":await first_profile(SONARR_URL,SONARR_KEY),
        "rootFolderPath":str(ANIME_ROOT if anime else TV_ROOT),
        "monitored":True,
        "seasonFolder":True,
        "seriesType":"anime" if anime else "standard",
        "addOptions":{"monitor":"all","searchForMissingEpisodes":True}
    })
    created=await post_json(SONARR_URL,SONARR_KEY,"/api/v3/series",item)
    new_id=(created or {}).get("id") if isinstance(created,dict) else None
    upsert_tracked_library_item(kind,str(new_id or f"tvdb-{resolved_tvdb or external_id}"),
                                item.get("title") or title,item.get("year") or "",
                                poster=next((i.get("remoteUrl") for i in (item.get("images") or []) if i.get("coverType")=="poster"),""),
                                overview=item.get("overview") or "",external_id=str(resolved_tvdb or ""),
                                catalog_source="sonarr",path=item.get("path") or "")
    remember_user_tracking(kind,new_id,resolved_tvdb or external_id,item.get("title") or title)
    log_activity("track-add",item.get("title") or title,
                 f"добавлено в Sonarr по кнопке · tvdb {resolved_tvdb or external_id}",True)
    return {"ok":True,"tracked":True,"hasFile":False,
            "message":"Отслеживается в Sonarr · ждём файлы"}

#!/usr/bin/env python3
"""MediaHub system setup manager.

This module is intentionally separate from the FastAPI application.  MediaHub itself can
be installed alone; optional media-stack components are installed later from the web UI.
"""
from __future__ import annotations

import fcntl
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import urllib.error
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

STATE_DIR = Path("/var/lib/mediahub/setup")
STATE_FILE = STATE_DIR / "state.json"
LOG_FILE = STATE_DIR / "install.log"
LOCK_FILE = STATE_DIR / "install.lock"
ENV_FILE = Path("/etc/mediahub.env")
STORAGE_CONFIG_FILE = STATE_DIR / "storage.json"
STORAGE_REQUEST_FILE = STATE_DIR / "storage-request.json"
FSTAB_FILE = Path("/etc/fstab")
FSTAB_BEGIN = "# BEGIN MEDIAHUB STORAGE"
FSTAB_END = "# END MEDIAHUB STORAGE"
BRANCH_ROOT = Path("/mnt/mediahub-disks")

RECOMMENDED = ["storage", "qbittorrent", "prowlarr", "radarr", "sonarr", "automation"]

COMPONENT_META = {
    "storage": {"title": "Хранилище", "description": "Структура фильмов, сериалов, аниме и входящих загрузок.", "port": None},
    "qbittorrent": {"title": "qBittorrent", "description": "Торрент-клиент и очередь загрузок.", "port": 8080},
    "prowlarr": {"title": "Prowlarr", "description": "Единый менеджер индексаторов и источников.", "port": 9696},
    "radarr": {"title": "Radarr", "description": "Автоматизация фильмов.", "port": 7878},
    "sonarr": {"title": "Sonarr", "description": "Автоматизация сериалов и аниме.", "port": 8989},
    "automation": {"title": "Автоматизация MediaHub", "description": "Кэш, обновление локальной библиотеки и Organizer.", "port": None},
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def write_state(**kwargs) -> None:
    ensure_state_dir()
    current = {}
    try:
        current = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    current.update(kwargs)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def read_state() -> Dict:
    ensure_state_dir()
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"running": False, "component": "", "progress": 0, "ok": None, "message": ""}


def log(message: str) -> None:
    ensure_state_dir()
    stamp = datetime.now().strftime("%H:%M:%S")
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {message}\n")
        f.flush()
    print(message, flush=True)


def log_tail(lines: int = 180) -> str:
    try:
        data = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(data[-lines:])
    except Exception:
        return ""


def run(cmd, *, check=True, env=None, cwd=None) -> subprocess.CompletedProcess:
    shown = " ".join(str(x) for x in cmd)
    log(f"$ {shown}")
    p = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, cwd=cwd)
    if p.stdout:
        for line in p.stdout.rstrip().splitlines():
            log(line)
    if check and p.returncode != 0:
        raise RuntimeError(f"Команда завершилась с кодом {p.returncode}: {shown}")
    return p


def read_env_file() -> Dict[str, str]:
    out: Dict[str, str] = {}
    if ENV_FILE.exists():
        for raw in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def media_root() -> Path:
    return Path(read_env_file().get("MEDIA_ROOT", "/mnt/media"))


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_storage_config() -> Dict:
    return _load_json(STORAGE_CONFIG_FILE, {})


def queue_storage_request(payload: Dict) -> None:
    ensure_state_dir()
    _save_json(STORAGE_REQUEST_FILE, payload)


def _run_json(cmd: List[str]) -> Dict:
    try:
        p = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=12)
        if p.returncode == 0 and p.stdout.strip():
            return json.loads(p.stdout)
    except Exception:
        pass
    return {}


def _lsblk_data() -> Dict:
    cols = "NAME,KNAME,PATH,TYPE,SIZE,FSTYPE,MOUNTPOINTS,MODEL,SERIAL,ROTA,RM,RO,UUID,PARTUUID,PKNAME"
    data = _run_json(["lsblk", "-J", "-b", "-o", cols])
    if data.get("blockdevices"):
        return data
    # Older util-linux versions may not support MOUNTPOINTS.
    cols = "NAME,KNAME,PATH,TYPE,SIZE,FSTYPE,MOUNTPOINT,MODEL,SERIAL,ROTA,RM,RO,UUID,PARTUUID,PKNAME"
    return _run_json(["lsblk", "-J", "-b", "-o", cols])


def _walk_nodes(node: Dict):
    yield node
    for child in node.get("children") or []:
        yield from _walk_nodes(child)


def _mounts_for_node(node: Dict) -> List[str]:
    raw = node.get("mountpoints")
    if raw is None:
        raw = [node.get("mountpoint")]
    elif isinstance(raw, str):
        raw = [raw]
    return [str(x) for x in (raw or []) if x not in (None, "")]


def _human_bytes(value: int) -> str:
    n = float(value or 0)
    for unit in ["B", "KB", "MB", "GB", "TB", "PB"]:
        if n < 1024 or unit == "PB":
            return f"{n:.1f} {unit}" if unit not in {"B", "KB"} else f"{n:.0f} {unit}"
        n /= 1024
    return str(value)


def _system_disk_paths() -> Set[str]:
    out: Set[str] = set()
    for target in ["/", "/boot", "/boot/efi"]:
        try:
            src = subprocess.check_output(["findmnt", "-rn", "-o", "SOURCE", target], text=True, timeout=4).strip()
        except Exception:
            continue
        if not src.startswith("/dev/"):
            continue
        src = os.path.realpath(src)
        try:
            p = subprocess.run(["lsblk", "-srno", "PATH,TYPE", src], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
            for line in (p.stdout or "").splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[-1] == "disk":
                    out.add(parts[0])
        except Exception:
            pass
    return out


def _disk_rows() -> List[Dict]:
    data = _lsblk_data()
    system_paths = _system_disk_paths()
    cfg = load_storage_config()
    member_uuids: Set[str] = {str(x.get("uuid", "")) for x in cfg.get("branches", []) if x.get("uuid")}
    rows: List[Dict] = []
    critical_mounts = {"/", "/boot", "/boot/efi", "/usr", "/var", "/opt"}
    for disk in data.get("blockdevices") or []:
        if disk.get("type") != "disk":
            continue
        nodes = list(_walk_nodes(disk))
        mounts = sorted({m for n in nodes for m in _mounts_for_node(n)})
        fstypes = sorted({str(n.get("fstype")) for n in nodes if n.get("fstype")})
        uuids = {str(n.get("uuid")) for n in nodes if n.get("uuid")}
        disk_path = disk.get("path") or ("/dev/" + str(disk.get("name") or ""))
        system = disk_path in system_paths or any(m in critical_mounts or m.startswith("/boot/") for m in mounts)
        swap = any(str(n.get("fstype") or "").lower() == "swap" for n in nodes)
        active_stack = any(str(n.get("type") or "").lower() in {"lvm","crypt","raid0","raid1","raid4","raid5","raid6","raid10","md"} for n in nodes if n is not disk)
        pool_member = bool(uuids & member_uuids)
        mounted = bool(mounts)
        readonly = bool(int(disk.get("ro") or 0))
        removable = bool(int(disk.get("rm") or 0))
        device_present = Path(disk_path).exists()
        device_writable = device_present and os.access(disk_path, os.W_OK)
        partitions = [
            {
                "path": n.get("path") or ("/dev/" + str(n.get("name") or "")),
                "type": n.get("type") or "",
                "size": int(n.get("size") or 0),
                "fstype": n.get("fstype") or "",
                "uuid": n.get("uuid") or "",
                "mounts": _mounts_for_node(n),
            }
            for n in nodes if n is not disk
        ]
        reasons = []
        if system: reasons.append("системный диск")
        if pool_member: reasons.append("уже в MediaPool")
        if mounted and not pool_member: reasons.append("есть смонтированные разделы")
        if swap: reasons.append("используется swap")
        if active_stack and not system: reasons.append("активный LVM/RAID/шифрованный том")
        if readonly: reasons.append("только чтение")
        if not device_present: reasons.append("устройство не передано в /dev")
        elif not device_writable: reasons.append("нет доступа на запись")
        eligible = not (system or pool_member or mounted or swap or active_stack or readonly) and device_present and device_writable
        rows.append({
            "name": disk.get("name") or "",
            "path": disk_path,
            "size": int(disk.get("size") or 0),
            "sizeText": _human_bytes(int(disk.get("size") or 0)),
            "model": (disk.get("model") or "").strip(),
            "serial": (disk.get("serial") or "").strip(),
            "rotational": bool(int(disk.get("rota") or 0)),
            "removable": removable,
            "readonly": readonly,
            "devicePresent": device_present,
            "deviceWritable": device_writable,
            "system": system,
            "poolMember": pool_member,
            "mounted": mounted,
            "eligible": eligible,
            "warning": ", ".join(reasons),
            "mounts": mounts,
            "filesystems": fstypes,
            "partitions": partitions,
            "hasExistingLayout": len(nodes) > 1 or bool(fstypes),
        })
    return rows


def _mount_stats(path: Path) -> Dict:
    try:
        st = os.statvfs(path)
        total = int(st.f_blocks * st.f_frsize)
        free = int(st.f_bavail * st.f_frsize)
        return {"total": total, "free": free, "used": max(0, total - free), "percent": round((1 - free / total) * 100, 1) if total else 0}
    except Exception:
        return {"total": 0, "free": 0, "used": 0, "percent": 0}


def storage_overview() -> Dict:
    cfg = load_storage_config()
    mountpoint = Path(cfg.get("mountpoint") or media_root())
    mounted = subprocess.run(["findmnt", "-rn", "-M", str(mountpoint)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    branches = []
    for b in cfg.get("branches", []):
        bb = dict(b)
        bp = Path(bb.get("mountpoint") or "/nonexistent")
        bb["mounted"] = subprocess.run(["findmnt", "-rn", "-M", str(bp)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        bb["stats"] = _mount_stats(bp) if bb["mounted"] else {"total":0,"free":0,"used":0,"percent":0}
        branches.append(bb)
    return {
        "configured": bool(cfg.get("type") == "mergerfs" and branches),
        "type": cfg.get("type") or "",
        "name": cfg.get("name") or "MediaPool",
        "mountpoint": str(mountpoint),
        "mounted": mounted,
        "reserveGb": int(cfg.get("reserve_gb") or 20),
        "pending": bool(cfg.get("pending")),
        "branches": branches,
        "stats": _mount_stats(mountpoint) if mounted else {"total":0,"free":0,"used":0,"percent":0},
        "disks": _disk_rows(),
        "container": host_info().get("container", False),
        "virtualization": host_info().get("virtualization", ""),
        "databasePath": read_env_file().get("MEDIAHUB_CACHE_DB", "/var/lib/mediahub/cache.db"),
    }


def _set_media_root_env(target: Path) -> None:
    vals = read_env_file()
    vals.update({
        "MEDIA_ROOT": str(target),
        "MOVIES_ROOT": str(target / "movies"),
        "TV_ROOT": str(target / "tv"),
        "ANIME_ROOT": str(target / "anime"),
        "INBOX_ROOT": str(target / "inbox"),
        "MEDIAHUB_CACHE_DB": str(target / ".mediahub" / "mediahub.db"),
    })
    # Preserve comments and ordering as much as possible.
    lines = ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines() if ENV_FILE.exists() else []
    update = {k: vals[k] for k in ["MEDIA_ROOT", "MOVIES_ROOT", "TV_ROOT", "ANIME_ROOT", "INBOX_ROOT", "MEDIAHUB_CACHE_DB"]}
    found: Set[str] = set()
    out: List[str] = []
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in update:
                out.append(f"{key}={update[key]}")
                found.add(key)
                continue
        out.append(line)
    for key, value in update.items():
        if key not in found:
            out.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    try: ENV_FILE.chmod(0o600)
    except Exception: pass
    # Keep systemd ordered after the actual media mount even when the user
    # chooses a custom mountpoint instead of /mnt/media.
    unit=Path("/etc/systemd/system/mediahub.service")
    try:
        if unit.exists():
            text=unit.read_text(encoding="utf-8",errors="replace")
            line=f"RequiresMountsFor={target}"
            if re.search(r"(?m)^RequiresMountsFor=.*$",text):
                text=re.sub(r"(?m)^RequiresMountsFor=.*$",line,text)
            else:
                text=text.replace("[Unit]\n","[Unit]\n"+line+"\n",1)
            unit.write_text(text,encoding="utf-8")
            subprocess.run(["systemctl","daemon-reload"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    except Exception:
        pass


def migrate_database_to_media_root(target: Path) -> Path:
    """Move MediaHub's SQLite state into the persistent media storage.

    sqlite backup() gives us a transactionally consistent copy even if the old
    database is in WAL mode. The web service keeps using its old open path until
    the Setup Center-requested restart, then systemd reads the new env path.
    """
    target=Path(target)
    state_dir=target / ".mediahub"
    state_dir.mkdir(parents=True,exist_ok=True)
    try: state_dir.chmod(0o700)
    except Exception: pass
    dest=state_dir / "mediahub.db"
    vals=read_env_file()
    src=Path(vals.get("MEDIAHUB_CACHE_DB") or "/var/lib/mediahub/cache.db")
    legacy=Path("/var/lib/mediahub/cache.db")
    try:
        if src.resolve()==dest.resolve() and not dest.exists() and legacy.exists():
            src=legacy
        if src.resolve()!=dest.resolve() and src.exists() and not dest.exists():
            old=sqlite3.connect(str(src),timeout=10)
            new=sqlite3.connect(str(dest),timeout=10)
            try: old.backup(new)
            finally: new.close();old.close()
            log(f"База MediaHub перенесена: {src} -> {dest}")
        elif not dest.exists():
            sqlite3.connect(str(dest)).close()
    except Exception as e:
        raise RuntimeError(f"Не удалось перенести базу MediaHub в MediaPool: {e}")
    try: dest.chmod(0o600)
    except Exception: pass
    return dest


def _safe_pool_mountpoint(raw: str) -> Path:
    target = Path((raw or "/mnt/media").strip()).resolve()
    forbidden = {Path("/"), Path("/etc"), Path("/usr"), Path("/var"), Path("/opt"), Path("/boot"), Path("/proc"), Path("/sys"), Path("/dev")}
    if any(ch.isspace() for ch in str(target)):
        raise RuntimeError("Точка монтирования не должна содержать пробелы")
    if target in forbidden or str(target).startswith("/etc/") or str(target).startswith("/var/lib/mediahub"):
        raise RuntimeError(f"Небезопасная точка монтирования: {target}")
    return target


def _managed_fstab_text(branches: List[Dict], mountpoint: Path, pool_name: str, reserve_gb: int) -> str:
    lines = [FSTAB_BEGIN, "# Managed by MediaHub Storage Wizard. Do not edit this block by hand."]
    for b in branches:
        lines.append(f"UUID={b['uuid']} {b['mountpoint']} ext4 defaults,nofail,x-systemd.device-timeout=30 0 2")
    source = ":".join(str(b["mountpoint"]) for b in branches)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", pool_name or "MediaHubPool")[:40] or "MediaHubPool"
    opts = f"defaults,allow_other,use_ino,category.create=pfrd,func.getattr=newest,minfreespace={max(1, int(reserve_gb))}G,moveonenospc=true,fsname={safe_name}"
    lines.append(f"{source} {mountpoint} mergerfs {opts} 0 0")
    lines.append(FSTAB_END)
    return "\n".join(lines) + "\n"


def _replace_fstab_block(block: str) -> None:
    old = FSTAB_FILE.read_text(encoding="utf-8", errors="replace") if FSTAB_FILE.exists() else ""
    pattern = re.compile(re.escape(FSTAB_BEGIN) + r".*?" + re.escape(FSTAB_END) + r"\n?", re.S)
    cleaned = pattern.sub("", old).rstrip() + "\n\n"
    backup = FSTAB_FILE.with_name("fstab.mediahub-backup")
    if FSTAB_FILE.exists() and not backup.exists():
        shutil.copy2(FSTAB_FILE, backup)
    FSTAB_FILE.write_text(cleaned + block, encoding="utf-8")


def _partition_for_disk(disk: str) -> str:
    for _ in range(15):
        p = subprocess.run(["lsblk", "-nrpo", "PATH,TYPE", disk], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        for line in (p.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[-1] == "part":
                return parts[0]
        time.sleep(0.4)
    raise RuntimeError(f"Не удалось найти новый раздел на {disk}")


def _format_pool_disk(disk: str, index: int) -> Dict:
    base = Path(disk).name
    log(f"Подготавливаю {disk}: таблица GPT + ext4. Все прежние данные на этом диске удаляются.")
    run(["wipefs", "-a", disk])
    run(["parted", "-s", disk, "mklabel", "gpt"])
    run(["parted", "-s", "-a", "optimal", disk, "mkpart", "primary", "ext4", "1MiB", "100%"])
    run(["partprobe", disk], check=False)
    run(["udevadm", "settle"], check=False)
    part = _partition_for_disk(disk)
    label = re.sub(r"[^A-Za-z0-9_-]", "", f"MH{index}_{base}")[:16]
    run(["mkfs.ext4", "-F", "-L", label, part])
    run(["tune2fs", "-m", "0", part], check=False)
    run(["udevadm", "settle"], check=False)
    uuid_value = subprocess.check_output(["blkid", "-s", "UUID", "-o", "value", part], text=True, timeout=8).strip()
    if not uuid_value:
        raise RuntimeError(f"Не удалось получить UUID для {part}")
    mount_dir = BRANCH_ROOT / ("disk-" + re.sub(r"[^A-Za-z0-9]", "", uuid_value)[:8].lower())
    mount_dir.mkdir(parents=True, exist_ok=True)
    return {"device": disk, "partition": part, "uuid": uuid_value, "label": label, "mountpoint": str(mount_dir)}


def _stop_storage_consumers() -> List[str]:
    units = [
        "jellyfin.service", qbit_unit(), "radarr.service", "sonarr.service", "prowlarr.service",
        "mediahub-cache.timer", "mediahub-local-cache.timer", "mediahub-organizer.timer",
    ]
    active = [u for u in units if systemctl_active(u)]
    for unit in active:
        run(["systemctl", "stop", unit], check=False)
    return active


def _restart_units(units: List[str]) -> None:
    for unit in units:
        run(["systemctl", "start", unit], check=False)


def _mount_pool(branches: List[Dict], mountpoint: Path) -> None:
    BRANCH_ROOT.mkdir(parents=True, exist_ok=True)
    mountpoint.mkdir(parents=True, exist_ok=True)
    for b in branches:
        Path(b["mountpoint"]).mkdir(parents=True, exist_ok=True)
        if subprocess.run(["findmnt", "-rn", "-M", b["mountpoint"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            run(["mount", b["mountpoint"]])
    run(["mount", str(mountpoint)])


def _pool_target_is_safe_for_first_mount(target: Path) -> None:
    if subprocess.run(["findmnt", "-rn", "-M", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        raise RuntimeError(f"{target} уже является точкой монтирования. Выбери другой путь или используй существующее хранилище.")
    # Sub-mounts under the target would be hidden as well. Never cover them.
    try:
        nested = subprocess.run(["findmnt", "-rn", "-R", str(target)], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5).stdout.strip()
        if nested:
            raise RuntimeError(f"Внутри {target} есть смонтированные файловые системы. MediaHub не будет их скрывать новым пулом.")
    except RuntimeError:
        raise
    except Exception:
        pass
    if target.exists():
        try:
            has_data = any(x.is_file() or x.is_symlink() for x in target.rglob("*"))
        except Exception:
            has_data = True
        if has_data:
            raise RuntimeError(f"{target} содержит данные. MediaHub не будет скрывать их новым пулом. Используй другой путь или режим существующей папки.")


def storage_pool_job() -> int:
    ensure_state_dir()
    with LOCK_FILE.open("w") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 2
        LOG_FILE.write_text("", encoding="utf-8")
        payload = _load_json(STORAGE_REQUEST_FILE, {})
        selected = [str(x) for x in payload.get("disks", []) if str(x).startswith("/dev/")]
        confirm = str(payload.get("confirm") or "")
        pool_name = str(payload.get("pool_name") or "MediaPool").strip()[:40] or "MediaPool"
        reserve_gb = max(1, min(10000, int(payload.get("reserve_gb") or 20)))
        cfg = load_storage_config()
        existing = bool(cfg.get("type") == "mergerfs" and cfg.get("branches"))
        target = _safe_pool_mountpoint(cfg.get("mountpoint") if existing else str(payload.get("mountpoint") or "/mnt/media"))
        if not selected:
            write_state(running=False, ok=False, progress=0, message="Не выбран ни один диск")
            return 1
        if confirm != "ERASE":
            write_state(running=False, ok=False, progress=0, message="Не подтверждено удаление данных")
            return 1
        # Re-read the hardware immediately before destructive operations.
        rows = {x["path"]: x for x in _disk_rows()}
        bad = [d for d in selected if d not in rows or not rows[d].get("eligible")]
        if bad:
            write_state(running=False, ok=False, progress=0, message="Диск больше не является безопасным для очистки: " + ", ".join(bad))
            return 1
        write_state(running=True, ok=None, component="storage-pool", progress=2, current="check", message="Проверяю диски и точку монтирования")
        log("MediaHub Storage Wizard")
        log("Выбрано: " + ", ".join(selected))
        stopped: List[str] = []
        try:
            if not existing:
                _pool_target_is_safe_for_first_mount(target)
            write_state(running=True, progress=8, current="packages", message="Устанавливаю инструменты хранилища")
            apt_install("parted", "e2fsprogs", "mergerfs", "util-linux")
            ensure_media_account()
            BRANCH_ROOT.mkdir(parents=True, exist_ok=True)
            fuse_conf = Path("/etc/fuse.conf")
            try:
                text = fuse_conf.read_text(encoding="utf-8", errors="replace") if fuse_conf.exists() else ""
                if not re.search(r"(?m)^\s*user_allow_other\s*$", text):
                    fuse_conf.write_text(text.rstrip() + "\nuser_allow_other\n", encoding="utf-8")
            except Exception:
                pass
            if existing:
                write_state(running=True, progress=14, current="stop", message="Приостанавливаю медиасервисы")
                stopped = _stop_storage_consumers()
                if subprocess.run(["findmnt", "-rn", "-M", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                    run(["umount", str(target)])
            branches = [dict(x) for x in cfg.get("branches", [])] if existing else []
            start_idx = len(branches) + 1
            total = len(selected)
            for offset, disk in enumerate(selected):
                pct = 20 + int((offset / max(1, total)) * 48)
                write_state(running=True, progress=pct, current=disk, message=f"Подготавливаю {disk}")
                branch = _format_pool_disk(disk, start_idx + offset)
                branches.append(branch)
                # Persist membership immediately. If the host loses power before the final mount,
                # the freshly formatted disk is still marked as a pool member and won't be offered for erasure again.
                _save_json(STORAGE_CONFIG_FILE, {
                    "type":"mergerfs", "name": cfg.get("name", pool_name) if existing else pool_name,
                    "mountpoint":str(target), "reserve_gb":int(cfg.get("reserve_gb") or reserve_gb) if existing else reserve_gb,
                    "branches":branches, "pending":True, "updated_at":now_iso(),
                })
            write_state(running=True, progress=72, current="fstab", message="Сохраняю постоянное монтирование")
            block = _managed_fstab_text(branches, target, pool_name if not existing else cfg.get("name", pool_name), reserve_gb if not existing else int(cfg.get("reserve_gb") or reserve_gb))
            _replace_fstab_block(block)
            run(["systemctl", "daemon-reload"], check=False)
            write_state(running=True, progress=80, current="mount", message="Монтирую MediaPool")
            _mount_pool(branches, target)
            _set_media_root_env(target)
            # install_storage now uses the new env value and prepares the folders inside the pool.
            install_storage()
            final_cfg = {
                "type": "mergerfs", "name": cfg.get("name", pool_name) if existing else pool_name,
                "mountpoint": str(target), "reserve_gb": int(cfg.get("reserve_gb") or reserve_gb) if existing else reserve_gb,
                "branches": branches, "pending": False, "updated_at": now_iso(),
            }
            _save_json(STORAGE_CONFIG_FILE, final_cfg)
            write_state(running=True, progress=94, current="verify", message="Проверяю пул")
            if subprocess.run(["findmnt", "-rn", "-M", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
                raise RuntimeError("MediaPool записан в fstab, но не смонтирован")
            if stopped:
                _restart_units(stopped)
                stopped = []
            write_state(running=False, ok=True, component="storage-pool", current="", progress=100, finished_at=now_iso(), message=("Диски добавлены в MediaPool" if existing else "MediaPool создан"), restartRequired=True)
            log("✓ MediaPool готов. Перезапусти MediaHub, чтобы приложение перечитало новый MEDIA_ROOT.")
            return 0
        except Exception as e:
            log(f"✗ Ошибка хранилища: {e}")
            if stopped:
                _restart_units(stopped)
            write_state(running=False, ok=False, component="storage-pool", finished_at=now_iso(), message=str(e))
            return 1


def systemctl_active(unit: str) -> bool:
    return subprocess.run(["systemctl", "is-active", "--quiet", unit], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def systemctl_enabled(unit: str) -> bool:
    return subprocess.run(["systemctl", "is-enabled", "--quiet", unit], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def unit_exists(unit: str) -> bool:
    paths = [Path("/etc/systemd/system") / unit, Path("/lib/systemd/system") / unit, Path("/usr/lib/systemd/system") / unit]
    if any(p.exists() for p in paths):
        return True
    p = subprocess.run(["systemctl", "list-unit-files", unit, "--no-legend"], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return bool(p.stdout.strip())


COMMAND_VERSIONS: Dict[tuple, str] = {}


QBIT_UNITS = ("qbittorrent-nox.service", "qbittorrent.service")
QBIT_UNIT_CACHE: Dict[str, object] = {}


def qbit_unit() -> str:
    """Служба qBittorrent на этом сервере: созданная MediaHUB, ручная qbittorrent.service или qbittorrent-nox@пользователь."""
    if QBIT_UNIT_CACHE.get("expires", 0) > time.time():
        return str(QBIT_UNIT_CACHE["unit"])
    unit = _detect_qbit_unit()
    QBIT_UNIT_CACHE.update(unit=unit, expires=time.time() + 30)
    return unit


def _detect_qbit_unit() -> str:
    """Найти службу qBittorrent без кэша."""
    candidates = list(QBIT_UNITS)
    try:
        p = subprocess.run(["systemctl", "list-units", "--all", "--plain", "--no-legend", "qbittorrent-nox@*.service"], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
        candidates += [line.split()[0] for line in p.stdout.splitlines() if line.split()]
    except Exception:
        pass
    # Работающая служба важнее созданной по умолчанию: иначе портал не видит
    # qBittorrent, установленный вручную, и мастер ставит второй экземпляр на тот же порт.
    for unit in candidates:
        if systemctl_active(unit):
            return unit
    for unit in candidates:
        if unit_exists(unit):
            return unit
    return QBIT_UNITS[0]


def command_version(command: List[str]) -> str:
    """Версия программы; Radarr/Sonarr/Prowlarr стартуют секундами, поэтому ответ хранится до замены файла."""
    try:
        key = (tuple(command), os.stat(shutil.which(command[0]) or command[0]).st_mtime_ns)
    except OSError:
        key = None
    if key in COMMAND_VERSIONS:
        return COMMAND_VERSIONS[key]
    version = ""
    try:
        p = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=5)
        text = (p.stdout or "").strip().splitlines()
        if text:
            m = re.search(r"\d+(?:\.\d+){1,4}(?:[-+][\w.]+)?", " ".join(text[:3]))
            version = m.group(0) if m else text[0][:80]
    except Exception:
        pass
    if key and version:
        COMMAND_VERSIONS[key] = version
    return version


def warm_versions() -> None:
    """Параллельно узнать версии сервисов при старте портала, чтобы первое открытие настроек не ждало."""
    from concurrent.futures import ThreadPoolExecutor
    commands = [[str(Path("/opt") / name / name), "--version"] for name in ("Radarr", "Sonarr", "Prowlarr")]
    commands = [c for c in commands if Path(c[0]).exists()]
    if shutil.which("qbittorrent-nox"):
        commands.append(["qbittorrent-nox", "--version"])
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(command_version, commands))


def dpkg_version(package: str) -> str:
    try:
        p = subprocess.run(["dpkg-query", "-W", "-f=${Version}", package], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
        return p.stdout.strip() if p.returncode == 0 else ""
    except Exception:
        return ""

def qbit_temporary_password(unit: str = QBIT_UNITS[0]) -> str:
    try:
        p=subprocess.run(["journalctl","-u",unit,"-b","-n","100","--no-pager"],text=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=5)
        text=p.stdout or ""
        matches=re.findall(r"temporary password[^:\n]*:\s*([^\s]+)",text,re.I)
        return matches[-1].strip() if matches else ""
    except Exception:
        return ""


def host_info() -> Dict:
    data = {}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                data[k] = v.strip().strip('"')
    except Exception:
        pass
    arch = platform.machine().lower()
    virt = ""
    containerized = False
    try:
        p = subprocess.run(["systemd-detect-virt"], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=3)
        virt = (p.stdout or "").strip() if p.returncode == 0 else ""
        containerized = subprocess.run(["systemd-detect-virt", "--container", "--quiet"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3).returncode == 0
    except Exception:
        pass
    return {
        "name": data.get("PRETTY_NAME") or data.get("NAME") or platform.system(),
        "id": data.get("ID", ""),
        "codename": data.get("VERSION_CODENAME", ""),
        "arch": arch,
        "virtualization": virt,
        "container": containerized,
        "supported": (data.get("ID") in {"debian", "ubuntu"} or "debian" in data.get("ID_LIKE", "") or "ubuntu" in data.get("ID_LIKE", "")) and arch in {"x86_64", "amd64", "aarch64", "arm64", "armv7l", "armhf"},
    }


def component_status() -> List[Dict]:
    root = media_root()
    host_ip = ""
    try:
        host_ip = subprocess.check_output(["hostname", "-I"], text=True, timeout=3).split()[0]
    except Exception:
        host_ip = "127.0.0.1"

    statuses = []
    for key in RECOMMENDED:
        meta = COMPONENT_META[key]
        installed = False
        running = False
        enabled = False
        version = ""
        detail = ""
        temporary_password = ""
        if key == "storage":
            required = [root / "movies", root / "tv", root / "anime", root / "inbox"]
            installed = all(p.exists() for p in required)
            running = installed
            enabled = installed
            detail = str(root)
        elif key == "qbittorrent":
            unit = qbit_unit()
            installed = shutil.which("qbittorrent-nox") is not None or unit_exists(unit)
            running = systemctl_active(unit)
            enabled = systemctl_enabled(unit)
            version = command_version(["qbittorrent-nox", "--version"]) if shutil.which("qbittorrent-nox") else ""
            temporary_password = qbit_temporary_password(unit) if running else ""
            detail = unit
        elif key == "jellyfin":
            installed = bool(dpkg_version("jellyfin")) or shutil.which("jellyfin") is not None
            running = systemctl_active("jellyfin.service")
            enabled = systemctl_enabled("jellyfin.service")
            version = dpkg_version("jellyfin")
        elif key in {"radarr", "sonarr", "prowlarr"}:
            proper = key.capitalize()
            binary = Path("/opt") / proper / proper
            installed = binary.exists()
            running = systemctl_active(f"{key}.service")
            enabled = systemctl_enabled(f"{key}.service")
            version = command_version([str(binary), "--version"]) if installed else ""
        elif key == "automation":
            units = ["mediahub-cache.timer", "mediahub-local-cache.timer", "mediahub-organizer.timer"]
            installed = all(unit_exists(u) for u in units)
            running = all(systemctl_active(u) for u in units)
            enabled = all(systemctl_enabled(u) for u in units)
            detail = "3 таймера MediaHub"
        port = meta.get("port")
        statuses.append({
            "key": key,
            "title": meta["title"],
            "description": meta["description"],
            "installed": installed,
            "running": running,
            "enabled": enabled,
            "version": version,
            "port": port,
            "url": f"http://{host_ip}:{port}" if port else "",
            "detail": detail,
            "temporaryPassword": temporary_password,
        })
    return statuses


def apt_install(*packages: str) -> None:
    run(["apt-get", "update", "-qq"])
    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"
    cmd = ["apt-get", "install", "-y", "-qq", *packages]
    log("Устанавливаю системные зависимости…")
    p = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    if p.stdout:
        for line in p.stdout.rstrip().splitlines():
            log(line)
    if p.returncode:
        raise RuntimeError(f"apt завершился с кодом {p.returncode}")


def ensure_media_account() -> None:
    if subprocess.run(["getent", "group", "media"], stdout=subprocess.DEVNULL).returncode != 0:
        run(["groupadd", "--system", "media"])
    if subprocess.run(["id", "media"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
        run(["useradd", "--system", "--gid", "media", "--home-dir", "/var/lib/media", "--create-home", "--shell", "/usr/sbin/nologin", "media"])


def install_storage() -> None:
    ensure_media_account()
    root = media_root()
    if not root.is_absolute() or str(root.resolve()) in {"/", "/etc", "/usr", "/var", "/opt"}:
        raise RuntimeError(f"Небезопасный MEDIA_ROOT: {root}")
    for p in [root / "movies", root / "tv", root / "anime", root / "inbox" / "movies", root / "inbox" / "tv", root / "inbox" / "anime", root / "inbox" / "manual"]:
        p.mkdir(parents=True, exist_ok=True)
        try:
            shutil.chown(p, user="media", group="media")
            p.chmod(0o2775)
        except Exception:
            pass
    # Keep the MediaHub catalog/history/cache with the media storage so an OS
    # reinstall does not throw away the local brain of the library.
    migrate_database_to_media_root(root)
    _set_media_root_env(root)
    log(f"Структура медиатеки готова: {root}; база: {root / '.mediahub' / 'mediahub.db'}")


def install_qbittorrent() -> None:
    install_storage()
    apt_install("qbittorrent-nox")
    ensure_media_account()
    existing = qbit_unit()
    if existing != QBIT_UNITS[0] and unit_exists(existing):
        # Уже настроенный qBittorrent не дублируем: второй экземпляр не займёт порт 8080
        # и потеряет торренты. Только даём его пользователю доступ к медиатеке и запускаем.
        user = subprocess.run(["systemctl", "show", "-p", "User", "--value", existing], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5).stdout.strip()
        if user and user != "root":
            run(["usermod", "-a", "-G", "media", user], check=False)
        run(["systemctl", "enable", "--now", existing])
        log(f"qBittorrent уже установлен как {existing} — использую эту службу.")
        return
    data = Path("/var/lib/qbittorrent")
    data.mkdir(parents=True, exist_ok=True)
    shutil.chown(data, user="media", group="media")
    config_dir=data / "qBittorrent" / "config"
    config_dir.mkdir(parents=True,exist_ok=True)
    config=config_dir / "qBittorrent.conf"
    if not config.exists():
        config.write_text("""[Preferences]\nWebUI\\Enabled=true\nWebUI\\Address=*\nWebUI\\Port=8080\nWebUI\\LocalHostAuth=false\nWebUI\\UseUPnP=false\n""")
    else:
        text=config.read_text(encoding="utf-8",errors="replace")
        if "WebUI\\LocalHostAuth=" in text:
            text=re.sub(r"(?m)^WebUI\\LocalHostAuth=.*$","WebUI\\LocalHostAuth=false",text)
        else:
            text += "\nWebUI\\LocalHostAuth=false\n"
        config.write_text(text,encoding="utf-8")
    for pp in [data, data / "qBittorrent", config_dir, config]:
        try: shutil.chown(pp,user="media",group="media")
        except Exception: pass
    unit = """[Unit]\nDescription=qBittorrent headless service for MediaHub\nAfter=network-online.target\nWants=network-online.target\n\n[Service]\nType=simple\nUser=media\nGroup=media\nUMask=0002\nEnvironment=HOME=/var/lib/qbittorrent\nExecStart=/usr/bin/qbittorrent-nox --confirm-legal-notice --webui-port=8080 --profile=/var/lib/qbittorrent\nRestart=on-failure\nRestartSec=3\n\n[Install]\nWantedBy=multi-user.target\n"""
    Path("/etc/systemd/system/qbittorrent-nox.service").write_text(unit)
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", "qbittorrent-nox.service"])
    QBIT_UNIT_CACHE.clear()
    log("qBittorrent запущен на порту 8080. При первом входе используй временный пароль из журнала qBittorrent и затем задай постоянный пароль.")


def servarr_arch() -> str:
    a = platform.machine().lower()
    if a in {"x86_64", "amd64"}:
        return "x64"
    if a in {"aarch64", "arm64"}:
        return "arm64"
    if a in {"armv7l", "armhf", "arm"}:
        return "arm"
    raise RuntimeError(f"Архитектура {a} пока не поддерживается автоматической установкой Servarr")


def _servarr_url(app: str, arch: str) -> str:
    if app == "sonarr":
        return f"https://services.sonarr.tv/v1/download/main/latest?version=4&os=linux&arch={arch}"
    return f"https://{app}.servarr.com/v1/update/master/updatefile?os=linux&runtime=netcore&arch={arch}"


def install_servarr(app: str) -> None:
    if app not in {"radarr", "sonarr", "prowlarr"}:
        raise RuntimeError("Unknown Servarr application")
    install_storage()
    apt_install("curl", "ca-certificates", "tar", "libicu-dev", "libssl-dev", "libsqlite3-0")
    ensure_media_account()
    proper = app.capitalize()
    arch = servarr_arch()
    url = _servarr_url(app, arch)
    dest = Path("/opt") / proper
    data = Path("/var/lib") / app
    if unit_exists(f"{app}.service"):
        run(["systemctl","stop",f"{app}.service"],check=False)
    with tempfile.TemporaryDirectory(prefix=f"mediahub-{app}-") as td:
        archive = Path(td) / f"{app}.tar.gz"
        extract = Path(td) / "extract"
        extract.mkdir()
        run(["curl", "-fL", "--retry", "3", "--retry-delay", "2", "-o", str(archive), url])
        run(["tar", "-xzf", str(archive), "-C", str(extract)])
        candidates = [p for p in extract.iterdir() if p.is_dir() and (p / proper).exists()]
        source = candidates[0] if candidates else extract
        if dest.exists():
            backup = Path(f"/opt/{proper}.mediahub-backup")
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
            dest.rename(backup)
        shutil.copytree(source, dest, dirs_exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)
    (data / "tmp").mkdir(parents=True, exist_ok=True)
    for p in [dest, data]:
        for root, dirs, files in os.walk(p):
            try:
                shutil.chown(root, user="media", group="media")
            except Exception:
                pass
            for name in dirs:
                try: shutil.chown(Path(root) / name, user="media", group="media")
                except Exception: pass
            for name in files:
                try: shutil.chown(Path(root) / name, user="media", group="media")
                except Exception: pass
    binary = dest / proper
    binary.chmod(binary.stat().st_mode | 0o111)
    unit = f"""[Unit]\nDescription={proper} daemon for MediaHub\nAfter=network-online.target\nWants=network-online.target\n\n[Service]\nType=simple\nUser=media\nGroup=media\nUMask=0002\nEnvironment=TMPDIR={data}/tmp\nExecStart={binary} -nobrowser -data={data}\nRestart=on-failure\nRestartSec=5\nTimeoutStopSec=20\nLimitNOFILE=65536\n\n[Install]\nWantedBy=multi-user.target\n"""
    Path(f"/etc/systemd/system/{app}.service").write_text(unit)
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", f"{app}.service"])
    run(["systemctl", "restart", f"{app}.service"])
    log(f"{proper} установлен и запущен.")


def install_automation() -> None:
    appdir = Path("/opt/mediahub")
    if not (appdir / "cache_refresh.py").exists():
        raise RuntimeError("Не найдены файлы MediaHub в /opt/mediahub")
    units = {
        "mediahub-cache.service": f"""[Unit]\nDescription=Refresh MediaHub provider/catalog cache\nAfter=network-online.target\nWants=network-online.target\n\n[Service]\nType=oneshot\nWorkingDirectory={appdir}\nEnvironmentFile=/etc/mediahub.env\nExecStart={appdir}/venv/bin/python {appdir}/cache_refresh.py --full\n""",
        "mediahub-cache.timer": """[Unit]\nDescription=Refresh MediaHub discovery cache every 6 hours\n\n[Timer]\nOnBootSec=3min\nOnCalendar=*-*-* 00,06,12,18:00:00\nRandomizedDelaySec=20min\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n""",
        "mediahub-local-cache.service": f"""[Unit]\nDescription=Refresh MediaHub local library cache\nAfter=network-online.target\nWants=network-online.target\n\n[Service]\nType=oneshot\nWorkingDirectory={appdir}\nEnvironmentFile=/etc/mediahub.env\nExecStart={appdir}/venv/bin/python {appdir}/cache_refresh.py --local\n""",
        "mediahub-local-cache.timer": """[Unit]\nDescription=Refresh MediaHub local library every 10 minutes\n\n[Timer]\nOnBootSec=1min\nOnUnitActiveSec=10min\nAccuracySec=30s\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n""",
        "mediahub-organizer.service": f"""[Unit]\nDescription=MediaHub completed download organizer\nAfter=network-online.target mediahub.service\nWants=network-online.target\n\n[Service]\nType=oneshot\nWorkingDirectory={appdir}\nEnvironmentFile=/etc/mediahub.env\nExecStart={appdir}/venv/bin/python {appdir}/download_organizer.py\n""",
        "mediahub-organizer.timer": """[Unit]\nDescription=Organize completed MediaHub downloads\n\n[Timer]\nOnBootSec=2min\nOnUnitActiveSec=1min\nAccuracySec=15s\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n""",
    }
    for name, body in units.items():
        Path("/etc/systemd/system", name).write_text(body)
    run(["systemctl", "daemon-reload"])
    for timer in ["mediahub-cache.timer", "mediahub-local-cache.timer", "mediahub-organizer.timer"]:
        run(["systemctl", "enable", "--now", timer])
    run(["systemctl", "start", "mediahub-local-cache.service"], check=False)
    log("Автоматизация MediaHub включена.")


def install_component(component: str) -> None:
    if component == "storage":
        install_storage()
    elif component == "qbittorrent":
        install_qbittorrent()
    elif component in {"radarr", "sonarr", "prowlarr"}:
        install_servarr(component)
    elif component == "automation":
        install_automation()
    else:
        raise RuntimeError(f"Неизвестный компонент: {component}")


def install_job(component: str) -> int:
    ensure_state_dir()
    with LOCK_FILE.open("w") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("Другой установщик уже работает.")
            return 2
        LOG_FILE.write_text("", encoding="utf-8")
        steps = RECOMMENDED if component == "recommended" else [component]
        if any(x not in RECOMMENDED for x in steps):
            write_state(running=False, ok=False, message="Неизвестный компонент", component=component)
            return 1
        write_state(running=True, ok=None, component=component, progress=0, started_at=now_iso(), finished_at=None, message="Начинаю установку")
        log(f"MediaHub Setup: {component}")
        try:
            for i, item in enumerate(steps, 1):
                pct = int((i - 1) / len(steps) * 100)
                write_state(running=True, component=component, current=item, progress=pct, message=f"Устанавливаю {COMPONENT_META[item]['title']}")
                log("")
                log(f"=== {COMPONENT_META[item]['title']} ({i}/{len(steps)}) ===")
                status = {x["key"]: x for x in component_status()}.get(item, {})
                if status.get("installed") and item != "storage":
                    log("Компонент уже установлен. Выполняю проверку/восстановление конфигурации.")
                install_component(item)
                write_state(running=True, component=component, current=item, progress=int(i / len(steps) * 100), message=f"Готово: {COMPONENT_META[item]['title']}")
            if component == "recommended":
                connect_services()
            write_state(running=False, ok=True, component=component, current="", progress=100, finished_at=now_iso(), message="Установка завершена")
            log("")
            log("✓ Установка завершена.")
            if component == "recommended":
                run(["systemd-run","--unit=mediahub-setup-restart","--collect","--on-active=3s","/usr/bin/systemctl","restart","mediahub.service"],check=False)
            return 0
        except Exception as e:
            log("")
            log(f"✗ Ошибка: {e}")
            write_state(running=False, ok=False, component=component, finished_at=now_iso(), message=str(e))
            return 1


def main() -> int:
    """Запустить системную задачу с общей блокировкой установки и обновления."""
    if len(sys.argv) == 2 and sys.argv[1] == "connect":
        ensure_state_dir()
        try:
            with LOCK_FILE.open('w') as lock:
                fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                write_state(running=True,component='connect',progress=0,message='Подключаю сервисы')
                connect_services()
                write_state(running=False,ok=True,component='connect',progress=100,message='Сервисы подключены')
            return 0
        except BlockingIOError:
            log('Другая системная задача уже выполняется')
            return 2
        except Exception as error:
            log(f"Не удалось связать сервисы: {error}")
            write_state(running=False,ok=False,component='connect',message='Не удалось связать сервисы: '+str(error))
            return 1
    if len(sys.argv) >= 2 and sys.argv[1] == "storage-pool":
        return storage_pool_job()
    if len(sys.argv) < 3 or sys.argv[1] != "install":
        print("Usage: system_setup.py install <component|recommended> | storage-pool")
        return 2
    return install_job(sys.argv[2])


def connect_services() -> None:
    """Связать локальные сервисы через их схемы API без повторного создания записей."""
    ports={"radarr":7878,"sonarr":8989,"prowlarr":9696}
    keys={}
    for name in ports:
        for attempt in range(60):
            try:
                key=ET.parse(Path('/var/lib')/name/'config.xml').getroot().findtext('ApiKey')
                if key:
                    req=urllib.request.Request(f'http://127.0.0.1:{ports[name]}/api/{"v1" if name=="prowlarr" else "v3"}/system/status',headers={'X-Api-Key':key})
                    with urllib.request.urlopen(req,timeout=5) as response: json.load(response)
                    keys[name]=key;break
            except Exception: time.sleep(2)
        else: raise RuntimeError(f'{name} не готов после ожидания')

    def api(name,route,payload=None):
        """Выполнить ограниченный по времени запрос к локальному API."""
        data=None if payload is None else json.dumps(payload).encode()
        req=urllib.request.Request(f'http://127.0.0.1:{ports[name]}/api/{"v1" if name=="prowlarr" else "v3"}/{route}',data=data,headers={'X-Api-Key':keys[name],'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=30) as response:
            body=response.read();return json.loads(body) if body else None

    def create_from_schema(name,route,implementation,title,values,extra):
        """Заполнить схему сервиса, сохраняя значения полей будущих версий API."""
        existing=api(name,route)
        if any(x.get('name')==title for x in existing):return
        schema=next((x for x in api(name,route+'/schema') if x.get('implementation')==implementation),None)
        if not schema:raise RuntimeError(f'Нет схемы {implementation} в {name}')
        schema.pop('id',None);schema.update(name=title,**extra)
        for field in schema.get('fields',[]):
            if field['name'] in values:field['value']=values[field['name']]
        api(name,route,schema)

    # MediaHUB и Servarr подключаются с localhost. Внешний Web UI qBittorrent
    # сохраняет стандартную авторизацию; её отключение для сети не требуется.
    root=media_root()
    for category in ('movies','tv','anime','manual'):
        data=urllib.parse.urlencode({'category':category,'savePath':str(root/'inbox'/category)}).encode()
        req=urllib.request.Request('http://127.0.0.1:8080/api/v2/torrents/createCategory',data=data)
        try:
            with urllib.request.urlopen(req,timeout=10):pass
        except urllib.error.HTTPError as error:
            if error.code!=409:raise
    for name,folders,category in (('radarr',['movies'],'movies'),('sonarr',['tv','anime'],'tv')):
        existing={x.get('path') for x in api(name,'rootfolder')}
        for folder in folders:
            path=str(root/folder)
            if path not in existing:api(name,'rootfolder',{'path':path})
        create_from_schema(name,'downloadclient','QBittorrent','MediaHUB qBittorrent',
            {'host':'127.0.0.1','port':8080,'useSsl':False,'username':'','password':'','movieCategory':category,'tvCategory':category},
            {'enable':True,'priority':1,'removeCompletedDownloads':True,'removeFailedDownloads':True})
        create_from_schema('prowlarr','applications',name.capitalize(),f'MediaHUB {name.capitalize()}',
            {'prowlarrUrl':'http://127.0.0.1:9696','baseUrl':f'http://127.0.0.1:{ports[name]}','apiKey':keys[name]},
            {'syncLevel':'fullSync'})
    # API-ключи записываются только в локальный root-only файл.
    current=ENV_FILE.read_text().splitlines()
    for name,key in keys.items():
        variable=name.upper()+'_API_KEY'
        current=[line for line in current if not line.startswith(variable+'=')]
        current.append(variable+'='+key)
    temporary=ENV_FILE.with_suffix('.tmp');temporary.write_text('\n'.join(current)+'\n');temporary.chmod(0o600);temporary.replace(ENV_FILE)
    log('Сервисы связаны. Добавьте нужные источники в Prowlarr; ключи сервисов вводить не нужно.')


if __name__ == "__main__":
    raise SystemExit(main())

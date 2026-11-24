"""TgWsProxy — локальный MTProto-прокси Telegram (Flowseal/tg-ws-proxy).

Официальные релизы Flowseal/tg-ws-proxy (MIT): трей-приложение поднимает
MTProto-прокси на 127.0.0.1:<port> и уводит трафик Telegram в DC по
WebSocket/TLS - обход частичной блокировки Telegram без сторонних серверов.

Роль Diaspas управляющая и пассивная:
* скачивает ОФИЦИАЛЬНЫЙ exe ассета ``TgWsProxy_windows.exe`` и сверяет
  sha256 с digest из GitHub API (антивирусы любят ругаться на упаковщик -
  проверка хэша обязательна, см. README проекта);
* запускает/останавливает процесс, держит автозапуск (HKCU\\Run);
* строит ссылку ``tg://proxy?...`` из конфига самого приложения;
* по желанию добавляет telegram-домены в ``list-general-user.txt`` zapret
  (прямой TCP до починки WS-прокси) и применяет README-фикс ``dc_ip``
  («не грузит фото/видео»: оставить только ``4:149.154.167.220``).

Конфиг приложения (его же читает трей): ``%APPDATA%\\TgWsProxy\\config.json``
- host/port/secret/dc_ip/autostart, см. docs/RU/TrayConfig.md.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import paths, release

# Без CREATE_NO_WINDOW дочерний консольный процесс (tasklist,
# powershell, taskkill) от GUI-сборки создавал бы мигающее окно:
# фоновый опрос state() дёргает tasklist каждые 5 секунд.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

REPO = "Flowseal/tg-ws-proxy"
ASSET = "TgWsProxy_windows.exe"
EXE_NAME = "TgWsProxy_windows.exe"
IMAGE_NAME = "TgWsProxy_windows.exe"     # имя процесса для tasklist/taskkill
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 1443
RUN_KEY_NAME = "DiaspasTgWsProxy"
# Домены Telegram для user-листа zapret: их же список читает и
# ``--filter-tcp 80,443`` (hostlist list-general), и QUIC-правило.
TELEGRAM_DOMAINS = ("telegram.org", "t.me", "telegra.ph",
                    "tdesktop.com", "telesco.pe")
# README-фикс dc_ip: без него у аккаунтов без Premium не грузятся
# фото/видео (см. README, раздел «Не грузит фото/видео?»).
DC_FIX = ["4:149.154.167.220"]


# -- чистые помощники (их же покрывают юнит-тесты) ---------------------------

def build_link(host: str, port, secret: str) -> str | None:
    """tg://proxy-ссылка; None - секрета нет, ссылку показывать нечего.

    Хэш-секрет MTProto хранится в конфиге 16 байт (32 hex), а в ссылке
    Telegram обязан получить байт-префикс 0xdd («простой секрет») - так же
    его печатает и сам трей («Connect: tg://proxy?...&secret=dd03a0cb…»).
    """
    secret = (secret or "").strip().lower()
    if not secret:
        return None
    if len(secret) == 32 and all(c in "0123456789abcdef" for c in secret):
        secret = "dd" + secret
    return f"tg://proxy?server={host}&port={port}&secret={secret}"


def verify_digest(path: Path, digest: str) -> bool:
    """sha256 файла против ``sha256:<hex>`` из GitHub API."""
    digest = (digest or "").strip().lower()
    if digest.startswith("sha256:"):
        digest = digest[7:]
    if len(digest) != 64:
        return False
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest() == digest


def parse_tg_release(payload) -> dict:
    """Первый непустой релиз с нужным ассетом -> {tag, url, size, sha256}.

    Ассеты у проекта НЕ-zip (сырые exe под каждую ОС) - это отличие от
    релизов zapret, поэтому и разбор свой.
    """
    for rel in payload if isinstance(payload, list) else []:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        for asset in rel.get("assets") or []:
            if str(asset.get("name", "")) != ASSET:
                continue
            url = asset.get("browser_download_url")
            if not url:
                continue
            return {
                "tag": str(rel.get("tag_name") or ""),
                "url": str(url),
                "size": int(asset.get("size") or 0),
                "sha256": str(asset.get("digest") or ""),
                "published": str(rel.get("published_at") or ""),
            }
    raise release.ReleaseError(
        f"в {REPO} нет ассета {ASSET} в свежих релизах")


def list_contains_domains(text: str, domains=TELEGRAM_DOMAINS) -> bool:
    lines = {ln.strip().lower() for ln in text.splitlines()}
    return any(d.lower() in lines for d in domains)


def strip_domains(text: str, domains=TELEGRAM_DOMAINS) -> str:
    """Убрать строки-домены и свой маркер-заголовок, остальное не трогать."""
    wanted = {d.lower() for d in domains}
    out_lines = []
    for line in text.splitlines():
        low = line.strip().lower()
        if low in wanted or low == "# diaspas tgws":
            continue
        out_lines.append(line.rstrip("\r"))
    while out_lines and not out_lines[-1].strip():
        out_lines.pop()
    return "\r\n".join(out_lines) + ("\r\n" if out_lines else "")


def add_domains(text: str, domains=TELEGRAM_DOMAINS) -> str:
    """Дописать домены блоком с маркером (идемпотентно, CRLF)."""
    lines = text.splitlines()
    present = {ln.strip().lower() for ln in lines}
    missing = [d for d in domains if d.lower() not in present]
    if not missing:
        return text
    body = [ln.rstrip("\r") for ln in lines]
    while body and not body[-1].strip():
        body.pop()
    if not any(ln.strip().lower() == "# diaspas tgws" for ln in body):
        body.append("")
        body.append("# Diaspas tgws")
    body.extend(missing)
    return "\r\n".join(body) + "\r\n"


# -- файлы/конфиг ------------------------------------------------------------

def config_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "TgWsProxy"


def config_file() -> Path:
    return config_dir() / "config.json"


def install_dir() -> Path:
    return paths.app_dir() / "tgwsproxy"


def exe_path() -> Path:
    return install_dir() / EXE_NAME


def version_file() -> Path:
    return install_dir() / "VERSION"


def read_config() -> dict:
    try:
        data = json.loads(config_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_config(cfg: dict) -> None:
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)


def cfg_host(cfg: dict | None = None) -> str:
    cfg = cfg if cfg is not None else read_config()
    return str(cfg.get("host") or DEFAULT_HOST)


def cfg_port(cfg: dict | None = None) -> int:
    cfg = cfg if cfg is not None else read_config()
    try:
        return int(cfg.get("port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        return DEFAULT_PORT


def cfg_secret(cfg: dict | None = None) -> str:
    cfg = cfg if cfg is not None else read_config()
    return str(cfg.get("secret") or "")


# -- релизы/установка --------------------------------------------------------

def latest() -> dict:
    """Свежий релиз: {tag, url, size, sha256}. Сеть обязательна."""
    payload = release._api_get(  # noqa: SLF001 - общий клиент GitHub API
        f"https://api.github.com/repos/{REPO}/releases?per_page=10")
    return parse_tg_release(payload)


def installed_tag() -> str | None:
    try:
        tag = version_file().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return tag or None


def download(info: dict, progress=None) -> Path:
    """Скачать ассет в кэш и сверить sha256 (повторная версия - из кэша)."""
    cache = paths.cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / f"tgws-{info['tag']}.exe"
    if target.is_file() and target.stat().st_size == int(info.get("size") or 0):
        if verify_digest(target, info["sha256"]):
            if progress:
                progress(target.stat().st_size, target.stat().st_size)
            return target
        target.unlink(missing_ok=True)

    tmp = target.with_suffix(".exe.part")
    h = hashlib.sha256()
    done = 0
    req = urllib.request.Request(info["url"], headers={
        "User-Agent": f"Diaspas-tgws/{info['tag']}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length") or 0) or info.get("size")
            with open(tmp, "wb") as out:
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    out.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, int(total) if total else None)
        if not verify_digest(tmp, info["sha256"]):
            raise release.ReleaseError(
                "sha256 скачанного файла не совпал с GitHub API - "
                "скачивание испорчено (или подменено), удалено")
        tmp.replace(target)
        return target
    except (urllib.error.URLError, OSError) as exc:
        tmp.unlink(missing_ok=True)
        raise release.ReleaseError(f"не удалось скачать {ASSET}: {exc}") from exc
    except release.ReleaseError:
        tmp.unlink(missing_ok=True)
        raise


def install(progress=None) -> dict:
    """Обновление exe до последнего релиза (с остановкой прокси)."""
    try:
        info = latest()
    except release.ReleaseError as exc:
        return {"ok": False, "error": str(exc)}
    current = installed_tag()
    try:
        src = download(info, progress=progress)
    except release.ReleaseError as exc:
        return {"ok": False, "error": str(exc)}

    stop()
    dest = install_dir()
    dest.mkdir(parents=True, exist_ok=True)
    staged = dest / (EXE_NAME + ".new")
    shutil.copy2(src, staged)
    try:
        staged.replace(exe_path())
    except OSError as exc:
        staged.unlink(missing_ok=True)
        return {"ok": False,
                "error": f"не удалось заменить exe (файл занят?): {exc}"}
    version_file().write_text(info["tag"], encoding="utf-8")
    return {"ok": True, "tag": info["tag"], "updated": bool(current and current != info["tag"]),
            "path": str(exe_path())}


# -- процесс/порт ------------------------------------------------------------

def _image_pids() -> list[int]:
    """PID всех процессов с именем TgWsProxy_windows.exe (tasklist, stdlib)."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {IMAGE_NAME}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15,
            creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return []
    pids: list[int] = []
    for line in (out.stdout or "").splitlines():
        parts = line.split('","')
        if len(parts) >= 2 and parts[0].startswith('"'):
            try:
                pids.append(int(parts[1].strip('"')))
            except ValueError:
                continue
    return pids


def _paths_of_pids(pids: list[int]) -> list[str]:
    """Пулы exe по PID - одной PowerShell-командой (для чужих копий).

    WQL для Win32_Process не принимает ``ProcessId in (...)`` (отклоняет
    весь фильтр) - собираем ``ProcessId=N OR ...``; для одного PID - как есть.
    """
    if not pids:
        return []
    cond = " OR ".join("ProcessId=%d" % p for p in dict.fromkeys(pids))
    cmd = ("Get-CimInstance Win32_Process -Filter \"" + cond +
           "\" | ForEach-Object { $_.ExecutablePath }")
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True, text=True, timeout=20,
            creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return []
    return [ln.strip() for ln in (out.stdout or "").splitlines() if ln.strip()]


# -- подключение клиента к прокси ------------------------------------------

def _parse_client_pids(buf: bytes, port: int) -> list[int]:
    """PID Established-клиентов на порт из буфера GetExtendedTcpTable.

    Чистая функция (без ctypes) - её покрываютюнит-тесты на крафщенной
    таблице. Формат: DWORD dwNumEntries, далее строки по 24 байта
    (MIB_TCPROW_OWNER_PID: state, laddr, lport, raddr, rport, pid);
    порты лежат в network byte order.
    """
    out: list[int] = []
    if len(buf) < 4:
        return out
    (num,) = struct.unpack_from("<I", buf, 0)
    for i in range(num):
        off = 4 + i * 24
        if off + 24 > len(buf):
            break
        state, _la, _lp, _ra, rp, pid = struct.unpack_from("<6I", buf, off)
        if state != 5:                     # MIBTCP_STATE_ESTAB
            continue
        if socket.ntohs(rp & 0xFFFF) == port:   # клиентский сокет: remote = прокси
            if pid not in out:             # у одного клиента несколько соединений
                out.append(pid)
    return out


def _proxy_client_pids(port: int) -> list[int]:
    """PID подключённых к прокси клиентов (iphlpapi, один системный вызов).

    Без subprocess - годится для фонового опроса раз в 5 с: читает таблицу
    TCP ядра напрямую, ничего не порождает и не мигает окнами.
    """
    import ctypes
    from ctypes import wintypes
    AF_INET = 2
    TABLE_CLASS = 4          # TCP_TABLE_OWNER_PID_CONNECTIONS
    ERROR_INSUFFICIENT_BUFFER = 122
    iphlp = ctypes.WinDLL("iphlpapi", use_last_error=True)
    fn = iphlp.GetExtendedTcpTable
    fn.argtypes = [wintypes.LPVOID, wintypes.LPDWORD, wintypes.BOOL,
                   wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
    fn.restype = wintypes.DWORD
    size = wintypes.DWORD(0)
    fn(None, ctypes.byref(size), False, AF_INET, TABLE_CLASS, 0)  # замер
    for _ in range(3):
        if size.value == 0:
            return []
        buf = ctypes.create_string_buffer(size.value)
        ret = fn(buf, ctypes.byref(size), False, AF_INET, TABLE_CLASS, 0)
        if ret == 0:                         # NO_ERROR
            return _parse_client_pids(buf.raw[:size.value], port)
        if ret != ERROR_INSUFFICIENT_BUFFER:
            return []
        # таблица выросла между вызовами - буфер обновлён, повторяем
    return []


def connect_dialog() -> dict:
    """Открыть диалог подключения прокси в Telegram (tg://proxy-ссылка).

    Windows отдаёт tg:// зарегистрированному обработчику - Telegram
    показывает «Add proxy?». Прокси должен быть запущен: не запущен -
    сообщаем в ответе, но ссылку всё равно открываем (настройку можно
    сделать заранее).
    """
    cfg = read_config()
    link = build_link(cfg_host(cfg), cfg_port(cfg), cfg_secret(cfg))
    if not link:
        return {"ok": False,
                "error": "в конфиге нет секрета - сначала запустите прокси"}
    try:
        os.startfile(link)  # noqa: S606 - tg:// обрабатывает Telegram
    except OSError as exc:
        return {"ok": False, "error": f"Telegram не открыл ссылку: {exc}"}
    return {"ok": True, "link": link, "running": bool(_image_pids())}


def check_connection() -> dict:
    """Кто подключён к прокси: число клиентов, их exe, есть ли Telegram."""
    port = cfg_port()
    pids = _proxy_client_pids(port)
    paths = _paths_of_pids(pids) if pids else []
    return {
        "ok": True,
        "connected": bool(pids),
        "clients": len(pids),
        "pids": pids,
        "paths": paths,
        "telegram": any("telegram" in p.lower() for p in paths),
    }


def listening(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def proxy_link() -> str | None:
    cfg = read_config()
    return build_link(cfg_host(cfg), cfg_port(cfg), cfg_secret(cfg))


def start() -> dict:
    """Запустить прокси отдельным процессом (без прав, трей-приложение)."""
    exe = exe_path()
    if not exe.is_file():
        return {"ok": False, "error": "не установлено - нажмите «Установить»"}
    if _image_pids():
        return {"ok": True, "already": True}
    try:
        subprocess.Popen(  # noqa: S603 - свой exe, аргументов нет
            [str(exe)],
            cwd=str(install_dir()),
            creationflags=(getattr(subprocess, "DETACHED_PROCESS", 0x08)
                           | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)),
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    time.sleep(1.2)
    return {"ok": True, "running": bool(_image_pids())}


def stop() -> dict:
    """Остановить все копии TgWsProxy (свою и чужую из папки юзера)."""
    pids = _image_pids()
    if not pids:
        return {"ok": True, "stopped": 0}
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", IMAGE_NAME],
            capture_output=True, timeout=20,
            creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": str(exc)}
    time.sleep(0.6)
    left = _image_pids()
    return {"ok": not left, "stopped": len(pids) - len(left),
            "remaining": len(left)}


def foreign_copies() -> list[str]:
    """Чужие копии exe (не в папке Diaspas) - например, ручная из S:\\Doc."""
    pids = _image_pids()
    paths_found = _paths_of_pids(pids)
    mine = str(exe_path().resolve()).lower()
    out = []
    for p in paths_found:
        try:
            if str(Path(p).resolve()).lower() != mine:
                out.append(p)
        except OSError:
            continue
    return sorted(set(out))


# -- автозапуск / конфиг / zapret-список -------------------------------------

def autostart_enabled() -> bool:
    import winreg
    try:
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            winreg.QueryValueEx(key, RUN_KEY_NAME)
        return True
    except OSError:
        return False


def set_autostart(enable: bool) -> dict:
    """Собственный Run-ключ Diaspas: стартует установленный exe при входе."""
    import winreg
    exe = exe_path()
    if enable and not exe.is_file():
        return {"ok": False, "error": "прокси не установлено"}
    try:
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0, winreg.KEY_SET_VALUE) as key:
            if enable:
                winreg.SetValueEx(key, RUN_KEY_NAME, 0, winreg.REG_SZ,
                                  f'"{exe}"')
            else:
                try:
                    winreg.DeleteValue(key, RUN_KEY_NAME)
                except FileNotFoundError:
                    pass
        return {"ok": True, "autostart": bool(enable)}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def fix_dc_ip() -> dict:
    """README-фикс «не грузит фото/видео»: в dc_ip остаётся DC 4."""
    cfg = read_config()
    if not cfg:
        return {"ok": False, "error": f"конфиг не найден: {config_file()}"}
    if _image_pids():
        return {"ok": False,
                "error": "прокси запущен - остановите его перед правкой конфига"}
    before = [str(x) for x in (cfg.get("dc_ip") or [])]
    if before == DC_FIX:
        return {"ok": True, "changed": False, "dc_ip": before}
    cfg["dc_ip"] = list(DC_FIX)
    try:
        write_config(cfg)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "changed": True, "was": before, "dc_ip": list(DC_FIX)}


def zapret_list_path() -> Path | None:
    """list-general-user.txt активной установки zapret (None - не найдена)."""
    from . import installs
    root = installs.active_path()
    if root is None:
        return None
    p = Path(root) / "lists" / "list-general-user.txt"
    return p if p.is_file() else None


def zapret_list_enabled() -> bool:
    p = zapret_list_path()
    if p is None:
        return False
    try:
        return list_contains_domains(p.read_text(encoding="utf-8",
                                                 errors="replace"))
    except OSError:
        return False


def set_zapret_list(enable: bool) -> dict:
    """Добавить/убрать telegram-домены из user-листа zapret.

    Список читает winws на старте - после изменения службу надо
    перезапустить (зовущая сторона делает это внутри операции).
    """
    p = zapret_list_path()
    if p is None:
        return {"ok": False, "error": "установка zapret не найдена"}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    new = add_domains(text) if enable else strip_domains(text)
    changed = new != text
    if changed:
        try:
            p.write_text(new, encoding="utf-8", newline="")
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
    return {"ok": True, "changed": changed, "enabled": bool(enable),
            "path": str(p)}


# -- состояние ----------------------------------------------------------------

def state(quick: bool = False) -> dict:
    """Срез для карточки UI.

    quick=True (фоновый опрос раз в 5 с): без проб порта и без поиска
    чужих копий - только процесс/тег/ссылка/флаги, чтобы poll оставался
    дешёвым и событие state не мигало без изменений.
    """
    exe = exe_path()
    cfg = read_config()
    host = cfg_host(cfg)
    port = cfg_port(cfg)
    pids = _image_pids()
    link = build_link(host, port, cfg_secret(cfg))
    # клиенты на порту: один вызов GetExtendedTcpTable, без subprocess -
    # kv «Подключение» живёт в фоновом опросе и не мигает консолями
    clients = len(_proxy_client_pids(port)) if pids else 0
    out: dict = {
        "installed": exe.is_file(),
        "path": str(exe) if exe.is_file() else None,
        "tag": installed_tag(),
        "running": bool(pids),
        "pids": pids,
        "host": host,
        "port": port,
        "link": link,
        "autostart": autostart_enabled(),
        "zapret_list": zapret_list_enabled(),
        "config_found": config_file().is_file(),
        "connected": clients > 0,
        "clients": clients,
    }
    if not quick:
        out["listening"] = bool(pids) and listening(host, port)
        out["foreign"] = foreign_copies()
    return out

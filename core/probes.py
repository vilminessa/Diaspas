"""TLS-пробы целевых сервисов: Discord, YouTube, EA, Ubisoft.

Замер - как в dpi.py из Synfronia: рукопожатие без проверки сертификата
(важен сам факт прохождения) плюс GET, успех = любой HTTP-ответ. Отдельно
фиксируется ПРИЧИНА отказа (conn/tls/reset/timeout/http): для строки
состояния важнее того, что не ответило, - почему не ответило.

Пробы идут параллельно и не зависят от состояния службы: результат
«без обхода» и есть диагноз для A/B-теста.
"""

from __future__ import annotations

import socket
import ssl
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

DEFAULT_TIMEOUT = 6.0

# (имя группы, [(подпись цели, url), ...])
# Список покрывает то, что ломалось у нас: авторизация EA/Ubisoft,
# Discord (веб+CDN), YouTube (веб+поток).
PROBE_GROUPS: dict[str, tuple[tuple[str, str], ...]] = {
    "discord": (
        ("web", "https://discord.com/"),
        ("api", "https://discord.com/api/gateway"),
        ("cdn", "https://cdn.discordapp.com/"),
    ),
    "youtube": (
        ("web", "https://www.youtube.com/"),
        ("media", "https://redirector.googlevideo.com/videoplayback"),
    ),
    "ea": (
        ("accounts", "https://accounts.ea.com/"),
        ("signin", "https://signin.ea.com/p/ui/identity"),
        ("www", "https://www.ea.com/"),
    ),
    "ubisoft": (
        ("account", "https://account.ubisoft.com/"),
        ("connect", "https://connect.ubisoft.com/"),
        ("services", "https://public-ubiservices.ubi.com/"),
    ),
}


def probe_url(url: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """{ok, ms, why} по одной цели.

    why: conn - TCP не сошёлся; tls - рукопожатие сброшено (DPI или
    сервер); timeout - тишина по маршруту; http - ответ не похож на HTTP;
    empty - соединение закрыли без ответа.
    """
    started = time.time()

    def out(ok: bool, why: str = "") -> dict:
        return {"ok": bool(ok), "ms": int((time.time() - started) * 1000),
                "why": why, "url": url}

    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    if not host:
        return out(False, "conn")

    try:
        raw = socket.create_connection((host, 443), timeout=timeout)
    except TimeoutError:
        return out(False, "timeout")
    except OSError:
        return out(False, "conn")

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    wrapped = None
    try:
        wrapped = ctx.wrap_socket(raw, server_hostname=host)
    except TimeoutError:
        raw.close()
        return out(False, "timeout")
    except ssl.SSLError:
        raw.close()
        return out(False, "tls")
    except OSError:
        raw.close()
        return out(False, "tls")

    try:
        wrapped.settimeout(timeout)
        request = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
                   f"User-Agent: Diaspas\r\nAccept: */*\r\n"
                   f"Connection: close\r\n\r\n")
        wrapped.sendall(request.encode("ascii", "ignore"))
        answer = wrapped.recv(4096)
        if not answer:
            return out(False, "empty")
        if not answer.startswith(b"HTTP/"):
            return out(False, "http")
        return out(True)
    except TimeoutError:
        return out(False, "timeout")
    except OSError:
        return out(False, "conn")
    finally:
        wrapped.close()


def probe_group(group: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Все цели одной группы параллельно: {state, count, total, targets}.

    state: full - отвечает всё, partial - часть, none - ничего.
    """
    targets_def = PROBE_GROUPS.get(group)
    if not targets_def:
        return {"state": "none", "ok": False, "count": 0, "total": 0,
                "targets": {}, "group": group}
    return _probe_many(targets_def, timeout, group)


def probe_all(timeout: float = DEFAULT_TIMEOUT,
              groups=None) -> dict:
    """Все группы разом: {state, count, total, groups:{имя: отчёт}}."""
    chosen = groups or list(PROBE_GROUPS)
    merged: dict[str, dict] = {}
    total = count = 0
    pool = ThreadPoolExecutor(max_workers=8)
    futures = {pool.submit(probe_group, name, timeout): name
               for name in chosen}
    try:
        for future in as_completed(futures, timeout=timeout + 4.0):
            name = futures[future]
            try:
                merged[name] = future.result()
            except Exception as exc:  # noqa: BLE001 - цель сорвалась
                merged[name] = {"state": "none", "ok": False, "count": 0,
                                "total": 0, "targets": {}, "group": name,
                                "error": str(exc)[:60]}
    except TimeoutError:
        for future, name in futures.items():
            if name not in merged:
                merged[name] = {"state": "none", "ok": False, "count": 0,
                                "total": 0, "targets": {}, "group": name,
                                "error": "timeout"}
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    for report in merged.values():
        total += int(report.get("total") or 0)
        count += int(report.get("count") or 0)
    if total and count == total:
        state = "full"
    elif count == 0:
        state = "none"
    else:
        state = "partial"
    return {"state": state, "ok": state == "full", "count": count,
            "total": total, "groups": merged}


def _probe_many(targets_def, timeout: float, group: str) -> dict:
    targets: dict = {}
    pool = ThreadPoolExecutor(max_workers=len(targets_def))
    futures = {pool.submit(probe_url, url, timeout): name
               for name, url in targets_def}
    try:
        for future in as_completed(futures, timeout=timeout + 2.0):
            name = futures[future]
            try:
                targets[name] = future.result()
            except Exception as exc:  # noqa: BLE001
                targets[name] = {"ok": False, "ms": 0,
                                 "why": f"error:{exc}"[:40]}
    except TimeoutError:
        for future, name in futures.items():
            if name not in targets:
                targets[name] = {"ok": False, "ms": int(timeout * 1000),
                                 "why": "timeout"}
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    # порядок - как в цели: иначе строка журнала меняла вид при каждом замере
    order = {name: i for i, (name, _u) in enumerate(targets_def)}
    targets = dict(sorted(targets.items(), key=lambda kv: order.get(kv[0], 99)))
    count = sum(1 for t in targets.values() if t.get("ok"))
    total = len(targets_def)
    if count == total:
        state = "full"
    elif count == 0:
        state = "none"
    else:
        state = "partial"
    return {"state": state, "ok": state == "full", "count": count,
            "total": total, "targets": targets, "group": group}


def probe_line(report: dict) -> str:
    """Строка журнала: маршрут одной строкой, без интерпретации."""
    parts = []
    for group, rep in (report.get("groups") or {}).items():
        for name, target in (rep.get("targets") or {}).items():
            if target.get("ok"):
                parts.append(f"{group}.{name}=ok({target.get('ms', 0)}ms)")
            else:
                parts.append(f"{group}.{name}={target.get('why', 'fail')}"
                             f"({target.get('ms', 0)}ms)")
    return (f"probe state={report.get('state', 'none')} "
            f"{report.get('count', 0)}/{report.get('total', 0)} "
            + " ".join(parts)).rstrip()


def failed_targets(report: dict) -> list[str]:
    """Список «группа.цель» для неответивших целей - для A/B-вывода."""
    failed = []
    for group, rep in (report.get("groups") or {}).items():
        for name, target in (rep.get("targets") or {}).items():
            if not target.get("ok"):
                failed.append(f"{group}.{name}")
    return failed

"""Скачивание версий zapret с GitHub: два источника, любая версия.

Источники (приём Synfronia):
* Flowseal/zapret-discord-youtube - релизы с zip-ассетами (layout
  flowseal: general*.bat + bin\\winws.exe);
* bol-van/zapret-win-bundle - релизов нет, ветка master через
  zipball/master (layout winws: preset*.cmd + winws.exe рядом).

Diaspas сам не распространяет бинарники: zip берётся напрямую с
репозиториев авторов, распаковывается в папку пользователя, и рядом
оказывается LICENSE.txt - условия MIT выполняются без нашего участия.

Используется только стандартная библиотека (urllib).
"""

from __future__ import annotations

import json
import shutil
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import paths

REPO_FLOWSEAL = "Flowseal/zapret-discord-youtube"
REPO_WINWS = "bol-van/zapret-win-bundle"
REPOS = (REPO_FLOWSEAL, REPO_WINWS)

# LICENSE каждого источника: у win-bundle файла нет - берём MIT оригинала
# bol-van (автора winws и win-bundle)
_LICENSE_URLS = {
    REPO_FLOWSEAL: ("https://raw.githubusercontent.com/"
                    "Flowseal/zapret-discord-youtube/main/LICENSE.txt"),
    REPO_WINWS: ("https://raw.githubusercontent.com/"
                 "bol-van/zapret/master/docs/LICENSE.txt"),
}

_USER_AGENT = "Diaspas-release-check"
_TIMEOUT = 30
# кэш списка релизов: у GitHub лимит 60 запросов/час для анонимусов
RELEASES_TTL = 900


class ReleaseError(RuntimeError):
    """Сетевая ошибка или неожиданный ответ GitHub."""


@dataclass(frozen=True)
class Release:
    repo: str
    tag: str
    name: str
    zip_url: str
    size: int = 0
    published: str = ""
    # master: ветка zipball вместо релиза (у win-bundle релизов нет)
    branch: bool = False

    @property
    def short_repo(self) -> str:
        return self.repo.split("/")[-1]

    @property
    def title(self) -> str:
        return f"{self.short_repo} {self.tag}"


def _api_get(url: str):
    """GET к API; возвращает dict ИЛИ list (releases - список)."""
    request = Request(url, headers={
        "User-Agent": _USER_AGENT,
        "Accept": "application/vnd.github+json",
    })
    try:
        with urlopen(request, timeout=_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 403:
            raise ReleaseError(
                "GitHub отклонил запрос (лимит запросов API без токена). "
                "Повторите через минуту или укажите папку zapret вручную."
            ) from exc
        raise ReleaseError(f"GitHub ответил ошибкой {exc.code}") from exc
    except URLError as exc:
        raise ReleaseError(
            f"нет связи с api.github.com: {exc.reason}") from exc
    except ValueError as exc:
        raise ReleaseError("GitHub вернул не JSON") from exc


def _cache_path():
    return paths.app_dir() / "github_releases.json"


def _release_entries(payload, repo: str) -> list[Release]:
    """Список релизов: только zip-ассеты; нет релизов -> ветка master.

    win-bundle не публикует релизы, поэтому его список - это всегда
    zipball/master (приём Synfronia: «у этого репо релизов нет, качаем
    архив ветки»).
    """
    entries: list[Release] = []
    for rel in payload if isinstance(payload, list) else []:
        assets = [a for a in (rel.get("assets") or [])
                  if str(a.get("name", "")).lower().endswith(".zip")]
        asset = assets[0] if assets else {}
        if not asset.get("browser_download_url"):
            continue
        entries.append(Release(
            repo=repo,
            tag=str(rel.get("tag_name") or ""),
            name=str(rel.get("name") or rel.get("tag_name") or ""),
            zip_url=str(asset["browser_download_url"]),
            size=int(asset.get("size") or 0),
            published=str(rel.get("published_at") or ""),
        ))
    if not entries:
        entries.append(Release(
            repo=repo, tag="master", name="master (ветка)",
            zip_url=(f"https://api.github.com/repos/{repo}"
                     f"/zipball/master"),
            size=0, branch=True))
    return entries


def releases(repo: str = REPO_FLOWSEAL, offline: bool = False) -> list[Release]:
    """Все версии репозитория (новые сверху); кэш живёт RELEASES_TTL.

    offline=True - только кэш (для сетевой ошибки возвращаем устаревший
    список с пометкой через кэш, а не падаем - приём Synfronia).
    """
    if repo not in REPOS:
        raise ReleaseError(f"неизвестный источник: {repo}")
    cache = None
    try:
        cache = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = None

    def cached() -> list[Release] | None:
        if (isinstance(cache, dict) and isinstance(cache.get("repos"), dict)
                and repo in cache["repos"]):
            items = cache["repos"][repo]
            return [Release(**item) for item in items]
        return None

    fresh = (isinstance(cache, dict)
             and time.time() - (cache.get("ts") or 0) < RELEASES_TTL)
    if not offline and fresh:
        got = cached()
        # ловушка «свежий кэш, но записи нет»: без проверки на None
        # повторный запрос другого репо возвращал пустой список вместо API
        if got is not None:
            return got

    if offline:
        got = cached()
        return got if got is not None else []

    payload = _api_get(f"https://api.github.com/repos/{repo}/releases"
                       f"?per_page=30")
    entries = _release_entries(payload, repo)
    repos = dict(cache.get("repos") or {}) if isinstance(cache, dict) else {}
    repos[repo] = [entry.__dict__ for entry in entries]
    try:
        _cache_path().parent.mkdir(parents=True, exist_ok=True)
        _cache_path().write_text(
            json.dumps({"ts": int(time.time()), "repos": repos},
                       ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass  # кэш - оптимизация, его отсутствие не критично
    return entries


def latest_release(repo: str = REPO_FLOWSEAL) -> Release:
    """Последняя версия репозитория (для «скачай последний релиз»)."""
    entries = releases(repo)
    if not entries:
        raise ReleaseError(f"в {repo} нет версий")
    return entries[0]


def release_by_tag(repo: str, tag: str) -> Release | None:
    """Найти версию в уже загруженном списке (без сети, если кэш свеж)."""
    for entry in releases(repo):
        if entry.tag == tag:
            return entry
    return None


def download(release: Release, progress=None) -> Path:
    """Скачать zip в кэш Diaspas; progress(байт, всего|None) - по ходу.

    Каждая версия живёт в кэше отдельно: переключение между версиями не
    перекачивает их заново. Повторное скачивание той же версии возвращает
    кэш, но только если он валиден (zip).
    """
    paths.cache_dir().mkdir(parents=True, exist_ok=True)
    target = paths.cache_dir() / (
        f"zapret-{release.short_repo}-{release.tag}.zip")
    if target.is_file() and target.stat().st_size > 0:
        if zipfile.is_zipfile(target):
            if progress:
                progress(target.stat().st_size, target.stat().st_size)
            return target
        # битый кэш от прошлой попытки (обрыв при записи) - удаляем
        try:
            target.unlink()
        except OSError:
            pass

    request = Request(release.zip_url, headers={"User-Agent": _USER_AGENT})
    tmp = target.with_suffix(".zip.part")
    try:
        with urlopen(request, timeout=_TIMEOUT) as response:
            total = int(response.headers.get("Content-Length") or 0) or None
            done = 0
            with open(tmp, "wb") as out:
                while True:
                    chunk = response.read(1 << 16)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
            if total is not None and done != total:
                raise ReleaseError(
                    f"скачивание оборвано: получено {done} из {total} байт")
            if not zipfile.is_zipfile(tmp):
                raise ReleaseError("скачанный файл не является zip-архивом")
            tmp.replace(target)
            return target
    except HTTPError as exc:
        _cleanup(tmp)
        raise ReleaseError(f"не удалось скачать: HTTP {exc.code}") from exc
    except URLError as exc:
        _cleanup(tmp)
        raise ReleaseError(f"обрыв скачивания: {exc.reason}") from exc
    except ReleaseError:
        _cleanup(tmp)
        raise
    except OSError as exc:
        _cleanup(tmp)
        raise ReleaseError(
            f"не удалось записать файл (проверьте антивирус/диск): {exc}"
        ) from exc


def _cleanup(part: Path) -> None:
    """Удалить недокачанный .part - следующая попытка начнётся чистой."""
    try:
        part.unlink(missing_ok=True)
    except OSError:
        pass


def _locate_root(staging: Path) -> Path | None:
    """Найти папку установки вглубь (до 3 уровней).

    zipball ветки (win-bundle) кладёт всё в <repo>-<sha>/zapret-winws/ -
    одноразовый поиск в корне распаковки его не находит. Критерий -
    service.bat, general*.bat или preset*.cmd (то, по чему install
   判定 установку валидной).
    """
    frontier = [staging]
    for _ in range(3):
        nxt: list[Path] = []
        for d in frontier:
            if ((d / "service.bat").is_file()
                    or list(d.glob("general*.bat"))
                    or list(d.glob("preset*.cmd"))):
                return d
            try:
                nxt.extend(p for p in d.iterdir() if p.is_dir())
            except OSError:
                continue
        if not nxt:
            break
        frontier = nxt
    return None


def install(zip_path: Path, dest: Path, repo: str = REPO_FLOWSEAL,
            progress=None, name: str = "") -> Path:
    """Распаковать релиз в dest (с заменой СВОЕЙ версии), вернуть папку.

    Соседние версии не трогаются: рядом могут жить 1.9.x, 1.10.x и
    win-bundle - хранение нескольких версий для переключения и отката.
    Распаковка идёт во временную staging-папку; в dest переезжает только
    готовый результат, а старая копия ТОЖ ЭТОЙ версии удаляется первой.

    name - имя папки установки; пусто - взять из архива. Для zipball
    ветки имя архива содержит хэш коммита (bol-van-...-6eb463a), который
    меняется от сборки к сборке - вызывающий передаёт стабильное имя.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    staging = dest / f".staging-{int(time.time())}"

    try:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            # у ZipInfo атрибут file_size, а не size - поиск несуществующего
            # поля ронял установку ДО распаковки и оставлял пустую папку
            members = zf.infolist()
            total = sum(m.file_size for m in members) or None
            zf.extractall(staging)
            if progress and total:
                progress(total, total)
    except zipfile.BadZipFile as exc:
        shutil.rmtree(staging, ignore_errors=True)
        # битый кэш убираем: иначе следующая попытка возьмёт его же
        try:
            Path(zip_path).unlink(missing_ok=True)
        except OSError:
            pass
        raise ReleaseError(
            "архив повреждён и удалён из кэша - повторите скачивание"
        ) from exc
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise ReleaseError(
            "распаковка не удалась (антивирус может блокировать запись "
            f"WinDivert): {exc}") from exc

    # Найти папку установки: у zipball она вглубь (zapret-winws/)
    root = _locate_root(staging)
    if root is None:
        shutil.rmtree(staging, ignore_errors=True)
        raise ReleaseError(
            "в распакованном релизе нет ни service.bat, ни preset*.cmd")
    _ensure_license(root, repo)

    # готово: переезд в dest/<имя версии>; старая копия этой же версии
    # удаляется, чужие версии не трогаются
    final = dest / (name or root.name)
    if root.resolve() == final.resolve():
        # flat-архив (без верхней папки): распакованное уже на месте
        return final
    if final.exists():
        shutil.rmtree(final, ignore_errors=True)
        if final.exists():
            # папка занята: чаще всего работающий winws держит свой exe -
            # вызывающий должен остановить службу ДО установки
            raise ReleaseError(
                f"папка {final.name} занята и не удаляется - "
                f"остановите службу zapret и повторите")
    try:
        root.rename(final)
    except OSError as exc:
        raise ReleaseError(
            f"не удалось заменить папку {final.name}: {exc}") from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return final


def _ensure_license(root: Path, repo: str = REPO_FLOWSEAL) -> None:
    """LICENSE.txt в папке установки - обязательство MIT.

    У win-bundle LICENSE нет (проверено на master) - берём MIT оригинала
    bol-van: он автор и winws, и win-bundle. Если и сеть недоступна -
    оставляем указатель на первоисточник: ссылка без текста - минимум.
    """
    target = root / "LICENSE.txt"
    if target.is_file() and target.stat().st_size > 200:
        return

    url = _LICENSE_URLS.get(repo)
    if url:
        try:
            request = Request(url, headers={"User-Agent": _USER_AGENT})
            with urlopen(request, timeout=_TIMEOUT) as response:
                text = response.read().decode("utf-8", "replace")
            if "License" in text or "MIT" in text:
                target.write_text(text, encoding="utf-8", newline="\n")
                return
        except (HTTPError, URLError, OSError, ValueError):
            pass  # ниже - запасной указатель

    try:
        target.write_text(
            "LICENSE.txt отсутствовал в релизе, а загрузить с GitHub не "
            "удалось. Оригинал:\n"
            f"{url or repo}\n\n"
            f"Источник: https://github.com/{repo}\n",
            encoding="utf-8", newline="\n")
    except OSError:
        pass

"""Скачивание релизов zapret-discord-youtube с GitHub.

Diaspas сам не распространяет бинарники: zip берётся напрямую с релизов
оригинального проекта, распаковывается в папку пользователя, и рядом
оказывается LICENSE.txt из архива - условия MIT выполняются без нашего
участия.

Используется только стандартная библиотека (urllib), чтобы не тянуть
зависимости в приложение, которое в основном про Tkinter.
"""

from __future__ import annotations

import json
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import paths

API_RELEASES = (f"https://api.github.com/repos/{paths.ZAPRET_OWNER}/"
                f"{paths.ZAPRET_REPO}/releases/latest")
_USER_AGENT = "Diaspas-release-check"
_TIMEOUT = 30


class ReleaseError(RuntimeError):
    """Сетевая ошибка или неожиданный ответ GitHub."""


@dataclass(frozen=True)
class Release:
    tag: str
    name: str
    zip_url: str
    published: str

    @property
    def size_hint(self) -> str:
        return self.tag


def _api_get(url: str) -> dict:
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


def latest_release() -> Release:
    """Последний релиз репозитория запрета."""
    data = _api_get(API_RELEASES)
    zip_url = ""
    for asset in data.get("assets") or []:
        name = str(asset.get("name") or "")
        if name.lower().endswith(".zip"):
            zip_url = str(asset.get("browser_download_url") or "")
            break
    if not zip_url:
        raise ReleaseError("в последнем релизе нет zip-вложения")
    return Release(
        tag=str(data.get("tag_name") or "?"),
        name=str(data.get("name") or data.get("tag_name") or "?"),
        zip_url=zip_url,
        published=str(data.get("published_at") or ""),
    )


def download(release: Release, progress=None) -> Path:
    """Скачать zip в кэш Diaspas; progress(байт, всего|None) - по ходу."""
    paths.cache_dir().mkdir(parents=True, exist_ok=True)
    target = paths.cache_dir() / f"zapret-{release.tag}.zip"
    if target.is_file() and target.stat().st_size > 0:
        if progress:
            progress(target.stat().st_size, target.stat().st_size)
        return target

    request = Request(release.zip_url, headers={"User-Agent": _USER_AGENT})
    try:
        with urlopen(request, timeout=_TIMEOUT) as response:
            total = int(response.headers.get("Content-Length") or 0) or None
            tmp = target.with_suffix(".zip.part")
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
            tmp.replace(target)
            return target
    except HTTPError as exc:
        raise ReleaseError(f"не удалось скачать релиз: HTTP {exc.code}") from exc
    except URLError as exc:
        raise ReleaseError(f"обрыв скачивания: {exc.reason}") from exc


def install(zip_path: Path, dest: Path, progress=None) -> Path:
    """Распаковать релиз в dest (с заменой), вернуть папку zapret внутри.

    Архив Flowseal распаковывается в подпапку с именем версии; если её нет
    (изменилась структура релиза), содержимое кладётся в dest напрямую.
    """
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = zf.infolist()
            total = sum(m.size for m in members) or None
            done = 0
            zf.extractall(dest)
            done = total or 0
            if progress and total:
                progress(done, total)
    except zipfile.BadZipFile as exc:
        raise ReleaseError("скачанный файл повреждён (не zip)") from exc

    # Найти распакованную папку: если zip содержал единственный каталог - он и есть.
    entries = [p for p in dest.iterdir() if p.is_dir()]
    root = entries[0] if len(entries) == 1 and not any(
        dest.glob("*.bat")) else dest

    if not (root / "service.bat").is_file():
        raise ReleaseError("в распакованном релизе нет service.bat")
    if not (root / "LICENSE.txt").is_file():
        # Не фатально, но NOTICE-обязательство важнее: пометим.
        (root / "LICENSE.txt").write_text(
            "LICENSE.txt отсутствовал в релизе. Оригинал:\n"
            f"https://github.com/{paths.ZAPRET_OWNER}/"
            f"{paths.ZAPRET_REPO}/blob/main/LICENSE.txt\n",
            encoding="utf-8")
    return root


def update_installed(dest: Path, progress=None) -> Path:
    """Скачать последний релиз и распаковать в dest одной операцией."""
    return install(download(latest_release(), progress), dest, progress)

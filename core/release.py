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
    """Скачать zip в кэш Diaspas; progress(байт, всего|None) - по ходу.

    Оборванное соединение не должно выглядеть как успех: полученные байты
    сверяются с Content-Length, а битый кэш с предыдущей попытки
    перекачивается заново, а не переиспользуется.
    """
    paths.cache_dir().mkdir(parents=True, exist_ok=True)
    target = paths.cache_dir() / f"zapret-{release.tag}.zip"
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
        raise ReleaseError(f"не удалось скачать релиз: HTTP {exc.code}") from exc
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


def install(zip_path: Path, dest: Path, progress=None) -> Path:
    """Распаковать релиз в dest (с заменой), вернуть папку zapret внутри.

    Архив Flowseal распаковывается в подпапку с именем версии; если её нет
    (изменилась структура релиза), содержимое кладётся в dest напрямую.

    Каждый шаг обёрнут в ReleaseError с указанием, где встало: пустая папка
    после сбоя - ровно та проблема, что ловили на живой системе.
    """
    dest = Path(dest)
    try:
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ReleaseError(
            f"не удалось подготовить папку {dest}: {exc}") from exc

    try:
        with zipfile.ZipFile(zip_path) as zf:
            # у ZipInfo атрибут file_size, а не size - поиск несуществующего
            # поля ронял установку ДО распаковки и оставлял пустую папку
            members = zf.infolist()
            total = sum(m.file_size for m in members) or None
            zf.extractall(dest)
            if progress and total:
                progress(total, total)
    except zipfile.BadZipFile as exc:
        # битый кэш убираем: иначе следующая попытка возьмёт его же
        try:
            Path(zip_path).unlink(missing_ok=True)
        except OSError:
            pass
        raise ReleaseError(
            "архив повреждён и удалён из кэша - повторите скачивание"
        ) from exc
    except OSError as exc:
        raise ReleaseError(
            "распаковка не удалась (антивирус может блокировать запись "
            f"WinDivert): {exc}") from exc

    # Найти распакованную папку: если zip содержал единственный каталог - он и есть.
    entries = [p for p in dest.iterdir() if p.is_dir()]
    root = entries[0] if len(entries) == 1 and not any(
        dest.glob("*.bat")) else dest

    if not (root / "service.bat").is_file():
        raise ReleaseError("в распакованном релизе нет service.bat")
    _ensure_license(root)
    return root


def _ensure_license(root: Path) -> None:
    """LICENSE.txt в папке установки - обязательство MIT.

    В релизах Flowseal файла нет (проверено на 1.10.3: в архиве только
    .bat), поэтому тянем оригинал с GitHub raw; если и там не вышло -
    оставляем указатель на первоисточник. Ссылка без текста - минимум,
    а не норма.
    """
    target = root / "LICENSE.txt"
    if target.is_file() and target.stat().st_size > 200:
        return

    url = (f"https://raw.githubusercontent.com/{paths.ZAPRET_OWNER}/"
           f"{paths.ZAPRET_REPO}/main/LICENSE.txt")
    try:
        request = Request(url, headers={"User-Agent": _USER_AGENT})
        with urlopen(request, timeout=_TIMEOUT) as response:
            text = response.read().decode("utf-8", "replace")
        if "MIT License" in text:
            target.write_text(text, encoding="utf-8", newline="\n")
            return
    except (HTTPError, URLError, OSError, ValueError):
        pass  # ниже - запасной указатель

    try:
        target.write_text(
            "LICENSE.txt отсутствовал в релизе, а загрузить с GitHub не "
            "удалось. Оригинал:\n"
            f"{url}\n\n"
            f"Источник: https://github.com/{paths.ZAPRET_OWNER}/"
            f"{paths.ZAPRET_REPO}\n",
            encoding="utf-8", newline="\n")
    except OSError:
        pass


def update_installed(dest: Path, progress=None) -> Path:
    """Скачать последний релиз и распаковать в dest одной операцией."""
    return install(download(latest_release(), progress), dest, progress)

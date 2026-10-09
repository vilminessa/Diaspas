"""Webview-интерфейс Diaspas (pywebview/EdgeChromium).

Повторяет каркас gui.py из Synfronia в урезанном виде:

* ``Api`` - класс, выставленный в ``js_api``: JS зовёт
  ``window.pywebview.api.<method>()`` и получает promise;
* ``Emitter`` - очередь событий Python->JS: ``evaluate_js`` вызывается
  ТОЛЬКО из одной нити последовательно (гонки evaluate_js при
  одновременных вызовах - известная проблема pywebview, Synfronia её
  обходит тем же приёмом);
* журнал пишется и в core/log (персистентно), и в окно.

Tkinter-окно остаётся резервным (ui/window.py) до финальной чистки.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import traceback
from pathlib import Path

import webview

from core import abtest, autoselect, log as dlog, paths, presets as presets_mod
from core import probes, release, service
from i18n import tr
from ui.bundle import INDEX_HTML


class Emitter:
    """Последовательная доставка событий в JS из единственной нити."""

    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()
        self._win = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def bind(self, win) -> None:
        self._win = win
        self._ready.set()

    def emit(self, kind: str, payload=None) -> None:
        self._q.put((kind, payload))

    def _run(self) -> None:
        while True:
            kind, payload = self._q.get()
            if not self._ready.is_set():
                # окно ещё не создано - вернуть в очередь и подождать
                self._q.put((kind, payload))
                self._ready.wait(1.0)
                continue
            try:
                data = json.dumps(payload if payload is not None else {},
                                  ensure_ascii=False)
                # U+2028/2029 - валидны в JSON, но рвут JS-строку
                data = data.replace("\u2028", "\\u2028").replace(
                    "\u2029", "\\u2029")
                self._win.evaluate_js(
                    f"window.diaspas && window.diaspas.emit({kind!r}, {data});")
            except Exception:  # noqa: BLE001 - пуш не должен ронять нитку
                dlog.exception(f"emit {kind}")
                self._ready.wait(0.5)


class App:
    """Связка core-логики с окном: лог, состояние, операции."""

    def __init__(self) -> None:
        self.emitter = Emitter()
        self.zapret_root: Path | None = paths.zapret_dir_from_settings()
        self.last_probes: dict | None = None
        self._select_running = False
        # один warning о пропавшей службе на сессию - без спама в журнал
        self._warned_missing_service = False

    # -- журнал --------------------------------------------------------

    def log(self, level: str, text: str) -> None:
        """Единая точка записи: файл (персистентно) + окно."""
        line = dlog.format_line(level, text)
        dlog.write(level, text)
        self.emitter.emit("log", line)

    def hello(self) -> dict:
        """Вызывается JS при инициализации: доказательство живости моста.

        Если в журнале нет этой строки - окно нарисовалось, но api не
        отвечает (типичная поломка после рефакторинга моста).
        """
        self.log("info", "фронтенд подключился: мост JS<->Python работает")
        return {"ok": True, "app": "Diaspas", "ui": "webview"}

    # -- состояние -----------------------------------------------------

    def state(self) -> dict:
        st = service.state(log=self.log)
        current = None
        if self.zapret_root is not None:
            try:
                current = presets_mod.current_preset(
                    self.zapret_root, st,
                    active_key=presets_mod.active_preset_key())
            except Exception:  # noqa: BLE001 - распознавание не валит статус
                self.log("exception", "распознавание пресета")
                current = None
        settings = paths.read_settings()

        # Папка релиза и служба - разные сущности: «скачал релиз» ≠
        # «служба стоит». Если папка есть, а службы нет - это либо свежая
        # установка (пресет не применён), либо службу удалили вне
        # интерфейса; во втором случае следующая диагностика должна
        # найти след в журнале, а не гадать.
        dir_ok = self.zapret_root is not None
        if dir_ok and not st.installed and not self._warned_missing_service:
            self._warned_missing_service = True
            self.log("warning",
                     "папка релиза на месте, а службы zapret нет - "
                     "пресет не применён или служба удалена вне интерфейса "
                     "(service.bat / ручная чистка)")

        return {
            "installed": st.installed,
            "running": st.running,
            "start_type": st.start_type,
            "strategy": st.strategy,
            "pid": st.pid,
            "dir_ok": dir_ok,
            "preset_key": current.key if current else None,
            "preset_title": current.title if current else None,
            # что предложить, если службы нет: последний применявшийся
            # пресет (маркер); без маркера - базовый, безопасный пресет
            # (без игровых фильтров), чтобы цикл «скачал -> создал службу»
            # не зависел от наличия маркера в settings
            "suggested_preset": (presets_mod.active_preset_key()
                                 or "discord_youtube"),
            "zapret_dir": str(self.zapret_root) if self.zapret_root else None,
            "release_note": settings.get("zapret_release") or "",
        }

    def presets(self) -> list[dict]:
        return [
            {"key": p.key, "title": p.title, "description": p.description,
             "strategy": p.strategy, "game_filter": p.game_filter,
             "ipset": p.ipset, "exclude_domains": list(p.exclude_domains),
             "off": p.off}
            for p in presets_mod.load_presets()
        ]

    # -- операции ------------------------------------------------------

    def apply_preset(self, key: str) -> dict:
        preset = next((p for p in presets_mod.load_presets()
                       if p.key == key), None)
        if preset is None:
            return {"ok": False, "error": f"пресет {key!r} не найден"}
        if self.zapret_root is None:
            return {"ok": False, "error": tr("err.no_dir")}
        self.log("info", f"применение пресета {preset.title!r}")
        res = presets_mod.apply(preset, self.zapret_root, log=self.log)
        self.emitter.emit("state", self.state())
        return res

    def probes_run(self) -> dict:
        self.log("info", "пробы: старт")
        report = probes.probe_all(timeout=6.0)
        self.last_probes = report
        line = probes.probe_line(report)
        self.log("info", f"пробы: {line}")
        self.emitter.emit("probes", report)
        return report

    def ab_run(self, group: str) -> dict:
        self.log("info", f"A/B-тест группы {group!r}")
        res = abtest.run(group, log=self.log, wait=10.0)
        self.emitter.emit("state", self.state())
        if res.get("ok"):
            self.log("info", f"A/B вывод: {res.get('verdict')}")
        return res

    def select_run(self) -> dict:
        if self._select_running:
            return {"ok": False, "error": "перебор уже идёт"}
        if self.zapret_root is None:
            return {"ok": False, "error": tr("err.no_dir")}
        self._select_running = True
        try:
            self.log("info", "автоподбор: старт")

            def on_progress(data: dict) -> None:
                self.emitter.emit("select_progress", data)

            res = autoselect.run(self.zapret_root, log=self.log,
                                 on_progress=on_progress, wait=900)
            if res is None:
                return {"ok": False, "error": tr("err.no_uac")}
            if not res.get("ok"):
                return {"ok": False, "error": str(res.get("error"))}
            # итог уже ушёл прогрессом, но дублируем финальным событием
            self.emitter.emit("select_progress",
                              {"done": res.get("done"),
                               "total": res.get("total"),
                               "best": res.get("best"),
                               "results": res.get("results")})
            self.log("info", f"автоподбор: лучшая {res.get('best')!r}")
            self.emitter.emit("state", self.state())
            return res
        finally:
            self._select_running = False

    def select_cancel(self) -> dict:
        autoselect.cancel()
        self.log("info", "автоподбор: запрошена отмена")
        return {"ok": True}

    def install_release(self) -> dict:
        self.log("info", "установка: запрос последнего релиза")

        def progress(done: int, total: int | None) -> None:
            if total:
                self.emitter.emit("toast",
                                  {"text": f"{done * 100 // total}% "
                                           f"({done // 1024}/{total // 1024} КБ)"})

        try:
            rel = release.latest_release()
            self.log("info", f"релиз {rel.tag} ({rel.published})")
            zip_path = release.download(rel, progress=progress)
            dest = paths.app_dir() / "zapret"
            root = release.install(zip_path, dest, progress=progress)
            # служба стартует только при наличии пользовательских списков -
            # service.bat их создаёт при каждом запуске менеджера, а мы
            # распаковываем «чистый» релиз
            presets_mod.ensure_user_lists(root)
            settings = paths.read_settings()
            settings["zapret_dir"] = str(root)
            settings["zapret_release"] = rel.tag
            paths.write_settings(settings)
            self.zapret_root = root
            self.log("info", f"установлено: {root} (релиз {rel.tag})")
            self.emitter.emit("state", self.state())
            # Релиз установлен, но служба создаётся только пресетом:
            # без явного следующего шага пользователь видит «не найдена»
            # и решает, что скачивание сломалось
            st = service.state()
            needs_preset = not st.installed
            result = {"ok": True, "tag": rel.tag, "path": str(root),
                      "needs_preset": needs_preset}
            if needs_preset:
                key = presets_mod.active_preset_key() or "discord_youtube"
                result["active_preset"] = key
                title = next(
                    (p.title for p in presets_mod.load_presets()
                     if p.key == key), key)
                self.log("info",
                         f"служба не создана - предлагаю пресет «{title}»")
            return result
        except release.ReleaseError as exc:
            self.log("error", f"установка: {exc}")
            return {"ok": False, "error": str(exc)}
        except Exception:  # noqa: BLE001
            self.log("exception", "установка релиза")
            return {"ok": False, "error": traceback.format_exc(limit=3)}

    def pick_dir(self) -> dict:
        """Диалог выбора папки - через окно pywebview."""
        import webview as wv
        try:
            win = wv.windows[0] if wv.windows else None
            if win is None:
                return {"ok": False, "error": "окно недоступно"}
            chosen = win.create_file_dialog(wv.FOLDER_DIALOG)
            if not chosen:
                return {"ok": False, "error": tr("err.cancelled")}
            path = Path(chosen[0] if isinstance(chosen, (list, tuple))
                        else chosen)
            if not paths.is_zapret_dir(path):
                return {"ok": False, "error": tr("err.no_dir")}
            settings = paths.read_settings()
            settings["zapret_dir"] = str(path)
            paths.write_settings(settings)
            self.zapret_root = path
            self.log("info", f"папка zapret: {path}")
            self.emitter.emit("state", self.state())
            return {"ok": True, "path": str(path)}
        except Exception:  # noqa: BLE001
            self.log("exception", "выбор папки")
            return {"ok": False, "error": traceback.format_exc(limit=3)}

    # -- журнал --------------------------------------------------------

    def log_tail(self, n: int = 400) -> list[str]:
        return dlog.tail(int(n))

    def open_log(self) -> dict:
        path = dlog.log_path()
        try:
            os.startfile(path)  # noqa: S606 - журнал открывается штатным ассоциированным
            return {"ok": True}
        except OSError as exc:
            self.log("error", f"открыть журнал: {exc}")
            return {"ok": False, "error": str(exc)}


def run() -> int:
    """Создать окно и отдать управление pywebview."""
    app = App()
    app.log("info", "запуск интерфейса (webview)")

    def guard(fn):
        """Обёртка api-метода: каждая ошибка становится журналом (с
        traceback - иначе «api hello» без причины бесполезно), а не
        молчаливым reject'ом promise без следа."""
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception:  # noqa: BLE001
                app.log("exception",
                        f"api {fn.__name__}: {traceback.format_exc(limit=6)}")
                return {"ok": False, "error": traceback.format_exc(limit=4)}
        wrapper.__name__ = fn.__name__
        return wrapper

    # Api собираем объектом, а не классом: функции В КЛАССЕ - дескрипторы,
    # pywebview вызвал бы их с self-инстансом и все аргументы JS съехали
    # бы на позицию (log_tail получил бы 3 аргумента вместо 2). Атрибуты
    # инстанса не биндятся - вызов приходит ровно как из JS.
    api = type("Api", (), {})()
    api.hello = guard(app.hello)
    api.state = guard(app.state)
    api.presets = guard(app.presets)
    api.apply_preset = guard(app.apply_preset)
    api.probes_run = guard(app.probes_run)
    api.ab_run = guard(app.ab_run)
    api.select_run = guard(app.select_run)
    api.select_cancel = guard(app.select_cancel)
    api.install_release = guard(app.install_release)
    api.pick_dir = guard(app.pick_dir)
    api.log_tail = guard(app.log_tail)
    api.open_log = guard(app.open_log)

    win = webview.create_window(
        title="Diaspas (διάσπασις)",
        html=INDEX_HTML,
        js_api=api,
        width=1180,
        height=800,
        min_size=(940, 640),
        background_color="#12161c",
        text_select=True,
    )
    app.emitter.bind(win)
    # хвост журнала прошлого запуска - сразу в окно, до первого вызова JS
    for line in dlog.tail(60):
        app.emitter.emit("log", line)

    webview.start(debug=False)
    app.log("info", "интерфейс закрыт")
    return 0

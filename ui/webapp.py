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
import sys
import threading
import traceback
from pathlib import Path

import webview

import version
from core import abtest, autoselect, autoswitch, log as dlog, paths, presets as presets_mod
from core import probes, release, service, strategies as strategies_mod, tray
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
        self._auto_running = False
        # один warning о пропавшей службе на сессию - без спама в журнал
        self._warned_missing_service = False
        # окно и трей: window - pywebview Window, tray - core.tray.Tray после
        # успешной регистрации; window_hidden - свой флаг, у pywebview нет
        # свойства visible у скрытого окна
        self.window = None
        self.tray: tray.Tray | None = None
        self.window_hidden = False
        self._tray_busy: set[str] = set()

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
        return {"ok": True, "app": "Diaspas", "ui": "webview",
                "label": version.label()}

    def version(self) -> dict:
        """Версия сборки для статус-бара.

        ``label`` - тег сборки (v0.1.2 / b0.1.2) или ``unreleased``, если exe
        собран не из тега: по этой подписи в журнале видно, какая сборка
        вела пробы и перебор стратегий.
        """
        return {"version": version.__version__, "label": version.label(),
                "beta": version.is_beta(),
                "built_from_tag": bool(version.BUILD_LABEL)}

    # -- состояние -----------------------------------------------------

    def state(self) -> dict:
        st = service.state(log=self.log)
        current = None
        infos: list[strategies_mod.StrategyInfo] = []
        if self.zapret_root is not None:
            try:
                current = presets_mod.current_preset(
                    self.zapret_root, st,
                    active_key=presets_mod.active_preset_key())
            except Exception:  # noqa: BLE001 - распознавание не валит статус
                self.log("exception", "распознавание пресета")
                current = None
            try:
                infos = strategies_mod.list_strategies(
                    self.zapret_root, st.strategy)
            except Exception:  # noqa: BLE001 - список стратегий не валит статус
                self.log("exception", "список стратегий")
                infos = []
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

        strategy_file = strategies_mod.current_file(self.zapret_root, st)
        return {
            "installed": st.installed,
            "running": st.running,
            "start_type": st.start_type,
            "strategy": st.strategy,
            # имя файла и короткая метка - для select'а и подсветки:
            # метка реестра («general (ALT11)») для GUI слишком длинная
            "strategy_file": strategy_file,
            "strategy_label": (paths.strategy_label(strategy_file)
                               if strategy_file else (st.strategy or "")),
            "strategy_mode": strategies_mod.mode(),
            "strategies": [{"file": info.file, "label": info.label,
                            "active": info.active} for info in infos],
            "auto_last_at": autoswitch.last_run(),
            "auto_last_strategy": autoswitch.last_strategy(),
            "auto_cooldown_left": autoswitch.cooldown_left(),
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
            # трей: включён ли, поднят ли, скрыто ли окно - UI показывает
            # чекбокс и кнопку «Скрыть» по этим значениям, а не по догадке
            "tray": tray.enabled(),
            "tray_running": bool(self.tray and self.tray.hwnd),
            "window_hidden": self.window_hidden,
        }

    def presets(self) -> list[dict]:
        """Пресеты для GUI: strategy - та, что реально будет поставлена.

        ``strategy`` берётся из выбора пользователя (core.strategies),
        ``default_strategy`` - значение из самого пресета. Фронт показывает
        effective, чтобы подтверждение «Пресет X» не обещало ALT11, когда
        выбрано что-то другое.
        """
        root = self.zapret_root
        return [
            {"key": p.key, "title": p.title, "description": p.description,
             "strategy": "" if p.off else strategies_mod.resolve(
                 p.strategy, root),
             "default_strategy": p.strategy,
             "game_filter": p.game_filter,
             "ipset": p.ipset, "exclude_domains": list(p.exclude_domains),
             "exclude_ips": list(p.exclude_ips),
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
            self.log("info", "перебор стратегий: старт")

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
            self.log("info", f"перебор: лучшая {res.get('best')!r}")
            self.emitter.emit("state", self.state())
            return res
        finally:
            self._select_running = False

    def select_cancel(self) -> dict:
        autoselect.cancel()
        self.log("info", "перебор: запрошена отмена")
        return {"ok": True}

    # -- смена стратегии ---------------------------------------------------

    def apply_strategy(self, file: str) -> dict:
        """Поставить выбранную стратегию; режимы установки сохраняются."""
        if self.zapret_root is None:
            return {"ok": False, "error": tr("err.no_dir")}
        res = strategies_mod.apply(self.zapret_root, str(file or ""),
                                   log=self.log)
        self.emitter.emit("state", self.state())
        return res

    def set_strategy_mode(self, mode: str) -> dict:
        """Включить/выключить автоподбор стратегий (manual | auto)."""
        value = str(mode or "").strip().lower()
        if value not in (strategies_mod.MANUAL, strategies_mod.AUTO):
            return {"ok": False, "error": f"неизвестный режим {value!r}"}
        if not strategies_mod.set_mode(value):
            return {"ok": False, "error": "режим не сохранён"}
        self.log("info", "режим смены стратегии: "
                         + ("авто" if value == strategies_mod.AUTO else "ручной"))
        self.emitter.emit("state", self.state())
        return {"ok": True, "mode": value}

    def auto_run(self, force: bool = False) -> dict:
        """Автоцикл: пробы → перебор → применение лучшей стратегии.

        Вызывается фронтендом при старте (если включён «авто») и после проб
        с сбоями. ``force`` - пропустить cooldown, но режим «авто» обязателен.
        """
        if self._auto_running:
            return {"ok": False, "error": "автоподбор уже идёт"}
        if self.zapret_root is None:
            return {"ok": False, "error": tr("err.no_dir")}
        self._auto_running = True
        try:
            def on_progress(data: dict) -> None:
                self.emitter.emit("select_progress",
                                  {**data, "auto": True})

            res = autoswitch.run(self.zapret_root, force=bool(force),
                                 log=self.log, on_progress=on_progress,
                                 wait=900)
            if res.get("probes"):
                self.last_probes = res["probes"]
                self.emitter.emit("probes", res["probes"])
            if res.get("results"):
                rows = res["results"]
                self.emitter.emit("select_progress",
                                  {"done": len(rows), "total": len(rows),
                                   "best": res.get("best"), "results": rows,
                                   "auto": True})
            self.emitter.emit("state", self.state())
            return res
        finally:
            self._auto_running = False

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

    # -- трей ------------------------------------------------------------

    def mode(self) -> str:
        """Текущий режим смены стратегий (для переключателя в меню трея)."""
        return strategies_mod.mode()

    def set_mode(self, mode: str) -> dict:
        return self.set_strategy_mode(mode)

    def notify(self, title: str, text: str) -> None:
        """Уведомление трея и запись в журнал: журнал остаётся правдой."""
        self.log("info", f"уведомление: {title} · {text}")
        if self.tray:
            try:
                self.tray.notify(title, text)
            except Exception:  # noqa: BLE001 - уведомление не валит операцию
                self.log("exception", "уведомление трея")

    def check_and_autofix(self) -> dict:
        """Пробы, и если режим «авто» и есть сбой - подбор и применение.

        Тот же триггер, что во фронтенде после проб. В Python он нужен для
        трея: со скрытым окном JS ничего не опрашивает, и автопереключение
        наступало бы только при открытом окне.
        """
        report = self.probes_run()
        failed = probes.failed_targets(report)
        if not failed:
            return {"ok": True, "probes": report}
        if strategies_mod.mode() != strategies_mod.AUTO:
            self.notify("Diaspas", f"цели не отвечают: {', '.join(failed)[:60]}")
            return {"ok": True, "probes": report, "failed": failed}
        res = self.auto_run()
        if res.get("ok"):
            label = (res.get("label")
                     or paths.strategy_label(res.get("strategy") or ""))
            if label:
                self.notify("Diaspas", f"авто: применена {label}")
        else:
            self.notify("Diaspas",
                        f"автоподбор: {res.get('error') or 'не удалось'}")
        return {"ok": True, "probes": report, "auto": res, "failed": failed}

    def show_window(self, tab: str | None = None) -> dict:
        """Показать окно (пункт трея «Открыть окно», «Журнал» - с вкладкой)."""
        if self.window is None:
            return {"ok": False, "error": "окно не создано"}
        try:
            self.window.show()
        except Exception:  # noqa: BLE001
            self.log("exception", "show_window")
            return {"ok": False, "error": traceback.format_exc(limit=3)}
        self.window_hidden = False
        self.log("info", tr("tray.shown"))
        if tab:
            self.emitter.emit("tab", {"tab": str(tab)})
        self.emitter.emit("state", self.state())
        return {"ok": True}

    def hide_window(self) -> dict:
        """Скрыть окно: обход и автоциклы продолжают работать."""
        if self.window is None:
            return {"ok": False, "error": "окно не создано"}
        try:
            self.window.hide()
        except Exception:  # noqa: BLE001
            self.log("exception", "hide_window")
            return {"ok": False, "error": traceback.format_exc(limit=3)}
        self.window_hidden = True
        self.log("info", tr("tray.hidden"))
        self.tray_update()
        return {"ok": True}

    def exit_app(self) -> dict:
        """Выход из трея. Спрашиваем только когда обход работает: служба
        zapret живёт отдельно, и закрытие GUI её не выключает."""
        if service.state().running and not tray.confirm(
                "Выход", tr("tray.exit_confirm")):
            self.log("info", "выход из трея отменён")
            return {"ok": True, "cancelled": True}
        self.log("info", "выход из трея: закрытие приложения")
        if self.tray:
            self.tray.quit()
        if self.window is not None:
            try:
                self.window.destroy()   # пустой список окон -> Application.Exit
            except Exception:  # noqa: BLE001
                self.log("exception", "destroy окна")
        return {"ok": True}

    def tray_update(self) -> None:
        if self.tray:
            try:
                self.tray.update()
            except Exception:  # noqa: BLE001
                self.log("exception", "обновление трея")

    def tray_spec(self) -> tray.TraySpec:
        """Меню трея по текущему состоянию: строится на каждый правый клик."""
        return tray.tray_model(self.state(), self.presets(),
                               busy={"auto": self._auto_running,
                                     "select": self._select_running},
                               last_probes=self.last_probes)

    def tray_action(self, action: str) -> dict:
        """Пункт меню трея -> операция.

        Долгие операции (пресет, стратегия, подбор, пробы) уходят в нить:
        цикл трея не может ждать минуты, а состояние в меню уже показано.
        Повторный клик той же операции отказывается, а не запускает вторую.
        """
        if tray.is_heavy(action):
            if action in self._tray_busy or (action == tray.AUTO_RUN
                                             and self._auto_running):
                return {"ok": False, "error": "операция уже выполняется"}
            self._tray_busy.add(action)

            def worker() -> None:
                try:
                    self.log("info", f"трей: {action}")
                    res = tray.handle(action, self)
                    if not res.get("ok"):
                        self.log("error", f"трей {action}: {res.get('error')}")
                except Exception:  # noqa: BLE001
                    self.log("exception", f"трей {action}")
                finally:
                    self._tray_busy.discard(action)
                    self.tray_update()

            threading.Thread(target=worker, daemon=True).start()
            return {"ok": True, "started": action}

        res = tray.handle(action, self)
        if not res.get("ok"):
            self.log("error", f"трей {action}: {res.get('error')}")
        self.tray_update()
        return res

    def start_tray(self) -> dict:
        """Поднять иконку. Мягкий отказ: GUI ведёт себя как без трея."""
        if self.tray:
            return {"ok": True, "hwnd": self.tray.hwnd}
        holder = tray.Tray(self.tray_spec, self.tray_action)
        res = holder.start()
        if res.get("ok"):
            self.tray = holder
            self.log("info",
                     f"трей: иконка зарегистрирована (hwnd={res.get('hwnd')})")
        else:
            self.log("warning", tr("tray.unavailable",
                                   reason=res.get("error") or "неизвестно"))
        return res

    def stop_tray(self) -> None:
        if self.tray:
            self.tray.quit()
            self.tray.join(2.0)
            self.tray = None

    def set_tray(self, value: bool) -> dict:
        """Включить/выключить трей; при выключении иконка снимается."""
        flag = tray.set_enabled(bool(value))
        if flag:
            res = self.start_tray()
            if not res.get("ok"):
                return {"ok": False, "tray": False,
                        "error": res.get("error") or "трей не поднялся"}
            self.log("info", tr("tray.on"))
        else:
            self.stop_tray()
            self.log("info", tr("tray.off"))
        self.emitter.emit("state", self.state())
        return {"ok": True, "tray": flag}


def run() -> int:
    """Создать окно и отдать управление pywebview.

    Трей поднимается в ``func`` после старта GUI: до ``events.shown``
    ``hide``/``destroy`` не работают (pywebview ждёт это событие), и второй
    экземпляр проверяется до создания окна - иначе он плодил бы иконку и
    второй автоцикл.
    """
    app = App()
    hidden_start = "--min" in sys.argv
    app.log("info", f"запуск интерфейса (webview) - сборка {version.label()}")

    holder: tray.Tray | None = None
    if tray.enabled():
        holder = tray.Tray(app.tray_spec, app.tray_action)
        claim = holder.claim_singleton()
        if not claim.get("ok"):
            app.log("warning", f"трей: {claim.get('error')}")
            holder = None
        elif not claim.get("owner"):
            app.log("info", tr("tray.second_instance"))
            return 0

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
    api.version = guard(app.version)
    api.state = guard(app.state)
    api.presets = guard(app.presets)
    api.apply_preset = guard(app.apply_preset)
    api.probes_run = guard(app.probes_run)
    api.ab_run = guard(app.ab_run)
    api.select_run = guard(app.select_run)
    api.select_cancel = guard(app.select_cancel)
    api.apply_strategy = guard(app.apply_strategy)
    api.set_strategy_mode = guard(app.set_strategy_mode)
    api.auto_run = guard(app.auto_run)
    api.install_release = guard(app.install_release)
    api.pick_dir = guard(app.pick_dir)
    api.log_tail = guard(app.log_tail)
    api.open_log = guard(app.open_log)
    api.set_tray = guard(app.set_tray)
    api.hide_window = guard(app.hide_window)

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
    app.window = win
    app.emitter.bind(win)
    # хвост журнала прошлого запуска - сразу в окно, до первого вызова JS
    for line in dlog.tail(60):
        app.emitter.emit("log", line)

    def bootstrap() -> None:
        """После старта GUI: иконка трея и старт скрытым по флагу --min."""
        if holder:
            res = holder.start()
            if res.get("ok"):
                app.tray = holder
                app.log("info",
                        f"трей: иконка зарегистрирована (hwnd={res.get('hwnd')})")
                app.tray_update()
            else:
                app.log("warning", tr("tray.unavailable",
                                     reason=res.get("error") or "неизвестно"))
        if hidden_start:
            app.hide_window()

    # --debug: DevTools WebView2 - консоль JS видна, пока правим фронтенд
    # (в собранном exe флага нет, debug остаётся False)
    webview.start(bootstrap, debug="--debug" in sys.argv)
    app.stop_tray()      # окно закрыли (крестик = выход) - иконку убираем
    app.window = None
    app.log("info", "интерфейс закрыт")
    return 0

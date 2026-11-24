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
import time
import traceback
from pathlib import Path

import webview

import version
from dataclasses import replace

from core import abtest, autoselect, autoswitch, history, installs, log as dlog, oplock, paths
from core import presets as presets_mod
from core import probes, release, service, strategies as strategies_mod, tgproxy, tray, win32
from i18n import tr
from ui.bundle import INDEX_HTML

# Период фонового опроса службы: достаточно часто, чтобы смена стратегии
# через service.bat или автозапуск после перезагрузки были замечены в UI
# без ручного «Обновить», и достаточно редко, чтобы не грузить систему.
_STATE_POLL_SECONDS = 5.0


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
        self.zapret_root, self.zapret_source = paths.zapret_dir_info()
        # формат настроек менялся: плоские переопределения стратегий и
        # ключ-путь установки. Мигрировать надо до первого чтения пресетов,
        # иначе выбор пользователя прочитался бы как «дефолт».
        for change in paths.migrate_settings():
            self.log("info", f"настройки обновлены: {change}")
        self.last_probes: dict | None = None
        self._select_running = False
        self._auto_running = False
        # итог последнего перебора: база для «повторить проваленные»
        self._select_last: dict | None = None
        # один warning о пропавшей службе на сессию - без спама в журнал
        self._warned_missing_service = False
        # окно и трей: window - pywebview Window, tray - core.tray.Tray после
        # успешной регистрации; window_hidden - свой флаг, у pywebview нет
        # свойства visible у скрытого окна
        self.window = None
        self.tray: tray.Tray | None = None
        self.window_hidden = False
        self._tray_busy: set[str] = set()
        # таймер background-обновления состояния (5 сек)
        self._state_timer: threading.Timer | None = None
        self._running = True  # флаг для остановки цикла
        # один warning о дрейфе пресета на сессию
        self._warned_drift = False

    # -- background обновление состояния -------------------------------

    def _refresh_state_loop(self) -> None:
        """Периодически перечитывает состояние службы (раз в 5 сек).

        Единственная точка обнаружения изменений, сделанных вне Diaspas
        (service.bat, автозапуск после перезагрузки ПК): фронтенд получает
        событие ``state`` без ручного «Обновить». Событие шлём только при
        реальном изменении, чтобы не перерисовывать окно вхолостую.
        """
        last: dict | None = None
        while self._running:
            try:
                data = self._collect_state()
                if data != last:
                    last = data
                    self.emitter.emit("state", data)
            except Exception:  # noqa: BLE001 - цикл не должен ронять нитку
                dlog.exception("фоновое обновление состояния")
            time.sleep(_STATE_POLL_SECONDS)

    def _start_state_polling(self) -> None:
        """Запустить background-обновление состояния."""
        self._running = True
        self._state_timer = threading.Timer(0, self._refresh_state_loop)
        self._state_timer.daemon = True
        self._state_timer.start()

    # -- журнал --------------------------------------------------------

    def log(self, level: str, text: str) -> None:
        """Единая точка записи: файл (персистентно) + окно."""
        line = dlog.format_line(level, text)
        dlog.write(level, text)
        self.emitter.emit("log", line)

    def hello(self) -> dict:
        """Вызывается JS при инициализации: доказательство живости моста.

        Если в журнале нет этой строки - окно нарисовалось, но api не
        отвечает (типичная поломка после рефакторинга моста). Здесь же окно
        получает значок: к этому моменту форма создана и её handle жив, а
        PostMessage не блокирует этот поток.
        """
        self.log("info", "фронтенд подключился: мост JS<->Python работает")
        if self.window is not None:
            _post_window_icon(self.window, self.log)
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

    # -- права администратора (warm-up задачи) ---------------------------

    def prepare_privileges(self) -> dict:
        """Есть ли служебная задача планировщика (нужна для операций).

        Нет задачи = первая операция потребует UAC. Фронтенд спрашивает
        согласие заранее - при старте, а не посреди работы.
        """
        have = service.task_exists()
        if not have:
            self.log("info", "служебная задача не зарегистрирована - "
                             "нужно одно подтверждение администратора")
        # тексты диалога приходят из Python: одна точка правды для перевода
        return {"ok": True, "have_task": have,
                "title": tr("uac.title"), "body": tr("uac.body"),
                "declined": tr("uac.declined")}

    def register_task_now(self) -> dict:
        """Регистрация задачи - ЕДИНСТВЕННЫЙ UAC за весь срок работы."""
        if service.task_exists():
            return {"ok": True, "already": True}
        self.log("info", "регистрация служебной задачи: "
                         "сейчас Windows спросит права администратора")
        self.emitter.emit("toast", {
            "text": tr("uac.incoming"), "kind": ""})
        ok = service.register_task()
        if ok:
            self.log("info", "задача зарегистрирована - дальше без UAC")
        else:
            self.log("warning", "регистрация отклонена - потребуется "
                                "подтверждение при каждой операции")
        return {"ok": True, "registered": bool(ok)}

    # -- состояние -----------------------------------------------------

    def state(self) -> dict:
        """Снимок состояния для фронтенда (по запросу или после операции)."""
        return self._collect_state()

    def _collect_state(self) -> dict:
        """Собрать состояние службы и распознать активный пресет.

        Побочные эффекты (по одному разу за сессию): предупреждение о
        пропавшей службе и о дрейфе пресета - маркер `active_preset` есть,
        но факт ему не отвечает (правки через service.bat, ручная чистка
        или автозапуск после перезагрузки ПК).
        """
        st = service.state()
        current = None
        # пресеты под активную папку: стратегия дефолта зависит от layout
        presets = presets_mod.load_presets(self.zapret_root)
        infos: list[strategies_mod.StrategyInfo] = []
        if self.zapret_root is not None:
            try:
                current = presets_mod.current_preset(
                    self.zapret_root, st, presets=presets,
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
        active_key = presets_mod.active_preset_key()
        # что предложить, если службы нет: последний применявшийся пресет
        # (маркер), без маркера - последний известный при дрейфе, иначе
        # базовый безопасный пресет. «Выкл» не кандидат: кнопка «Создать
        # службу» должна службу создавать, а из off-пресета она упиралась
        # бы в тост «выберите пресет» - цикл обрывался.
        suggested = (active_key
                     or presets_mod.last_known_preset_key() or "")
        if suggested not in {p.key for p in presets if not p.off}:
            suggested = next((p.key for p in presets if not p.off),
                             "discord_youtube")
        # Дрейф: служба стоит, маркер есть, но состояние разошлось. Факт
        # важнее маркера - подсветка уйдёт в инференс/«не распознан», а
        # последний известный пресет сохраним для восстановления одним
        # нажатием (needs_restore ниже).
        if (not self._warned_drift and st.installed
                and active_key and active_key != "off" and current is None):
            settings["last_known_preset"] = active_key
            paths.write_settings(settings)
            self.log("warning",
                     f"дрейф пресета «{active_key}»: состояние не отвечает "
                     f"маркеру - правки вне Diaspas (service.bat) или "
                     f"автозапуск после перезагрузки")
            self._warned_drift = True

        return {
            "installed": st.installed,
            "running": st.running,
            "start_type": st.start_type,
            "strategy": st.strategy,
            # имя файла и короткая метка - для select'а и подсветки:
            # метка реестра («general (ALT11)») для GUI слишком длинная
            "strategy_file": strategy_file,
            # короткая метка фактической стратегии (ALT11, а не
            # "general (ALT11)"): окно показывает то, что реально стоит
            "strategy_label": self._strategy_label(st),
            "strategy_mode": strategies_mod.mode(),
            "strategies": [{"file": info.file, "label": info.label,
                            "active": info.active} for info in infos],
            "auto_last_at": autoswitch.last_run(),
            "auto_last_strategy": autoswitch.last_strategy(),
            "auto_cooldown_left": autoswitch.cooldown_left(),
            "pid": st.pid,
            "dir_ok": dir_ok,
            "dir_source": self.zapret_source,
            "preset_key": current.key if current else None,
            "preset_title": current.title if current else None,
            # что предложить, если службы нет: вычисленный выше рабочий
            # пресет (без «Выкл»), чтобы цикл «скачал -> создал службу»
            # не зависел от наличия маркера в settings
            "suggested_preset": suggested,
            # служба работает, но пресет не распознан: предлагаем вернуть
            # управление, применив последний известный пресет
            "needs_restore": bool(st.running and not current
                                  and self.zapret_root is not None),
            "zapret_dir": str(self.zapret_root) if self.zapret_root else None,
            "release_note": settings.get("zapret_release") or "",
            # трей: включён ли, поднят ли, скрыто ли окно - UI показывает
            # чекбокс и кнопку «Скрыть» по этим значениям, а не по догадке
            "tray": tray.enabled(),
            "tray_running": bool(self.tray and self.tray.hwnd),
            "window_hidden": self.window_hidden,
            # TgWsProxy: дешёвый срез (без проб порта) - карточка
            # «Telegram» перерисовывается тем же событием state
            "tg": tgproxy.state(quick=True),
        }

    def set_install(self, path, source: str = "settings") -> None:
        """Единственная точка смены активной установки.

        Присваивания были разбросаны по четырём операциям (переключение
        версии, обновление, скачивание релиза, выбор папки вручную), и
        фоновый опрос мог ещё несколько секунд читать состояние старой
        папки. Здесь смена пути сопровождается событием state.
        """
        self.zapret_root = Path(path) if path is not None else None
        self.zapret_source = source or "settings"
        self.emitter.emit("state", self._collect_state())

    def _strategy_label(self, st: service.ServiceState) -> str:
        """Короткая метка фактической стратегии службы."""
        if not self.zapret_root or not st.strategy:
            return ""
        try:
            name = presets_mod.current_strategy_file(self.zapret_root, st)
        except Exception:  # noqa: BLE001 - метка не должна валить статус
            name = ""
        return paths.strategy_label(name or st.strategy)

    def presets(self) -> list[dict]:
        """Пресеты для GUI: strategy - та, что реально будет поставлена.

        ``strategy`` берётся из выбора пользователя (core.strategies),
        ``default_strategy`` - значение из самого пресета. Фронт показывает
        effective, чтобы подтверждение «Пресет X» не обещало ALT11, когда
        выбрано что-то другое.
        """
        root = self.zapret_root

        def row(p):
            eff = "" if p.off else strategies_mod.resolve(p.strategy, root)
            return {"key": p.key, "title": p.title,
                    "description": p.description,
                    "strategy": eff,
                    "default_strategy": p.strategy,
                    "strategy_label": paths.strategy_label(eff),
                    "overridden": bool(presets_mod.strategy_override(
                        p.key, root)),
                    "game_filter": p.game_filter,
                    "ipset": p.ipset,
                    "exclude_domains": list(p.exclude_domains),
                    "exclude_ips": list(p.exclude_ips),
                    "off": p.off}

        return [row(p) for p in presets_mod.load_presets(self.zapret_root)]

    def strategies(self) -> dict:
        """Стратегии активной установки (для селектора).

        flowseal: general*.bat; winws: preset*.cmd. Плюс привязка к цели:

        * ``current_strategy`` - что ФАКТИЧЕСКИ стоит в службе (метка
          реестра превращена в имя файла), а не обещание пресета;
        * ``target_preset`` - пресет, к которому привяжется выбор: маркер
          -> распознанный пресет -> подсказка. Без этой цепочки смена
          стратегии была доступна только тем, у кого маркер уже есть, а
          служба, поставленная service.bat, переключаться не давала.
        """
        root = self.zapret_root
        if root is None:
            return {"ok": False, "error": tr("err.no_dir")}
        st = service.state()
        presets = presets_mod.load_presets(root)
        marker = presets_mod.active_preset_key()
        try:
            inferred = presets_mod.current_preset(root, st, presets=presets,
                                                 active_key=marker)
        except Exception:  # noqa: BLE001 - распознавание не валит селектор
            inferred = None
        target = (marker
                  or (inferred.key if inferred else None)
                  or presets_mod.last_known_preset_key()
                  # службы нет / пресет не распознан: целимся в первый
                  # рабочий пресет, иначе выбор стратегии вообще некуда
                  or next((p.key for p in presets if not p.off), ""))
        current = presets_mod.current_strategy_file(root, st)
        preset_strategy = next(
            (p.strategy for p in presets if p.key == target), "")
        return {
            "ok": True,
            "layout": installs.detect_layout(root)["layout"] or "",
            "items": presets_mod.available_strategies(root),
            "active_preset": marker,
            "target_preset": target,
            # текущая стратегия активного пресета - что селектор подсветит
            "preset_strategy": preset_strategy,
            # факт: подсвечиваем его, иначе кнопка «Применить» висит на
            # уже выбранном пункте и «текущее» выглядит врём
            "current_strategy": current or preset_strategy,
        }

    def set_preset_strategy(self, key: str, strategy: str) -> dict:
        """Выбрать стратегию для пресета (не применяя службу сразу).

        Главный путь после автоподбора: «взять ALT7» = set + apply.
        Файл сверяется с установкой до записи в настройки - битое имя не
        должно остаться в settings и падать при каждом следующем
        применении.
        """
        root = self.zapret_root
        if root is None:
            return {"ok": False, "error": tr("err.no_dir")}
        preset = next((p for p in presets_mod.load_presets(root)
                       if p.key == key), None)
        if preset is None:
            return {"ok": False, "error": f"пресет {key!r} не найден"}
        if preset.off:
            return {"ok": False,
                    "error": "пресет «Выкл» стратегии не содержит - "
                             "выберите рабочий пресет"}
        files = presets_mod.available_strategies(root)
        if strategy and strategy not in files:
            return {"ok": False,
                    "error": f"в установке нет стратегии {strategy} - "
                             f"выберите одну из {len(files)} в списке"}
        presets_mod.set_strategy(key, strategy or None, root)
        self.log("info", f"стратегия пресета {key!r}: "
                         f"{paths.strategy_label(strategy) if strategy else 'дефолт'}")
        return {"ok": True, "strategy": strategy,
                "strategy_label": paths.strategy_label(strategy)
                                  if strategy else ""}

    def strategy_detail(self, name: str) -> dict:
        """Что делает стратегия и чем отличается от стоящей сейчас.

        Выбор стратегии раньше был выбором вслепую: в списке только имя
        файла. Здесь - разобранные аргументы winws, diff относительно
        фактической стратегии службы и подсказка из кэша перебора.
        """
        root = self.zapret_root
        if root is None:
            return {"ok": False, "error": tr("err.no_dir")}
        st = service.state()
        current = presets_mod.current_strategy_file(root, st)
        chosen = strategies_mod.describe(root, name)
        baseline = strategies_mod.describe(root, current) if current else None
        best = autoselect.load_best(str(root)) or ""
        age = autoselect.best_age(str(root))
        return {"ok": True, "detail": chosen,
                "diff": strategies_mod.diff(baseline, chosen),
                "current": current or "",
                "best": best,
                "best_age": int(age) if age is not None else None,
                # подбор был больше трёх часов назад: показываем это, а не
                # выдаём старый результат за свежий
                "best_stale": bool(best and (age or 0) > 3 * 3600)}

    def try_strategy(self, key: str, strategy: str = "") -> dict:
        """«Попробовать без применения»: поставить, проверить пробами, вернуть.

        Оценка рекомендации перебора до того, как пользователь согласится
        пересоздать службу. Выбор в настройки НЕ пишется: проба не меняет
        состояние пресета, иначе «попробовать» стало бы «применить».
        """
        root = self.zapret_root
        if root is None:
            return {"ok": False, "error": tr("err.no_dir")}
        preset = next((p for p in presets_mod.load_presets(root)
                       if p.key == key), None)
        if preset is None:
            return {"ok": False, "error": f"пресет {key!r} не найден"}
        if preset.off:
            return {"ok": False, "error": "пресет «Выкл» не содержит стратегии"}
        if strategy:
            if strategy not in presets_mod.available_strategies(root):
                return {"ok": False,
                        "error": f"в установке нет стратегии {strategy}"}
            preset = replace(preset, strategy=strategy)

        self.emitter.emit("busy", {"busy": True,
                                   "label": f"пробую {paths.strategy_label(preset.strategy)}…"})
        try:
            res = presets_mod.try_strategy(preset, root, log=self.log)
        finally:
            self.emitter.emit("busy", {"busy": False, "label": "готов"})
            self.emitter.emit("state", self.state())
        return res

    # -- перенос связки «пресет + стратегия + списки» ----------------------

    def export_preset(self, key: str) -> dict:
        """Связка пресета как json: перенос на другую машину/версию."""
        root = self.zapret_root
        if root is None:
            return {"ok": False, "error": tr("err.no_dir")}
        preset = next((p for p in presets_mod.load_presets(root)
                       if p.key == key), None)
        if preset is None:
            return {"ok": False, "error": f"пресет {key!r} не найден"}
        bundle = presets_mod.export_bundle(preset)
        return {"ok": True, "bundle": bundle,
                "json": json.dumps(bundle, ensure_ascii=False, indent=2)}

    def import_preset(self, payload) -> dict:
        """Добавить связку из json как пользовательский пресет.

        Чужой файл стратегии не записывается: пресет создаётся с дефолтом
        этой установки, а стратегию пользователь выбирает селектором.
        """
        root = self.zapret_root
        if root is None:
            return {"ok": False, "error": tr("err.no_dir")}
        try:
            data = json.loads(payload) if isinstance(payload, str) else payload
            key = presets_mod.import_bundle(data, root)
        except (ValueError, TypeError) as exc:
            return {"ok": False, "error": str(exc)}
        self.log("info", f"импортирован пресет {key!r}")
        self.emitter.emit("state", self.state())
        return {"ok": True, "key": key}

    # -- версии zapret ----------------------------------------------------

    def versions(self) -> dict:
        """Реестр установленных версий для селектора в UI.

        Помимо списка - готовые подсказки: что доступно к откату, что
        предложить к удалению сверх лимита, и не отстала ли активная
        версия от последнего релиза (без сети - только по кэшу релиза).
        """
        installs.ensure_registry()
        active = installs.active_path()
        keep = int(installs.load().get("keep_versions")
                   or installs.DEFAULT_KEEP)
        stale = installs.prune()
        settings = paths.read_settings()
        return {
            "items": [
                {"path": str(i.path), "version": i.version,
                 "source": i.source, "last_used": i.last_used,
                 "layout": i.layout or installs.detect_layout(
                     i.dir)["layout"] or "",
                 "active": str(i.path) == str(active),
                 "valid": installs.is_valid(i.path)}
                for i in installs.items()
            ],
            "active": str(active) if active else None,
            "rollback": str(installs.rollback_path() or ""),
            "keep": keep,
            "stale": [{"path": s.path, "version": s.version}
                      for s in stale],
            # лучшая стратегия этой установки - не подмешивать чужой
            "best_strategy": (autoselect.load_best(str(active))
                              if active else None),
            "release_note": settings.get("zapret_release") or "",
        }

    def switch_version(self, path: str) -> dict:
        """Переключиться на другую установленную версию.

        Переключение = активная папка меняется и служба переустанавливается
        на неё с текущим пресетом: списки живут внутри папки версии, ничего
        чужого не подмешивается.
        """
        path = str(Path(path))
        if not installs.is_valid(path):
            return {"ok": False,
                    "error": f"папка недоступна или повреждена: {path}"}
        current = installs.active_path()
        if current is not None and str(current) == path:
            return {"ok": True, "unchanged": True}

        self.log("info", f"переключение версии: {Path(path).name}")
        installs.set_active(path)
        settings = paths.read_settings()
        settings["zapret_dir"] = path
        paths.write_settings(settings)
        self.set_install(path, "settings")

        # служба переустанавливается на новый путь текущим пресетом.
        # Пресеты собираются для НОВОЙ папки: переопределения хранятся по
        # установке, поэтому чужой выбор (ALT13 из 1.10.x) тут не сработает
        # и не уронит переключение версии.
        presets = presets_mod.load_presets(self.zapret_root)
        key = presets_mod.active_preset_key() or "discord_youtube"
        preset = next((p for p in presets if p.key == key), None)
        if preset is None:
            preset = next((p for p in presets if not p.off), presets[0])
        # пресет off остаётся off: служба удаляется, а не молча включается,
        # иначе «Выкл» превратилось бы в обход при смене версии
        res = presets_mod.apply(preset, self.zapret_root, log=self.log)
        self.emitter.emit("state", self.state())
        if not res.get("ok"):
            return {"ok": False, "error": str(res.get("error")),
                    "rolled_back": False}
        return {"ok": True, "applied": preset.key,
                "rollback": str(installs.rollback_path() or "")}

    def rollback_version(self) -> dict:
        """Откатиться на предыдущую активную версию одним действием."""
        target = installs.rollback_path()
        if target is None:
            return {"ok": False, "error": "откатывать некуда"}
        self.log("info", f"откат на {target.name}")
        res = self.switch_version(str(target))
        if res.get("ok"):
            self.log("info", f"откат выполнен: {target.name}")
        return res

    def remove_version(self, path: str) -> dict:
        """Удалить неактивную версию; активную и откатную - нельзя."""
        path = str(Path(path))
        # служба может указывать на удаляемую папку при рассинхроне
        # реестра (register переключает активную, но службу не трогает) -
        # winws держит exe, и папка не удалится
        if installs.service_points_to(path):
            self.log("info", "служба указывает на удаляемую папку - "
                             "останавливаю")
            service.stop(log=self.log)
        if not installs.remove(path):
            return {"ok": False,
                    "error": "не удалось удалить: папка активная, откатная "
                             "или файлы заняты (остановите службу)"}
        self.log("info", f"версия удалена: {Path(path).name}")
        self.emitter.emit("versions", self.versions())
        return {"ok": True}

    # -- обновление и откат ------------------------------------------------

    def available_releases(self) -> dict:
        """Все версии обоих источников для скачивания.

        Flowseal - реальные релизы с zip-ассетами; win-bundle - ветка
        master (релизов нет). Списки кэшируются release-ом (TTL 900 с);
        сетевая ошибка возвращает устаревший кэш с пометкой stale.
        """
        out: dict = {"ok": True, "repos": []}
        for repo in release.REPOS:
            try:
                entries = release.releases(repo)
            except release.ReleaseError as exc:
                stale = release.releases(repo, offline=True)
                out["repos"].append({
                    "repo": repo, "error": str(exc),
                    "entries": [e.__dict__ for e in stale], "stale": True})
                continue
            out["repos"].append({
                "repo": repo, "entries": [e.__dict__ for e in entries],
                "stale": False})
        return out

    def download_release(self, repo: str, tag: str) -> dict:
        """Скачать и установить выбранную версию (рядом с текущими).

        Путь общий с install_release: служба останавливается заранее,
        версия попадает в реестр, предлагается пресет.
        """
        entry = release.release_by_tag(repo, tag)
        if entry is None:
            return {"ok": False,
                    "error": f"версия {tag} не найдена в списке (кэш "
                             f"устарел?) - обновите список"}
        return self._install_release_entry(entry)

    def check_update(self) -> dict:
        """Есть ли релиз новее активной версии (сеть требуется)."""
        active = installs.active_path()
        current = installs.version_of(active) if active else None
        try:
            rel = release.latest_release()
        except release.ReleaseError as exc:
            return {"ok": False, "error": str(exc), "current": current}
        return {"ok": True, "current": current, "latest": rel.tag,
                "update_available": bool(current and rel.tag != current)}

    def update_latest(self) -> dict:
        """Обновление до последнего релиза с автопроверкой и откатом.

        Новая версия ставится РЯДОМ (соседи не удаляются), пресет
        применяется к ней, пробы решают судьбу: всё зелёное - остаёмся,
        что-то красное - откатываемся на прежнюю версию одним шагом.
        """
        active = installs.active_path()
        prev = str(active) if active else None
        current = installs.version_of(active) if active else None

        def progress(done: int, total: int | None) -> None:
            if total:
                self.emitter.emit("toast", {
                    "text": f"{done * 100 // total}% "
                            f"({done // 1024}/{total // 1024} КБ)"})

        try:
            rel = release.latest_release()
        except release.ReleaseError as exc:
            self.log("error", str(exc))
            return {"ok": False, "error": str(exc)}
        if current == rel.tag:
            return {"ok": True, "up_to_date": True, "tag": rel.tag}
        self.log("info", f"обновление: {current} -> {rel.tag}")
        # новая версия ставится рядом, но пресет применяется к ней через
        # пересоздание службы - прежнюю (если она на этой же папке)
        # останавливаем заранее, иначе exe заблокирован
        if service.state().installed:
            self.log("info", "остановка службы перед установкой")
            service.stop(log=self.log)

        try:
            zip_path = release.download(rel, progress=progress)
            root = release.install(zip_path, paths.app_dir() / "zapret",
                                   progress=progress)
            presets_mod.ensure_user_lists(root)
        except release.ReleaseError as exc:
            self.log("error", f"обновление: {exc}")
            return {"ok": False, "error": str(exc)}

        settings = paths.read_settings()
        settings["zapret_dir"] = str(root)
        settings["zapret_release"] = rel.tag
        paths.write_settings(settings)
        installs.register(root, source="download")   # prev -> rollback
        self.set_install(root, "settings")

        key = presets_mod.active_preset_key() or "discord_youtube"
        presets = presets_mod.load_presets(root)
        preset = next((p for p in presets if p.key == key), None)
        if preset is None or preset.off:
            # пресет под новую папку: переопределений прежней версии тут
            # нет, а без обхода проверка обновления бессмысленна
            preset = next((p for p in presets if not p.off), presets[0])
        res = presets_mod.apply(preset, root, log=self.log)
        if not res.get("ok"):
            self._rollback_after_update(prev)
            return {"ok": False, "error": str(res.get("error")),
                    "rolled_back": True, "tag": rel.tag}

        # автопроверка: обновление не должно ломать обход
        self.log("info", "обновление: проверяю обход на новой версии")
        report = probes.probe_all(timeout=8.0)
        self.emitter.emit("probes", report)
        self.emitter.emit("state", self.state())
        if report["state"] != "full":
            failed = probes.failed_targets(report)
            self.log("error",
                     f"обновление {rel.tag} сломало обход: {failed}")
            rolled = self._rollback_after_update(prev)
            return {"ok": False, "rolled_back": bool(rolled),
                    "tag": rel.tag, "failed": failed,
                    "error": "новая версия не прошла проверку обхода"}
        self.log("info", f"обновление {rel.tag}: обход проверен (11/11)")
        return {"ok": True, "tag": rel.tag, "up_to_date": False,
                "stale": [{"path": s.path, "version": s.version}
                          for s in installs.prune()]}

    def _rollback_after_update(self, prev: str | None) -> bool:
        """Вернуть прежнюю версию после неудачного обновления."""
        if not prev or not installs.is_valid(prev):
            return False
        self.log("warning", f"возврат на прежнюю версию: {Path(prev).name}")
        try:
            self.switch_version(prev)
            return True
        except Exception:  # noqa: BLE001
            self.log("exception", "откат после обновления")
            return False

    # -- ремонт установки ------------------------------------------------

    def check_install(self) -> dict:
        """Целостность активной установки: winws.exe, файлы стратегий."""
        root = self.zapret_root
        if root is None:
            return {"ok": False, "error": tr("err.no_dir")}
        missing = installs.integrity(root)
        return {"ok": True, "missing": missing,
                "version": installs.version_of(root)}

    def repair_install(self) -> dict:
        """Восстановить недостающее из архива этой же версии (если есть)."""
        root = self.zapret_root
        if root is None:
            return {"ok": False, "error": tr("err.no_dir")}
        version = installs.version_of(root)
        zip_path = paths.cache_dir() / f"zapret-{version}.zip"
        if not zip_path.is_file():
            # архив мог быть переименован другим тегом - ищем по версии
            candidates = list(paths.cache_dir().glob(f"zapret-*{version}*.zip"))
            zip_path = candidates[0] if candidates else zip_path
        if not zip_path.is_file():
            return {"ok": False,
                    "error": f"архив {version} не найден в кэше - "
                             f"скачайте релиз заново"}
        try:
            restored = installs.repair(root, zip_path)
        except release.ReleaseError as exc:
            self.log("error", str(exc))
            return {"ok": False, "error": str(exc)}
        self.log("info", f"ремонт {version}: восстановлено файлов {restored}")
        return {"ok": True, "restored": restored}

    # -- операции ------------------------------------------------------

    def apply_preset(self, key: str, strategy: str | None = None,
                     verify: bool = False) -> dict:
        """Применить пресет; strategy - переопределение его стратегии.

        Стратегия сохраняется в настройках ЭТОЙ установки (маркер/инференс/
        установка службы сходятся), затем пресет применяется как обычно.
        При неудаче переопределение остаётся: это выбор пользователя, а не
        состояние службы, и следующий «Применить» обязан использовать его.
        """
        if self.zapret_root is None:
            return {"ok": False, "error": tr("err.no_dir")}
        if strategy:
            res = self.set_preset_strategy(key, strategy)
            if not res.get("ok"):
                return res
        preset = next((p for p in presets_mod.load_presets(self.zapret_root)
                       if p.key == key), None)
        if preset is None:
            return {"ok": False, "error": f"пресет {key!r} не найден"}
        # стратегия в журнале: без неё все применения выглядели как ALT11
        self.log("info", f"применение пресета {preset.title!r}"
                         + ("" if preset.off
                            else f" ({paths.strategy_label(preset.strategy)})"))
        if not service.task_exists():
            # окно UAC появится неожиданно - предупредить заранее
            self.log("info", tr("uac.incoming"))
            self.emitter.emit("toast",
                              {"text": tr("uac.incoming"), "kind": ""})
        # verify: проверить обход пробами и откатать, если он пропал.
        # Дорого по времени (секунды), поэтому только по явному запросу UI.
        res = (presets_mod.apply_verified(preset, self.zapret_root,
                                          log=self.log)
               if verify and not preset.off
               else presets_mod.apply(preset, self.zapret_root, log=self.log))
        self.emitter.emit("state", self.state())
        return res

    def probes_run(self) -> dict:
        self.log("info", "пробы: старт")
        report = probes.probe_all(timeout=6.0)
        self.last_probes = report
        line = probes.probe_line(report)
        self.log("info", f"пробы: {line}")
        # замер в историю: «было/стало» нужно и после закрытия окна, и
        # когда пользователь менял стратегию в прошлый сеанс
        st = service.state()
        history.append({
            "state": report.get("state"), "count": report.get("count"),
            "total": report.get("total"),
            "preset": presets_mod.active_preset_key() or "",
            "strategy": st.strategy or "",
            "install": str(self.zapret_root or ""),
            "groups": {name: {"state": rep.get("state"),
                              "count": rep.get("count"),
                              "total": rep.get("total")}
                       for name, rep in (report.get("groups") or {}).items()},
        })
        self.emitter.emit("probes", report)
        return report

    def probe_history(self, n: int = 12) -> dict:
        """Последние замеры: история проб с привязкой к пресету и стратегии."""
        try:
            limit = max(1, min(int(n or 12), history.LIMIT))
        except (TypeError, ValueError):
            limit = 12
        return {"ok": True, "items": history.tail(limit),
                "total": len(history.tail(history.LIMIT))}

    def probe_groups_add(self, name: str, url: str) -> dict:
        """Добавить цель в пользовательскую группу пробы."""
        name = (name or "").strip()
        url = (url or "").strip()
        if not name:
            return {"ok": False, "error": "нужно имя группы"}
        if not (url.startswith("http://") or url.startswith("https://")):
            return {"ok": False, "error": "адрес должен начинаться с http:// или https://"}
        settings = paths.read_settings()
        groups = settings.get("probe_groups") or {}
        if not isinstance(groups, dict):
            groups = {}
        items = groups.get(name) or []
        if not isinstance(items, list):
            items = []
        if any((isinstance(i, dict) and i.get("url") == url) or
               (isinstance(i, (list, tuple)) and len(i) == 2 and i[1] == url)
               for i in items):
            return {"ok": False, "error": "такая цель уже есть"}
        label = url.split("/")[2] if "//" in url else url
        items.append({"name": label, "url": url})
        groups[name] = items
        settings["probe_groups"] = groups
        paths.write_settings(settings)
        self.log("info", f"пробы: группа {name!r} += {url}")
        return {"ok": True, "group": name, "items": items}

    def probe_groups_remove(self, name: str) -> dict:
        settings = paths.read_settings()
        groups = settings.get("probe_groups") or {}
        if not isinstance(groups, dict) or name not in groups:
            return {"ok": False, "error": f"группы {name!r} нет в пользовательских"}
        groups.pop(name, None)
        settings["probe_groups"] = groups
        paths.write_settings(settings)
        self.log("info", f"пробы: группа {name!r} удалена")
        return {"ok": True}

    def probe_groups(self) -> dict:
        """Доступные группы пробы: встроенные и пользовательские."""
        user = probes.user_groups()
        return {"ok": True,
                "builtin": list(probes.PROBE_GROUPS),
                "user": {name: list(targets) for name, targets in user.items()}}

    def ab_run(self, group: str) -> dict:
        self.log("info", f"A/B-тест группы {group!r}")
        res = abtest.run(group, log=self.log, wait=10.0)
        self.emitter.emit("state", self.state())
        if res.get("ok"):
            self.log("info", f"A/B вывод: {res.get('verdict')}")
        return res

    def select_run(self, strategies=None, failed_only: bool = False) -> dict:
        """Перебор стратегий. failed_only - только не давшие полного обхода.

        Полный перебор пересоздаёт службу на каждую стратегию и длится
        долго; повторить имеет смысл именно проваленные, если обход
        сломался после смены сети/провайдера.
        """
        if self._select_running:
            return {"ok": False, "error": "перебор уже идёт"}
        if self.zapret_root is None:
            return {"ok": False, "error": tr("err.no_dir")}
        names: list[str] | None = None
        if failed_only:
            names = autoselect.failed_names(self._select_last or {})
            if not names:
                return {"ok": False,
                        "error": "проваленных стратегий нет - полный перебор не нужен"}
            self.log("info", f"автоподбор: повтор {len(names)} проваленных")
        elif strategies:
            names = [str(s) for s in strategies]

        self._select_running = True
        try:
            self.log("info", "перебор стратегий: старт")

            def on_progress(data: dict) -> None:
                self.emitter.emit("select_progress", data)

            res = autoselect.run(self.zapret_root, strategies=names, log=self.log,
                                 on_progress=on_progress, wait=900)
            if res is None:
                return {"ok": False, "error": tr("err.no_uac")}
            if not res.get("ok"):
                return {"ok": False, "error": str(res.get("error"))}
            self._select_last = res      # база для «перебрать проваленные»
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
        """Установить последний релиз основного источника (Flowseal)."""
        self.log("info", "установка: запрос последнего релиза")
        try:
            rel = release.latest_release()
        except release.ReleaseError as exc:
            self.log("error", f"установка: {exc}")
            return {"ok": False, "error": str(exc)}
        return self._install_release_entry(rel)

    def _install_release_entry(self, rel: release.Release) -> dict:
        """Общий путь установки выбранной версии (и последней, и любой).

        Версия ставится рядом с соседями, попадает в реестр (прежняя
        активная становится точкой отката); дальше фронт замыкает цикл:
        пресет создаёт службу.
        """
        def progress(done: int, total: int | None) -> None:
            if total:
                self.emitter.emit("toast",
                                  {"text": f"{done * 100 // total}% "
                                           f"({done // 1024}/{total // 1024} КБ)"})

        try:
            self.log("info", f"релиз {rel.title} ({rel.published or 'ветка'})")
            # переустановка своей версии: работающий winws держит exe и
            # не даёт заменить папку - служба останавливается заранее
            if service.state().installed:
                self.log("info", "остановка службы перед установкой")
                service.stop(log=self.log)
            zip_path = release.download(rel, progress=progress)
            dest = paths.app_dir() / "zapret"
            # zipball ветки распаковывается в <repo>-<sha> (хэш меняется) -
            # даём стабильное имя zapret-<repo>-<tag>
            name = (f"zapret-{rel.short_repo}-{rel.tag}"
                    if rel.branch else "")
            root = release.install(zip_path, dest, repo=rel.repo,
                                   progress=progress, name=name)
            # служба стартует только при наличии пользовательских списков -
            # service.bat их создаёт при каждом запуске менеджера, а мы
            # распаковываем «чистый» релиз; у winws-layout списков нет
            from core import installs as _installs
            if _installs.detect_layout(root)["layout"] != \
                    _installs.LAYOUT_WINWS:
                presets_mod.ensure_user_lists(root)
            settings = paths.read_settings()
            settings["zapret_dir"] = str(root)
            settings["zapret_release"] = rel.tag
            paths.write_settings(settings)
            self.set_install(root, "settings")
            # версия попадает в реестр и становится активной; прежняя
            # активная сохраняется как точка отката
            installs.register(root, source="download")
            self.log("info", f"установлено: {root} (релиз {rel.tag})")
            self.emitter.emit("state", self.state())
            # Релиз установлен, но служба создаётся только пресетом:
            # без явного следующего шага пользователь видит «не найдена»
            # и решает, что скачивание сломалось
            st = service.state()
            needs_preset = not st.installed
            result = {"ok": True, "tag": rel.tag, "path": str(root),
                      "needs_preset": needs_preset,
                      # версии сверх лимита - фронт предложит удалить
                      "stale": [{"path": s.path, "version": s.version}
                                for s in installs.prune()]}
            if needs_preset:
                # какой пресет предложить: маркер, но не «Выкл» - служба
                # из off-пресета не создаётся, и кнопка упиралась бы в тост
                presets = presets_mod.load_presets(root)
                key = presets_mod.active_preset_key()
                preset = next((p for p in presets
                               if p.key == key and not p.off), None)
                if preset is None:
                    preset = next((p for p in presets if not p.off),
                                  presets[0])
                result["active_preset"] = preset.key
                result["active_strategy"] = paths.strategy_label(
                    preset.strategy)
                self.log("info", f"служба не создана - предлагаю пресет "
                                 f"«{preset.title}» "
                                 f"({result['active_strategy']})")
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
            self.set_install(path, "manual")
            # ручная папка тоже попадает в реестр версий
            installs.register(path, source="manual")
            self.log("info", f"папка zapret: {path}")
            self.emitter.emit("state", self.state())
            return {"ok": True, "path": str(path)}
        except Exception:  # noqa: BLE001
            self.log("exception", "выбор папки")
            return {"ok": False, "error": traceback.format_exc(limit=3)}

    # -- журнал --------------------------------------------------------

    def cancel_operation(self) -> dict:
        """Отменить текущую операцию (эффектно для долгих закачек)."""
        asked = oplock.cancel()
        self.log("info", f"запрос отмены: {oplock.current() or 'нет операции'}")
        return {"ok": asked, "current": oplock.current()}

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
        holder = tray.Tray(self.tray_spec, self.tray_action,
                           log=lambda text: self.log("warning", text))
        res = holder.start()
        if res.get("ok"):
            self.tray = holder
            self.log("info",
                     f"трей: иконка зарегистрирована (hwnd={res.get('hwnd')}, {holder.icon_kind})")
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

    # -- TgWsProxy: локальный MTProto-прокси Telegram ------------------------

    def tg_state(self) -> dict:
        """Полный срез: процесс, порт, ссылка, чужие копии."""
        return {"ok": True, "tg": tgproxy.state()}

    def tg_check_update(self) -> dict:
        """Свежий релиз Flowseal/tg-ws-proxy против установленного."""
        try:
            info = tgproxy.latest()
        except release.ReleaseError as exc:
            return {"ok": False, "error": str(exc)}
        current = tgproxy.installed_tag()
        return {"ok": True, "current": current, "latest": info["tag"],
                "update_available": bool(current != info["tag"])}

    def tg_install(self) -> dict:
        """Скачать/обновить официальный exe (sha256 сверяется с GitHub API)."""

        def progress(done: int, total: int | None) -> None:
            if total:
                self.emitter.emit("toast", {
                    "text": f"TgWsProxy: {done * 100 // total}%"})

        self.log("info", "TgWsProxy: скачиваю последний релиз")
        res = tgproxy.install(progress=progress)
        if not res.get("ok"):
            self.log("error", f"TgWsProxy: {res.get('error')}")
            return res
        verb = "обновлён" if res.get("updated") else "установлен"
        self.log("info", f"TgWsProxy {verb}: {res.get('tag')}")
        self.emitter.emit("state", self.state())
        return res

    def tg_start(self) -> dict:
        res = tgproxy.start()
        if not res.get("ok"):
            self.log("error", f"TgWsProxy: {res.get('error')}")
            return res
        self.log("info", "TgWsProxy запущен"
                 + (" (уже работал)" if res.get("already") else ""))
        self.emitter.emit("state", self.state())
        return res

    def tg_stop(self) -> dict:
        res = tgproxy.stop()
        if not res.get("ok"):
            self.log("error", f"TgWsProxy: не остановился ({res})")
            return res
        self.log("info", f"TgWsProxy остановлен (снято процессов: "
                         f"{res.get('stopped', 0)})")
        self.emitter.emit("state", self.state())
        return res

    def tg_autostart(self, enable: bool) -> dict:
        res = tgproxy.set_autostart(bool(enable))
        if res.get("ok"):
            self.log("info", "автозапуск TgWsProxy: "
                     + ("включён" if enable else "выключен"))
            self.emitter.emit("state", self.state())
        else:
            self.log("error", f"автозапуск TgWsProxy: {res.get('error')}")
        return res

    def tg_link(self) -> dict:
        link = tgproxy.proxy_link()
        if not link:
            return {"ok": False,
                    "error": "в конфиге нет секрета - запустите прокси "
                             "и подключитесь из трея вручную"}
        return {"ok": True, "link": link}

    def tg_connect(self) -> dict:
        """Открыть системный диалог подключения прокси в Telegram."""
        res = tgproxy.connect_dialog()
        if res.get("ok"):
            self.log("info", "TgWsProxy: открыт диалог подключения в Telegram"
                     + ("" if res.get("running")
                        else " (прокси не запущен - сначала «Запустить»)"))
        else:
            self.log("error", f"TgWsProxy: подключение: {res.get('error')}")
        return res

    def tg_check_conn(self) -> dict:
        """Кто подключён к прокси: число клиентов и есть ли Telegram."""
        res = tgproxy.check_connection()
        if res.get("ok"):
            self.log("info", "TgWsProxy: подключение - "
                     + (f"клиентов {res['clients']}"
                        + (" (Telegram)" if res.get("telegram") else "")
                        if res.get("connected") else "клиентов нет"))
        return res

    def tg_open_dir(self) -> dict:
        """Открыть %APPDATA%\\TgWsProxy (конфиг и логи прокси)."""
        try:
            os.startfile(tgproxy.config_dir())  # noqa: S606 - как open_log
            return {"ok": True}
        except OSError as exc:
            self.log("error", f"открыть папку прокси: {exc}")
            return {"ok": False, "error": str(exc)}

    def tg_zapret_list(self, enable: bool) -> dict:
        """Telegram-домены в user-листе zapret (+перезапуск службы)."""
        res = tgproxy.set_zapret_list(bool(enable))
        if not res.get("ok"):
            self.log("error", f"список Telegram: {res.get('error')}")
            return res
        if res.get("changed"):
            self.log("info", "telegram-домены в user-листе zapret: "
                     + ("добавлены" if enable else "убраны"))
            st = service.state()
            if st.running:
                self.log("info", "перезапуск службы для применения списка")
                service.stop(log=self.log)
                service.start(log=self.log)
                st = service.state()
                if not st.running:
                    self.log("error",
                             "служба не перезапустилась после правки списка")
                    res = {"ok": False, "error": "служба не перезапустилась"}
        self.emitter.emit("state", self.state())
        return res

    def tg_fix_dc(self) -> dict:
        """README-фикс dc_ip: оставить только DC 4 (фото/видео в TG)."""
        res = tgproxy.fix_dc_ip()
        if not res.get("ok"):
            self.log("error", f"dc_ip: {res.get('error')}")
            return res
        if res.get("changed"):
            self.log("info", f"dc_ip исправлен: {res.get('was')} -> "
                             f"{res.get('dc_ip')}")
            self.emitter.emit("state", self.state())
        return res

    def stop(self) -> None:
        """Погасить фоновый опрос состояния (поток-демон завершится сам)."""
        self._running = False


def _post_window_icon(win, log) -> None:
    """Значок окна и панели задач: WM_SETICON сообщением, не свойством формы.

    pywebview создаёт форму на своём потоке, и в момент создания этот поток
    исполняет Python-колбэк. Присвоение `native.Icon = ...` из потока api-вызова
    ждало поток-владелец формы; тот ждал GIL, а GIL держал этот же поток -
    вставали GUI и нить трея вместе (живой процесс, py-spy: окно не отвечало,
    меню не поднимался). PostMessage ответа не ждёт: форма обрабатывает
    WM_SETICON сама, когда качает сообщения. HICON - из кэша трея (та же метка,
    нейтральный инк == icons.WINDOW_INK), им владеет живой .NET Icon.
    """
    if sys.platform != "win32":
        return
    native = getattr(win, "native", None)
    if native is None or not getattr(native, "IsHandleCreated", False):
        log("warning", "иконка окна: форма ещё не создана")
        return
    dotnet = tray._dotnet_hicon("window")   # ICON_TINTS["off"] == icons.WINDOW_INK
    if not dotnet:
        log("warning", "иконка окна: .NET значок не вышел")
        return
    handle = int(native.Handle.ToInt64())
    for which in (win32.ICON_SMALL, win32.ICON_BIG):
        win32.user32.PostMessageW(handle, win32.WM_SETICON, which, dotnet[0])
    log("info", "иконка окна: метка из ICO (WM_SETICON)")


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
        holder = tray.Tray(app.tray_spec, app.tray_action,
                           log=lambda text: app.log("warning", text))
        claim = holder.claim_singleton()
        if not claim.get("ok"):
            app.log("warning", f"трей: {claim.get('error')}")
            holder = None
        elif not claim.get("owner"):
            app.log("info", tr("tray.second_instance"))
            return 0

    # Запускаем background-обновление состояния сразу при старте
    app._start_state_polling()

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

    def op(name: str):
        """Операция с установкой единолично (см. core.oplock).

        pywebview зовёт api из пула потоков: «применить пресет» поверх
        переключения версии или скачивания пересоздал бы службу в
        середине чужой операции. Занятость - не исключение, а ответ с
        понятной причиной, которую UI показывает тостом.
        """
        def deco(fn):
            def wrapper(*args, **kwargs):
                try:
                    with oplock.operation(name):
                        return fn(*args, **kwargs)
                except oplock.BusyError as exc:
                    app.log("warning", f"операция отклонена: {exc}")
                    return {"ok": False, "error": str(exc), "busy": True}
                except oplock.Cancelled:
                    app.log("info", f"{name}: отменено")
                    return {"ok": False, "error": tr("err.cancelled"),
                            "cancelled": True}
            wrapper.__name__ = fn.__name__
            return wrapper
        return deco

    api.hello = guard(app.hello)
    api.version = guard(app.version)
    api.prepare_privileges = guard(app.prepare_privileges)
    api.register_task_now = op("регистрация задачи")(guard(app.register_task_now))
    api.state = guard(app.state)
    api.strategies = guard(app.strategies)
    api.set_preset_strategy = guard(app.set_preset_strategy)
    api.strategy_detail = guard(app.strategy_detail)
    api.export_preset = guard(app.export_preset)
    api.import_preset = guard(app.import_preset)
    api.versions = guard(app.versions)
    api.check_update = guard(app.check_update)
    api.check_install = guard(app.check_install)
    api.presets = guard(app.presets)
    api.probes_run = guard(app.probes_run)
    api.probe_history = guard(app.probe_history)
    api.probe_groups = guard(app.probe_groups)
    api.probe_groups_add = guard(app.probe_groups_add)
    api.probe_groups_remove = guard(app.probe_groups_remove)
    api.select_cancel = guard(app.select_cancel)
    # dev-линия: стратегии, авто-режим, трей
    api.apply_strategy = guard(app.apply_strategy)
    api.set_strategy_mode = guard(app.set_strategy_mode)
    api.auto_run = guard(app.auto_run)
    api.set_tray = guard(app.set_tray)
    api.hide_window = guard(app.hide_window)
    api.cancel_operation = guard(app.cancel_operation)
    api.log_tail = guard(app.log_tail)
    api.open_log = guard(app.open_log)
    api.pick_dir = guard(app.pick_dir)
    # операции, которые трогают службу и установку: только по одной сразу
    api.try_strategy = op("проба стратегии")(guard(app.try_strategy))
    api.apply_preset = op("применение пресета")(guard(app.apply_preset))
    api.switch_version = op("переключение версии")(guard(app.switch_version))
    api.rollback_version = op("откат версии")(guard(app.rollback_version))
    api.remove_version = op("удаление версии")(guard(app.remove_version))
    api.update_latest = op("обновление zapret")(guard(app.update_latest))
    api.repair_install = op("восстановление файлов")(guard(app.repair_install))
    api.install_release = op("скачивание релиза")(guard(app.install_release))
    # список версий и скачивание выбранной (кнопки вкладки «Скачивание»)
    api.available_releases = guard(app.available_releases)
    api.download_release = op("скачивание версии")(guard(app.download_release))
    api.ab_run = op("A/B-тест")(guard(app.ab_run))
    api.select_run = op("перебор стратегий")(guard(app.select_run))
    # TgWsProxy: чтение/ссылка/автозапуск - без блокировки, мутации - по одной
    api.tg_state = guard(app.tg_state)
    api.tg_check_update = guard(app.tg_check_update)
    api.tg_autostart = guard(app.tg_autostart)
    api.tg_link = guard(app.tg_link)
    api.tg_connect = guard(app.tg_connect)
    api.tg_check_conn = guard(app.tg_check_conn)
    api.tg_open_dir = guard(app.tg_open_dir)
    api.tg_install = op("установка TgWsProxy")(guard(app.tg_install))
    api.tg_start = op("старт TgWsProxy")(guard(app.tg_start))
    api.tg_stop = op("остановка TgWsProxy")(guard(app.tg_stop))
    api.tg_zapret_list = op("список Telegram для zapret")(guard(app.tg_zapret_list))
    api.tg_fix_dc = op("фикс dc_ip TgWsProxy")(guard(app.tg_fix_dc))

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

    # Закрытие окна останавливает background-обновление (None - не
    # отменяем закрытие, просто гасим цикл)
    def on_closing():
        app.stop()

    win.events.closing += on_closing

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
                        f"трей: иконка зарегистрирована (hwnd={res.get('hwnd')}, {holder.icon_kind})")
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
    # Погасить фоновый опрос (поток-демон всё равно умрёт вместе с процессом)
    app.stop()
    return 0

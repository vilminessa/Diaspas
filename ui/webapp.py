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
import time
import traceback
from pathlib import Path

import webview

from dataclasses import replace

from core import abtest, autoselect, installs, log as dlog, paths
from core import presets as presets_mod
from core import probes, release, service, strategies as strategies_mod
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
        self.last_probes: dict | None = None
        self._select_running = False
        # один warning о пропавшей службе на сессию - без спама в журнал
        self._warned_missing_service = False
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
        отвечает (типичная поломка после рефакторинга моста).
        """
        self.log("info", "фронтенд подключился: мост JS<->Python работает")
        return {"ok": True, "app": "Diaspas", "ui": "webview"}

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
        if self.zapret_root is not None:
            try:
                current = presets_mod.current_preset(
                    self.zapret_root, st, presets=presets,
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
            # короткая метка фактической стратегии (ALT11, а не
            # "general (ALT11)"): окно показывает то, что реально стоит
            "strategy_label": self._strategy_label(st),
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
        }

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
        """Пресеты с их стратегией: пользователь обязан видеть, что
        применится, и иметь возможность поменять её у конкретного пресета.
        """
        return [
            {"key": p.key, "title": p.title, "description": p.description,
             "strategy": p.strategy,
             "strategy_label": paths.strategy_label(p.strategy)
                               if p.strategy else "",
             "overridden": bool(presets_mod.strategy_override(
                 p.key, self.zapret_root)),
             "game_filter": p.game_filter,
             "ipset": p.ipset, "exclude_domains": list(p.exclude_domains),
             "off": p.off}
            for p in presets_mod.load_presets(self.zapret_root)
        ]

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
        return {"ok": True, "detail": chosen,
                "diff": strategies_mod.diff(baseline, chosen),
                "current": current or "",
                "best": autoselect.load_best(str(root)) or ""}

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
        self.zapret_root = Path(path)
        self.zapret_source = "settings"

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
        self.zapret_root = root
        self.zapret_source = "settings"

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
            self.zapret_root = root
            self.zapret_source = "settings"
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
            self.zapret_root = path
            self.zapret_source = "settings"
            # ручная папка тоже попадает в реестр версий
            installs.register(path, source="manual")
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

    def stop(self) -> None:
        """Погасить фоновый опрос состояния (поток-демон завершится сам)."""
        self._running = False


def run() -> int:
    """Создать окно и отдать управление pywebview."""
    app = App()
    app.log("info", "запуск интерфейса (webview)")

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
    api.hello = guard(app.hello)
    api.prepare_privileges = guard(app.prepare_privileges)
    api.register_task_now = guard(app.register_task_now)
    api.state = guard(app.state)
    api.strategies = guard(app.strategies)
    api.set_preset_strategy = guard(app.set_preset_strategy)
    api.strategy_detail = guard(app.strategy_detail)
    api.try_strategy = guard(app.try_strategy)
    api.export_preset = guard(app.export_preset)
    api.import_preset = guard(app.import_preset)
    api.versions = guard(app.versions)
    api.switch_version = guard(app.switch_version)
    api.rollback_version = guard(app.rollback_version)
    api.remove_version = guard(app.remove_version)
    api.check_update = guard(app.check_update)
    api.update_latest = guard(app.update_latest)
    api.check_install = guard(app.check_install)
    api.repair_install = guard(app.repair_install)
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

    # Закрытие окна останавливает background-обновление (None - не
    # отменяем закрытие, просто гасим цикл)
    def on_closing():
        app.stop()

    win.events.closing += on_closing

    # хвост журнала прошлого запуска - сразу в окно, до первого вызова JS
    for line in dlog.tail(60):
        app.emitter.emit("log", line)

    webview.start(debug=False)
    app.log("info", "интерфейс закрыт")
    # Погасить фоновый опрос (поток-демон всё равно умрёт вместе с процессом)
    app.stop()
    return 0

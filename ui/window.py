"""Главное окно Diaspas: карточка состояния, пресеты, пробы, журнал.

Окно однопоточное: тяжёлые операции (сетевые пробы, ожидание помощника)
идут в ``threading.Thread``, а обновление виджетов - только через
``after``-мост главного потока (см. ``_ui``). Так Tkinter не зависает,
а журнал остаётся последовательным.
"""

from __future__ import annotations

import queue
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import version
from core import abtest, autoselect, autoswitch, presets as presets_mod, probes, release, service
from core import log as dlog
from core import paths, strategies as strategies_mod, tray
from i18n import tr


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        # подпись сборки в заголовке: по ней видно, из какого тега взят exe
        self.title(f"{tr('app.title')} - сборка {version.label()}")
        self.geometry("900x720")
        self.minsize(760, 600)

        # очередь из фоновых потоков в главный
        self._q: queue.Queue = queue.Queue()
        self._busy = False
        self._zapret_root: Path | None = paths.zapret_dir_from_settings()
        self._last_ab: dict | None = None
        self._last_select: dict | None = None
        self._last_probe_report: dict | None = None
        self._strategy_infos: list[strategies_mod.StrategyInfo] = []
        self._auto_checked = False      # авто при старте - один раз на сессию
        self._auto_running = False
        # трей: иконка после успешной регистрации, скрыто ли окно, идущие
        # операции трея (повторный клик той же операции отказывается)
        self._tray: tray.Tray | None = None
        self._tray_busy: set[str] = set()
        self._hidden = False

        self._build()
        self.log("info", f"запуск интерфейса (Tkinter) - сборка {version.label()}")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(120, self._pump)
        self.after(200, self._initial_state)

    # -- сборка -----------------------------------------------------------

    def _build(self) -> None:
        pad = {"padx": 8, "pady": 4}
        root = ttk.Frame(self, padding=8)
        root.pack(fill="both", expand=True)

        # 1. состояние
        card = ttk.LabelFrame(root, text=tr("status.title"), padding=8)
        card.pack(fill="x", **pad)
        self._state_var = tk.StringVar(value="...")
        ttk.Label(card, textvariable=self._state_var,
                  justify="left", anchor="w").pack(fill="x")
        row = ttk.Frame(card)
        row.pack(fill="x", pady=(6, 0))
        ttk.Button(row, text=tr("status.check"),
                   command=self.check_state).pack(side="left")
        ttk.Button(row, text=tr("release.browse"),
                   command=self.pick_dir).pack(side="left", padx=(6, 0))
        ttk.Button(row, text=tr("release.install"),
                   command=self.install_release).pack(side="left", padx=(6, 0))
        self._release_var = tk.StringVar(value="")
        ttk.Label(row, textvariable=self._release_var,
                  foreground="#666").pack(side="left", padx=(10, 0))

        # смена стратегии: ручная (combobox) + авто-режим
        srow = ttk.Frame(card)
        srow.pack(fill="x", pady=(6, 0))
        self._strategy_combo = ttk.Combobox(srow, width=24, state="readonly",
                                            values=[])
        self._strategy_combo.pack(side="left")
        self._strategy_btn = ttk.Button(srow, text=tr("strategies.apply"),
                                        command=self.apply_strategy)
        self._strategy_btn.pack(side="left", padx=(6, 0))
        self._auto_var = tk.BooleanVar(value=False)
        self._auto_check = ttk.Checkbutton(srow, text=tr("strategies.auto"),
                                           variable=self._auto_var,
                                           command=self.toggle_auto)
        self._auto_check.pack(side="left", padx=(6, 0))
        self._strategy_note = tk.StringVar(value="")
        ttk.Label(srow, textvariable=self._strategy_note, foreground="#666",
                  wraplength=420, justify="left").pack(side="left",
                                                       padx=(10, 0))

        # трей: иконка + скрытие окна (обход при этом не выключается)
        trow = ttk.Frame(card)
        trow.pack(fill="x", pady=(6, 0))
        self._tray_var = tk.BooleanVar(value=tray.enabled())
        self._tray_check = ttk.Checkbutton(
            trow, text=tr("tray.enabled"), variable=self._tray_var,
            command=self.toggle_tray)
        self._tray_check.pack(side="left")
        self._tray_hide_btn = ttk.Button(trow, text=tr("tray.hide"),
                                         command=self.hide_window)
        self._tray_hide_btn.pack(side="left", padx=(6, 0))
        self._tray_note = tk.StringVar(value=tr("tray.note"))
        ttk.Label(trow, textvariable=self._tray_note, foreground="#666",
                  wraplength=360, justify="left").pack(side="left",
                                                       padx=(10, 0))

        # 2. пресеты
        pf = ttk.LabelFrame(root, text=tr("presets.title"), padding=8)
        pf.pack(fill="x", **pad)
        self._preset_list = presets_mod.load_presets()
        self._preset_buttons: dict[str, ttk.Button] = {}
        grid = ttk.Frame(pf)
        grid.pack(fill="x")
        for i, preset in enumerate(self._preset_list):
            btn = ttk.Button(
                grid, text=preset.title,
                command=lambda p=preset: self.apply_preset(p))
            btn.grid(row=i // 3, column=i % 3, sticky="ew", padx=3, pady=3)
            self._preset_buttons[preset.key] = btn
            grid.columnconfigure(i % 3, weight=1)
        self._preset_hint = tk.StringVar(value="")
        ttk.Label(pf, textvariable=self._preset_hint,
                  foreground="#555", wraplength=820,
                  justify="left").pack(fill="x", pady=(6, 0))

        # 3. пробы + A/B
        pf2 = ttk.LabelFrame(root, text=tr("probe.title"), padding=8)
        pf2.pack(fill="x", **pad)
        row2 = ttk.Frame(pf2)
        row2.pack(fill="x")
        self._probe_btn = ttk.Button(row2, text=tr("probe.run"),
                                     command=self.run_probes)
        self._probe_btn.pack(side="left")
        self._ab_btn = ttk.Button(row2, text=tr("probe.ab"),
                                  command=self.run_ab)
        self._ab_btn.pack(side="left", padx=(6, 0))
        self._probe_var = tk.StringVar(value="")
        ttk.Label(pf2, textvariable=self._probe_var, wraplength=820,
                  justify="left").pack(fill="x", pady=(6, 0))
        self._verdict_var = tk.StringVar(value="")
        ttk.Label(pf2, textvariable=self._verdict_var, wraplength=820,
                  justify="left", foreground="#0a6").pack(fill="x")

        # 4. автоподбор
        sf = ttk.LabelFrame(root, text=tr("select.title"), padding=8)
        sf.pack(fill="x", **pad)
        row3 = ttk.Frame(sf)
        row3.pack(fill="x")
        self._select_btn = ttk.Button(row3, text=tr("select.run"),
                                      command=self.run_select)
        self._select_btn.pack(side="left")
        self._cancel_btn = ttk.Button(row3, text=tr("select.cancel"),
                                      command=autoselect.cancel,
                                      state="disabled")
        self._cancel_btn.pack(side="left", padx=(6, 0))
        self._best_apply_btn = ttk.Button(row3, text=tr("select.use"),
                                          command=self.apply_best_selected,
                                          state="disabled")
        self._best_apply_btn.pack(side="left", padx=(6, 0))
        self._select_best = tk.StringVar(value=tr("select.none"))
        ttk.Label(row3, textvariable=self._select_best).pack(side="left",
                                                             padx=(10, 0))
        self._progress = ttk.Progressbar(sf, mode="determinate", length=300)
        self._progress.pack(fill="x", pady=(6, 0))

        # 5. журнал
        lf = ttk.LabelFrame(root, text=tr("log.title"), padding=8)
        lf.pack(fill="both", expand=True, **pad)
        self._log = tk.Text(lf, height=10, state="disabled",
                            font=("Consolas", 9), wrap="word")
        scroll = ttk.Scrollbar(lf, command=self._log.yview)
        self._log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self._log.pack(fill="both", expand=True)

        # дисклеймер
        ttk.Label(root, text=tr("app.disclaimer"),
                  foreground="#888", wraplength=820,
                  justify="left").pack(fill="x", padx=8)

        # хвост персистентного журнала - при повторном запуске видно,
        # что было до закрытия окна
        for line in dlog.tail(15):
            self._append_log("old", line)

    # -- журнал и мост потоков ---------------------------------------------

    def log(self, level: str, text: str) -> None:
        """Потокобезопасная запись: в окно (очередь) и на диск.

        Диск обязателен: журнал окна живёт в памяти и исчезает вместе с
        закрытым окном - именно поэтому диагностика «посмотри лог» была
        невозможна.
        """
        dlog.write(level, text)
        self._q.put(("log", level, text))

    def _append_log(self, level: str, text: str) -> None:
        self._log.configure(state="normal")
        # строки из tail() уже содержат метку времени - не дублируем
        prefix = "" if level == "old" else f"[{level}] "
        self._log.insert("end", f"{prefix}{text}\n")
        self._log.see("end")
        self._log.configure(state="disabled")

    def _pump(self) -> None:
        """Перекачать очередь в главный поток; работает и для after-задач."""
        try:
            while True:
                kind, *payload = self._q.get_nowait()
                if kind == "log":
                    self._append_log(payload[0], payload[1])
                elif kind == "state":
                    self._render_state(payload[0])
                elif kind == "probes":
                    self._render_probes(payload[0])
                elif kind == "verdict":
                    self._render_verdict(payload[0])
                elif kind == "progress":
                    self._render_progress(payload[0])
                elif kind == "select_done":
                    self._render_select_done(payload[0])
                elif kind == "auto_done":
                    self._render_auto(payload[0])
                elif kind == "busy":
                    self._set_busy(payload[0])
                elif kind == "release":
                    self._release_var.set(payload[0])
                elif kind == "tray_show":
                    self._show_window()
                elif kind == "tray_hide":
                    self._hide_window()
                elif kind == "tray_exit":
                    self._stop_tray()
                    self.destroy()
                elif kind == "tray_mode":
                    self._auto_var.set(payload[0] == strategies_mod.AUTO)
                    self._sync_strategy_controls()
                elif kind == "error":
                    messagebox.showerror("Diaspas", payload[0], parent=self)
        except queue.Empty:
            pass
        self.after(120, self._pump)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        for btn in self._preset_buttons.values():
            btn.configure(state=state)
        for btn in (self._probe_btn, self._ab_btn, self._select_btn):
            btn.configure(state=state)
        self._cancel_btn.configure(state="normal" if busy else "disabled")
        self._sync_strategy_controls()

    def _sync_strategy_controls(self) -> None:
        """Доступность контролов стратегии: занято, авто-режим, нет папки.

        В авто-режиме ручная смена заблокирована - пользователь видит, что
        стратегию выбирает подбор, а не он (в webview та же логика).
        """
        auto = strategies_mod.mode() == strategies_mod.AUTO
        no_dir = self._zapret_root is None
        locked = self._busy or auto or no_dir
        self._strategy_combo.configure(
            state="disabled" if locked else "readonly")
        self._strategy_btn.configure(
            state="disabled" if locked else "normal")
        self._auto_check.configure(
            state="disabled" if self._busy or no_dir else "normal")

    def _bg(self, fn, *args, **kwargs) -> None:
        """Запустить в фоне; исходная ошибка попадает в журнал (с диска)."""

        def runner() -> None:
            try:
                fn(*args, **kwargs)
            except Exception:  # noqa: BLE001 - GUI не должно падать
                dlog.exception("фоновая операция")
                self.log("error", traceback.format_exc(limit=6))

        threading.Thread(target=runner, daemon=True).start()

    # -- начальное состояние ------------------------------------------------

    def _initial_state(self) -> None:
        self.check_state()

    def check_state(self) -> None:
        self._bg(self._state_worker)

    def _state_worker(self) -> None:
        state = service.state(log=self.log)
        root = self._zapret_root
        current = None
        if root is not None:
            try:
                current = presets_mod.current_preset(
                    root, state, active_key=presets_mod.active_preset_key())
            except Exception:  # noqa: BLE001 - распознавание не должно валить статус
                current = None
        self._q.put(("state", {"state": state, "root": root,
                               "preset": current}))

    def _render_state(self, data: dict) -> None:
        state: service.ServiceState = data["state"]
        root: Path | None = data["root"]
        if state.installed:
            svc = (tr("status.running") if state.running
                   else tr("status.stopped"))
        else:
            svc = tr("status.not_installed")
        strategy = state.strategy or tr("status.unknown")
        preset = data.get("preset")
        preset_text = preset.title if preset else tr("presets.none")
        dir_text = str(root) if root else tr("status.no_dir")
        self._state_var.set(
            f"{tr('status.service')}: {svc}\n"
            f"{tr('status.strategy')}: {strategy}\n"
            f"{tr('status.preset')}: {preset_text}\n"
            f"{tr('status.zapret')}: {dir_text}")
        # подсветка активного пресета
        active = preset.key if preset else None
        for key, btn in self._preset_buttons.items():
            btn.configure(text=self._preset_title(key)
                          + (" ●" if key == active else ""))

        self._render_strategy(state, root)

    def _render_strategy(self, state: service.ServiceState,
                         root: Path | None) -> None:
        """Список стратегий, текущая, режим и пауза автоподбора."""
        infos = strategies_mod.list_strategies(root, state.strategy)
        self._strategy_infos = infos
        self._strategy_combo.configure(values=[i.label for i in infos])
        idx = next((k for k, i in enumerate(infos) if i.active), -1)
        if idx >= 0:
            self._strategy_combo.current(idx)
        else:
            self._strategy_combo.set(state.strategy or "")

        auto = strategies_mod.mode() == strategies_mod.AUTO
        self._auto_var.set(auto)
        self._sync_strategy_controls()

        parts: list[str] = []
        if auto:
            parts.append(tr("strategies.auto_on"))
            left = autoswitch.cooldown_left()
            if left > 0:
                parts.append(tr("strategies.auto_cooldown", minutes=left // 60))
        else:
            parts.append(tr("strategies.manual"))
        last = autoswitch.last_strategy()
        if last:
            parts.append(tr("strategies.auto_last",
                            label=paths.strategy_label(last)))
        self._strategy_note.set(" · ".join(parts))

        # Авто при старте: проверяем доступность и, если есть сбои, подбираем
        # стратегию. Один раз на сессию, только когда служба работает.
        if auto and state.running and not self._auto_checked:
            self._auto_checked = True
            self._bg(self._auto_worker)

    def _preset_title(self, key: str) -> str:
        for preset in self._preset_list:
            if preset.key == key:
                return preset.title
        return key

    def pick_dir(self) -> None:
        chosen = filedialog.askdirectory(parent=self,
                                         title=tr("release.browse"))
        if not chosen:
            return
        path = Path(chosen)
        if not paths.is_zapret_dir(path):
            messagebox.showwarning("Diaspas", tr("err.no_dir"), parent=self)
            return
        settings = paths.read_settings()
        settings["zapret_dir"] = str(path)
        paths.write_settings(settings)
        self._zapret_root = path
        self.log("info", f"папка zapret: {path}")
        self.check_state()

    def install_release(self) -> None:
        if self._busy:
            return
        if messagebox.askyesno(
                "Diaspas",
                "Скачать последний релиз zapret-discord-youtube и "
                "распаковать его в %LOCALAPPDATA%\\Diaspas\\zapret?\n\n"
                "Бинарники останутся в LICENSE.txt из архива.",
                parent=self) is not True:
            return
        self._bg(self._install_worker)

    def _install_worker(self) -> None:
        self._q.put(("busy", True))
        try:
            def progress(done: int, total: int | None) -> None:
                if total:
                    self._q.put(("release",
                                 f"{done // 1024} / {total // 1024} КБ"))

            self.log("info", "запрос последнего релиза к api.github.com")
            rel = release.latest_release()
            self.log("info", f"релиз {rel.tag} ({rel.published})")
            zip_path = release.download(rel, progress=progress)
            dest = paths.app_dir() / "zapret"
            root = release.install(zip_path, dest, progress=progress)
            settings = paths.read_settings()
            settings["zapret_dir"] = str(root)
            paths.write_settings(settings)
            self._zapret_root = root
            self._q.put(("release", tr("release.version", tag=rel.tag)))
            self.log("info", f"установлено: {root}")
            self._q.put(("state", {"state": service.state(), "root": root,
                                   "preset": None}))
        except release.ReleaseError as exc:
            self.log("error", str(exc))
            self._q.put(("error", str(exc)))
        finally:
            self._q.put(("busy", False))

    # -- пресеты ------------------------------------------------------------

    def apply_preset(self, preset) -> None:
        if self._busy:
            return
        if self._zapret_root is None:
            messagebox.showwarning("Diaspas", tr("err.no_dir"), parent=self)
            return
        self._preset_hint.set(preset.description)
        if messagebox.askyesno(
                "Diaspas",
                f"Применить пресет «{preset.title}»?\n\n{preset.description}",
                parent=self) is not True:
            return
        self._bg(self._apply_worker, preset)

    def _apply_worker(self, preset) -> None:
        self._q.put(("busy", True))
        try:
            res = presets_mod.apply(preset, self._zapret_root, log=self.log)
            if not res.get("ok"):
                self._q.put(("error", str(res.get("error"))))
            self.check_state_no_bg()
        finally:
            self._q.put(("busy", False))

    def check_state_no_bg(self) -> None:
        """Обновить состояние прямо в текущем фоновом потоке."""
        self._state_worker()

    # -- смена стратегии ---------------------------------------------------

    def apply_strategy(self) -> None:
        """Поставить стратегию из combobox; режимы установки не меняются."""
        if self._busy or self._zapret_root is None:
            return
        idx = self._strategy_combo.current()
        if idx < 0 or idx >= len(self._strategy_infos):
            messagebox.showwarning("Diaspas", tr("strategies.pick"),
                                   parent=self)
            return
        info = self._strategy_infos[idx]
        if info.active:
            messagebox.showinfo("Diaspas", tr("strategies.already_active"),
                                parent=self)
            return
        if messagebox.askyesno("Diaspas",
                               tr("strategies.apply_confirm", label=info.label),
                               parent=self) is not True:
            return
        self._bg(self._strategy_worker, info.file)

    def apply_best_selected(self) -> None:
        """«Взять эту»: применить победителя перебора как ручную стратегию."""
        if self._busy or self._zapret_root is None:
            return
        best = (self._last_select or {}).get("best")
        if not best:
            return
        if messagebox.askyesno("Diaspas",
                               tr("strategies.best_confirm",
                                  label=paths.strategy_label(str(best))),
                               parent=self) is not True:
            return
        self._bg(self._strategy_worker, str(best))

    def _strategy_worker(self, strategy_file: str) -> None:
        self._q.put(("busy", True))
        try:
            res = strategies_mod.apply(self._zapret_root, strategy_file,
                                       log=self.log)
            if not res.get("ok"):
                self._q.put(("error", str(res.get("error"))))
            # успех уже записан в журнал из core.strategies.apply
            self.check_state_no_bg()
        finally:
            self._q.put(("busy", False))

    def toggle_auto(self) -> None:
        want = (strategies_mod.AUTO if self._auto_var.get()
                else strategies_mod.MANUAL)
        if want == strategies_mod.AUTO:
            if messagebox.askyesno("Diaspas", tr("strategies.auto_confirm"),
                                   parent=self) is not True:
                self._auto_var.set(False)
                self._sync_strategy_controls()
                return
        strategies_mod.set_mode(want)
        self.log("info", tr("strategies.mode_set",
                            mode="авто" if want == strategies_mod.AUTO else "ручной"))
        self._sync_strategy_controls()
        self._state_worker()

    def _auto_worker(self, report: dict | None = None,
                     force: bool = False) -> None:
        """Автоцикл: пробы → перебор с остановкой на первой рабочей → применение."""
        if self._auto_running or self._zapret_root is None:
            return
        self._auto_running = True
        self._q.put(("busy", True))
        try:
            def on_progress(data: dict) -> None:
                self._q.put(("progress", {**data, "auto": True}))

            res = autoswitch.run(self._zapret_root, report=report, force=force,
                                 log=self.log, on_progress=on_progress,
                                 wait=900)
            self._q.put(("auto_done", res))
            if res.get("probes"):
                self._q.put(("probes", res["probes"]))
            # уведомление трея: результат виден, даже когда окно скрыто
            if res.get("applied"):
                self._tray_notify("Diaspas",
                                  f"авто: применена {res.get('label') or ''}")
            elif not res.get("ok"):
                self._tray_notify("Diaspas",
                                  f"автоподбор: {res.get('error') or 'не удалось'}")
            self._state_worker()
        finally:
            self._auto_running = False
            self._q.put(("busy", False))

    def _render_auto(self, res: dict) -> None:
        if not res.get("ok"):
            text = res.get("error") or tr("strategies.auto_off")
            self._select_best.set(f"{tr('select.best')}: {text}")
            return
        if res.get("applied"):
            label = res.get("label") or paths.strategy_label(
                str(res.get("best") or ""))
            # вывод уже в журнале из core.autoswitch.run
            self._select_best.set(tr("strategies.auto_applied", label=label))
        else:
            reason = res.get("reason") or ""
            text = {
                "off": tr("strategies.auto_off"),
                "full": tr("strategies.auto_full"),
                "cooldown": tr("strategies.auto_cooldown",
                               minutes=int(res.get("cooldown_left") or 0) // 60),
                "preset_off": tr("strategies.auto_preset_off"),
                "none": tr("strategies.auto_none"),
                "already": tr("strategies.auto_already"),
            }.get(reason, reason)
            self._select_best.set(f"авто: {text}")
        self._sync_strategy_controls()

    # -- пробы и A/B ---------------------------------------------------------

    def run_probes(self) -> None:
        if self._busy:
            return
        self._bg(self._probes_worker)

    def _probes_worker(self) -> None:
        self._q.put(("busy", True))
        try:
            self._probe_var.set(tr("probe.running"))
            report = probes.probe_all(timeout=6.0)
            self._q.put(("probes", report))
            failed = probes.failed_targets(report)
            if failed and strategies_mod.mode() != strategies_mod.AUTO:
                # в ручном режиме никто не подбирает - уведомление говорит,
                # что сбой есть и требует решения
                self._tray_notify("Diaspas",
                                  f"цели не отвечают: {', '.join(failed)[:60]}")
            # В авто-режиме сбой проб - триггер: подбор и применение
            # лучшей стратегии прямо в этом же фоновом потоке.
            if (strategies_mod.mode() == strategies_mod.AUTO
                    and report.get("state") != "full" and report.get("total")):
                self._auto_worker(report=report)
        finally:
            self._q.put(("busy", False))

    def _render_probes(self, report: dict) -> None:
        self._last_probe_report = report
        parts = []
        for group, rep in (report.get("groups") or {}).items():
            state = rep.get("state")
            mark = {"full": "✓", "partial": "~", "none": "✗"}.get(state, "?")
            parts.append(f"{group}: {mark} {rep.get('count', 0)}/"
                         f"{rep.get('total', 0)}")
        self._probe_var.set("   ".join(parts))
        self.log("info", probes.probe_line(report))

    def run_ab(self) -> None:
        if self._busy:
            return
        group = self._detect_worst_group()
        if not group:
            messagebox.showinfo("Diaspas",
                                "Сначала нажмите «Проверить всё» - "
                                "A/B нужен для неработающей группы.",
                                parent=self)
            return
        if messagebox.askyesno(
                "Diaspas",
                f"A/B-тест для «{group}»: служба будет остановлена и "
                f"запущена заново. Продолжить?",
                parent=self) is not True:
            return
        self._bg(self._ab_worker, group)

    def _detect_worst_group(self) -> str | None:
        """Группа с наибольшей долей сбоев по последнему замеру."""
        # кэш последнего отчёта пробы
        last = getattr(self, "_last_probe_report", None)
        if not last:
            return None
        worst, worst_ratio = None, 0.0
        for group, rep in (last.get("groups") or {}).items():
            total = int(rep.get("total") or 0)
            if not total:
                continue
            ratio = 1.0 - int(rep.get("count") or 0) / total
            if ratio > worst_ratio:
                worst, worst_ratio = group, ratio
        return worst

    def _ab_worker(self, group: str) -> None:
        self._q.put(("busy", True))
        try:
            self._probe_var.set(tr("probe.ab_running"))
            res = abtest.run(group, log=self.log)
            self._q.put(("verdict", res))
            self._state_worker()
        finally:
            self._q.put(("busy", False))

    def _render_verdict(self, res: dict) -> None:
        if not res.get("ok"):
            self._verdict_var.set(f"Ошибка: {res.get('error')}")
            return
        with_ok = bool((res.get("with") or {}).get("ok"))
        without = res.get("without")
        text = [f"{tr('probe.verdict')}: {res.get('hint', '')}"]
        text.append(f"с обходом: {'✓' if with_ok else '✗'}"
                    + ("" if without is None else
                       f" | без обхода: {'✓' if without.get('ok') else '✗'}"))
        self._verdict_var.set("\n".join(text))

    # -- автоподбор -----------------------------------------------------------

    def run_select(self) -> None:
        if self._busy:
            return
        if self._zapret_root is None:
            messagebox.showwarning("Diaspas", tr("err.no_dir"), parent=self)
            return
        if messagebox.askyesno(
                "Diaspas",
                "Перебор стратегий: служба zapret будет пересоздаваться "
                "для каждой стратегии, Discord и YouTube могут кратковременно "
                "прерываться. Продолжить?",
                parent=self) is not True:
            return
        self._progress.configure(mode="indeterminate")
        self._progress.start(12)
        self._bg(self._select_worker)

    def _select_worker(self) -> None:
        self._q.put(("busy", True))
        try:
            def on_progress(data: dict) -> None:
                self._q.put(("progress", data))

            res = autoselect.run(self._zapret_root, log=self.log,
                                 on_progress=on_progress, wait=900)
            if res is None:
                self._q.put(("error", tr("err.no_uac")))
            elif not res.get("ok"):
                self._q.put(("error", str(res.get("error"))))
            else:
                self._last_select = res
                self._q.put(("select_done", res))
        finally:
            self._q.put(("busy", False))

    def _render_progress(self, data: dict) -> None:
        done = int(data.get("done") or 0)
        total = int(data.get("total") or 0)
        if self._progress.cget("mode") != "determinate":
            self._progress.stop()
            self._progress.configure(mode="determinate")
        self._progress.configure(maximum=max(total, 1), value=done)
        best = data.get("best")
        prefix = "авто: " if data.get("auto") else ""
        self._select_best.set(
            f"{prefix}{tr('select.progress', done=done, total=total)}  "
            f"{tr('select.best')}: {best or '—'}")
        # журнал по ходу: последняя стратегия из прогресса
        results = data.get("results") or []
        if results:
            last = results[-1]
            self.log("info",
                     f"{last.get('name')}: {last.get('state')} "
                     f"{last.get('count')}/{last.get('total')}")

    def _render_select_done(self, res: dict) -> None:
        self._progress.stop()
        self._progress.configure(mode="determinate")
        best = res.get("best")
        rows = autoselect.summarize(res, self._zapret_root)
        ok_rows = [r for r in rows if r["state"] == "full"]
        partial = [r for r in rows if r["state"] == "partial"]
        self._select_best.set(
            f"{tr('select.best')}: {best or tr('select.none')}  "
            f"(полных: {len(ok_rows)}, частичных: {len(partial)})")
        self.log("info", "итоги перебора:")
        for row in rows:
            mark = "★" if row["best"] else " "
            groups = " ".join(f"{g}:{v}" for g, v in row["groups"].items())
            self.log("info",
                     f" {mark} {row['label']:<16} {row['state']:<8} "
                     f"{row['count']}/{row['total']} {groups}")
        # победитель: кнопка «Взять эту» и сразу предложение применить
        if best:
            self._best_apply_btn.configure(state="normal")
            self._offer_best(best)
        else:
            self._best_apply_btn.configure(state="disabled")

    def _offer_best(self, strategy_file: str) -> None:
        """После перебора: предложить поставить победителя службой.

        Раньше подставлялось в пресет Apex/EA - это меняло ещё и режимы
        (Game Filter, IPSet, списки), хотя перебор проверял только стратегию.
        """
        label = paths.strategy_label(strategy_file)
        if messagebox.askyesno("Diaspas",
                               tr("strategies.best_confirm", label=label),
                               parent=self) is True:
            self._bg(self._strategy_worker, strategy_file)

    # -- трей -------------------------------------------------------------

    def _tray_state(self) -> dict:
        """Словарь для модели меню - те же ключи, что у ``App.state`` webview."""
        st = service.state()
        current = None
        if self._zapret_root is not None:
            try:
                current = presets_mod.current_preset(
                    self._zapret_root, st,
                    active_key=presets_mod.active_preset_key())
            except Exception:  # noqa: BLE001 - распознавание не валит меню
                current = None
        strategy_file = strategies_mod.current_file(self._zapret_root, st)
        return {
            "dir_ok": self._zapret_root is not None,
            "installed": st.installed,
            "running": st.running,
            "strategy": st.strategy,
            "strategy_label": (paths.strategy_label(strategy_file)
                               if strategy_file else (st.strategy or "")),
            "strategy_mode": strategies_mod.mode(),
            "preset_key": current.key if current else None,
            "preset_title": current.title if current else None,
            "auto_cooldown_left": autoswitch.cooldown_left(),
            "auto_last_strategy": autoswitch.last_strategy(),
            "strategies": [{"file": info.file, "label": info.label,
                            "active": info.active}
                           for info in self._strategy_infos],
        }

    def _tray_spec(self) -> tray.TraySpec:
        presets = [{"key": p.key, "title": p.title, "off": p.off}
                   for p in presets_mod.load_presets()]
        return tray.tray_model(self._tray_state(), presets,
                               busy={"auto": self._auto_running,
                                     "select": bool(self._busy)},
                               last_probes=self._last_probe_report)

    def tray_action(self, action: str) -> dict:
        """Пункт меню трея -> операция. Долгие уходят в фон: цикл трея не может
        ждать минуты, а виджет обновится через очередь."""
        ops = _TrayOps(self)
        if tray.is_heavy(action):
            if action in self._tray_busy or (action == tray.AUTO_RUN
                                             and self._auto_running):
                return {"ok": False, "error": "операция уже выполняется"}
            self._tray_busy.add(action)

            def worker() -> None:
                try:
                    self.log("info", f"трей: {action}")
                    res = tray.handle(action, ops)
                    if not res.get("ok"):
                        self.log("error", f"трей {action}: {res.get('error')}")
                except Exception:  # noqa: BLE001
                    self.log("exception", f"трей {action}")
                finally:
                    self._tray_busy.discard(action)
                    self._tray_update()

            threading.Thread(target=worker, daemon=True).start()
            return {"ok": True, "started": action}

        res = tray.handle(action, ops)
        self._tray_update()
        return res

    def _tray_update(self) -> None:
        if self._tray:
            try:
                self._tray.update()
            except Exception:  # noqa: BLE001
                dlog.exception("обновление трея")

    def _tray_notify(self, title: str, text: str) -> None:
        """Уведомление трея + запись в журнал (журнал - источник правды)."""
        self.log("info", f"уведомление: {title} · {text}")
        if self._tray:
            try:
                self._tray.notify(title, text)
            except Exception:  # noqa: BLE001
                dlog.exception("уведомление трея")

    def start_tray(self) -> dict:
        """Поднять иконку; мягкий отказ - GUI работает как без трея."""
        if self._tray:
            return {"ok": True, "hwnd": self._tray.hwnd}
        holder = tray.Tray(self._tray_spec, self.tray_action)
        res = holder.start()
        if res.get("ok"):
            self._tray = holder
            self.log("info",
                     f"трей: иконка зарегистрирована (hwnd={res.get('hwnd')})")
            self._tray_note.set(tr("tray.note"))
            self._tray_hide_btn.configure(state="normal")
        else:
            reason = res.get("error") or "неизвестно"
            self.log("warning", tr("tray.unavailable", reason=reason))
            self._tray_note.set(tr("tray.unavailable", reason=reason))
            self._tray_hide_btn.configure(state="disabled")
        return res

    def _stop_tray(self) -> None:
        if self._tray:
            self._tray.quit()
            self._tray.join(2.0)
            self._tray = None

    def toggle_tray(self) -> None:
        """Чекбокс «Скрывать в трей»."""
        flag = tray.set_enabled(bool(self._tray_var.get()))
        if flag:
            res = self.start_tray()
            self.log("info", tr("tray.on"))
            if not res.get("ok"):
                self._tray_var.set(False)      # отказ - чекбокс не врёт
        else:
            self._stop_tray()
            self._tray_hide_btn.configure(state="disabled")
            self.log("info", tr("tray.off"))
        self._tray_update()

    def hide_window(self) -> None:
        """Кнопка «Скрыть окно»: обход и автоциклы продолжают работать."""
        if not tray.enabled() or not self._tray:
            messagebox.showwarning("Diaspas", tr("tray.off"), parent=self)
            return
        self._q.put(("tray_hide",))

    def _hide_window(self) -> None:
        self.withdraw()
        self._hidden = True
        self.log("info", tr("tray.hidden"))
        self._tray_update()

    def _show_window(self) -> None:
        self.deiconify()
        self.lift()
        self._hidden = False
        self.log("info", tr("tray.shown"))
        self._tray_update()

    def _on_close(self) -> None:
        """Крестик = выход: иконку убираем, службу zapret не трогаем."""
        self._stop_tray()
        self.destroy()


class _TrayOps:
    """Адаптер трея к Tk: тот же набор операций, что у webview-App.

    Подтверждений нет - пункт меню выбран осознанно; исключение только
    «Выход» при работающем обходе. Долгие операции уходят в ``_bg`` и
    возвращаются в UI через очередь.
    """

    def __init__(self, app: "App") -> None:
        self._app = app

    def apply_preset(self, key: str) -> dict:
        preset = next((p for p in presets_mod.load_presets()
                       if p.key == key), None)
        if preset is None:
            return {"ok": False, "error": f"пресет {key!r} не найден"}
        if self._app._zapret_root is None:
            return {"ok": False, "error": tr("err.no_dir")}
        self._app._bg(self._app._apply_worker, preset)
        return {"ok": True, "started": key}

    def apply_strategy(self, file: str) -> dict:
        if self._app._zapret_root is None:
            return {"ok": False, "error": tr("err.no_dir")}
        self._app._bg(self._app._strategy_worker, str(file))
        return {"ok": True, "started": str(file)}

    def mode(self) -> str:
        return strategies_mod.mode()

    def set_mode(self, mode: str) -> dict:
        value = str(mode or "").strip().lower()
        if value not in (strategies_mod.MANUAL, strategies_mod.AUTO):
            return {"ok": False, "error": f"неизвестный режим {value!r}"}
        if not strategies_mod.set_mode(value):
            return {"ok": False, "error": "режим не сохранён"}
        self._app.log("info", tr("strategies.mode_set",
                                 mode="авто" if value == strategies_mod.AUTO
                                 else "ручной"))
        self._app._q.put(("tray_mode", value))
        return {"ok": True, "mode": value}

    def auto_run(self, force: bool = False) -> dict:
        if self._app._zapret_root is None:
            return {"ok": False, "error": tr("err.no_dir")}
        self._app._bg(self._app._auto_worker, None, force)
        return {"ok": True, "started": "auto"}

    def check_and_autofix(self) -> dict:
        """Пробы; сбой в режиме «авто» сам запускает подбор (_probes_worker)."""
        if self._app._zapret_root is None:
            return {"ok": False, "error": tr("err.no_dir")}
        self._app._bg(self._app._probes_worker)
        return {"ok": True, "started": "probes"}

    def show_window(self, tab: str | None = None) -> dict:
        self._app._q.put(("tray_show", tab))
        return {"ok": True}

    def hide_window(self) -> dict:
        self._app._q.put(("tray_hide",))
        return {"ok": True}

    def exit_app(self) -> dict:
        if service.state().running and not tray.confirm(
                "Выход", tr("tray.exit_confirm")):
            self._app.log("info", "выход из трея отменён")
            return {"ok": True, "cancelled": True}
        self._app.log("info", "выход из трея: закрытие приложения")
        self._app._q.put(("tray_exit",))
        return {"ok": True}


def main(hidden: bool = False) -> int:
    """Tk-резерв: тот же трей, что в webview, и тот же одиночный экземпляр.

    Проверка экземпляра - до создания окна: второй запуск не должен показывать
    своё окно и запускать второй автоцикл.
    """
    app = App()
    if tray.enabled():
        claim = tray.Tray(app._tray_spec, app.tray_action).claim_singleton()
        if claim.get("ok") and not claim.get("owner"):
            app.log("info", tr("tray.second_instance"))
            app.destroy()
            return 0
        if claim.get("ok"):
            app.start_tray()
        else:
            app.log("warning", f"трей: {claim.get('error')}")
    if hidden:
        app.after(150, app.hide_window)
    app.mainloop()
    app._stop_tray()
    return 0

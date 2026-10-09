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

from core import abtest, autoselect, presets as presets_mod, probes, release, service
from core import log as dlog
from core import paths
from i18n import tr


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(tr("app.title"))
        self.geometry("900x720")
        self.minsize(760, 600)

        # очередь из фоновых потоков в главный
        self._q: queue.Queue = queue.Queue()
        self._busy = False
        self._zapret_root: Path | None = paths.zapret_dir_from_settings()
        self._last_ab: dict | None = None
        self._last_select: dict | None = None
        self._last_probe_report: dict | None = None

        self._build()
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
                elif kind == "busy":
                    self._set_busy(payload[0])
                elif kind == "release":
                    self._release_var.set(payload[0])
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
                current = presets_mod.current_preset(root, state)
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
        self._select_best.set(
            f"{tr('select.progress', done=done, total=total)}  "
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
        # предложить применить лучший пресет со стратегией-победителем
        if best:
            self._offer_best(best)

    def _offer_best(self, strategy_file: str) -> None:
        """После перебора: подставить победителя в пресет Apex/EA (или первый)."""
        target = None
        for preset in self._preset_list:
            if preset.key == "apex_ea":
                target = preset
                break
        if target is None:
            return
        from dataclasses import replace
        candidate = replace(target, strategy=strategy_file,
                            title=f"{target.title} ({paths.strategy_label(strategy_file)})")
        if messagebox.askyesno(
                "Diaspas",
                f"Лучшая стратегия: {paths.strategy_label(strategy_file)}.\n"
                f"Применить пресет «{candidate.title}» с ней?",
                parent=self) is True:
            self._bg(self._apply_worker, candidate)


def main() -> int:
    app = App()
    app.mainloop()
    return 0

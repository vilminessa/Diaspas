"""Трей в webview-интерфейсе: окно, меню, автоцикл из-под трея.

Проверяется граница, которую нельзя увидеть офлайн-тестами модели: App
должен принимать те же операции, что диспетчер трея, и не запускать одну
операцию дважды. Win32-клей подменён фиктивным объектом.
"""

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import paths, probes, service, strategies as strategies_mod, tray  # noqa: E402
from ui import webapp  # noqa: E402

DIR = Path("C:/zapret/zapret-discord-youtube-1.10.3")


class FakeWindow:
    """Pywebview Window: фиксирует hide/show/destroy."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def hide(self) -> None:
        self.calls.append("hide")

    def show(self) -> None:
        self.calls.append("show")

    def destroy(self) -> None:
        self.calls.append("destroy")


class FakeTray:
    """core.tray.Tray без Win32: отмечает update/notify/quit."""

    def __init__(self, spec=None, on_action=None) -> None:
        self.spec = spec
        self.on_action = on_action
        self.hwnd = 4242
        self.updates = 0
        self.notices: list[tuple[str, str]] = []
        self.quit_called = False

    def start(self) -> dict:
        return {"ok": True, "hwnd": self.hwnd}

    def update(self) -> None:
        self.updates += 1

    def notify(self, title: str, text: str) -> None:
        self.notices.append((title, text))

    def quit(self) -> None:
        self.quit_called = True

    def join(self, timeout: float = 3.0) -> None:
        pass

    def claim_singleton(self) -> dict:
        return {"ok": True, "owner": True}


def make_app(tmp_path, monkeypatch, root=DIR):
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    paths.write_settings({"active_preset": "ubisoft"})
    st = service.ServiceState(installed=True, running=True,
                              start_type="Automatic",
                              strategy="general (ALT11)", pid=1234, binpath="")
    monkeypatch.setattr(service, "state", lambda log=None: st)
    app = webapp.App()
    app.zapret_root = root
    app.window = FakeWindow()
    monkeypatch.setattr(webapp.tray, "Tray", FakeTray)
    return app


def test_state_reports_tray(tmp_path, monkeypatch):
    """UI различает «трей включён», «иконка поднята» и «окно скрыто»."""
    app = make_app(tmp_path, monkeypatch)
    state = app.state()
    assert state["tray"] is True
    assert state["tray_running"] is False
    assert state["window_hidden"] is False

    assert app.start_tray()["ok"] is True
    state = app.state()
    assert state["tray_running"] is True

    assert app.hide_window()["ok"] is True
    state = app.state()
    assert state["window_hidden"] is True


def test_hide_and_show_drive_window(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    assert app.start_tray()["ok"] is True
    app.hide_window()
    app.show_window(tab="journal")
    assert app.window.calls == ["hide", "show"]
    assert app.window_hidden is False


def test_hide_window_without_window_is_error(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    app.window = None
    assert app.hide_window()["ok"] is False
    assert app.show_window()["ok"] is False


def test_set_tray_off_removes_icon(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    assert app.set_tray(True)["ok"] is True
    holder = app.tray
    assert holder is not None and holder.hwnd
    assert app.set_tray(False)["ok"] is True
    assert app.tray is None
    assert holder.quit_called is True
    assert app.state()["tray"] is False


def test_set_tray_failure_does_not_claim_success(tmp_path, monkeypatch):
    """Мягкий отказ: чекбокс не должен показать включённый трей."""
    app = make_app(tmp_path, monkeypatch)

    class Broken(FakeTray):
        def start(self) -> dict:
            return {"ok": False, "error": "нет интерактивного десктопа"}

    monkeypatch.setattr(webapp.tray, "Tray", Broken)
    res = app.set_tray(True)
    assert res["ok"] is False
    assert res["tray"] is False
    assert app.tray is None


def test_notify_writes_journal_and_tray(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    app.start_tray()
    app.notify("Diaspas", "авто: применена ALT4")
    assert app.tray.notices == [("Diaspas", "авто: применена ALT4")]
    lines = app.log_tail(50)
    assert any("авто: применена ALT4" in line for line in lines)


def test_check_and_autofix_runs_auto_only_on_failure(tmp_path, monkeypatch):
    """Триггер из JS перенесён в Python: со скрытым окном JS ничего не ждёт."""
    app = make_app(tmp_path, monkeypatch)
    app.start_tray()
    broken = {"state": "partial", "count": 10, "total": 11, "groups": {},
              "ok": False}
    fixed = {"state": "full", "count": 11, "total": 11, "groups": {},
             "ok": True}
    monkeypatch.setattr(probes, "probe_all",
                        lambda timeout=6.0, groups=None: dict(broken))
    monkeypatch.setattr(probes, "failed_targets",
                        lambda rep: ["youtube.media"])
    auto_calls: list[bool] = []

    def fake_auto(force: bool = False) -> dict:
        auto_calls.append(force)
        return {"ok": True, "label": "ALT4"}

    monkeypatch.setattr(app, "auto_run", fake_auto)

    # режим manual: подбор не запускается, но уведомление о сбое есть
    strategies_mod.set_mode(strategies_mod.MANUAL)
    res = app.check_and_autofix()
    assert res["failed"] == ["youtube.media"]
    assert auto_calls == []
    assert any("не отвечают" in n[1] for n in app.tray.notices)

    strategies_mod.set_mode(strategies_mod.AUTO)
    res = app.check_and_autofix()
    assert auto_calls == [False]        # cooldown не форсируется
    assert any("ALT4" in n[1] for n in app.tray.notices)

    monkeypatch.setattr(probes, "probe_all",
                        lambda timeout=6.0, groups=None: dict(fixed))
    monkeypatch.setattr(probes, "failed_targets", lambda rep: [])
    auto_calls.clear()
    res = app.check_and_autofix()
    assert auto_calls == []             # всё отвечает - менять нечего


def test_tray_action_rejects_duplicate_heavy_operation(tmp_path, monkeypatch):
    """Повторный клик той же операции отказывается, а не запускает вторую."""
    app = make_app(tmp_path, monkeypatch)
    app.start_tray()
    gate = threading.Event()
    started = threading.Event()

    def slow_apply(key: str) -> dict:
        started.set()
        gate.wait(2.0)
        return {"ok": True, "preset": key}

    monkeypatch.setattr(app, "apply_preset", slow_apply)
    first = app.tray_action("preset:apex_ea")
    assert first["ok"] is True and first["started"] == "preset:apex_ea"
    assert started.wait(1.0)
    second = app.tray_action("preset:apex_ea")
    assert second["ok"] is False
    assert "уже выполняется" in second["error"]
    gate.set()
    for _ in range(40):
        if not app._tray_busy:
            break
        threading.Event().wait(0.05)
    assert not app._tray_busy


def test_tray_action_light_item_runs_inline(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    app.start_tray()
    res = app.tray_action(tray.SHOW)
    assert res["ok"] is True
    assert app.window.calls == ["show"]
    assert app.tray.updates >= 1      # состояние иконки обновлено


def test_tray_action_auto_run_respects_cycle_lock(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    app.start_tray()
    app._auto_running = True
    res = app.tray_action(tray.AUTO_RUN)
    assert res["ok"] is False
    assert "уже выполняется" in res["error"]


def test_tray_action_unknown_item_logs_error(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    app.start_tray()
    res = app.tray_action("bogus:item")
    assert res["ok"] is False
    assert any("bogus" in line for line in app.log_tail(50))


def test_exit_confirms_only_when_bypass_running(tmp_path, monkeypatch):
    """«Выход» спрашивает, когда обход работает; без обхода - без вопроса."""
    app = make_app(tmp_path, monkeypatch)
    asked: list[str] = []
    monkeypatch.setattr(webapp.tray, "confirm",
                        lambda title, text: (asked.append(text) or False))
    res = app.exit_app()
    assert asked and res.get("cancelled") is True
    assert app.window.calls == []

    monkeypatch.setattr(service, "state", lambda log=None: service.ServiceState(
        installed=False, running=False, start_type="", strategy="", pid=0,
        binpath=""))
    asked.clear()
    res = app.exit_app()
    assert asked == []
    assert app.window.calls == ["destroy"]


def test_tray_spec_reflects_busy_and_probes(tmp_path, monkeypatch):
    """Меню строится по реальному состоянию: цвет, tooltip, отметки."""
    root = tmp_path / "zapret"
    root.mkdir()
    for name in ("general (ALT11).bat", "general (ALT7).bat"):
        (root / name).write_text('start /min "%BIN%winws.exe" --wf-tcp=80\r\n',
                                 encoding="utf-8")
    app = make_app(tmp_path, monkeypatch, root=root)
    app.last_probes = {"state": "partial", "count": 10, "total": 11}
    app._auto_running = True

    spec = app.tray_spec()
    assert spec.icon_state == "warn"          # обход работает, но есть сбой
    assert "ALT11" in spec.tooltip
    assert "подбор идёт" in spec.tooltip

    presets = next(e for e in spec.items
                   if isinstance(e, tray.Submenu) and e.id == "presets")
    assert presets.items and all(not i.checked for i in presets.items)
    # идущий подбор гасит выбор пресета и стратегии
    assert all(not i.enabled for i in presets.items)

    strategies = next(e for e in spec.items
                      if isinstance(e, tray.Submenu) and e.id == "strategies")
    # порядок естественной сортировки: ALT7 раньше ALT11 (как в service.bat)
    assert [i.id for i in strategies.items] == [
        "strategy:general (ALT7).bat", "strategy:general (ALT11).bat"]
    assert [i.checked for i in strategies.items] == [False, True]

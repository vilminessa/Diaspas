"""Модель меню трея, диспетчер пунктов и настройки.

Тесты без Win32: NotifyIcon требует интерактивного десктопа, поэтому
проверяется то, что от десктопа не зависит - порядок и состояние пунктов,
tooltip, цвет иконки, разбор id и отображение пунктов в операции.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import paths, tray  # noqa: E402

STATE_RUN = {
    "dir_ok": True, "installed": True, "running": True,
    "strategy": "general (ALT11)", "strategy_label": "ALT11",
    "strategy_mode": "auto", "preset_key": "discord_youtube",
    "preset_title": "Discord / YouTube", "auto_cooldown_left": 0,
    "auto_last_strategy": "general (ALT4).bat",
    "strategies": [{"file": "general (ALT11).bat", "label": "ALT11",
                    "active": True},
                   {"file": "general (ALT4).bat", "label": "ALT4",
                    "active": False}],
}
PRESETS = [{"key": "discord_youtube", "title": "Discord / YouTube", "off": False},
           {"key": "apex_ea", "title": "Apex / EA", "off": False},
           {"key": "off", "title": "Выкл", "off": True}]


def ids(spec: tray.TraySpec) -> list[str]:
    """Все id пунктов, включая подменю - в порядке меню."""
    out: list[str] = []
    for entry in spec.items:
        if isinstance(entry, tray.Separator):
            continue
        if isinstance(entry, tray.Submenu):
            out.extend(item.id for item in entry.items)
        else:
            out.append(entry.id)
    return out


def item(spec: tray.TraySpec, action: str) -> tray.Item | None:
    for entry in spec.items:
        if isinstance(entry, tray.Submenu):
            for it in entry.items:
                if it.id == action:
                    return it
        elif isinstance(entry, tray.Item) and entry.id == action:
            return entry
    return None


def test_menu_order_and_shape():
    spec = tray.tray_model(STATE_RUN, PRESETS)
    kinds = [type(e).__name__ for e in spec.items]
    assert kinds == ["Item", "Item", "Separator", "Submenu", "Submenu",
                     "Separator", "Item", "Item", "Item", "Separator",
                     "Item", "Item"], kinds
    labels = [e.label for e in spec.items if isinstance(e, tray.Item)]
    assert labels == ["Открыть окно", "Скрыть окно", "Автоподбор стратегий",
                      "Применить лучшую стратегию", "Проверить цели",
                      "Журнал", "Выход"]


def test_menu_excludes_off_preset():
    spec = tray.tray_model(STATE_RUN, PRESETS)
    assert "preset:off" not in ids(spec)
    assert "preset:discord_youtube" in ids(spec)


def test_radio_marks_active_preset_and_strategy():
    spec = tray.tray_model(STATE_RUN, PRESETS)
    marked = [i for i in ids(spec)
              if (it := item(spec, i)) and it.checked]
    assert marked == ["preset:discord_youtube", "strategy:general (ALT11).bat",
                      "mode.toggle"]


def test_strategies_without_dir_are_disabled():
    """Нет папки - пункты пресетов и стратегий серые, а не «ошибка при клике»."""
    spec = tray.tray_model({**STATE_RUN, "dir_ok": False}, PRESETS)
    assert all(not item(spec, i).enabled for i in ids(spec)
               if i.startswith((tray.PRESET_PREFIX, tray.STRATEGY_PREFIX)))
    assert item(spec, tray.MODE_TOGGLE).enabled is False
    assert item(spec, tray.AUTO_RUN).enabled is False
    assert item(spec, tray.PROBES_RUN).enabled is False
    # окно и выход доступны всегда
    assert item(spec, tray.SHOW).enabled and item(spec, tray.EXIT).enabled


def test_running_cycle_disables_choice_items():
    """Идущий подбор: выбор пресета/стратегии гасится, чтобы не перебить цикл."""
    spec = tray.tray_model(STATE_RUN, PRESETS, busy={"auto": True})
    assert item(spec, "preset:apex_ea").enabled is False
    assert item(spec, "preset:apex_ea").note == "подбор идёт"
    assert item(spec, "strategy:general (ALT4).bat").enabled is False
    assert item(spec, tray.PROBES_RUN).enabled is False
    assert item(spec, tray.AUTO_RUN).enabled is False


def test_auto_run_needs_auto_mode_and_no_cooldown():
    spec = tray.tray_model(STATE_RUN, PRESETS)
    assert item(spec, tray.AUTO_RUN).enabled is True
    manual = tray.tray_model({**STATE_RUN, "strategy_mode": "manual"}, PRESETS)
    assert item(spec, tray.MODE_TOGGLE).checked is True
    assert item(manual, tray.MODE_TOGGLE).checked is False
    assert item(manual, tray.AUTO_RUN).enabled is False
    paused = tray.tray_model({**STATE_RUN, "auto_cooldown_left": 600}, PRESETS)
    assert item(paused, tray.AUTO_RUN).enabled is False
    assert item(paused, tray.AUTO_RUN).note == "через 10 мин"


def test_action_ids_are_unique_and_stable():
    spec = tray.tray_model(STATE_RUN, PRESETS)
    found = ids(spec)
    assert len(found) == len(set(found)), "дубли id в меню"
    # порядок пунктов меняется - id остаются теми же операциями
    assert tray.SHOW in found and tray.EXIT in found
    assert tray.preset_key_of("preset:apex_ea") == "apex_ea"
    assert tray.preset_key_of("strategy:general.bat") is None
    assert tray.strategy_file_of("strategy:general (ALT7).bat") == "general (ALT7).bat"
    assert tray.strategy_file_of("preset:apex_ea") is None


def test_heavy_actions_are_the_long_ones():
    """Долгие операции - пресет, стратегия, подбор, пробы: их уводит в нить."""
    assert tray.is_heavy("preset:apex_ea")
    assert tray.is_heavy("strategy:general (ALT7).bat")
    assert tray.is_heavy(tray.AUTO_RUN)
    assert tray.is_heavy(tray.PROBES_RUN)
    assert not tray.is_heavy(tray.SHOW)
    assert not tray.is_heavy(tray.HIDE)
    assert not tray.is_heavy(tray.MODE_TOGGLE)
    assert not tray.is_heavy(tray.EXIT)


def test_icon_state_follows_service_and_probes():
    assert tray.icon_state(STATE_RUN) == "ok"
    assert tray.icon_state(STATE_RUN, {"state": "partial"}) == "warn"
    assert tray.icon_state(STATE_RUN, {"state": "none"}) == "err"
    assert tray.icon_state({**STATE_RUN, "running": False}) == "off"
    assert tray.icon_state({**STATE_RUN, "dir_ok": False}) == "off"


def test_tooltip_is_short_and_reads_state():
    text = tray.tooltip(STATE_RUN, last_probes={"count": 11, "total": 11})
    assert len(text) <= tray.TIP_LIMIT, text
    assert "ALT11" in text and "Discord / YouTube" in text and "11/11" in text
    assert "авто" in text
    # последний автовыбор не влезает: факультативное не добавляется куском
    assert "ALT4" not in text

    without_preset = tray.tooltip(
        {k: v for k, v in STATE_RUN.items() if k != "preset_title"},
        last_probes={"count": 10, "total": 11})
    assert "авто: ALT4" in without_preset and "10/11" in without_preset
    assert len(without_preset) <= tray.TIP_LIMIT

    busy = tray.tooltip(STATE_RUN, busy={"auto": True})
    assert "подбор идёт" in busy
    paused = tray.tooltip({**STATE_RUN, "auto_cooldown_left": 1200})
    assert "мин" in paused
    off = tray.tooltip({"dir_ok": True, "installed": False, "running": False})
    assert "обход выключен" in off
    nodir = tray.tooltip({"dir_ok": False})
    assert "нет папки zapret" in nodir


def test_icon_resources_are_distinct_system_signs():
    """Каждое состояние - свой системный значок; они различимы в трее."""
    assert set(tray.ICON_RESOURCES) == {"ok", "warn", "err", "off"}
    assert len(set(tray.ICON_RESOURCES.values())) == 4, "значки не различаются"
    # ординалы IDI_* из Windows (проверены пробом LoadIconW)
    assert tray.ICON_RESOURCES["off"] == 32512   # IDI_APPLICATION
    assert tray.ICON_RESOURCES["err"] == 32513   # IDI_ERROR
    assert tray.ICON_RESOURCES["warn"] == 32515  # IDI_WARNING
    assert tray.ICON_RESOURCES["ok"] == 32516    # IDI_INFORMATION


def test_icon_state_only_those_four_values():
    """Модель выдаёт только ключи ICON_RESOURCES: диспетчер не промахивается."""
    values = {
        tray.icon_state(STATE_RUN),
        tray.icon_state(STATE_RUN, {"state": "full"}),
        tray.icon_state(STATE_RUN, {"state": "partial"}),
        tray.icon_state(STATE_RUN, {"state": "none"}),
        tray.icon_state(STATE_RUN, {"state": "bogus"}),
        tray.icon_state({**STATE_RUN, "running": False}),
    }
    assert values <= set(tray.ICON_RESOURCES), values


class FakeOps:
    """Записывает вызовы - диспетчер проверяется без UI и без прав."""

    def __init__(self, mode: str = "manual") -> None:
        self.calls: list[tuple[str, object]] = []
        self._mode = mode

    def apply_preset(self, key):
        self.calls.append(("preset", key))
        return {"ok": True, "preset": key}

    def apply_strategy(self, file):
        self.calls.append(("strategy", file))
        return {"ok": True, "file": file}

    def mode(self):
        return self._mode

    def set_mode(self, mode):
        self._mode = mode
        self.calls.append(("mode", mode))
        return {"ok": True, "mode": mode}

    def auto_run(self, force=False):
        self.calls.append(("auto", force))
        return {"ok": True}

    def check_and_autofix(self):
        self.calls.append(("fix", None))
        return {"ok": True}

    def show_window(self, tab=None):
        self.calls.append(("show", tab))
        return {"ok": True}

    def hide_window(self):
        self.calls.append(("hide", None))
        return {"ok": True}

    def exit_app(self):
        self.calls.append(("exit", None))
        return {"ok": True}


def test_dispatch_maps_every_item():
    ops = FakeOps()
    assert tray.handle("preset:apex_ea", ops)["ok"]
    assert tray.handle("strategy:general (ALT7).bat", ops)["ok"]
    assert tray.handle(tray.SHOW, ops)["ok"]
    assert tray.handle(tray.JOURNAL, ops)["ok"]
    assert tray.handle(tray.HIDE, ops)["ok"]
    assert tray.handle(tray.AUTO_RUN, ops)["ok"]
    assert tray.handle(tray.PROBES_RUN, ops)["ok"]
    assert tray.handle(tray.EXIT, ops)["ok"]
    assert [c[0] for c in ops.calls] == [
        "preset", "strategy", "show", "show", "hide", "auto", "fix", "exit"]
    assert ops.calls[3] == ("show", "journal")


def test_dispatch_toggle_flips_mode():
    ops = FakeOps(mode="manual")
    tray.handle(tray.MODE_TOGGLE, ops)
    assert ops.mode() == "auto"
    tray.handle(tray.MODE_TOGGLE, ops)
    assert ops.mode() == "manual"


def test_dispatch_unknown_item_is_error_not_exception():
    ops = FakeOps()
    res = tray.handle("bogus:item", ops)
    assert res["ok"] is False
    assert "bogus" in res["error"]
    assert ops.calls == []


def test_tray_setting_default_on(tmp_path, monkeypatch):
    """Без записи в настройках трей включён: скрытие полезно каждому запуску."""
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    assert tray.enabled() is True
    assert tray.set_enabled(False) is False
    assert tray.enabled() is False
    assert tray.set_enabled(True) is True
    assert tray.enabled() is True


def test_tray_setting_survives_other_keys(tmp_path, monkeypatch):
    """Запись трея не затирает остальные настройки (strategy_mode и т.п.)."""
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    paths.write_settings({"strategy_mode": "auto", "zapret_dir": "D:\\zapret"})
    tray.set_enabled(False)
    settings = paths.read_settings()
    assert settings["tray"] is False
    assert settings["strategy_mode"] == "auto"
    assert settings["zapret_dir"] == "D:\\zapret"

"""Смена стратегии: список, сохранённый выбор, ручное применение.

Служба в этих тестах не трогается реально - `service.state` и
`service.install_strategy` подменены: важнее всего контракт
``apply`` (что уходит в установку и что остаётся в настройках).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import paths, presets as pm, service, strategies as sm  # noqa: E402
from core.service import ServiceState  # noqa: E402

# Играет роль service.bat: диапазоны Game Filter подставляются переменными.
BAT = ('start /min "%BIN%winws.exe" --wf-tcp=%GameFilterTCP% '
       '--wf-udp=%GameFilterUDP%\r\n')

ALT11_STATE = ServiceState(installed=True, running=True, start_type="Automatic",
                           strategy="general (ALT11)", pid=1, binpath="")


def make_root(tmp_path: Path) -> Path:
    """Мини-установка zapret: несколько стратегий и файлы режимов."""
    (tmp_path / "lists").mkdir()
    (tmp_path / "utils").mkdir()
    for name in ("general (ALT11).bat", "general (ALT12).bat",
                 "general (ALT7).bat", "general.bat"):
        (tmp_path / name).write_text(BAT, encoding="utf-8")
    (tmp_path / "service.bat").write_text("@echo off\r\n", encoding="utf-8")
    (tmp_path / "lists" / "list-exclude-user.txt").write_text(
        "# Never leave this file empty\r\ndomain.example.abc\r\n",
        encoding="utf-8")
    (tmp_path / "lists" / "ipset-all.txt").write_text(
        "1.0.0.0/24\r\n", encoding="ascii")
    return tmp_path


@pytest.fixture()
def isolated_settings(tmp_path, monkeypatch):
    """settings.json указывает во временную папку, а не в профиль."""
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    return tmp_path


def capture_install(monkeypatch):
    """Записывает аргументы установки службы вместо реального помощника."""
    calls: list[dict] = []

    def fake(binpath, strategy_label, exe="", log=None):
        calls.append({"binpath": binpath, "label": strategy_label})
        return {"ok": True, "state": {"installed": True, "running": True}}

    monkeypatch.setattr(service, "install_strategy", fake)
    return calls


# -- список ---------------------------------------------------------------

def test_list_labels_and_active_flag(tmp_path):
    root = make_root(tmp_path)
    infos = sm.list_strategies(root, "general (ALT11)")
    assert {info.label for info in infos} == {"ALT11", "ALT12", "ALT7", "general"}
    assert [info.file for info in infos if info.active] == ["general (ALT11).bat"]


def test_unknown_registry_label_still_shown(tmp_path):
    """Службу пересоздали вне Diaspas - GUI обязан показать правду."""
    root = make_root(tmp_path)
    infos = sm.list_strategies(root, "general (CUSTOM)")
    active = [info for info in infos if info.active]
    assert len(active) == 1 and active[0].label == "CUSTOM"


def test_list_empty_without_root():
    assert sm.list_strategies(None, "general (ALT11)") == []


# -- сохранённый выбор и режим -------------------------------------------

def test_resolve_prefers_saved_choice(tmp_path, isolated_settings):
    root = make_root(tmp_path)
    sm.remember("general (ALT7).bat")
    assert sm.resolve("general (ALT11).bat", root) == "general (ALT7).bat"


def test_resolve_falls_back_when_file_gone(tmp_path, isolated_settings):
    """Обновление релиза могло убрать файл - default пресета спасает."""
    root = make_root(tmp_path)
    sm.remember("general (ALT99).bat")
    assert sm.resolve("general (ALT11).bat", root) == "general (ALT11).bat"


def test_mode_default_manual_and_set(isolated_settings):
    assert sm.mode() == sm.MANUAL
    assert sm.set_mode("auto") is True
    assert sm.mode() == sm.AUTO
    assert sm.set_mode("weird") is False
    assert sm.mode() == sm.AUTO


def test_validate_rejects_paths_and_unknown_files(tmp_path):
    root = make_root(tmp_path)
    assert sm.validate(root, "general (ALT7).bat") is None
    assert sm.validate(root, "..\\evil.bat") is not None
    assert sm.validate(root, r"C:\zapret\general (ALT7).bat") is not None
    assert sm.validate(root, "service.bat") is not None
    assert sm.validate(root, "general (ALT99).bat") is not None
    assert sm.validate(root, "") is not None


# -- ручное применение ----------------------------------------------------

def test_apply_keeps_modes_and_records_choice(tmp_path, isolated_settings,
                                              monkeypatch):
    """Смена стратегии меняет только аргументы службы, не режимы."""
    root = make_root(tmp_path)
    (root / "utils" / "game_filter.enabled").write_text("all\r\n",
                                                        encoding="ascii")
    pm.remember_active("ubisoft")
    monkeypatch.setattr(service, "state", lambda log=None: ALT11_STATE)
    calls = capture_install(monkeypatch)

    res = sm.apply(root, "general (ALT7).bat")
    assert res["ok"] is True, res
    assert len(calls) == 1
    assert calls[0]["label"] == "general (ALT7)"
    # Game Filter "all" -> оба диапазона портов открыты
    assert "1024-65535" in calls[0]["binpath"]

    # режимы и списки не тронуты
    assert (root / "utils" / "game_filter.enabled").read_text(
        encoding="ascii").strip() == "all"
    assert "1.0.0.0/24" in (root / "lists" / "ipset-all.txt").read_text(
        encoding="ascii")
    assert "domain.example.abc" in (root / "lists" / "list-exclude-user.txt"
                                   ).read_text(encoding="utf-8")

    settings = paths.read_settings()
    assert settings["active_strategy"] == "general (ALT7).bat"
    assert settings["strategy_mode"] == sm.MANUAL
    assert settings["active_preset"] == "ubisoft"   # пресет не переписан


def test_apply_ports_off_when_filter_absent(tmp_path, isolated_settings,
                                            monkeypatch):
    """Без game_filter.enabled порты идут как у выключенного фильтра ('12')."""
    root = make_root(tmp_path)
    monkeypatch.setattr(service, "state", lambda log=None: ALT11_STATE)
    calls = capture_install(monkeypatch)
    assert sm.apply(root, "general (ALT12).bat")["ok"] is True
    assert "--wf-tcp 12" in calls[0]["binpath"]
    assert "--wf-udp 12" in calls[0]["binpath"]


def test_apply_without_service_uses_suggested_preset(tmp_path,
                                                    isolated_settings,
                                                    monkeypatch):
    """Скачан релиз, службы нет: стратегия создаёт службу по базовому пресету."""
    root = make_root(tmp_path)
    pm.remember_active("apex_ea")
    monkeypatch.setattr(service, "state",
                        lambda log=None: ServiceState(
                            installed=False, running=False, start_type="",
                            strategy="", pid=0, binpath=""))
    calls = capture_install(monkeypatch)

    res = sm.apply(root, "general (ALT7).bat")
    assert res["ok"] is True, res
    assert calls[0]["label"] == "general (ALT7)"
    # списки обязательны: без них winws не стартует
    assert (root / "lists" / "list-general-user.txt").is_file()
    assert (root / "utils" / "game_filter.enabled").is_file()   # apex_ea = all
    assert paths.read_settings()["active_preset"] == "apex_ea"


def test_apply_rejects_unknown_strategy(tmp_path, isolated_settings,
                                        monkeypatch):
    root = make_root(tmp_path)
    monkeypatch.setattr(service, "state", lambda log=None: ALT11_STATE)
    calls = capture_install(monkeypatch)
    res = sm.apply(root, "general (ALT99).bat")
    assert res["ok"] is False
    assert calls == []
    assert "active_strategy" not in paths.read_settings()


def test_apply_failure_keeps_previous_choice(tmp_path, isolated_settings,
                                             monkeypatch):
    """Неудачная установка не должна переписывать выбранную стратегию."""
    root = make_root(tmp_path)
    sm.remember("general (ALT11).bat")
    monkeypatch.setattr(service, "state", lambda log=None: ALT11_STATE)
    monkeypatch.setattr(service, "install_strategy",
                        lambda *a, **k: {"ok": False, "error": "нет прав"})
    res = sm.apply(root, "general (ALT7).bat")
    assert res["ok"] is False
    assert paths.read_settings()["active_strategy"] == "general (ALT11).bat"


def test_apply_records_auto_mode(tmp_path, isolated_settings, monkeypatch):
    root = make_root(tmp_path)
    monkeypatch.setattr(service, "state", lambda log=None: ALT11_STATE)
    capture_install(monkeypatch)
    sm.apply(root, "general (ALT12).bat", mode_value=sm.AUTO)
    settings = paths.read_settings()
    assert settings["active_strategy"] == "general (ALT12).bat"
    assert settings["strategy_mode"] == sm.AUTO


# -- пресеты используют сохранённую стратегию -----------------------------

def test_preset_apply_honours_saved_strategy(tmp_path, isolated_settings,
                                             monkeypatch):
    """Пресет не возвращает ALT11, если пользователь выбрал ALT7."""
    root = make_root(tmp_path)
    sm.remember("general (ALT7).bat")
    monkeypatch.setattr(service, "state", lambda log=None: ALT11_STATE)
    monkeypatch.setattr(service, "install_strategy",
                        lambda *a, **k: {"ok": True, "state": {}})
    monkeypatch.setattr(service, "remove", lambda log=None: {"ok": True})

    ubisoft = next(p for p in pm.DEFAULT_PRESETS if p.key == "ubisoft")
    res = pm.apply(ubisoft, root)
    assert res["ok"] is True
    assert res["strategy"] == "general (ALT7).bat"
    assert paths.read_settings()["active_strategy"] == "general (ALT7).bat"


def test_current_file_maps_registry_label(tmp_path):
    root = make_root(tmp_path)
    assert sm.current_file(root, ALT11_STATE) == "general (ALT11).bat"
    assert sm.current_file(root, ServiceState(
        installed=True, running=False, start_type="Automatic",
        strategy="general (ALT7)", pid=0, binpath="")) == "general (ALT7).bat"
    assert sm.current_file(root, ServiceState(
        installed=False, running=False, start_type="", strategy="",
        pid=0, binpath="")) == ""

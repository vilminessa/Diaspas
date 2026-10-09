"""Маркер активного пресета (settings.active_preset).

Регрессия: после SAFE_EXCLUDE три игровых пресета стали конфигурационно
идентичны, инференс по состоянию возвращал первый по порядку (apex_ea),
и UI показывал «Apex / EA» даже после применения Ubisoft. Маркер
различает то, что состояние различать не может.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import paths, presets as pm  # noqa: E402
from core.presets import DEFAULT_PRESETS, Preset, apply_files, current_preset  # noqa: E402
from core.service import ServiceState  # noqa: E402

ALT11_STATE = ServiceState(installed=True, running=True,
                           start_type="Automatic",
                           strategy="general (ALT11)", pid=1, binpath="")


def make_root(tmp_path: Path) -> Path:
    (tmp_path / "lists").mkdir()
    (tmp_path / "utils").mkdir()
    (tmp_path / "general (ALT11).bat").write_text(
        'start /min "%BIN%winws.exe" --wf-tcp=80\r\n', encoding="utf-8")
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


def test_marker_wins_over_identical_signatures(tmp_path, isolated_settings):
    """Ключевой кейс: ubisoft применён, но сигнатуры apex/ubisoft равны."""
    root = make_root(tmp_path)
    ubisoft = next(p for p in DEFAULT_PRESETS if p.key == "ubisoft")
    apply_files(root, ubisoft)

    # без маркера детекция возвращает первый по порядку - apex_ea
    inferred = current_preset(root, ALT11_STATE)
    assert inferred is not None and inferred.key == "apex_ea"

    # с маркером - применённый пресет
    pm._remember_active("ubisoft")
    assert pm.active_preset_key() == "ubisoft"
    found = current_preset(root, ALT11_STATE,
                           active_key=pm.active_preset_key())
    assert found is not None and found.key == "ubisoft"


def test_marker_ignored_when_state_diverged(tmp_path, isolated_settings):
    """Состояние разошлось с маркером (ручные правки) -> fallback."""
    root = make_root(tmp_path)
    ubisoft = next(p for p in DEFAULT_PRESETS if p.key == "ubisoft")
    apply_files(root, ubisoft)          # game_filter=all, ipset=any
    pm._remember_active("ubisoft")

    # ручная правка: game filter выключен - ожидания маркера не выполняются
    (root / "utils" / "game_filter.enabled").unlink()
    found = current_preset(root, ALT11_STATE,
                           active_key=pm.active_preset_key())
    assert found is not None and found.key == "discord_youtube"


def test_no_marker_falls_back_to_inference(tmp_path, isolated_settings):
    """Без маркера (старые установки, правки вне Diaspas) - прежний инференс."""
    root = make_root(tmp_path)
    apex = next(p for p in DEFAULT_PRESETS if p.key == "apex_ea")
    apply_files(root, apex)
    found = current_preset(root, ALT11_STATE)
    assert found is not None and found.key == "apex_ea"


def test_unknown_marker_ignored(tmp_path, isolated_settings):
    """Маркер от несуществующего пресета не должен ронять детекцию."""
    root = make_root(tmp_path)
    apex = next(p for p in DEFAULT_PRESETS if p.key == "apex_ea")
    apply_files(root, apex)
    pm._remember_active("deleted_preset")
    found = current_preset(root, ALT11_STATE,
                           active_key=pm.active_preset_key())
    assert found is not None and found.key == "apex_ea"


def test_off_marker_not_used_for_highlight(tmp_path, isolated_settings):
    """Маркер off не подсвечивается, пока служба работает (инференс)."""
    root = make_root(tmp_path)
    apex = next(p for p in DEFAULT_PRESETS if p.key == "apex_ea")
    apply_files(root, apex)
    pm._remember_active("off")
    found = current_preset(root, ALT11_STATE,
                           active_key=pm.active_preset_key())
    assert found is not None and found.key == "apex_ea"


def test_failed_apply_keeps_old_marker(tmp_path, isolated_settings,
                                       monkeypatch):
    """Неудачное применение не перетирает маркер прежнего пресета."""
    root = make_root(tmp_path)
    pm._remember_active("ubisoft")

    monkeypatch.setattr(pm.service, "install_strategy",
                        lambda *a, **k: {"ok": False, "error": "нет прав"})
    apex = next(p for p in DEFAULT_PRESETS if p.key == "apex_ea")
    res = pm.apply(apex, root)
    assert not res["ok"]
    assert pm.active_preset_key() == "ubisoft", "маркер перетёрт при ошибке"


def test_successful_apply_writes_marker(tmp_path, isolated_settings,
                                        monkeypatch):
    root = make_root(tmp_path)
    monkeypatch.setattr(pm.service, "install_strategy",
                        lambda *a, **k: {"ok": True, "state": {}})
    ubisoft = next(p for p in DEFAULT_PRESETS if p.key == "ubisoft")
    res = pm.apply(ubisoft, root)
    assert res["ok"]
    assert pm.active_preset_key() == "ubisoft"


def test_merge_domains_no_duplicate_header(tmp_path):
    """Повторные применения не плодят дубли # Diaspas (регрессия)."""
    root = make_root(tmp_path)
    p1 = Preset(key="a", title="A", strategy="general (ALT11).bat",
                exclude_domains=("ea.com",))
    p2 = Preset(key="b", title="B", strategy="general (ALT11).bat",
                exclude_domains=("ubisoft.com",))
    apply_files(root, p1)
    apply_files(root, p2)
    text = (root / "lists" / "list-exclude-user.txt").read_text(
        encoding="utf-8")
    assert text.count("# Diaspas") == 1, text
    assert "ea.com" in text and "ubisoft.com" in text

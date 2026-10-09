"""Тесты пресетов: то, что применяется БЕЗ прав (флаги, ipset, списки).

Служба в этих тестах не трогается - apply_files не ходит в service, и это
главное свойство: почти всё применение пресета детерминировано и локально.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.presets import (  # noqa: E402
    DEFAULT_PRESETS,
    Preset,
    apply_files,
    current_preset,
)
from core.service import ServiceState  # noqa: E402


def make_root(tmp_path: Path) -> Path:
    """Мини-установка zapret: нужные файлы для записи пресета."""
    (tmp_path / "lists").mkdir()
    (tmp_path / "utils").mkdir()
    (tmp_path / "general (ALT11).bat").write_text(
        'start /min "%BIN%winws.exe" --wf-tcp=80\r\n', encoding="utf-8")
    (tmp_path / "service.bat").write_text("@echo off\r\n", encoding="utf-8")
    (tmp_path / "lists" / "list-exclude-user.txt").write_text(
        "# Never leave this file empty\r\ndomain.example.abc\r\n",
        encoding="utf-8")
    (tmp_path / "lists" / "list-general-user.txt").write_text(
        "# Never leave this file empty\r\ndomain.example.abc\r\n",
        encoding="utf-8")
    (tmp_path / "lists" / "ipset-all.txt").write_text(
        "1.0.0.0/24\r\n1.1.1.0/24\r\n", encoding="ascii")
    return tmp_path


def test_game_filter_flag_written(tmp_path):
    root = make_root(tmp_path)
    preset = Preset(key="t", title="T", strategy="general (ALT11).bat",
                    game_filter="all", ipset="any")
    apply_files(root, preset)
    assert (root / "utils" / "game_filter.enabled").read_text(
        encoding="ascii").strip() == "all"
    # ipset any = пустой файл (семантика service.bat)
    assert (root / "lists" / "ipset-all.txt").read_text(encoding="ascii") == ""


def test_game_filter_off_removes_flag(tmp_path):
    root = make_root(tmp_path)
    apply_files(root, Preset(key="t", title="T",
                             strategy="general (ALT11).bat",
                             game_filter="all"))
    assert (root / "utils" / "game_filter.enabled").is_file()
    apply_files(root, Preset(key="t2", title="T2",
                             strategy="general (ALT11).bat",
                             game_filter=""))
    assert not (root / "utils" / "game_filter.enabled").exists()


def test_ipset_none_marker(tmp_path):
    root = make_root(tmp_path)
    apply_files(root, Preset(key="t", title="T",
                             strategy="general (ALT11).bat",
                             ipset="none"))
    text = (root / "lists" / "ipset-all.txt").read_text(encoding="ascii")
    assert "203.0.113.113/32" in text


def test_ipset_loaded_restores_backup(tmp_path):
    root = make_root(tmp_path)
    (root / "lists" / "ipset-all.txt.backup").write_text(
        "9.9.9.0/24\r\n", encoding="ascii")
    apply_files(root, Preset(key="t", title="T",
                             strategy="general (ALT11).bat",
                             ipset="loaded"))
    assert "9.9.9.0/24" in (root / "lists" / "ipset-all.txt").read_text(
        encoding="ascii")


def test_domains_merged_once(tmp_path):
    root = make_root(tmp_path)
    preset = Preset(key="ea", title="EA", strategy="general (ALT11).bat",
                    exclude_domains=("ea.com", "ubisoft.com"))
    apply_files(root, preset)
    apply_files(root, preset)          # повторное применение не дублирует
    text = (root / "lists" / "list-exclude-user.txt").read_text(
        encoding="utf-8")
    assert text.count("ea.com") == 1
    assert text.count("ubisoft.com") == 1
    assert "domain.example.abc" in text   # чужие строки не тронуты
    assert "# Diaspas" in text            # пометка авторства вставки


def test_missing_strategy_raises(tmp_path):
    root = make_root(tmp_path)
    try:
        apply_files(root, Preset(key="x", title="X",
                                 strategy="general (ALT99).bat"))
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("ожидался FileNotFoundError для несуществующей стратегии")


def test_current_preset_detects_apex(tmp_path):
    """Состояние «ALT11 + game filter all + ipset any + ea.com» = пресет Apex.

    Метка в реестре - как её пишет service.bat: имя файла без .bat.
    """
    root = make_root(tmp_path)
    apex = next(p for p in DEFAULT_PRESETS if p.key == "apex_ea")
    apply_files(root, apex)

    state = ServiceState(installed=True, running=True, start_type="Automatic",
                         strategy="general (ALT11)", pid=1, binpath="...")
    found = current_preset(root, state)
    assert found is not None
    assert found.key == "apex_ea"


def test_current_preset_none_for_off(tmp_path):
    root = make_root(tmp_path)
    state = ServiceState(installed=False, running=False, start_type="",
                         strategy="", pid=0, binpath="")
    found = current_preset(root, state)
    assert found is not None and found.off

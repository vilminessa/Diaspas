"""Область переопределения стратегий: «по установке» и плоское наследие.

Регрессии:
* без найденной установки выбор терялся в том же вызове set_strategy
  (плоская запись создавалась и тут же сносилась pop'ом) - переключение
  стратегии молча возвращалось в дефолт;
* выбор ALT13 в 1.10.x не должен применяться к 1.9.x, где такого файла нет;
* явный выбор по установке снимает плоское наследие того же пресета,
  иначе сброс «вернуть дефолт» возвращал старую глобальную запись;
* дефолт пресета разрешается под layout win-bundle (preset*.cmd).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import paths, presets as pm  # noqa: E402
from test_cmdparser_layout import make_winws_install  # noqa: E402


def make_flowseal(root: Path, strategies: tuple[str, ...]) -> Path:
    """Мини-установка Flowseal: service.bat + набор general*.bat."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "service.bat").write_text("@echo off\r\n", encoding="utf-8")
    (root / "bin").mkdir(exist_ok=True)
    (root / "bin" / "winws.exe").write_bytes(b"MZ fake")
    (root / "lists").mkdir(exist_ok=True)
    (root / "utils").mkdir(exist_ok=True)
    for name in strategies:
        (root / name).write_text('start /min "%BIN%winws.exe" --wf-tcp=80\r\n',
                                 encoding="utf-8")
    return root


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """Настройки во временной папке; установка задаётся тестом."""
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(paths, "zapret_dir_info", lambda: (None, ""))
    return tmp_path


def use_install(monkeypatch, root):
    """Симулировать «активная установка = root» (как в живом UI)."""
    monkeypatch.setattr(paths, "zapret_dir_info", lambda: (root, "settings"))


def strategy_of(key: str, root=None) -> str:
    return next(p.strategy for p in pm.load_presets(root) if p.key == key)


# -- без найденной установки ------------------------------------------------

def test_choice_survives_when_install_not_found(isolated, monkeypatch):
    """Регрессия: плоская запись не должна сноситься в том же вызове."""
    assert strategy_of("apex_ea") == "general (ALT11).bat"
    pm.set_strategy("apex_ea", "general (ALT7).bat")
    assert strategy_of("apex_ea") == "general (ALT7).bat"
    stored = paths.read_settings()["preset_strategies"]
    assert stored == {"*": {"apex_ea": "general (ALT7).bat"}}, \
        "без установки выбор ложится под ключ «для любой установки»"

    # другой пресет не затронут
    assert strategy_of("discord_youtube") == "general (ALT11).bat"

    pm.set_strategy("apex_ea", None)
    assert strategy_of("apex_ea") == "general (ALT11).bat"
    assert paths.read_settings()["preset_strategies"] == {}


# -- выбор по установке -----------------------------------------------------

def test_choice_is_scoped_to_install(isolated, monkeypatch, tmp_path):
    """ALT13 в 1.10.x не переезжает в 1.9.x, где файла нет."""
    new = make_flowseal(tmp_path / "zapret-1.10.3",
                        ("general (ALT11).bat", "general (ALT13).bat"))
    old = make_flowseal(tmp_path / "zapret-1.9.8",
                        ("general (ALT11).bat", "general (ALT12).bat"))

    use_install(monkeypatch, new)
    pm.set_strategy("apex_ea", "general (ALT13).bat")
    assert strategy_of("apex_ea", new) == "general (ALT13).bat"

    use_install(monkeypatch, old)
    assert strategy_of("apex_ea", old) == "general (ALT11).bat"
    # и через default-root тот же результат: запись лежит под другим ключом
    assert strategy_of("apex_ea") == "general (ALT11).bat"


def test_explicit_choice_clears_flat_legacy(isolated, monkeypatch, tmp_path):
    """Явный выбор по установке снимает плоское наследие пресета."""
    paths.write_settings({"preset_strategies":
                          {"apex_ea": "general (ALT7).bat"}})
    root = make_flowseal(tmp_path / "zap",
                         ("general (ALT11).bat", "general (ALT7).bat"))
    use_install(monkeypatch, root)

    pm.set_strategy("apex_ea", "general (ALT7).bat", root)
    stored = paths.read_settings()["preset_strategies"]
    assert "apex_ea" not in stored, "плоская запись пережила явный выбор"
    assert stored[str(root)]["apex_ea"] == "general (ALT7).bat"

    # сброс к дефолту обязан убрать ОБА scope, иначе «дефолт» = старая ALT7
    pm.set_strategy("apex_ea", None, root)
    assert paths.read_settings()["preset_strategies"] == {}
    assert strategy_of("apex_ea", root) == "general (ALT11).bat"


def test_flat_legacy_until_explicit_choice_replaces_it_globally(isolated,
                                                               tmp_path):
    """Наследие действует «для любой установки», пока есть явный выбор.

    Явный выбор по установке снимает плоскую запись этого пресета во всех
    установках (см. set_strategy): иначе сброс к дефолту в одной версии
    «всплывал» бы старой глобальной стратегией в другой. Остальные
    установки после этого получают дефолт - он всегда существует.
    """
    paths.write_settings({"preset_strategies":
                          {"apex_ea": "general (ALT7).bat"}})
    root = make_flowseal(tmp_path / "zap",
                         ("general (ALT11).bat", "general (ALT7).bat",
                          "general (ALT9).bat"))
    other = make_flowseal(tmp_path / "other",
                          ("general (ALT11).bat", "general (ALT7).bat"))
    assert strategy_of("apex_ea", root) == "general (ALT7).bat"
    assert strategy_of("apex_ea", other) == "general (ALT7).bat"

    pm.set_strategy("apex_ea", "general (ALT9).bat", root)
    assert strategy_of("apex_ea", root) == "general (ALT9).bat"
    assert strategy_of("apex_ea", other) == "general (ALT11).bat"


def test_strategy_override_reports_scope(isolated, monkeypatch, tmp_path):
    """strategy_override читает именно эту установку, а не соседнюю."""
    a = make_flowseal(tmp_path / "a", ("general (ALT11).bat",
                                      "general (ALT2).bat"))
    b = make_flowseal(tmp_path / "b", ("general (ALT11).bat",
                                      "general (ALT3).bat"))
    pm.set_strategy("apex_ea", "general (ALT2).bat", a)
    assert pm.strategy_override("apex_ea", a) == "general (ALT2).bat"
    assert pm.strategy_override("apex_ea", b) == ""


# -- layout и списки --------------------------------------------------------

def test_available_strategies_per_layout(isolated, tmp_path):
    flow = make_flowseal(tmp_path / "flow",
                         ("general.bat", "general (ALT).bat",
                          "general (ALT2).bat", "general (ALT11).bat"))
    names = pm.available_strategies(flow)
    # порядок меню service.bat: ALT, ALT2 ... ALT13, затем остальные
    assert names == ["general (ALT).bat", "general (ALT2).bat",
                     "general (ALT11).bat", "general.bat"], names

    win = make_winws_install(tmp_path / "win")
    assert pm.available_strategies(win) == ["preset1.cmd"]


def test_default_resolves_for_winws_layout(isolated, tmp_path):
    """Дефолт flowseal (ALT11) в win-bundle подменяется на preset*.cmd.

    Иначе первое же применение падало бы «нет стратегии», и переключать
    было бы нечего.
    """
    win = make_winws_install(tmp_path / "win")
    apex = next(p for p in pm.DEFAULT_PRESETS if p.key == "apex_ea")
    assert apex.strategy == "general (ALT11).bat"

    got = next(p for p in pm.load_presets(win) if p.key == "apex_ea")
    assert got.strategy == "preset1.cmd"
    # выбор пользователя в win-bundle имеет приоритет над подстановкой
    pm.set_strategy("apex_ea", "preset1.cmd", win)
    assert strategy_of("apex_ea", win) == "preset1.cmd"


def test_custom_preset_honours_override(isolated, monkeypatch, tmp_path):
    """Пользовательские пресеты (settings.presets) тоже получают override."""
    root = make_flowseal(tmp_path / "zap", ("general (ALT11).bat",
                                           "general (ALT4).bat"))
    paths.write_settings({
        "presets": [{"key": "my", "title": "Мой",
                     "strategy": "general (ALT11).bat",
                     "description": "свой пресет"}],
        "preset_strategies": {str(root): {"my": "general (ALT4).bat"}},
    })
    use_install(monkeypatch, root)
    assert strategy_of("my", root) == "general (ALT4).bat"

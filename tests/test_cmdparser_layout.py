"""winws-layout (bol-van zapret-win-bundle) и выбор конкретной стратегии.

Регрессии:
* парсер preset*.cmd: %~dp0, вложенные "=" (sni=www.google.com),
  --wf-raw-part=@<файл> - токенизация по "=" (как в batparser) их ломала;
* переопределение стратегии пресета: set_strategy -> load_presets ->
  маркер/инференс/установка службы сходятся;
* winws-layout: apply_files не создаёт lists/ (там нет user-списков),
  _matches сравнивает только стратегию.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import cmdparser, installs, presets as pm  # noqa: E402
from core.presets import Preset, _matches, _snapshot  # noqa: E402
from core.service import ServiceState  # noqa: E402

# реальный preset из master zapret-win-bundle (урезанный, но суть та же)
PRESET_SAMPLE = (
    'start "zapret" /min "%~dp0winws.exe" ^\r\n'
    "--wf-tcp=80,443 ^\r\n"
    '--wf-raw-part=@"%~dp0windivert.filter\\part.discord.txt" ^\r\n'
    '--filter-tcp=443 --hostlist="%~dp0files\\list-youtube.txt" ^\r\n'
    "--dpi-desync-fake-tls-mod=rnd,dupsid,sni=www.google.com --new\r\n"
)


def make_winws_install(root: Path) -> Path:
    """Мини-установка win-bundle: winws.exe и preset*.cmd рядом."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "winws.exe").write_bytes(b"MZ fake")
    (root / "files").mkdir(exist_ok=True)
    (root / "files" / "list-youtube.txt").write_text("youtube.com\r\n",
                                                     encoding="ascii")
    (root / "windivert.filter").mkdir(exist_ok=True)
    (root / "windivert.filter" / "part.discord.txt").write_text("x")
    (root / "preset1.cmd").write_text(PRESET_SAMPLE, encoding="utf-8")
    return root


def test_cmdparser_expands_dp0(tmp_path):
    root = make_winws_install(tmp_path / "zapret-winws")
    args = cmdparser.parse_preset(root / "preset1.cmd")
    # %~dp0 раскрыт в абсолютные пути установки
    assert "%~dp0" not in args
    assert str(root) in args
    # winws.exe НЕ включается в аргументы (его путь добавит apply)
    assert not args.strip().startswith(str(root / "winws.exe"))


def test_cmdparser_keeps_nested_equals(tmp_path):
    """sni=www.google.com - вложенное =, которое ломала бы токенизация."""
    root = make_winws_install(tmp_path / "zapret-winws")
    args = cmdparser.parse_preset(root / "preset1.cmd")
    assert "sni=www.google.com" in args, args
    assert "--dpi-desync-fake-tls-mod=rnd,dupsid,sni=www.google.com" in args


def test_cmdparser_keeps_wf_raw_part(tmp_path):
    """--wf-raw-part=@<файл> сохраняет =@ и кавычки."""
    root = make_winws_install(tmp_path / "zapret-winws")
    args = cmdparser.parse_preset(root / "preset1.cmd")
    assert 'wf-raw-part=@"' in args, args
    assert "part.discord.txt" in args


def test_cmdparser_drops_caret(tmp_path):
    root = make_winws_install(tmp_path / "zapret-winws")
    args = cmdparser.parse_preset(root / "preset1.cmd")
    assert " ^" not in args and not args.endswith("^")


def test_detect_layout_both(tmp_path):
    # flowseal
    fl = tmp_path / "flow"
    (fl / "bin").mkdir(parents=True)
    (fl / "bin" / "winws.exe").write_bytes(b"MZ")
    (fl / "general (ALT11).bat").write_text("x")
    info = installs.detect_layout(fl)
    assert info["layout"] == installs.LAYOUT_FLOWSEAL
    assert info["strategies"] == ["general (ALT11).bat"]

    # winws
    ws = make_winws_install(tmp_path / "win")
    info = installs.detect_layout(ws)
    assert info["layout"] == installs.LAYOUT_WINWS
    assert info["strategies"] == ["preset1.cmd"]
    assert Path(info["winws"]) == ws / "winws.exe"


def test_integrity_winws_layout(tmp_path):
    """winws-layout: проверяются файлы из preset*.cmd (files/, filter/)."""
    root = make_winws_install(tmp_path / "win")
    assert installs.integrity(root) == []
    (root / "files" / "list-youtube.txt").unlink()
    (root / "windivert.filter" / "part.discord.txt").unlink()
    missing = installs.integrity(root)
    assert any("list-youtube" in m for m in missing), missing
    assert any("part.discord" in m for m in missing), missing


# -- выбор конкретной стратегии ---------------------------------------------

def test_set_strategy_override(tmp_path, monkeypatch):
    """set_strategy -> пресет получает новую стратегию, маркер согласован."""
    from core import paths
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    apex = next(p for p in pm.DEFAULT_PRESETS if p.key == "apex_ea")
    assert apex.strategy == "general (ALT11).bat"

    pm.set_strategy("apex_ea", "general (ALT7).bat")
    found = next(p for p in pm.load_presets() if p.key == "apex_ea")
    assert found.strategy == "general (ALT7).bat"
    # дефолтный пресет не тронут
    assert next(p for p in pm.load_presets()
                if p.key == "discord_youtube").strategy == \
        "general (ALT11).bat"

    # возврат к дефолту
    pm.set_strategy("apex_ea", None)
    found = next(p for p in pm.load_presets() if p.key == "apex_ea")
    assert found.strategy == "general (ALT11).bat"


def test_marker_matches_overridden_strategy(tmp_path, monkeypatch):
    """Регрессия: пресет с переопределённой стратегией распознаётся.

    Без этого подсветка «теряла» пресет после «взять ALT7».
    """
    from core import paths
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)

    root = tmp_path / "zapret"
    (root / "lists").mkdir(parents=True)
    (root / "utils").mkdir()
    (root / "service.bat").write_text("x")
    (root / "general (ALT11).bat").write_text("x")
    (root / "general (ALT7).bat").write_text("x")
    (root / "lists" / "list-exclude-user.txt").write_text(
        "# Never leave this file empty\r\ndomain.example.abc\r\n"
        "# Diaspas\r\nea.com\r\nubisoft.com\r\nubi.com\r\n")
    (root / "lists" / "ipset-all.txt").write_text("", encoding="ascii")
    (root / "utils" / "game_filter.enabled").write_text("all\r\n")

    apex = next(p for p in pm.DEFAULT_PRESETS if p.key == "apex_ea")
    pm.apply_files(root, apex)      # файлы для ALT11... с ожиданием ALT11
    # переопределение: apply_files для ALT7 уже не нужен - файл существует
    state = ServiceState(installed=True, running=True,
                         start_type="Automatic",
                         strategy="general (ALT7)", pid=1, binpath="")

    # без переопределения пресет не распознается (стратегия не совпала)
    snap = _snapshot(root, state)
    assert not _matches(apex, snap)

    # с переопределением - распознаётся
    pm.set_strategy("apex_ea", "general (ALT7).bat")
    overridden = next(p for p in pm.load_presets() if p.key == "apex_ea")
    assert _matches(overridden, snap)
    found = pm.current_preset(root, state,
                              active_key=pm.active_preset_key())
    # маркер-то ещё от старого применения; главное - инференс понимает
    assert found is not None and found.key == "apex_ea"


def test_winws_layout_apply_files_skips_lists(tmp_path, monkeypatch):
    """winws-layout: apply_files не создаёт user-списки и флаги."""
    from core import paths
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    root = make_winws_install(tmp_path / "win")
    preset = Preset(key="t", title="T", strategy="preset1.cmd")
    pm.apply_files(root, preset)
    assert not (root / "lists").exists(), "создан чужой lists/"
    assert not (root / "utils").exists(), "создан чужой utils/"


def test_winws_layout_apply_files_checks_strategy(tmp_path, monkeypatch):
    """Пресет со стратегией flowseal в winws-установке - понятная ошибка."""
    from core import paths
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    root = make_winws_install(tmp_path / "win")
    preset = Preset(key="t", title="T", strategy="general (ALT11).bat")
    with pytest.raises(FileNotFoundError, match="preset\\*\\.cmd"):
        pm.apply_files(root, preset)


def test_winws_layout_matches_by_strategy_only(tmp_path, monkeypatch):
    """В winws-layout gf/ipset/excludes не участвуют: нет их файлов."""
    from core import paths
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    root = make_winws_install(tmp_path / "win")
    state = ServiceState(installed=True, running=True,
                         start_type="Automatic",
                         strategy="preset1.cmd", pid=1, binpath="")
    snap = _snapshot(root, state)
    assert snap.layout == installs.LAYOUT_WINWS
    # пресет с игровыми фильтрами и исключениями - всё равно совпадает,
    # потому что winws-установка их физически не поддерживает
    gaming = Preset(key="g", title="G", strategy="preset1.cmd",
                    game_filter="all", ipset="any",
                    exclude_domains=("ea.com",))
    assert _matches(gaming, snap)
    other = Preset(key="o", title="O", strategy="preset2.cmd")
    assert not _matches(other, snap)

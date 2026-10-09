"""Золотой тест batparser: вывод должен байт-в-байт совпадать с аргументами,
которые service.bat записал в реестр рабочей службы (эталон снят с машины,
где служба ставилась штатным менеджером).

Эталон лежит рядом с этим файлом; при отсутствии (перенос репозитория)
тест пропускается, а не падает - он защищает от регрессии парсера, а не
требует конкретного окружения.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.batparser import BatParseError, parse_strategy  # noqa: E402

HERE = Path(__file__).resolve().parent
GOLDEN = HERE / "golden_alt11.args"
# Стратегия, из которой снят эталон; живой файл нужен только если есть и он,
# и эталон - иначе сравнивать не с чем.
BAT = Path(r"C:\zapret-discord-youtube-1.10.2\general (ALT11).bat")


@pytest.mark.skipif(not GOLDEN.is_file() or not BAT.is_file(),
                    reason="нет эталона или файла стратегии")
def test_matches_service_bat_output():
    expected = GOLDEN.read_text(encoding="ascii")
    assert parse_strategy(BAT) == expected


def test_mergeargs_join(tmp_path):
    """--wf-tcp=80,443 даёт «--wf-tcp 80,443»: первое значение через пробел,
    последующие через запятую - как в реестровом ImagePath."""
    bat = tmp_path / "general (X).bat"
    bat.write_text(
        '@echo off\r\n'
        'start "zapret" /min "%BIN%winws.exe" --wf-tcp=80,443,443 '
        '--hostlist="%LISTS%list-general.txt" --new ^\r\n'
        '--filter-tcp=443 --dpi-desync=fake,multisplit\r\n',
        encoding="utf-8")
    got = parse_strategy(tmp_path / "general (X).bat")
    assert " --wf-tcp 80,443,443 " in got
    assert " --hostlist " in got
    assert '\\"' in got            # путь в кавычках экранирован
    assert " --dpi-desync fake,multisplit" in got
    assert got.count("winws.exe") == 0   # префикс отрезан


def test_game_filter_ports(tmp_path):
    """%GameFilterTCP% подставляется, «12» service.bat ставит при выключенном
    фильтре - диапазон не должен появиться в аргументах."""
    bat = tmp_path / "general (X).bat"
    bat.write_text(
        'start "s" /min "%BIN%winws.exe" --wf-tcp=80,%GameFilterTCP%\r\n',
        encoding="utf-8")
    assert "--wf-tcp 80,1024-65535" in parse_strategy(bat)
    assert "--wf-tcp 80,12" in parse_strategy(bat, game_filter_tcp="12")


def test_missing_winws(tmp_path):
    bat = tmp_path / "general (X).bat"
    bat.write_text("@echo off\r\nrem nothing\r\n", encoding="utf-8")
    with pytest.raises(BatParseError):
        parse_strategy(bat)


def test_unexpanded_variable_rejected(tmp_path):
    bat = tmp_path / "general (X).bat"
    bat.write_text('start /min "%BIN%winws.exe" --hostlist=%UNDEFINED%\r\n',
                   encoding="utf-8")
    with pytest.raises(BatParseError):
        parse_strategy(bat)

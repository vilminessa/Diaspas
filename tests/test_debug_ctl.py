"""Debug-консоль проекта: токены меню, опасные пункты, фильтруемые процессы.

Реальные команды не запускаются - подменены `_run` и контекст: проверяется
решение консоли (что выполнить, что спросить, чего не трогать).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import service  # noqa: E402
from core.service import ServiceState  # noqa: E402
from tools import debug_ctl as dc  # noqa: E402

RUNNING = ServiceState(installed=True, running=True, start_type="Automatic",
                       strategy="general (ALT11)", pid=1, binpath="")
STOPPED = ServiceState(installed=True, running=False, start_type="Automatic",
                       strategy="general (ALT11)", pid=0, binpath="")
ABSENT = ServiceState(installed=False, running=False, start_type="",
                      strategy="", pid=0, binpath="")


@pytest.fixture()
def quiet(monkeypatch):
    """Консоль без git/службы и без запуска subprocess."""
    calls: list[list[str]] = []

    def fake_run(cmd, args=None):
        calls.append([str(part) for part in cmd] + list(args or []))
        return 0

    monkeypatch.setattr(dc, "_run", fake_run)
    monkeypatch.setattr(dc, "_capture", lambda cmd: "")
    monkeypatch.setattr(dc, "context", lambda: {
        "branch": "main", "dirty": 0, "dir": None, "installed": False,
        "running": False, "strategy": "", "mode": "manual", "cooldown": 0,
        "preset": None, "version": "0.1.1", "label": "unreleased"})
    return calls


def test_menu_lists_every_item(quiet):
    """Пункты меню видны: без токена у пункта debug.bat ничего бы не выполнил."""
    dc.print_menu()
    tokens = [op["keys"][0] for op in dc.OPS]
    assert len(tokens) == len(set(tokens)), "дубли токенов"
    for op in dc.OPS:
        assert op["title"]
        assert callable(op["fn"])


def test_number_and_alias_are_same_item(quiet):
    assert dc.find("5") is dc.find("tests")
    assert dc.find("B") is dc.find("set")
    assert dc.find("0") is dc.find("exit")
    assert dc.find("zz") is None


def test_unknown_token_exits_with_code(quiet):
    assert dc.main(["zz"]) == 2
    assert quiet == []


def test_scripted_dangerous_item_refused_without_yes(quiet):
    """Опасный пункт в быстром режиме не выполняется молча: нужен --yes."""
    assert dc.main(["I", "--scripted"]) == 1   # переустановка релиза
    assert quiet == []


def test_scripted_dangerous_item_with_yes_runs(quiet):
    assert dc.main(["I", "--yes", "--scripted"]) == 0
    assert quiet == [[dc.PY, "tests/run_e2e_install.py"]]


def test_safe_items_run_without_confirmation(quiet):
    assert dc.main(["5", "--scripted"]) == 0
    assert quiet == [[dc.PY, "-m", "pytest", "tests/", "-q"]]


def test_scripted_mode_is_not_leaked_to_children(quiet):
    """Режим передаётся аргументом: env под пунктом CI попал бы в pytest."""
    assert dc.main(["5"]) == 0     # меню-режим: без --scripted
    assert dc.SCRIPTED is False
    assert quiet == [[dc.PY, "-m", "pytest", "tests/", "-q"]]


def test_strategy_set_needs_argument_when_scripted(monkeypatch, quiet):
    """Опасный пункт в быстром режиме: --yes подтверждает, имя всё равно нужно."""
    monkeypatch.setattr(dc.paths, "zapret_dir_from_settings",
                        lambda: Path("C:/zapret"))
    assert dc.main(["B", "--yes", "--scripted"]) == 2
    assert quiet == []
    assert dc.main(["B", "--yes", "--scripted", "general (ALT7).bat"]) == 0
    assert quiet[-1] == [dc.PY, "tests/run_strategy.py", "general (ALT7).bat"]


def test_ci_steps_in_ci_order(quiet):
    """Порядок шагов как в workflows: сверка бандла -> тесты -> линт -> конвейер."""
    assert dc.main(["8", "--scripted"]) == 0
    steps = [" ".join(step) for step in quiet]
    assert len(steps) == 4
    assert "build_ui.py --check" in steps[0]
    assert "pytest tests/" in steps[1]
    assert "pyflakes" in steps[2]
    assert "check_workflows.py" in steps[3]


def test_ab_group_argument(monkeypatch, quiet):
    """A/B подтверждается в меню-режиме и принимает группу аргументом."""
    monkeypatch.setattr(dc, "_ask", lambda text: "y")
    assert dc.main(["F", "ea"]) == 0
    assert quiet == [[dc.PY, "tests/run_ab.py", "ea"]]


def test_tray_launch_starts_hidden(quiet):
    """Пункт трея запускает GUI скрытым (--min), окно - из иконки."""
    assert dc.main(["T"]) == 0
    assert quiet[-1] == [dc.PY, "main.py", "--min"]


def test_tray_launch_can_target_tk(quiet):
    assert dc.main(["T", "--tk"]) == 0
    assert quiet[-1] == [dc.PY, "main.py", "--min", "--tk"]


def test_tray_launch_refused_in_scripted_mode(quiet):
    """Быстрый режим не запускает GUI: он блокирует консоль до выхода из трея."""
    assert dc.main(["T", "--scripted"]) == 2
    assert quiet == []


def test_ab_declined_at_the_prompt(monkeypatch, quiet):
    monkeypatch.setattr(dc, "_ask", lambda text: "n")
    assert dc.main(["F", "ea"]) == 1
    assert quiet == []


def test_service_report_encodes_state(monkeypatch, quiet, tmp_path):
    """Код пункта отражает состояние: скрипт должен отличать «работает» от «нет»."""
    monkeypatch.setattr(dc.paths, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(dc.paths, "zapret_dir_from_settings",
                        lambda: Path("C:/zapret"))
    monkeypatch.setattr(dc.service, "state", lambda log=None: RUNNING)
    assert dc.op_service([]) == 0
    monkeypatch.setattr(dc.service, "state", lambda log=None: STOPPED)
    assert dc.op_service([]) == 1
    monkeypatch.setattr(dc.service, "state", lambda log=None: ABSENT)
    assert dc.op_service([]) == 2
    assert quiet == []   # пункт читает состояние напрямую, без subprocess


def test_files_check_skipped_without_service(monkeypatch, quiet):
    monkeypatch.setattr(dc.service, "state", lambda log=None: ABSENT)
    assert dc.op_files_check([]) == 2
    assert quiet == []


def test_kill_targets_never_touches_winws():
    processes = [
        {"pid": 10, "name": "python.exe", "commandline": "python main.py"},
        {"pid": 11, "name": "Diaspas.exe", "commandline": '"dist/Diaspas.exe"'},
        {"pid": 12, "name": "winws.exe", "commandline": '"bin/winws.exe" --wf-tcp'},
        {"pid": 13, "name": "python.exe", "commandline": "python -m pytest"},
        {"pid": 14, "name": "powershell.exe", "commandline": "Get-CimInstance"},
    ]
    assert dc.kill_targets(processes) == [10, 11]


def test_context_reports_strategy_mode(monkeypatch):
    """Контекст меню обязан показывать режим и автопаузу: иначе авто выглядит магией."""
    def fake_capture(cmd: list[str]) -> str:
        if "rev-parse" in cmd:
            return "main"
        if "status" in cmd:
            return " M core/presets.py\n M README.md"
        return ""

    monkeypatch.setattr(dc, "_capture", fake_capture)
    monkeypatch.setattr(dc.paths, "zapret_dir_from_settings", lambda: None)
    monkeypatch.setattr(dc.autoswitch, "cooldown_left", lambda: 600)
    ctx = dc.context()
    assert ctx["branch"] == "main"
    assert ctx["dirty"] == 2
    assert ctx["mode"] in ("manual", "auto")
    assert ctx["cooldown"] == 600
    assert ctx["dir"] is None
    # версия и подпись сборки: без них debug-консоль не говорит, что соберётся
    assert ctx["version"] == dc.app_version.__version__
    assert ctx["label"] == dc.app_version.label()


def test_version_item_is_read_only(quiet):
    assert dc.main(["V"]) == 0
    assert quiet == []   # пункт печатает версию напрямую, без subprocess


def test_version_stamp_needs_tag_when_scripted(quiet):
    assert dc.main(["U", "--yes", "--scripted"]) == 2
    assert quiet == []
    assert dc.main(["U", "--yes", "--scripted", "v0.1.2"]) == 0
    assert quiet[-1] == [dc.PY, "tools/stamp_version.py", "v0.1.2"]

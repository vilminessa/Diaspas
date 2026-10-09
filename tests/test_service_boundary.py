"""Граница установки службы: экранированные кавычки batparser'а должны
доходить до New-Service плоскими.

Регрессия: без нормализации winws получал пути с ``\\"`` , не мог открыть
списки и служба падала с WIN32_EXIT_CODE 1067.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import batparser, service  # noqa: E402


def test_install_strategy_normalizes_quotes(monkeypatch):
    """install_strategy передаёт в помощник binpath без \\"."""
    captured = {}

    def fake_run_action(action, payload=None, **kwargs):
        captured["action"] = action
        captured["payload"] = payload or {}
        return {"ok": True, "state": {}}

    monkeypatch.setattr(service, "run_action", fake_run_action)

    raw = r'"C:\zapret\bin\winws.exe" --hostlist \"C:\zapret\lists\a.txt\"'
    service.install_strategy(raw, "general (ALT11)")

    assert captured["action"] == "install"
    sent = captured["payload"]["binpath"]
    assert '\\"' not in sent, f"не нормализовано: {sent}"
    assert '--hostlist "C:\\zapret\\lists\\a.txt"' in sent
    # путь к самому exe в кавычках остаётся
    assert sent.startswith('"C:\\zapret\\bin\\winws.exe"')


def test_batparser_output_is_escaped_by_design():
    """Контракт: парсер отдаёт \\", а нормализация - забота установки.

    Если кто-то «упростит» парсер до плоских кавычек, этот тест напомнит,
    что эталон service.bat (golden_alt11.args) содержит именно \\"
    """
    golden = Path(__file__).resolve().parent / "golden_alt11.args"
    if golden.is_file():
        text = golden.read_text(encoding="ascii")
        assert '\\"' in text, "эталон потерял экранирование"


def test_winws_command_shape():
    """winws_command: exe в кавычках + аргументы, без \\ в начале."""
    from pathlib import Path as P
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = P(tmp)
        (root / "general (X).bat").write_text(
            'start /min "%BIN%winws.exe" --wf-tcp=80,443\r\n', encoding="utf-8")
        cmd = batparser.winws_command(root, "general (X).bat")
        assert cmd.startswith('"')
        assert cmd.endswith("--wf-tcp 80,443")
        assert "winws.exe" in cmd.split('"')[1]

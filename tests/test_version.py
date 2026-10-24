"""Версия приложения: формат, проставление из тега, путь к VersionInfo exe.

Грань регрессии: workflow обещает «версия берётся из тега», но без
tools/stamp_version.py exe и интерфейс выходили вообще без версии.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import version  # noqa: E402
from tools import stamp_version as sv  # noqa: E402

SPEC = Path(__file__).resolve().parents[1] / "diaspas.spec"

SAMPLE_TEXT = (
    '"""Версия."""\n\n__version__ = "0.1.1"\nBUILD_LABEL = ""\n\n'
    'def label():\n    return BUILD_LABEL or "unreleased"\n'
)
# CRLF как в реальном файле репозитория (git autocrlf): замена обязана
# сохранить переводы строк, а не переписать файл целиком.
SAMPLE = SAMPLE_TEXT.replace("\n", "\r\n").encode("utf-8")


def test_version_format_is_numeric():
    assert version.__version__.replace(".", "").isdigit()
    parts = version.__version__.split(".")
    assert 3 <= len(parts) <= 4, "Windows VersionInfo принимает не больше 4"


def test_unreleased_until_tagged():
    """Локальная сборка и dispatch без тега: BUILD_LABEL пуст, виден «unreleased»."""
    assert version.BUILD_LABEL == ""
    assert version.label() == "unreleased"
    assert version.is_beta() is False


def test_stamp_parses_both_bands():
    assert sv.stamp("v0.1.2") == ("0.1.2", "v0.1.2")
    assert sv.stamp("b0.1.2") == ("0.1.2", "b0.1.2")
    assert sv.stamp("v1.2.3.4") == ("1.2.3.4", "v1.2.3.4")


@pytest.mark.parametrize("bad", ["", "1.0.0", "x0.1.2", "v0.1", "v0.1.2.3.4",
                                 "v1.0.0-rc1"])
def test_stamp_rejects_wrong_tag(bad):
    with pytest.raises(ValueError):
        sv.stamp(bad)


def test_stamp_writes_both_values(tmp_path, monkeypatch):
    """Оба значения вместе: числовая версия для spec, подпись для интерфейса."""
    target = tmp_path / "version.py"
    target.write_bytes(SAMPLE)
    monkeypatch.setattr(sv, "ROOT", tmp_path)

    assert sv.main(["prog", "b0.1.4"]) == 0
    text = target.read_bytes().decode("utf-8")
    assert '__version__ = "0.1.4"' in text
    assert 'BUILD_LABEL = "b0.1.4"' in text


def test_stamp_keeps_line_endings_and_is_idempotent(tmp_path, monkeypatch):
    """Байтовая замена: CRLF и кодировка не меняются, повторный запуск тихий."""
    target = tmp_path / "version.py"
    target.write_bytes(SAMPLE)
    monkeypatch.setattr(sv, "ROOT", tmp_path)

    assert sv.main(["prog", "v0.1.4"]) == 0
    first = target.read_bytes()
    assert b"\r\n" in first
    assert first.count(b"\r\n") == SAMPLE.count(b"\r\n")
    assert b"\n" not in first.replace(b"\r\n", b""), "не должно появиться LF-строк"

    assert sv.main(["prog", "v0.1.4"]) == 0
    assert target.read_bytes() == first, "повторный запуск должен быть идемпотентен"


def test_stamp_reports_missing_field(tmp_path, monkeypatch):
    """Файл без полей - ненулевой код: сборка не должна уехать без версии."""
    target = tmp_path / "version.py"
    target.write_bytes("# версия не описана\n".encode("utf-8"))
    monkeypatch.setattr(sv, "ROOT", tmp_path)
    assert sv.main(["prog", "v0.1.4"]) == 1


def test_stamp_without_argument_prints_help():
    assert sv.main(["prog"]) == 2


def test_spec_wires_version_info():
    """Spec обязан генерировать VersionInfo из version.py: иначе Properties exe пусты."""
    text = SPEC.read_text(encoding="utf-8")
    assert "from version import" in text
    assert "VSVersionInfo" in text
    assert "filevers=" in text
    assert "version=_version_file" in text
    assert "FileVersion" in text and "ProductVersion" in text

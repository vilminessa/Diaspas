"""Почему integrity не видит недостающий .bin? Разбор regex/кавычек."""
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import batparser, installs  # noqa: E402


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    (tmp / "bin").mkdir()
    (tmp / "bin" / "winws.exe").write_bytes(b"MZ")
    (tmp / "lists").mkdir()
    (tmp / "service.bat").write_text("@echo off", encoding="ascii")
    (tmp / "general (ALT11).bat").write_text(
        'start /min "%BIN%winws.exe" --dpi-desync-fake-quic='
        '"%BIN%quic_initial_www_google_com.bin" '
        '--hostlist="%LISTS%list-general.txt"\r\n', encoding="utf-8")

    args = batparser.parse_strategy(tmp / "general (ALT11).bat")
    print("args:", args)
    found = re.findall(r'"([^"]+)"', args)
    print("regex-совпадения:", found)
    for ref in found:
        print("   path:", repr(ref), "exists:",
              (tmp / Path(ref).name).is_file())

    (tmp / "bin" / "quic_initial_www_google_com.bin").unlink()
    print("integrity после удаления quic:", installs.integrity(tmp))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

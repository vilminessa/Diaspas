"""Сравнить текущий ImagePath службы с рабочим эталоном."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                        r"System\CurrentControlSet\Services\zapret") as key:
        current = str(winreg.QueryValueEx(key, "ImagePath")[0])

    print("ТЕКУЩИЙ ImagePath (после Diaspas):")
    print(repr(current[:260]))
    print()
    print("содержит подряд " + r"\"" + " ?", r"\"" in current)
    print()

    # Что получилось бы из эталонного args_alt11.txt
    golden = Path(__file__).resolve().parent / "golden_alt11.args"
    if golden.is_file():
        args = golden.read_text(encoding="ascii")
        would = '"C:\\zapret-discord-youtube-1.10.2\\bin\\winws.exe"' + args
        print("ЭТАЛОН (что отдал бы батпарсер):")
        print(repr(would[:260]))
        print()
        print("разница: текущий содержит " + repr(current[:80]))
        print("         эталон  содержит " + repr(would[:80]))


if __name__ == "__main__":
    main()

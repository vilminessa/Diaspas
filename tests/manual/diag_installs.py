"""Диагностика: где реально лежит активная установка zapret."""
import sys
import winreg
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _guard import require_live  # noqa: E402

require_live(__doc__ or 'эти прогоны')


from core import installs, paths  # noqa: E402


def main() -> int:
    base = paths.app_dir()
    print("app_dir:", base, "exists:", base.exists())
    zapret = base / "zapret"
    print("app_dir/zapret:", zapret.exists())
    if zapret.exists():
        for kid in zapret.iterdir():
            print("  ", kid.name, "is_dir=", kid.is_dir(),
                  "valid=", paths.is_zapret_dir(kid) if kid.is_dir() else "-")

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"System\CurrentControlSet\Services\zapret") as k:
            binpath = str(winreg.QueryValueEx(k, "ImagePath")[0])
    except OSError:
        print("служба не установлена")
        return 0
    root = Path(binpath.split('"')[1]).parent.parent
    print("служба смотрит на:", root, "exists:", root.exists(),
          "valid:", paths.is_zapret_dir(root))

    data = installs.ensure_registry()
    print("реестр: items=%d active=%s" % (len(data["items"]), data["active"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

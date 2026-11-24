"""Разбор7 FAIL полного E2E: где неточность скрипта, а где среда."""
import re
import sys
import winreg
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _guard import require_live  # noqa: E402

require_live(__doc__ or 'эти прогоны')


from core import batparser, paths  # noqa: E402


def main() -> int:
    print("=== 1. автопоиск: какие кандидаты и есть ли они ===")
    for cand in paths._CANDIDATE_DIRS:
        print(f"  {cand}: существует={Path(cand).exists()}")
    print(f"  автопоиск -> {paths.zapret_install_dir()}")

    print("\n=== 2. batparser vs ImagePath (discord_youtube: порты 12) ===")
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                        r"System\CurrentControlSet\Services\zapret") as k:
        binpath = str(winreg.QueryValueEx(k, "ImagePath")[0])
    root = Path(binpath.split('"')[1]).parent.parent

    norm = lambda s: re.sub(r"\s+", " ", s.replace('\\"', '"')).strip()
    for tcp, udp, label in (("1024-65535", "1024-65535", "дефолт (E2E-скрипт)"),
                            ("12", "12", "discord_youtube (факт)")):
        regen = batparser.winws_command(root, "general (ALT11).bat", tcp, udp)
        same = norm(regen) == norm(binpath)
        print(f"  {label}: совпадает={same}")
        if not same:
            a, b = norm(regen), norm(binpath)
            for i, (x, y) in enumerate(zip(a, b)):
                if x != y:
                    print(f"    regen: ...{a[max(0, i-30):i+50]}")
                    print(f"    Image: ...{b[max(0, i-30):i+50]}")
                    break
            else:
                print(f"    длины: {len(a)} vs {len(b)}")

    print("\n=== 3. задача DiaspasService ===")
    import subprocess
    out = subprocess.run(["schtasks", "/Query", "/TN", "DiaspasService"],
                         capture_output=True, timeout=25)
    text = (out.stdout or b"").decode("cp866", "replace")
    print("  найдена:", out.returncode == 0)
    for line in text.splitlines()[:4]:
        if line.strip():
            print("   ", line.strip()[:100])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

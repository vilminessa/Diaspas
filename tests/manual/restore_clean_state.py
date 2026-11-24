"""Приборка после E2E: чистая установка релиза + пресет Ubisoft.

E2E оставил тестовые копии (1.9.9, 1.10.1) и пресет Discord/YouTube.
Возвращаем то, что пользователь использовал до тестов.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _guard import require_live  # noqa: E402

require_live(__doc__ or 'эти прогоны')


from core import installs, presets as pm  # noqa: E402
from ui.webapp import App  # noqa: E402


def main() -> int:
    app = App()

    print("===1. установка релиза 1.10.3 ===")
    res = app.install_release()
    print("install ok:", res.get("ok"), res.get("error", ""))
    if not res.get("ok"):
        return 1
    time.sleep(1)

    print("===2. пресет Ubisoft ===")
    ubisoft = next(p for p in pm.load_presets() if p.key == "ubisoft")
    res = pm.apply(ubisoft, app.zapret_root,
                   log=lambda l, m: print(f"  [{l}] {m}"))
    time.sleep(2)
    st = app.state()
    print("пресет =", st["preset_title"], "| running =", st["running"],
          "| pid =", st["pid"])
    if not st["running"]:
        return 1

    print("===3. удаление тестовых копий ===")
    for inst in installs.items():
        if str(inst.path) == str(installs.active_path()):
            continue
        print(f"  удаление {inst.version} ({inst.source})…")
        r = app.remove_version(inst.path)
        print("   ", "ok" if r.get("ok") else r.get("error"))

    print("=== итог ===")
    st = app.state()
    for i in installs.items():
        print(f"  {i.version} {i.source}"
              f"{' (активная)' if str(i.path) == st['zapret_dir'] else ''}")
    print("пресет:", st["preset_title"], "| running:", st["running"],
          "| pid:", st["pid"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

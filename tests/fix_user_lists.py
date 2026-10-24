"""Проверка фикса: пользовательские списки + служба + пробы."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import paths, presets as pm, probes, service  # noqa: E402


def main() -> int:
    root = paths.zapret_dir_from_settings()
    if root is None:
        print("папка zapret не найдена - укажите её в GUI или в settings.json")
        return 1
    print(f"папка: {root}")

    print("=== пользовательские списки ===")
    pm.ensure_user_lists(root)
    for name in ("ipset-exclude-user.txt", "list-general-user.txt",
                 "list-exclude-user.txt"):
        p = root / "lists" / name
        state = f"{p.stat().st_size} байт" if p.is_file() else "НЕТ"
        print(f"  {name}: {state}")

    print("=== применение пресета apex_ea ===")
    apex = next(p for p in pm.load_presets() if p.key == "apex_ea")
    res = pm.apply(apex, root, log=lambda lvl, msg: print(f"  [{lvl}] {msg}"))
    print("apply ok =", res.get("ok"), res.get("error", ""))
    if not res.get("ok"):
        return 1

    time.sleep(3)
    st = service.state()
    print(f"служба: running={st.running} pid={st.pid} strategy={st.strategy}")
    if not st.running:
        print("FAIL: служба не работает")
        return 2

    rep = probes.probe_all(timeout=6)
    print(f"пробы: {rep['state']} {rep['count']}/{rep['total']}")
    if rep["count"] < rep["total"]:
        print("сбойные:", probes.failed_targets(rep))
        return 3
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

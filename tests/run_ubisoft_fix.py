"""Интеграционный тест: применить пресет Ubisoft и проверить починку."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import paths, presets as pm, probes, service  # noqa: E402


def log(level: str, text: str) -> None:
    print(f"[{level}] {text}", flush=True)


def show(title: str, rep: dict) -> None:
    failed = [k for k, v in (rep.get("targets") or {}).items() if not v["ok"]]
    print(f"{title}: {rep['state']} {rep['count']}/{rep['total']} "
          f"fail={failed or 'нет'}")


if __name__ == "__main__":
    root = paths.zapret_dir_from_settings()
    if root is None:
        print("папка zapret не найдена - укажите её в GUI или в settings.json")
        raise SystemExit(1)
    print(f"папка: {root}")

    show("ДО  ubisoft", probes.probe_group("ubisoft"))
    show("ДО  ea     ", probes.probe_group("ea"))

    ubisoft = next(p for p in pm.load_presets() if p.key == "ubisoft")
    print("\n=== применение пресета Ubisoft ===")
    res = pm.apply(ubisoft, root, log=log)
    print("apply ok =", res.get("ok"), res.get("error", ""))
    time.sleep(2)

    st = service.state()
    print(f"служба: running={st.running} strategy={st.strategy}")

    print()
    show("ПОСЛЕ ubisoft", probes.probe_group("ubisoft"))
    show("ПОСЛЕ ea     ", probes.probe_group("ea"))
    show("ПОСЛЕ discord", probes.probe_group("discord"))

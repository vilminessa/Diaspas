"""Интеграционный тест: применить пресет Ubisoft и проверить починку."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import presets as pm, probes, service  # noqa: E402

ROOT = Path(r"C:\zapret-discord-youtube-1.10.2")


def log(level: str, text: str) -> None:
    print(f"[{level}] {text}", flush=True)


def show(title: str, rep: dict) -> None:
    failed = [k for k, v in (rep.get("targets") or {}).items() if not v["ok"]]
    print(f"{title}: {rep['state']} {rep['count']}/{rep['total']} "
          f"fail={failed or 'нет'}")


if __name__ == "__main__":
    show("ДО  ubisoft", probes.probe_group("ubisoft"))
    show("ДО  ea     ", probes.probe_group("ea"))

    ubisoft = next(p for p in pm.load_presets() if p.key == "ubisoft")
    print("\n=== применение пресета Ubisoft ===")
    res = pm.apply(ubisoft, ROOT, log=log)
    print("apply ok =", res.get("ok"), res.get("error", ""))
    time.sleep(2)

    st = service.state()
    print(f"служба: running={st.running} strategy={st.strategy}")

    print()
    show("ПОСЛЕ ubisoft", probes.probe_group("ubisoft"))
    show("ПОСЛЕ ea     ", probes.probe_group("ea"))
    show("ПОСЛЕ discord", probes.probe_group("discord"))

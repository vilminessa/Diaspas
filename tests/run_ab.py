"""Интеграционный A/B-тест по группе ubisoft (ручной прогон, нужен UAC)."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import abtest, service  # noqa: E402

GROUP = sys.argv[1] if len(sys.argv) > 1 else "ubisoft"


def log(level: str, text: str) -> None:
    print(f"[{level}] {text}", flush=True)


if __name__ == "__main__":
    print("ДО:", service.state().__dict__)
    started = time.time()
    result = abtest.run(GROUP, log=log, wait=10.0)
    print(f"\n=== итог за {time.time() - started:.1f}s ===")
    if not result.get("ok"):
        print("ОШИБКА:", result.get("error"))
        raise SystemExit(1)
    print("вердикт:", result.get("verdict"))
    print("подсказка:", result.get("hint"))
    with_rep = result.get("with") or {}
    without = result.get("without")
    print(f"с обходом:  {with_rep.get('count')}/{with_rep.get('total')}")
    if without:
        print(f"без обхода: {without.get('count')}/{without.get('total')}")
    print("ПОСЛЕ:", service.state().__dict__)

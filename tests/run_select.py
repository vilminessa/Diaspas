"""Тест автоподбора на подмножестве стратегий (ручной прогон, нужен UAC)."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import autoselect, service  # noqa: E402

ROOT = Path(r"C:\zapret-discord-youtube-1.10.2")
# Три стратегии: известный рабочий кандидат и два для сравнения
SUBSET = ["general (ALT11).bat", "general (ALT7).bat", "general (ALT12).bat"]


def log(level: str, text: str) -> None:
    print(f"[{level}] {text}", flush=True)


def on_progress(data: dict) -> None:
    done = data.get("done")
    total = data.get("total")
    best = data.get("best")
    print(f"  прогресс: {done}/{total} лучшая={best}", flush=True)


if __name__ == "__main__":
    print("служба до:", service.state().running)
    started = time.time()
    result = autoselect.run(ROOT, strategies=SUBSET, log=log,
                            on_progress=on_progress, wait=300)
    print(f"\n=== итог за {time.time() - started:.1f}s ===")
    if result is None:
        print("ОШИБКА: нет прав (UAC отклонён)")
        raise SystemExit(1)
    if not result.get("ok"):
        print("ОШИБКА:", result.get("error"))
        raise SystemExit(1)
    print("лучшая:", result.get("best"))
    print("строки:")
    for row in autoselect.summarize(result, ROOT):
        star = "★" if row["best"] else " "
        print(f" {star} {row['label']:<10} {row['state']:<8} "
              f"{row['count']}/{row['total']} {row['groups']}")
    print("служба после:", service.state().running,
      "(helper должен был погасить winws)")

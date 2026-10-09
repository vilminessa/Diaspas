"""Прогон проб всех групп - ручная проверка, не юнит-тест (нужна сеть)."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import probes  # noqa: E402

if __name__ == "__main__":
    started = time.time()
    report = probes.probe_all(timeout=6.0)
    print(f"state={report['state']} {report['count']}/{report['total']} "
          f"за {time.time() - started:.1f}s")
    print(probes.probe_line(report))
    failed = probes.failed_targets(report)
    print("сбойные:", failed or "нет")

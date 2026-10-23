"""Разовая приборка: снять защиту откатной версии и удалить тестовую копию.

1.9.9 - тестовая копия E2E, ставшая «откатной» при переключениях.
Откат на неё бессмысленен (это клон 1.10.3), поэтому удаляем вручную:
forget() снимает запись и ссылку rollback, затем remove() убирает папку.
"""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _guard import require_live  # noqa: E402

require_live(__doc__ or 'эти прогоны')


from core import installs  # noqa: E402


def main() -> int:
    target = None
    for inst in installs.items():
        if inst.version == "1.9.9":
            target = inst
            break
    if target is None:
        print("1.9.9 в реестре нет - прибираться нечего")
        return 0
    print("снимаю защиту и удаляю:", target.path)
    installs.forget(target.path)            # запись + ссылка rollback
    if target.dir.is_dir():
        shutil.rmtree(target.dir, ignore_errors=True)
        print("папка удалена:", not target.dir.exists())
    print("осталось версий:", len(installs.items()),
          "-", ", ".join(i.version for i in installs.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

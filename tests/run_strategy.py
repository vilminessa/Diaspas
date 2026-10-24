"""Ручной прогон смены стратегии (нужна установка zapret; часть — UAC).

    python tests/run_strategy.py --list                 # список стратегий и текущая
    python tests/run_strategy.py "general (ALT7).bat"   # поставить стратегию вручную
    python tests/run_strategy.py --auto                 # автоцикл: пробы → подбор → применение
    python tests/run_strategy.py --auto --force         # автоцикл без cooldown

Установка берётся из настроек Diaspas (или из стандартных папок), как в GUI.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import autoswitch, paths, probes, service, strategies as sm  # noqa: E402


def log(level: str, text: str) -> None:
    print(f"[{level}] {text}", flush=True)


def show_state(title: str) -> None:
    st = service.state()
    print(f"{title}: installed={st.installed} running={st.running} "
          f"strategy={st.strategy or '—'} pid={st.pid}")


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    root = paths.zapret_dir_from_settings()
    if root is None:
        print("папка zapret не найдена - укажите её в GUI или в settings.json")
        return 1

    st = service.state()
    infos = sm.list_strategies(root, st.strategy)
    print(f"папка: {root}")
    show_state("служба до")
    print(f"режим: {sm.mode()}  сохранённый выбор: {sm.saved(root) or 'нет'}")

    if "--auto" in sys.argv:
        if sm.mode() != sm.AUTO:
            sm.set_mode(sm.AUTO)
            print("режим включён: auto")
        res = autoswitch.run(
            root, force="--force" in sys.argv, log=log,
            on_progress=lambda d: print(
                f"  прогресс: {d.get('done')}/{d.get('total')} "
                f"лучшая={d.get('best')}", flush=True))
        print(f"\n=== итог: reason={res.get('reason')} "
              f"applied={res.get('applied')} best={res.get('best')} ===")
        report = res.get("probes") or probes.probe_all(timeout=6.0)
        print("пробы:", probes.probe_line(report))
        show_state("служба после")
        return 0 if res.get("ok") else 1

    if "--list" in sys.argv or not args:
        for info in infos:
            print(("  ★ " if info.active else "    ")
                  + f"{info.label:<24} {info.file}")
        return 0

    target = args[0]
    res = sm.apply(root, target, log=log)
    print("apply ok =", res.get("ok"), res.get("error") or "")
    if not res.get("ok"):
        return 1
    time.sleep(2)
    show_state("служба после")
    print("пробы:", probes.probe_line(probes.probe_all(timeout=6.0)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

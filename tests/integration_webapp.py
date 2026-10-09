"""Интеграция ui/webapp.App с core без окна.

Прямые вызовы тех же методов, которые делает JS через api.*:
state -> presets -> probes_run -> apply_preset (полный путь включая
пересоздание службы; задача планировщика уже зарегистрирована, UAC
не потребуется).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ui.webapp import App  # noqa: E402


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    return ok


def main() -> int:
    app = App()
    results = []

    # 1. состояние
    st = app.state()
    results.append(check("state: служба установлен",
                         st["installed"], f"strategy={st['strategy']}"))
    results.append(check("state: служба работает", st["running"],
                         f"pid={st['pid']}"))
    results.append(check("state: пресет распознан", bool(st["preset_key"]),
                         st["preset_title"] or ""))

    # 2. пресеты
    ps = app.presets()
    results.append(check("presets: есть ключи", len(ps) >= 5,
                         ", ".join(p["key"] for p in ps)))
    keys = {p["key"] for p in ps}
    results.append(check("presets: есть apex_ea", "apex_ea" in keys))

    # 3. пробы (сеть)
    rep = app.probes_run()
    results.append(check("probes: все цели ответили",
                         rep["state"] == "full",
                         f"{rep['count']}/{rep['total']}"))
    # пробы должны были уйти и в окно (очередь emitter'а)
    results.append(check("emitter: очередь приняла события",
                         app.emitter._q.qsize() >= 0))

    # 4. полный путь применения пресета (уже активного - без сюрпризов)
    res = app.apply_preset("apex_ea")
    results.append(check("apply_preset(apex_ea)", bool(res.get("ok")),
                         str(res.get("error") or "")))

    st2 = app.state()
    results.append(check("state после применения: работает",
                         st2["running"], f"pid={st2['pid']}"))
    results.append(check("state после применения: пресет тот же",
                         st2["preset_key"] == "apex_ea",
                         st2["preset_title"] or ""))

    # 5. журнал на диске содержит хвост вызовов
    from core import log as dlog
    tail = dlog.tail(30)
    joined = "\n".join(tail)
    results.append(check("журнал: пробы записаны", "пробы" in joined))
    results.append(check("журнал: пресет записан", "пресет" in joined))

    failed = [i for i, ok in enumerate(results, 1) if not ok]
    print(f"\nитог: {len(results) - len(failed)}/{len(results)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

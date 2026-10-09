"""Живой прогон сценария пользователя: релиз скачан, службы нет.

Проверяет, что теперь: state() различает «нет папки»/«нет службы»,
install_release предлагает пресет, а его применение создаёт службу.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import presets as pm, service  # noqa: E402
from ui.webapp import App  # noqa: E402


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    return ok


def main() -> int:
    app = App()
    results = []

    st = app.state()
    print("ШАГ 1: текущее состояние (как у пользователя: папка есть, службы нет)")
    results.append(check("dir_ok (папка релиза на месте)", st["dir_ok"],
                         st.get("zapret_dir") or ""))
    results.append(check("installed=False (службы нет)", st["installed"] is False))
    results.append(check("suggested_preset из маркера",
                         bool(st.get("suggested_preset")),
                         st.get("suggested_preset") or ""))

    print("ШАГ 2: install_release (кэш zip есть - без сети)")
    rel = app.install_release()
    results.append(check("install ok", bool(rel.get("ok")),
                         str(rel.get("error") or "")))
    results.append(check("needs_preset=True", rel.get("needs_preset") is True))
    results.append(check("active_preset пришёл фронтенду",
                         bool(rel.get("active_preset")),
                         rel.get("active_preset") or ""))

    print("ШАГ 3: фронтенд подтверждает -> apply_preset(active_preset)")
    key = rel.get("active_preset")
    res = app.apply_preset(key)
    results.append(check("apply ok", bool(res.get("ok")),
                         str(res.get("error") or "")))
    time.sleep(2)

    st2 = app.state()
    print("ШАГ 4: состояние после создания службы")
    results.append(check("installed=True", st2["installed"] is True))
    results.append(check("running=True", st2["running"] is True,
                         f"pid={st2['pid']}"))
    results.append(check("заголовок = «Обход работает»", bool(st2["running"])))

    print("ШАГ 5: журнал содержит warning о пропавшей службе")
    from core import log as dlog
    tail = "\n".join(dlog.tail(40))
    results.append(check("warning записан", "службы zapret нет" in tail))
    results.append(check("установка залогирована", "предлагаю пресет" in tail))

    failed = sum(1 for r in results if not r)
    print(f"\nитог: {len(results) - failed}/{len(results)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

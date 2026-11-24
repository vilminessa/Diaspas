"""Живой E2E многоверсионности: установка -> две версии -> переключение ->
откат -> целостность/ремонт -> ротация.

Требует: сети (релиз), службы (apply), задачи планировщика (UAC при первой
операции).
"""
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _guard import require_live  # noqa: E402

require_live(__doc__ or 'эти прогоны')


from core import installs, paths, probes, service  # noqa: E402
from ui.webapp import App  # noqa: E402

RESULTS: list[bool] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append(bool(ok))
    print(f"  {'OK  ' if ok else 'FAIL'} {name}"
          + (f" — {detail}" if detail else ""), flush=True)


def stage(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def main() -> int:
    app = App()
    dest = paths.app_dir() / "zapret"

    stage("0. чистое состояние")
    if service.state().installed:
        print("  удаляю службу прошлого прогона…")
        service.remove(log=lambda l, m: print(f"    [{l}] {m}"))
    # реестр и папки прошлого прогона - в бэкап: E2E задуман для чистоты
    reg_file = installs.installs_file()
    if reg_file.is_file():
        reg_file.rename(reg_file.with_name(
            f"installs.json.e2e-{int(time.time())}"))
        print("  реестр прошлого прогона сохранён в бэкап")
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
        print("  папки прошлого прогона удалены")
    installs.set_keep(installs.DEFAULT_KEEP)
    check("реестр пуст", not installs.items())
    check("папок нет", not dest.exists())

    stage("1. установка первой версии (релиз GitHub)")
    rel = app.install_release()
    check("install ok", bool(rel.get("ok")), str(rel.get("error") or ""))
    v1 = installs.active_path()
    check("активная записана", v1 is not None, str(v1))
    check("stale при первой версии пуст", not rel.get("stale"))

    stage("2. вторая «версия» рядом (копия - сосед не уничтожается)")
    v2 = dest / "zapret-discord-youtube-1.9.9"
    if v2.exists():
        shutil.rmtree(v2, ignore_errors=True)
    shutil.copytree(v1, v2)
    installs.register(v2, source="manual")
    check("реестр содержит 2 версии", len(installs.items()) == 2,
          ", ".join(i.version for i in installs.items()))

    stage("3. установка релиза не уничтожает соседей")
    rel = app.install_release()
    check("повторная установка ok", bool(rel.get("ok")))
    check("соседняя версия цела", (v2 / "service.bat").is_file())

    stage("4. переключение на вторую версию")
    res = app.switch_version(str(v2))
    check("переключение ok", bool(res.get("ok")), str(res.get("error") or ""))
    time.sleep(1)
    st = app.state()
    check("служба работает", st["running"], f"pid={st['pid']}")
    check("active = вторая версия", installs.active_path() == v2)
    check("rollback = первая версия", installs.rollback_path() == v1,
          str(installs.rollback_path()))

    stage("5. пробы на переключённой версии")
    rep = probes.probe_all(timeout=8.0)
    check("обход работает", rep["state"] == "full",
          f"{rep['count']}/{rep['total']} {probes.failed_targets(rep)}")

    stage("6. откат одной командой")
    res = app.rollback_version()
    check("откат ok", bool(res.get("ok")), str(res.get("error") or ""))
    time.sleep(1)
    st = app.state()
    check("служба работает после отката", st["running"], f"pid={st['pid']}")
    check("active снова первая", installs.active_path() == v1)

    stage("7. целостность и ремонт")
    check_res = app.check_install()
    check("integrity ok", bool(check_res.get("ok")))
    check("файлы целы", not check_res.get("missing"),
          ", ".join(check_res.get("missing") or []))
    # «съедаем» winws.exe и чиним из архива; файл запущенного winws
    # заблокирован службой - в реальном сценарии (антивирус удалил файл)
    # служба уже упала, тут останавливаем сами
    service.stop(log=lambda l, m: print(f"    [{l}] {m}"))
    time.sleep(1)
    (v1 / "bin" / "winws.exe").unlink()
    check_res = app.check_install()
    check("повреждение обнаружено",
          "BIN/winws.exe" in (check_res.get("missing") or []),
          ", ".join(check_res.get("missing") or []))
    rep_res = app.repair_install()
    check("ремонт ok", bool(rep_res.get("ok")), str(rep_res.get("error") or ""))
    check("файл восстановлен", (v1 / "bin" / "winws.exe").is_file())
    check("integrity снова цела",
          not app.check_install().get("missing"))
    start_res = service.start(log=lambda l, m: print(f"    [{l}] {m}"))
    check("служба снова работает", bool(start_res.get("ok")))

    stage("8. ротация (лимит версий)")
    # три версии при лимите2 -> кандидат один (лимит = сколько держать)
    v3 = dest / "zapret-discord-youtube-1.10.1"
    if v3.exists():
        shutil.rmtree(v3, ignore_errors=True)
    shutil.copytree(v1, v3)
    installs.register(v3, source="manual")
    installs.set_keep(2)
    stale = installs.prune()
    check("кандидат на удаление найден", len(stale) == 1,
          ", ".join(s.version for s in stale))
    check("активная не предлагается",
          all(s.path != str(installs.active_path()) for s in stale))
    if stale:
        res = app.remove_version(stale[0].path)
        check("удаление версии ok", bool(res.get("ok")),
              str(res.get("error") or ""))
        check("папка удалена", not Path(stale[0].path).exists())
    check("версий осталось 2", len(installs.items()) == 2,
          ", ".join(i.version for i in installs.items()))

    ok_total = sum(RESULTS)
    print(f"\nИТОГ: {ok_total}/{len(RESULTS)}")
    return 0 if ok_total == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())

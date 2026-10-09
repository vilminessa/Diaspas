"""Полный E2E: от чистого состояния до проверенных обходов.

Последовательность повторяет путь пользователя через API (тот же код,
что дергает JS):

    чистое состояние -> state -> UAC-warm-up -> скачать релиз ->
    создать службу (без второго UAC) -> состояние -> файлы службы ->
    пробы всех групп -> curl-контроль

UAC на шаге warm-up требует подтверждения человека.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import paths, presets as presets_mod, probes, service  # noqa: E402
from ui.webapp import App  # noqa: E402

RESULTS: list[bool] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append(bool(ok))
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""), flush=True)


def stage(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def main() -> int:
    # --- 0. чистое состояние ------------------------------------------------
    stage("0. чистое состояние")
    base = paths.app_dir()
    # имя с меткой времени: переименование не упрётся в существующий бэкап
    backup = base.with_name(f"{base.name}.e2e-bak-{int(time.time())}")
    if base.exists():
        base.rename(backup)
        print(f"  старое сохранено: {backup.name}")
    check("app_dir не существует", not base.exists())
    # служба от прошлого прогона тоже должна уйти - иначе «чистое
    # состояние» не чистое, а проверки 1/3 ловят чужой хвост
    if service.state().installed:
        print("  удаляю службу прошлого прогона…")
        service.remove(log=lambda l, m: print(f"    [{l}] {m}"))
    check("службы нет", not service.state().installed)

    app = App()

    # --- 1. первый запуск: состояние -----------------------------------------
    stage("1. state при первом запуске")
    st = app.state()
    # автопоиск может ничего не найти (чистая машина) - тогда честный
    # «папки нет» и спасает «Скачать релиз»; порядок проверки произвольный
    autodiscovered = paths.zapret_install_dir() is not None
    check("dir_ok согласуется с автопоиском",
          st["dir_ok"] is autodiscovered,
          f"dir_ok={st['dir_ok']} autodiscover={autodiscovered}")
    check("dir_source согласуется", (st.get("dir_source") == "autodiscover")
          is autodiscovered, st.get("dir_source") or "(none)")
    check("installed=False", st["installed"] is False)
    check("suggested_preset подсказан", bool(st.get("suggested_preset")),
          st.get("suggested_preset") or "")

    # --- 2. warm-up UAC -------------------------------------------------------
    stage("2. warm-up UAC (задача уже есть -> без диалога)")
    info = app.prepare_privileges()
    check("have_task согласуется с системой",
          info["have_task"] is service.task_exists())
    check("тексты диалога приходят из i18n",
          bool(info.get("title")) and bool(info.get("body")))
    res = app.register_task_now()
    check("задача готова (зарегистрирована или уже была)",
          bool(res.get("registered") or res.get("already")))
    check("задача существует", service.task_exists())

    # --- 3. скачать релиз -----------------------------------------------------
    stage("3. установка релиза (GitHub)")
    rel = app.install_release()
    check("install ok", bool(rel.get("ok")), str(rel.get("error") or ""))
    check("запись в settings", bool(paths.read_settings().get("zapret_dir")))
    st = app.state()
    check("dir_source=settings", st.get("dir_source") == "settings",
          st.get("dir_source") or "")
    check("needs_preset (службы ещё нет)", rel.get("needs_preset") is True)

    # --- 4. создать службу ------------------------------------------------------
    stage("4. создание службы пресетом (без второго UAC)")
    key = rel.get("active_preset") or "ubisoft"
    t0 = time.time()
    res = app.apply_preset(key)
    check("apply ok", bool(res.get("ok")), str(res.get("error") or ""))
    check("быстро (задача уже зарегистрирована)", time.time() - t0 < 60,
          f"{time.time() - t0:.0f}s")

    # --- 5. состояние -----------------------------------------------------------
    stage("5. состояние после создания")
    st = app.state()
    check("installed=True", st["installed"] is True)
    check("running=True", st["running"] is True, f"pid={st['pid']}")
    check("пресет подсвечен", st["preset_key"] == key,
          st.get("preset_title") or "")

    # --- 6. файлы службы -------------------------------------------------------
    stage("6. целостность службы")
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                        r"System\CurrentControlSet\Services\zapret") as k:
        binpath = str(winreg.QueryValueEx(k, "ImagePath")[0])
    import re
    refs = re.findall(r'"([^"]+)"', binpath)
    missing = [r for r in refs if not Path(r).is_file()]
    check(f"все файлы аргументов существуют ({len(refs)})",
          not missing, "; ".join(missing[:3]))
    from core import batparser
    strategy = (st["strategy"] or "general (ALT11)") + ".bat"
    root = Path(binpath.split('"')[1]).parent.parent
    # порты Game Filter — как у применённого пресета (у discord_youtube
    # игровые фильтры выключены = 12, у игровых = 1024-65535)
    applied = next((p for p in presets_mod.load_presets()
                    if p.key == key), None)
    gf = applied.game_filter if applied else ""
    tcp = "1024-65535" if gf in ("all", "tcp") else "12"
    udp = "1024-65535" if gf in ("all", "udp") else "12"
    regen = batparser.winws_command(root, strategy, tcp, udp)
    norm = lambda s: re.sub(r"\s+", " ", s.replace('\\"', '"')).strip()
    check("batparser воспроизводит ImagePath", norm(regen) == norm(binpath))

    # --- 7. пробы всех групп ----------------------------------------------------
    stage("7. пробы всех групп (discord, youtube, ea, ubisoft)")
    rep = probes.probe_all(timeout=8.0)
    check("все цели ответили", rep["state"] == "full",
          f"{rep['count']}/{rep['total']} " + ",".join(probes.failed_targets(rep)))
    for g, r in sorted((rep.get("groups") or {}).items()):
        print(f"      {g}: {r['count']}/{r['total']}")

    # --- 8. curl-контроль ключевых хостов -----------------------------------------
    stage("8. curl-контроль")
    import subprocess
    for url in ("https://accounts.ea.com/",
                "https://discord.com/",
                "https://www.youtube.com/",
                "https://account.ubisoft.com/"):
        out = subprocess.run(
            ["curl", "-sS", "-o", "NUL", "-w", "%{http_code}",
             "--connect-timeout", "6", "--max-time", "12", url],
            capture_output=True, text=True, timeout=30)
        code = out.stdout.strip()
        # успех = любой валидный HTTP-ответ:404 (корень accounts.ea.com)
        # и307 (редирект логина Ubisoft) означают, что TLS прошёл и
        # сервер ответил; "000" у curl = не достучался
        ok = code.isdigit() and code != "000"
        check(url, ok, f"HTTP {code}")

    ok_total = sum(RESULTS)
    print(f"\nИТОГ: {ok_total}/{len(RESULTS)}")
    return 0 if ok_total == len(RESULTS) else 1


if __name__ == "__main__":
    import shutil
    raise SystemExit(main())

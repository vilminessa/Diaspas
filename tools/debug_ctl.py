#!/usr/bin/env python3
"""Консоль управления проектом Diaspas: контекст, меню, одиночные операции.

debug.bat - тонкий ASCII-диспетчер; весь текст и логика здесь. Причина: cmd
перечитывает батч по байтовому offset после ``chcp 65001``, кириллица сдвигает
этот offset и выполняются фрагменты строк (воспроизводилось на шестистроковом
файле в Synfronya). Русские строки печатает Python.

    python tools/debug_ctl.py menu         # контекст + список пунктов
    python tools/debug_ctl.py 5            # выполнить пункт 5 и выйти
    python tools/debug_ctl.py ab ubisoft   # пункт с аргументом
    python tools/debug_ctl.py I --yes      # опасный пункт без вопроса

Опасные пункты (UAC, перезапуск службы, удаление задач, переустановка релиза,
git push, убийство процессов) в меню спрашивают; в быстром режиме
(``DIASPAS_SCRIPTED=1``) требуют ``--yes`` и иначе отказываются.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import autoswitch, log as dlog, paths  # noqa: E402
from core import presets as presets_mod, service, strategies as strategies_mod  # noqa: E402
import version as app_version  # noqa: E402 - версия приложения для контекста и пункта V

PY = sys.executable
NODE = shutil.which("node")

# Служебные задачи планировщика: одна для службы, одна для перебора.
TASKS = ("DiaspasService", "DiaspasSelector")

# winws.exe в список не входит: это процесс обхода (служба zapret), убить его
# = гасить обход, а debug-консоль управляет проектом, не сетевым обходом.
KILL_NAMES = ("diaspas.exe", "python.exe", "pythonw.exe")


def _run(cmd: list[str], args: list[str] | None = None) -> int:
    """Запустить команду в корне проекта; вывод идёт в эту же консоль."""
    full = [str(part) for part in cmd] + [str(a) for a in (args or [])]
    print("$ " + " ".join(full))
    try:
        return subprocess.run(full, cwd=str(ROOT)).returncode
    except OSError as exc:
        print(f"не удалось запустить: {exc}")
        return 1


def _decode(raw: bytes) -> str:
    """Вывод внешних инструментов: git пишет UTF-8, schtasks - OEM-кодовку."""
    for encoding in ("utf-8", "cp866", "cp1251"):
        try:
            return raw.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace").strip()


def _capture(cmd: list[str]) -> str:
    """Короткий вывод команды для контекста меню (без падения)."""
    try:
        out = subprocess.run([str(part) for part in cmd], cwd=str(ROOT),
                             capture_output=True, timeout=15)
        return _decode(out.stdout or b"")
    except (OSError, subprocess.SubprocessError):
        return ""


# Быстрый режим debug.bat (--scripted): подтверждений нет, опасные пункты
# требуют --yes. Флаг приходит аргументом, а не переменной окружения: env
# наследуется дочерними процессами и pytest под пунктом CI видел бы
# «неинтерактивный режим», которого он не просил.
SCRIPTED = False


def scripted() -> bool:
    return SCRIPTED


def _ask(text: str) -> str:
    print(text)
    try:
        return input("> ").strip()
    except EOFError:
        return ""


def confirmed(op: dict, args: list[str]) -> bool:
    if "--yes" in args:
        return True
    if scripted():
        print(f"{op['warn']}\nБыстрый режим: добавьте --yes для подтверждения.")
        return False
    answer = _ask(op["warn"])
    return answer.strip().lower() in ("y", "yes", "д", "да")


# -- пункты ---------------------------------------------------------------

def op_run(args: list[str]) -> int:
    return _run([PY, "main.py"], args)


def op_debug(args: list[str]) -> int:
    """Окно с DevTools WebView2: консоль JS видна при правке фронтенда."""
    return _run([PY, "main.py", "--debug"], args)


def op_tk(args: list[str]) -> int:
    """Tkinter-резерв: тот же core, без pywebview (проверка резервного пути)."""
    return _run([PY, "main.py", "--tk"], args)


def op_smoke_gui(args: list[str]) -> int:
    return _run([PY, "tests/smoke_webview.py"], args)


def op_tests(args: list[str]) -> int:
    return _run([PY, "-m", "pytest", "tests/", "-q"], args)


def op_lint(args: list[str]) -> int:
    """Синтаксис и линт: compileall, pyflakes, node, сверка бандла, сверка конвейера."""
    steps: list[tuple[str, list[str]]] = [
        ("compileall", [PY, "-m", "compileall", "-q", "core", "ui", "tools",
                        "tests", "i18n.py", "main.py", "version.py"]),
        ("pyflakes", [PY, "-m", "pyflakes",
                      "core", "ui", "tools", "i18n.py", "main.py", "version.py"]),
        ("node --check ui_src/app.js",
         [NODE, "--check", "ui_src/app.js"] if NODE else []),
        ("сверка бандла UI", [PY, "tools/build_ui.py", "--check"]),
        ("линт workflows", [PY, "tools/check_workflows.py"]),
    ]
    code = 0
    for name, cmd in steps:
        if not cmd:
            print(f"  {name}: не найден node - пропускаю")
            continue
        print(f"--- {name}")
        rc = _run(cmd)
        code = code or rc
    if code == 0:
        print("синтаксис и линт в порядке")
    return code


def op_bundle(args: list[str]) -> int:
    return _run([PY, "tools/build_ui.py"], args)


def op_ci(args: list[str]) -> int:
    """Локальный прогон шагов CI: сверка бандла -> pytest -> линт -> линт workflows."""
    for cmd in ([PY, "tools/build_ui.py", "--check"],
                [PY, "-m", "pytest", "tests/", "-q"],
                [PY, "-m", "pyflakes",
                 "core", "ui", "tools", "i18n.py", "main.py", "version.py"],
                [PY, "tools/check_workflows.py"]):
        rc = _run(cmd)
        if rc:
            return rc
    print("CI-шаги локально прошли")
    return 0


def op_service(args: list[str]) -> int:
    """Состояние службы и установки без прав: sc query + реестр + режимы."""
    root = paths.zapret_dir_from_settings()
    st = service.state()
    print(f"папка zapret: {root or 'не найдена'}")
    print(f"служба: {'установлена' if st.installed else 'не установлена'}"
          f" / {'работает' if st.running else 'остановлена'}"
          f" start={st.start_type or '-'} pid={st.pid or '-'}")
    if not st.installed:
        print("служба не создана - пресет не применён или удалена вне интерфейса")
        return 2

    active = strategies_mod.current_file(root, st)
    label = paths.strategy_label(active) if active else st.strategy
    print(f"стратегия: {label or '-'} ({active or st.strategy})")
    print(f"режим смены стратегии: {strategies_mod.mode()}"
          f"  сохранённый выбор: {strategies_mod.saved(root) or '-'}")
    left = autoswitch.cooldown_left()
    if left:
        print(f"автоподбор: пауза ещё {left // 60} мин"
              f" (последняя стратегия {autoswitch.last_strategy() or '-'})")
    preset = None
    if root is not None:
        try:
            preset = presets_mod.current_preset(
                root, st, active_key=presets_mod.active_preset_key())
        except Exception:  # noqa: BLE001 - диагностика не должна падать
            preset = None
    print(f"пресет: {preset.title if preset else 'не распознан'}")
    return 0 if st.running else 1


def op_files_check(args: list[str]) -> int:
    """Все файлы, на которые ссылается BinaryPathName службы (ошибка 1067)."""
    if not service.state().installed:
        print("службы нет - проверять ссылку ImagePath нечего")
        return 2
    return _run([PY, "tests/check_service_files.py"], args)


def op_strategies(args: list[str]) -> int:
    return _run([PY, "tests/run_strategy.py", "--list"], args)


def op_strategy_set(args: list[str]) -> int:
    """Поставить стратегию вручную: аргументом имя файла или вопрос в меню."""
    root = paths.zapret_dir_from_settings()
    if root is None:
        print("папка zapret не найдена")
        return 1
    name = next((a for a in args if not a.startswith("--")), "")
    if not name:
        if scripted():
            print('укажите стратегию аргументом: debug.bat B "general (ALT7).bat"')
            return 2
        infos = strategies_mod.list_strategies(root, service.state().strategy)
        for info in infos:
            print(("  * " if info.active else "    ") + f"{info.label:<24} {info.file}")
        name = _ask("Имя файла стратегии:")
        if not name:
            return 1
    return _run([PY, "tests/run_strategy.py"], [name])


def op_strategy_auto(args: list[str]) -> int:
    extra = ["--force"] if "--force" in args else []
    return _run([PY, "tests/run_strategy.py", "--auto"], extra)


def op_select(args: list[str]) -> int:
    return _run([PY, "tests/run_select.py"], args)


def op_probes(args: list[str]) -> int:
    return _run([PY, "tests/run_probes.py"], args)


def op_ab(args: list[str]) -> int:
    group = next((a for a in args if not a.startswith("--")), "")
    if not group:
        if scripted():
            print("укажите группу: debug.bat F ea")
            return 2
        group = _ask("Группа целей (discord|youtube|ea|ubisoft):") or "ubisoft"
    return _run([PY, "tests/run_ab.py"], [group])


def op_tasks(args: list[str]) -> int:
    """Состояние служебных задач: они зарегистрированы один раз и больше UAC не дергают."""
    code = 0
    for task in TASKS:
        out = _capture(["schtasks", "/Query", "/TN", task])
        print(f"{task}: {'есть' if out else 'не зарегистрирована'}")
        code = code or (0 if out else 1)
    if code:
        print("без задач первая операция со службой запросит UAC")
    return 0


def op_tasks_delete(args: list[str]) -> int:
    code = 0
    for task in TASKS:
        rc = _run(["schtasks", "/End", "/TN", task])
        rc = rc or _run(["schtasks", "/Delete", "/TN", task, "/F"])
        print(f"{task}: удалена (rc={rc})")
        code = code or rc
    print("без задач первая операция со службой снова запросит UAC")
    return code


def op_release(args: list[str]) -> int:
    return _run([PY, "tests/run_e2e_install.py"], args)


def op_exe(args: list[str]) -> int:
    rc = _run([PY, "-m", "PyInstaller", "diaspas.spec", "--noconfirm"])
    if rc:
        return rc
    return _run([PY, "tests/smoke_exe.py"], args)


def op_version_show(args: list[str]) -> int:
    """Версия и подпись сборки - то, что показывает статус-бар и Properties exe."""
    print(f"числовая версия: {app_version.__version__}")
    print(f"подпись сборки: {app_version.label()}")
    band = ("бета" if app_version.is_beta()
            else "релиз" if app_version.BUILD_LABEL else "не из тега")
    print(f"полоса: {band}")
    return 0


def op_version_stamp(args: list[str]) -> int:
    """Записать версию из тега в рабочее дерево перед ручной сборкой exe."""
    tag = next((a for a in args if not a.startswith("--")), "")
    if not tag:
        if scripted():
            print("укажите тег аргументом: debug.bat U v0.1.2")
            return 2
        tag = _ask("Тег сборки (v0.1.2 или b0.1.2):")
        if not tag:
            return 1
    rc = _run([PY, "tools/stamp_version.py", tag])
    if rc == 0:
        print("дальше: python -m PyInstaller diaspas.spec")
    return rc


def op_git(args: list[str]) -> int:
    rc = _run(["git", "status", "-sb"])
    print("--- последние коммиты")
    return rc or _run(["git", "log", "--oneline", "-6"])


def op_push(args: list[str]) -> int:
    _run(["git", "status", "-sb"])
    message = next((a for a in args if not a.startswith("--")), "")
    if not message:
        if scripted():
            print("сообщение коммита: debug.bat L \"текст\"")
            return 2
        message = _ask("Сообщение коммита (пусто = отмена):")
        if not message:
            return 0
    rc = _run(["git", "add", "-A"])
    rc = rc or _run(["git", "commit", "-m", message])
    if rc:
        return rc
    return _run(["git", "push", "origin", "HEAD"])


def op_profile(args: list[str]) -> int:
    directory = paths.app_dir()
    if not directory.is_dir():
        print(f"{directory} ещё нет - сначала запустите приложение")
        return 1
    try:
        os.startfile(str(directory))  # noqa: S606 - папка профиля штатным viewer
        return 0
    except OSError as exc:
        print(f"не удалось открыть папку: {exc}")
        return 1


def op_log_open(args: list[str]) -> int:
    path = dlog.log_path()
    if not path.is_file():
        print(f"{path} ещё нет")
        return 1
    try:
        os.startfile(str(path))  # noqa: S606 - журнал штатным viewer
        return 0
    except OSError as exc:
        print(f"не удалось открыть журнал: {exc}")
        return 1


def op_backup(args: list[str]) -> int:
    """Копия профиля: настройки и журнал в backup/<timestamp>."""
    src = paths.app_dir()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = src / "backup" / stamp
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    for name in ("settings.json", "diaspas.log", "diaspas.log.old"):
        path = src / name
        if path.is_file():
            shutil.copy2(path, dest / name)
            copied += 1
    print(f"копия: {dest} ({copied} файлов)")
    return 0 if copied else 1


def kill_targets(processes: list[dict]) -> list[int]:
    """PID процессов Diaspas из снимка Win32_Process.

    Только python/pythonw с main.py в командной строке и Diaspas.exe.
    winws.exe исключён по определению: это обход, не процесс проекта.
    """
    pids: list[int] = []
    for proc in processes:
        name = str(proc.get("name") or "").lower()
        line = str(proc.get("commandline") or "").lower()
        if name not in KILL_NAMES:
            continue
        if name == "diaspas.exe" or "main.py" in line:
            pids.append(int(proc.get("pid") or 0))
    return [pid for pid in pids if pid]


def op_kill(args: list[str]) -> int:
    """Закрыть процессы проекта; обход (winws) остаётся целым."""
    list_cmd = ("Get-CimInstance Win32_Process | "
                "Where-Object { 'python.exe','pythonw.exe','Diaspas.exe' -contains "
                "$_.Name } | ForEach-Object { '{0}|{1}|{2}' -f $_.ProcessId, "
                "$_.Name, $_.CommandLine }")
    out = _capture(["powershell", "-NoProfile", "-Command", list_cmd])
    procs: list[dict] = []
    for line in out.splitlines():
        pid, _, rest = line.partition("|")
        name, _, command = rest.partition("|")
        procs.append({"pid": pid.strip(), "name": name.strip(),
                      "commandline": command})
    pids = kill_targets(procs)
    if not pids:
        print("процессов Diaspas нет")
        return 0
    joined = ",".join(str(pid) for pid in pids)
    print(f"закрываю PID: {joined}")
    return _run(["powershell", "-NoProfile", "-Command",
                 f"Stop-Process -Id {joined} -Force -ErrorAction SilentlyContinue"])


# -- меню -----------------------------------------------------------------

OPS: list[dict] = [
    {"keys": ["1", "run"], "title": "Запустить приложение (окно)",
     "fn": op_run},
    {"keys": ["2", "debug"], "title": "Запустить с отладкой WebView (--debug)",
     "fn": op_debug},
    {"keys": ["3", "tk"], "title": "Запустить Tkinter-резерв (--tk)",
     "fn": op_tk},
    {"keys": ["4", "smoke"], "title": "Smoke GUI: окно, мост JS<->Python, журнал",
     "fn": op_smoke_gui},
    {"keys": ["5", "tests"], "title": "Тесты офлайн (pytest)",
     "fn": op_tests},
    {"keys": ["6", "lint"], "title": "Линт и синтаксис: compileall, pyflakes, node, бандл",
     "fn": op_lint},
    {"keys": ["7", "bundle"], "title": "Пересобрать бандл UI (ui_src -> ui/bundle.py)",
     "fn": op_bundle},
    {"keys": ["8", "ci"], "title": "CI-шаги локально: сверка бандла -> pytest -> pyflakes",
     "fn": op_ci},
    {"keys": ["9", "service"], "title": "Состояние службы и установки (без прав)",
     "fn": op_service},
    {"keys": ["Q", "files"], "title": "Проверка файлов службы (ошибка 1067)",
     "fn": op_files_check},
    {"keys": ["A", "strategies"], "title": "Стратегии: список и активная",
     "fn": op_strategies},
    {"keys": ["B", "set"], "title": "Стратегия: сменить вручную",
     "fn": op_strategy_set,
     "warn": "Служба zapret будет пересоздана; обход на секунду прерывается."},
    {"keys": ["C", "auto"], "title": "Стратегия: автоцикл (пробы + подбор + применение)",
     "fn": op_strategy_auto,
     "warn": "Автоцикл перезапускает службу и может запросить UAC (перебор до 22 стратегий)."},
    {"keys": ["D", "select"], "title": "Полный перебор стратегий (UAC, минуты)",
     "fn": op_select,
     "warn": "Перебор гасит winws между стратегиями: Discord и YouTube прерываются."},
    {"keys": ["E", "probes"], "title": "Пробы всех групп (сеть)",
     "fn": op_probes},
    {"keys": ["F", "ab"], "title": "A/B-тест группы (сеть + UAC)",
     "fn": op_ab,
     "warn": "Служба будет остановлена и запущена заново для замера без обхода."},
    {"keys": ["G", "tasks"], "title": "Служебные задачи: DiaspasService / DiaspasSelector",
     "fn": op_tasks},
    {"keys": ["H", "tasksdel"], "title": "Удалить служебные задачи (UAC)",
     "fn": op_tasks_delete,
     "warn": "Удаление задач вернёт обратно первый запрос UAC на операцию со службой."},
    {"keys": ["I", "release"], "title": "Установка релиза с GitHub (сеть)",
     "fn": op_release,
     "warn": "Очистит %LOCALAPPDATA%\\Diaspas\\zapret и перепишет zapret_dir: "
             "живая служба останется от старой папки."},
    {"keys": ["J", "exe"], "title": "Собрать exe + smoke-прогон",
     "fn": op_exe},
    {"keys": ["V", "version"], "title": "Версия и подпись сборки (как в статус-баре)",
     "fn": op_version_show},
    {"keys": ["U", "stamp"], "title": "Проставить версию из тега (stamp_version)",
     "fn": op_version_stamp,
     "warn": "Меняет version.py в рабочем дереве: __version__ и BUILD_LABEL из тега."},
    {"keys": ["K", "git"], "title": "git: статус и последние коммиты",
     "fn": op_git},
    {"keys": ["L", "push"], "title": "git: add + commit + push",
     "fn": op_push,
     "warn": "git add -A заберёт все изменения, push отправит ветку в origin."},
    {"keys": ["M", "profile"], "title": "Открыть папку профиля (%LOCALAPPDATA%\\Diaspas)",
     "fn": op_profile},
    {"keys": ["N", "log"], "title": "Открыть журнал (diaspas.log)",
     "fn": op_log_open},
    {"keys": ["O", "backup"], "title": "Копия профиля в backup (settings + журнал)",
     "fn": op_backup},
    {"keys": ["P", "kill"], "title": "Убить процессы Diaspas (winws не трогает)",
     "fn": op_kill,
     "warn": "Закроет окно приложения (python/Diaspas.exe); обход не трогаем."},
    {"keys": ["0", "exit"], "title": "Выход", "fn": lambda _args: 0},
]


def find(token: str) -> dict | None:
    lowered = str(token).strip().lower()
    for op in OPS:
        if lowered in [key.lower() for key in op["keys"]]:
            return op
    return None


def context() -> dict:
    """Что показать над меню: git, папка, служба, стратегия, режим."""
    ctx = {"branch": "?", "dirty": "?", "dir": None, "installed": False,
           "running": False, "strategy": "", "mode": strategies_mod.mode(),
           "cooldown": 0, "preset": None,
           "version": app_version.__version__, "label": app_version.label()}
    branch = _capture(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if branch:
        ctx["branch"] = branch
    dirty = _capture(["git", "status", "--porcelain"])
    ctx["dirty"] = len([line for line in dirty.splitlines() if line])
    root = paths.zapret_dir_from_settings()
    ctx["dir"] = root
    ctx["cooldown"] = autoswitch.cooldown_left()
    if root is not None:
        st = service.state()
        ctx["installed"] = st.installed
        ctx["running"] = st.running
        active = strategies_mod.current_file(root, st)
        ctx["strategy"] = paths.strategy_label(active) if active else st.strategy
        try:
            preset = presets_mod.current_preset(
                root, st, active_key=presets_mod.active_preset_key())
            ctx["preset"] = preset.title if preset else None
        except Exception:  # noqa: BLE001
            ctx["preset"] = None
    return ctx


def print_menu() -> int:
    ctx = context()
    print("=" * 62)
    print("  Diaspas - консоль управления проектом")
    print("=" * 62)
    print(f"  ветка: {ctx['branch']}    не закоммичено: {ctx['dirty']}")
    print(f"  версия: {ctx['version']}    сборка: {ctx['label']}")
    print(f"  папка zapret: {ctx['dir'] or 'не найдена'}")
    service_text = ("не установлена" if not ctx["installed"]
                    else ("работает" if ctx["running"] else "остановлена"))
    print(f"  служба: {service_text}    стратегия: {ctx['strategy'] or '-'}")
    print(f"  режим стратегии: {ctx['mode']}"
          + (f"    автопауза: {ctx['cooldown'] // 60} мин"
             if ctx["cooldown"] else ""))
    print(f"  пресет: {ctx['preset'] or 'не распознан'}")
    print("-" * 62)
    for op in OPS:
        token = op["keys"][0]
        print(f"  [{token}] {op['title']}")
    print("-" * 62)
    print("  Быстрый режим: debug.bat 5, debug.bat F ea, debug.bat I --yes")
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0].lower() == "menu":
        return print_menu()

    token = argv[0]
    flags = argv[1:]
    global SCRIPTED
    SCRIPTED = "--scripted" in flags
    # служебные флаги консоли не уезжают в команду пункта
    args = [flag for flag in flags if flag not in ("--scripted", "--yes")]
    op = find(token)
    if op is None:
        print(f"неизвестный пункт: {token}")
        print_menu()
        return 2

    warn = op.get("warn")
    if warn and not confirmed(op, flags):
        print("отменено")
        return 1
    return op["fn"](args)


if __name__ == "__main__":
    # Windows console иначе падает на кириллице в cp1252 (тот же приём, что в CI)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main(sys.argv[1:]))

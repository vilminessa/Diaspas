"""Почему proc.kill() не закрывает Diaspas.exe?

Ad-hoc диагностика процесса PyInstaller (onefile = bootstrap + приложение).
Путь по умолчанию - ``<репозиторий>/dist/Diaspas.exe``.
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXE = ROOT / "dist" / "Diaspas.exe"

if __name__ == "__main__":
    exe = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_EXE
    if not exe.is_file():
        print(f"нет {exe} - сначала соберите: pyinstaller diaspas.spec")
        raise SystemExit(1)
    proc = subprocess.Popen([str(exe)])
    pid = proc.pid
    print(f"запущен PID {pid}")
    time.sleep(6)
    alive1 = proc.poll() is None
    print(f"жив через 6s: {alive1}")

    proc.terminate()
    time.sleep(2)
    # poll() возвращает None, пока процесс не завершён в системе
    print(f"после terminate: poll={proc.poll()}")

    if proc.poll() is None:
        proc.kill()
        time.sleep(2)
        print(f"после kill: poll={proc.poll()}")

    # независимая проверка
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(Get-Process -Id {pid} -ErrorAction SilentlyContinue) -ne $null"],
        capture_output=True, text=True, timeout=20)
    print("Get-Process говорит, что жив:", out.stdout.strip())

    if proc.poll() is None:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True, timeout=20)
        time.sleep(2)
        print(f"после taskkill: poll={proc.poll()}")

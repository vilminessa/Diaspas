"""Почему proc.kill() не закрывает Diaspas.exe?"""
import subprocess
import sys
import time

EXE = r"B:\projs\Diaspas\dist\Diaspas.exe"

if __name__ == "__main__":
    proc = subprocess.Popen([EXE])
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

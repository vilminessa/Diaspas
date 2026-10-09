"""Наблюдение за первым запуском CI-exe на чистом состоянии.

Запускает скачанный exe, ждёт окно и журнальные записи, затем закрывает
деревом (onefile = bootstrap + приложение).
"""
import subprocess
import time
from pathlib import Path

EXE = Path.home() / "Downloads/Diaspas.exe"
LOG = Path.home() / "AppData/Local/Diaspas/diaspas.log"

FIND_WINDOW = r'''
Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class W {
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr p);
    [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
    public delegate bool EnumProc(IntPtr h, IntPtr p);
}
"@
$found = 0
$cb = [W+EnumProc]{ param($h, $p)
    $sb = New-Object System.Text.StringBuilder 256
    [W]::GetWindowText($h, $sb, 256) | Out-Null
    if ($sb.ToString() -match "Diaspas" -and [W]::IsWindowVisible($h)) { $script:found++ }
    return $true
}
[W]::EnumWindows($cb, [IntPtr]::Zero) | Out-Null
"окон Diaspas: $found"
'''


def main() -> int:
    if not EXE.is_file():
        print(f"нет exe: {EXE}")
        return 1
    print(f"exe: {EXE} ({EXE.stat().st_size} байт)")
    print(f"журнал существует до запуска: {LOG.is_file()}")

    proc = subprocess.Popen([str(EXE)])
    print(f"запущен PID {proc.pid}")
    time.sleep(10)

    if proc.poll() is not None:
        print(f"УПАЛ, код {proc.returncode}")
        return 1
    print("процесс жив")

    out = subprocess.run(["powershell", "-NoProfile", "-Command", FIND_WINDOW],
                         capture_output=True, text=True, timeout=30)
    print(out.stdout.strip() or out.stderr.strip())

    print(f"журнал существует после запуска: {LOG.is_file()}")
    if LOG.is_file():
        text = LOG.read_text(encoding="utf-8-sig")
        print(f"размер журнала: {len(text)} байт")
        for line in text.splitlines()[-8:]:
            print("  |", line)

    # НЕ убиваем: пользователю предстоит кликать в окне. Пишем PID.
    print(f"ОКНО ОСТАВЛЕНО ОТКРЫТЫМ: PID={proc.pid} — кликай в нём")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Smoke-тест exe: запустить, дождаться окна, закрыть.

Путь по умолчанию - ``<репозиторий>/dist/Diaspas.exe`` (результат
``pyinstaller diaspas.spec``); первым аргументом можно задать другой exe.
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXE = ROOT / "dist" / "Diaspas.exe"
sys.path.insert(0, str(ROOT / "tests"))
from _console import decode  # noqa: E402 - вывод PowerShell в OEM-кодовке

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
$found = @()
$cb = [W+EnumProc]{ param($h, $p)
    $sb = New-Object System.Text.StringBuilder 256
    [W]::GetWindowText($h, $sb, 256) | Out-Null
    $t = $sb.ToString()
    if ($t -match "Diaspas") {
        $script:found += ("handle={0} visible={1} title='{2}'" -f $h, [W]::IsWindowVisible($h), $t)
    }
    return $true
}
[W]::EnumWindows($cb, [IntPtr]::Zero) | Out-Null
if ($found) { $found } else { "окон с Diaspas нет" }
'''

if __name__ == "__main__":
    exe = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_EXE
    if not exe.is_file():
        print(f"нет {exe} - сначала соберите: pyinstaller diaspas.spec")
        sys.exit(1)
    proc = subprocess.Popen([str(exe)])
    print(f"запущен PID {proc.pid}")
    time.sleep(8)
    if proc.poll() is not None:
        print(f"УПАЛ, код {proc.returncode}")
        sys.exit(1)
    print("процесс жив")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", FIND_WINDOW],
                         capture_output=True, timeout=30)
    print(decode(out.stdout).strip() or decode(out.stderr).strip())
    # PyInstaller onefile = два процесса (bootstrap + приложение): убиваем
    # дерево, иначе дочерний сиротеет с висящим окном
    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                   capture_output=True, timeout=20)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    print(f"закрыт, poll={proc.poll()}")
    if proc.poll() is None:
        print("ОШИБКА: процесс не завершился")
        sys.exit(1)

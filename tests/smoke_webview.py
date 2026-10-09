"""Smoke webview-окна: запуск main.py, проверка окна и журнала, закрытие."""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
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
$found = @()
$cb = [W+EnumProc]{ param($h, $p)
    $sb = New-Object System.Text.StringBuilder 256
    [W]::GetWindowText($h, $sb, 256) | Out-Null
    if ($sb.ToString() -match "Diaspas") {
        $script:found += "visible=" + [W]::IsWindowVisible($h)
    }
    return $true
}
[W]::EnumWindows($cb, [IntPtr]::Zero) | Out-Null
if ($found) { $found } else { "нет окон" }
'''


def main() -> int:
    before = LOG.read_text(encoding="utf-8-sig") if LOG.is_file() else ""
    proc = subprocess.Popen([sys.executable, str(ROOT / "main.py")],
                            cwd=str(ROOT))
    print(f"запущен PID {proc.pid}")
    time.sleep(9)

    if proc.poll() is not None:
        print(f"УПАЛ, код {proc.returncode}")
        return 1
    print("процесс жив")

    out = subprocess.run(["powershell", "-NoProfile", "-Command", FIND_WINDOW],
                         capture_output=True, text=True, timeout=30)
    print("окна:", out.stdout.strip() or out.stderr.strip())

    # журнал должен содержать следы: веб-запуск и живость моста
    after = LOG.read_text(encoding="utf-8-sig") if LOG.is_file() else ""
    new_lines = [l for l in after.splitlines() if l not in before.splitlines()]
    bridge_lines = [l for l in new_lines if "мост JS<->Python" in l]
    exc_lines = [l for l in new_lines if "[exception]" in l]
    print(f"новых строк: {len(new_lines)}, мост: {len(bridge_lines)}, "
          f"исключений: {len(exc_lines)}")
    for l in (bridge_lines + exc_lines)[:4]:
        print("  ", l[:160])

    # закрытие дерева процессов (python -> webview-дочерние)
    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                   capture_output=True, timeout=20)
    time.sleep(1)
    print("закрыт, poll =", proc.poll())

    ok = (out.stdout and "нет окон" not in out.stdout
          and len(bridge_lines) > 0 and not exc_lines)
    print("ИТОГ:", "OK" if ok else "ПРОВАЛ")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Управление службой zapret с одним запросом UAC на весь срок.

WinDivert работает только от администратора, а службу создавать/пересоздавать
надо часто (переключение пресета). Как в Synfronia (dpi.py): один раз
регистрируется служебная задача планировщика с уровнем Highest - это и есть
единственный диалог UAC; дальше Diaspas кладёт запрос в JSON и дёргает
``schtasks /Run``, а служба планировщика сама поднимает PowerShell-помощник.

Помощник живёт отдельным файлом (пишется на месте, обновляется при каждом
запуске), работает строго по одному запросу и кладёт результат атомарно
(tmp -> move), чтобы читатель не увидел полузаписанный JSON.
"""

from __future__ import annotations

import ctypes
import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import paths

TASK_NAME = "DiaspasService"
RUNNER_NAME = "service_runner.ps1"
_REQUEST = "request.json"
_RESULT = "result-"
_PROGRESS = "progress-"


# -- PowerShell-помощник ---------------------------------------------------
# ASCII-исходник: пишется на диск и выполняется системным PowerShell.
# Держим его без кириллицы - кодировка запускаемых .ps1 по умолчанию ANSI.
_RUNNER_SOURCE = r'''
param(
    [Parameter(Mandatory = $true)][string]$Work,
    [switch]$Register
)
$ErrorActionPreference = "SilentlyContinue"
$sw = [Diagnostics.Stopwatch]::StartNew()
function LG($m) {
    try { Add-Content -Path (Join-Path $Work "runner.log") -Value ("{0} {1}" -f (Get-Date -Format "HH:mm:ss"), $m) -Encoding ASCII } catch { }
}
LG "boot register=$Register"

if ($Register) {
    try {
        $ps = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
        $ps1 = Join-Path $Work "service_runner.ps1"
        $action = New-ScheduledTaskAction -Execute $ps -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ps1`" -Work `"$Work`""
        $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date
        $principal = New-ScheduledTaskPrincipal -UserId ("{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME) -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances Parallel -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Register-ScheduledTask -TaskName "DiaspasService" -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
        LG "task registered"
    } catch { LG "register failed: $($_.Exception.Message)"; exit 1 }
    exit 0
}

$token = "0"
$action = ""
$req = $null
try {
    $req = Get-Content (Join-Path $Work "request.json") -Raw -Encoding UTF8 | ConvertFrom-Json
    $token = "$($req.token)"; $action = "$($req.action)"
    LG "req action=$action token=$token"
} catch { LG "request read failed: $($_.Exception.Message)" }

$outPath = Join-Path $Work ("result-" + $token + ".json")
function Finish($Payload) {
    try {
        $tmp = "$outPath.tmp"
        $Payload | ConvertTo-Json -Depth 8 | Set-Content -Path $tmp -Encoding UTF8
        Move-Item -Path $tmp -Destination $outPath -Force
        LG "result written action=$action"
    } catch { LG "result write failed: $($_.Exception.Message)" }
}

function Get-State {
    $svc = $null
    try { $svc = Get-Service -Name "zapret" -ErrorAction Stop } catch { }
    $label = ""
    try {
        $r = Get-ItemProperty "HKLM:\System\CurrentControlSet\Services\zapret" -ErrorAction Stop
        $label = "$($r.'zapret-discord-youtube')"
    } catch { }
    $proc = Get-Process -Name "winws" -ErrorAction SilentlyContinue | Select-Object -First 1
    $bin = ""
    try {
        $q = Get-CimInstance Win32_Service -Filter "Name='zapret'" -ErrorAction Stop
        if ($q) { $bin = "$($q.PathName)" }
    } catch { }
    return [PSCustomObject]@{
        installed = [bool]$svc
        running   = ($svc -and $svc.Status -eq "Running")
        start     = $(if ($svc) { "$($svc.StartType)" } else { "" })
        strategy  = $label
        pid       = $(if ($proc) { $proc.Id } else { 0 })
        binpath   = $bin
    }
}

try {
    switch ($action) {
        "state" {
            Finish ([PSCustomObject]@{ ok = $true; action = "state"; state = (Get-State) })
        }
        "install" {
            # Пересоздание службы: путь к winws + аргументы приходят готовыми.
            & sc.exe stop zapret 2>&1 | Out-Null
            Start-Sleep -Milliseconds 800
            & sc.exe delete zapret 2>&1 | Out-Null
            Start-Sleep -Milliseconds 800
            $bin = "$($req.binpath)"
            $label = "$($req.strategy)"
            $exe = "$($req.exe)"
            if (-not $bin) { Finish ([PSCustomObject]@{ ok = $false; action = "install"; error = "empty binpath" }); break }
            try {
                New-Service -Name "zapret" -BinaryPathName $bin -DisplayName "zapret" `
                    -Description "Zapret DPI bypass software (managed by Diaspas)" `
                    -StartupType Automatic | Out-Null
                reg add "HKLM\System\CurrentControlSet\Services\zapret" /v zapret-discord-youtube /t REG_SZ /d "$label" /f | Out-Null
                # timestamps включает service.bat перед созданием службы
                netsh interface tcp set global timestamps=enabled 2>&1 | Out-Null
                & sc.exe start zapret 2>&1 | Out-Null
                Start-Sleep -Seconds 2
                LG "installed strategy=$label"
                Finish ([PSCustomObject]@{ ok = $true; action = "install"; state = (Get-State) })
            } catch {
                LG "install failed: $($_.Exception.Message)"
                Finish ([PSCustomObject]@{ ok = $false; action = "install"; error = "$($_.Exception.Message)" })
            }
        }
        "start" {
            & sc.exe start zapret 2>&1 | Out-Null
            Start-Sleep -Seconds 2
            Finish ([PSCustomObject]@{ ok = $true; action = "start"; state = (Get-State) })
        }
        "stop" {
            # Службу гасим через SCM (иначе состояние "прервана"), процесс - силой.
            & sc.exe stop zapret 2>&1 | Out-Null
            $wait = [Diagnostics.Stopwatch]::StartNew()
            while ($wait.ElapsedMilliseconds -lt 8000) {
                $p = Get-Process -Name "winws" -ErrorAction SilentlyContinue
                if (-not $p) { break }
                Start-Sleep -Milliseconds 400
            }
            Get-Process -Name "winws" -ErrorAction SilentlyContinue | Stop-Process -Force
            Start-Sleep -Milliseconds 700
            Finish ([PSCustomObject]@{ ok = $true; action = "stop"; state = (Get-State) })
        }
        "remove" {
            & sc.exe stop zapret 2>&1 | Out-Null
            Start-Sleep -Milliseconds 800
            Get-Process -Name "winws" -ErrorAction SilentlyContinue | Stop-Process -Force
            & sc.exe delete zapret 2>&1 | Out-Null
            Finish ([PSCustomObject]@{ ok = $true; action = "remove"; state = (Get-State) })
        }
        default {
            Finish ([PSCustomObject]@{ ok = $false; action = $action; error = "unknown action" })
        }
    }
} catch {
    LG "fatal: $($_.Exception.Message)"
    Finish ([PSCustomObject]@{ ok = $false; action = $action; error = "$($_.Exception.Message)" })
}
LG "elapsed=$($sw.ElapsedMilliseconds)ms"
'''


def work_dir() -> Path:
    """Папка обмена с помощником: запрос, результат, журнал."""
    d = paths.app_dir() / "service"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _schtasks() -> str:
    import os
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                       "System32", "schtasks.exe")
    return exe if os.path.isfile(exe) else "schtasks.exe"


def _powershell() -> str:
    import os
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                       r"System32\WindowsPowerShell\v1.0\powershell.exe")
    return exe if os.path.isfile(exe) else "powershell.exe"


def _elevated(exe: str, params: str) -> bool:
    """Запрос прав через ShellExecuteW(runas). False - отказ или ошибка."""
    shell = ctypes.windll.shell32
    try:
        code = shell.ShellExecuteW(None, "runas", exe, params, None, 1) or 0
    except (OSError, ValueError):
        return False
    return code > 32


def write_runner() -> Path:
    """Держит помощника свежим: задача ссылается на путь, содержимое - нет."""
    runner = work_dir() / RUNNER_NAME
    try:
        runner.write_text(_RUNNER_SOURCE, encoding="utf-8", newline="\n")
    except OSError:
        pass
    return runner


def task_exists() -> bool:
    try:
        out = subprocess.run([_schtasks(), "/Query", "/TN", TASK_NAME],
                             capture_output=True, timeout=25)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def register_task() -> bool:
    """Регистрация задачи - ЕДИНСТВЕННЫЙ запрос прав за весь срок."""
    runner = write_runner()
    params = (f'-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden '
              f'-File "{runner}" -Work "{runner.parent}" -Register')
    if not _elevated(_powershell(), params):
        return False
    for _ in range(30):
        if task_exists():
            return True
        time.sleep(0.5)
    return task_exists()


def _booted(work: Path, timeout: float = 20.0) -> bool:
    """Помощник успел написать первую строку журнала - значит, запустился."""
    log = work / "runner.log"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if log.is_file() and log.stat().st_size > 0:
                return True
        except OSError:
            pass
        time.sleep(0.4)
    return False


def _read_result(work: Path, token: str, wait: float,
                 on_progress=None) -> dict | None:
    path = work / f"{_RESULT}{token}.json"
    progress_path = work / f"{_PROGRESS}{token}.json"
    deadline = time.time() + wait
    last = None
    while time.time() < deadline:
        data = _read_json(path)
        if data is not None:
            for stale in (path, progress_path):
                try:
                    stale.unlink()
                except OSError:
                    pass
            return data
        if on_progress is not None:
            cur = _read_json(progress_path)
            if cur is not None and cur != last:
                last = cur
                try:
                    on_progress(cur)
                except Exception:  # noqa: BLE001 - прогресс не должен валить ожидание
                    pass
        time.sleep(0.3)
    return None


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def run_action(action: str, payload: dict | None = None, wait: float = 60.0,
               on_progress=None, log=None) -> dict | None:
    """Выполнить операцию, требующую прав. None - запустить не удалось.

    Путь: задача есть -> schtasks /Run (без UAC); нет -> регистрация (один
    UAC); отказ от регистрации -> runas на каждый вызов (запасной путь).
    """
    work = work_dir()
    runner = write_runner()
    for stale in (list(work.glob(f"{_RESULT}*.json*"))
                  + list(work.glob(f"{_PROGRESS}*.json*"))
                  + [work / "runner.log"]):
        try:
            stale.unlink()
        except OSError:
            pass

    token = str(int(time.time() * 1000))
    req = {"action": action, "token": token}
    if payload:
        req.update(payload)
    try:
        (work / _REQUEST).write_text(
            json.dumps(req, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        if log:
            log("error", str(exc))
        return {"ok": False, "error": str(exc)}

    have_task = task_exists()
    if not have_task:
        if log:
            log("info", "регистрация служебной задачи (запрос прав)...")
        have_task = register_task()

    if have_task:
        subprocess.run([_schtasks(), "/End", "/TN", TASK_NAME],
                       capture_output=True, timeout=25)
        time.sleep(1.0)
        launched = False
        for _ in range(3):
            out = subprocess.run([_schtasks(), "/Run", "/TN", TASK_NAME],
                                 capture_output=True, timeout=30)
            if out.returncode == 0:
                launched = True
                break
            time.sleep(1.5)
        if launched and not _booted(work):
            if log:
                log("warning", "задача не стартовала, пробуем напрямую")
            launched = _elevated(
                _powershell(),
                f'-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden '
                f'-File "{runner}" -Work "{work}"')
    else:
        launched = _elevated(
            _powershell(),
            f'-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden '
            f'-File "{runner}" -Work "{work}"')

    if not launched:
        return None
    data = _read_result(work, token, wait, on_progress=on_progress)
    if data is None:
        return {"ok": False, "timeout": True}
    return data


# -- удобные обёртки -------------------------------------------------------

@dataclass
class ServiceState:
    installed: bool
    running: bool
    start_type: str
    strategy: str
    pid: int
    binpath: str

    @classmethod
    def from_dict(cls, data: dict) -> "ServiceState":
        return cls(
            installed=bool(data.get("installed")),
            running=bool(data.get("running")),
            start_type=str(data.get("start") or ""),
            strategy=str(data.get("strategy") or ""),
            pid=int(data.get("pid") or 0),
            binpath=str(data.get("binpath") or ""),
        )


def state(log=None) -> ServiceState:
    """Снимок службы; при недоступности помощника - «не установлено»."""
    res = run_action("state", wait=30, log=log)
    if res and res.get("ok") and res.get("state"):
        return ServiceState.from_dict(res["state"])
    # Быстрый путь без прав: статус читается и без помощника.
    return _state_local()


def _state_local() -> ServiceState:
    """Состояние без повышения прав - хватает для отображения в GUI."""
    import ctypes.wintypes

    try:
        out = subprocess.run(["sc", "query", "zapret"],
                             capture_output=True, timeout=15)
        text = (out.stdout or b"").decode("cp1251", "replace")
        running = "RUNNING" in text
        installed = out.returncode == 0
    except (OSError, subprocess.SubprocessError):
        running = installed = False

    label = ""
    binpath = ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"System\CurrentControlSet\Services\zapret") as key:
            try:
                label = str(winreg.QueryValueEx(key, "zapret-discord-youtube")[0])
            except OSError:
                pass
            binpath = str(winreg.QueryValueEx(key, "ImagePath")[0])
    except OSError:
        pass

    return ServiceState(installed=installed, running=running,
                        start_type="", strategy=label,
                        pid=_winws_pid(), binpath=binpath)


def _winws_pid() -> int:
    """PID winws.exe через toolhelp - без прав и без всплывающих окон."""

    class _Entry(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.wintypes.DWORD),
            ("cntUsage", ctypes.wintypes.DWORD),
            ("th32ProcessID", ctypes.wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", ctypes.wintypes.DWORD),
            ("cntThreads", ctypes.wintypes.DWORD),
            ("th32ParentProcessID", ctypes.wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == 0xFFFFFFFFFFFFFFFF:
        return 0
    try:
        entry = _Entry()
        entry.dwSize = ctypes.sizeof(_Entry)
        if not k32.Process32FirstW(snap, ctypes.byref(entry)):
            return 0
        while True:
            if entry.szExeFile.lower() == "winws.exe":
                return int(entry.th32ProcessID)
            if not k32.Process32NextW(snap, ctypes.byref(entry)):
                return 0
    finally:
        k32.CloseHandle(snap)


def install_strategy(binpath: str, strategy_label: str,
                     exe: str = "", log=None) -> dict:
    """Пересоздать службу с новым BinaryPathName и меткой стратегии."""
    return run_action("install", {"binpath": binpath,
                                  "strategy": strategy_label,
                                  "exe": exe},
                      wait=90, log=log) or {"ok": False, "error": "нет прав"}


def start(log=None) -> dict:
    return run_action("start", wait=45, log=log) or {"ok": False,
                                                     "error": "нет прав"}


def stop(log=None) -> dict:
    return run_action("stop", wait=60, log=log) or {"ok": False,
                                                    "error": "нет прав"}


def remove(log=None) -> dict:
    return run_action("remove", wait=45, log=log) or {"ok": False,
                                                      "error": "нет прав"}

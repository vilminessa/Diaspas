"""Автоподбор рабочей стратегии: перебор general*.bat одной повышенной сессией.

Идея из service.bat (12. Run Tests) и Synfronia: каждая стратегия
запускается, прогоняется по целям, результат пишется по ходу - карточка
рисует «7/12» и точки стратегий прямо во время прогона, обрыв не теряет
уже измеренное.

Отличие от Run Tests: цели - наши (Discord, YouTube, EA, Ubisoft), а не
список в targets.txt, и прогресс доступен вызывающему коду.

Перебор идёт внутри service.run_action (одна повышенная сессия): построчный
UAC на каждую стратегию был бы невыносим.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import service
# -- помощник (PowerShell) -------------------------------------------------
# Тот же контракт, что у service_runner: request.json -> result-<token>.json,
# промежуточные итоги - progress-<token>.json.
_SELECT_SOURCE = r'''
param(
    [Parameter(Mandatory = $true)][string]$Work,
    [switch]$Register
)
$ErrorActionPreference = "SilentlyContinue"
function LG($m) {
    try { Add-Content -Path (Join-Path $Work "runner.log") -Value ("{0} {1}" -f (Get-Date -Format "HH:mm:ss"), $m) -Encoding ASCII } catch { }
}
LG "select boot register=$Register"

if ($Register) {
    try {
        $ps = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
        $ps1 = Join-Path $Work "selector.ps1"
        $action = New-ScheduledTaskAction -Execute $ps -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ps1`" -Work `"$Work`""
        $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date
        $principal = New-ScheduledTaskPrincipal -UserId ("{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME) -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances Parallel -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Register-ScheduledTask -TaskName "DiaspasSelector" -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
        LG "select task registered"
    } catch { LG "select register failed: $($_.Exception.Message)"; exit 1 }
    exit 0
}

$token = "0"
$root = ""
$configs = @()
try {
    $req = Get-Content (Join-Path $Work "request.json") -Raw -Encoding UTF8 | ConvertFrom-Json
    $token = "$($req.token)"
    $root = "$($req.root)"
    $configs = @($req.configs)
    LG "select req token=$token configs=$($configs.Count)"
} catch { LG "select request failed: $($_.Exception.Message)" }

$outPath = Join-Path $Work ("result-" + $token + ".json")
$ProgressPath = Join-Path $Work ("progress-" + $token + ".json")
$CancelPath = Join-Path $Work "cancel.flag"

function Finish($Payload) {
    try {
        $tmp = "$outPath.tmp"
        $Payload | ConvertTo-Json -Depth 8 | Set-Content -Path $tmp -Encoding UTF8
        Move-Item -Path $tmp -Destination $outPath -Force
        LG "select result written"
    } catch { LG "select result failed: $($_.Exception.Message)" }
}
function Write-Progress($Payload) {
    try {
        $tmp = "$ProgressPath.tmp"
        $Payload | ConvertTo-Json -Depth 8 | Set-Content -Path $tmp -Encoding UTF8
        Move-Item -Path $tmp -Destination $ProgressPath -Force
    } catch { }
}
function Test-Cancel { return (Test-Path $CancelPath) }

$env:NO_UPDATE_CHECK = "1"

function Stop-Winws {
    Get-Process -Name "winws" -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Milliseconds 500
}

function Test-Http {
    param([string]$Url, [int]$Timeout = 5)
    $Curl = "$env:SystemRoot\System32\curl.exe"
    if (-not (Test-Path $Curl)) { $Curl = "curl.exe" }
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $code = & $Curl -sS -o NUL -m $Timeout --connect-timeout $Timeout --ssl-no-revoke -w "%{http_code}" $Url 2>$null
    $exit = $LASTEXITCODE
    $ms = [int]$sw.ElapsedMilliseconds
    $code = ("$code").Trim()
    if (($exit -eq 0) -and $code -and ($code -ne "000")) {
        return [PSCustomObject]@{ ok = $true; ms = $ms; why = "" }
    }
    $why = "timeout"
    if ($exit -eq 35 -or $exit -eq 56) { $why = "tls" }
    elseif ($exit -eq 7 -or $exit -eq 28) { $why = "conn" }
    return [PSCustomObject]@{ ok = $false; ms = $ms; why = $why }
}

# Цели - те же, что в core/probes.py: группы, а не отдельные URL, чтобы
# результат читался «Discord ok, EA fail» и совпадал с карточкой GUI.
$Targets = @(
    @{ g = "discord"; n = "web";   u = "https://discord.com/" },
    @{ g = "discord"; n = "api";   u = "https://discord.com/api/gateway" },
    @{ g = "youtube"; n = "web";   u = "https://www.youtube.com/" },
    @{ g = "ea";      n = "acc";   u = "https://accounts.ea.com/" },
    @{ g = "ea";      n = "id";    u = "https://signin.ea.com/p/ui/identity" },
    @{ g = "ubisoft"; n = "acct";  u = "https://account.ubisoft.com/" }
)

function Measure-All {
    $results = @()
    $good = 0
    foreach ($t in $Targets) {
        $r = Test-Http $t.u 5
        $results += [PSCustomObject]@{ group = $t.g; name = $t.n; ok = [bool]$r.ok; ms = [int]$r.ms; why = "$($r.why)" }
        if ($r.ok) { $good++ }
    }
    $state = "partial"
    if ($good -eq $Targets.Count) { $state = "full" } elseif ($good -eq 0) { $state = "none" }
    return [PSCustomObject]@{ state = $state; count = $good; total = $Targets.Count; targets = $results }
}

$results = @()
$done = 0
$total = $configs.Count
$best = $null
try {
    foreach ($cfg in $configs) {
        if (Test-Cancel) { LG "select cancelled at $done"; break }
        $name = "$cfg"
        $bat = Join-Path $Root $name
        Stop-Winws
        if (-not (Test-Path $bat)) {
            $results += [PSCustomObject]@{ name = $name; ok = $false; error = "missing"; state = "none"; count = 0; total = 0; targets = @() }
            $done++
            Write-Progress ([PSCustomObject]@{ done = $done; total = $total; best = $best; results = $results })
            continue
        }
        Start-Process -FilePath "cmd.exe" -ArgumentList "/c `"$bat`"" -WorkingDirectory $Root -WindowStyle Hidden | Out-Null
        $up = $false
        for ($i = 0; $i -lt 30 -and -not $up; $i++) {
            if (Get-Process -Name "winws" -ErrorAction SilentlyContinue) { $up = $true } else { Start-Sleep -Milliseconds 300 }
        }
        if ($up) {
            Get-Process -Name "winws" -ErrorAction SilentlyContinue | ForEach-Object {
                if ($_.MainWindowHandle -ne 0) {
                    Add-Type -Namespace Diasp -Name Win -MemberDefinition '[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);' -ErrorAction SilentlyContinue
                    [Diasp.Win]::ShowWindow($_.MainWindowHandle, 0) | Out-Null
                }
            }
            Start-Sleep -Milliseconds 900
            $m = Measure-All
            if ($m.state -eq "none") { Start-Sleep -Milliseconds 600; $m = Measure-All }
        } else {
            $m = [PSCustomObject]@{ state = "none"; count = 0; total = $Targets.Count; targets = @() }
        }
        $row = [PSCustomObject]@{ name = $name; ok = ($m.state -eq "full"); state = $m.state; count = $m.count; total = $m.total; targets = $m.targets; error = "" }
        $results += $row
        if ($row.ok -and (-not $best)) { $best = $name }
        $done++
        Write-Progress ([PSCustomObject]@{ done = $done; total = $total; best = $best; results = $results })
        LG "select $name -> $($m.state) $($m.count)/$($m.total)"
    }
} finally {
    Stop-Winws
}

Finish ([PSCustomObject]@{ ok = $true; action = "select"; best = $best; results = $results; done = $done; total = $total })
'''


def _work_dir() -> Path:
    d = service.work_dir()
    return d


def _write_selector() -> Path:
    path = _work_dir() / "selector.ps1"
    try:
        path.write_text(_SELECT_SOURCE, encoding="utf-8", newline="\n")
    except OSError:
        pass
    return path


def _register() -> bool:
    import ctypes
    import os
    import subprocess

    runner = _write_selector()
    ps = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                      r"System32\WindowsPowerShell\v1.0\powershell.exe")
    params = (f'-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden '
              f'-File "{runner}" -Work "{runner.parent}" -Register')
    shell = ctypes.windll.shell32
    try:
        code = shell.ShellExecuteW(None, "runas", ps, params, None, 1) or 0
    except (OSError, ValueError):
        return False
    if code <= 32:
        return False
    schtasks = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                            "System32", "schtasks.exe")
    for _ in range(30):
        try:
            out = subprocess.run([schtasks, "/Query", "/TN", "DiaspasSelector"],
                                 capture_output=True, timeout=25)
            if out.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            break
        time.sleep(0.5)
    return False


def run(root, strategies: list[str] | None = None, log=None,
        on_progress=None, wait: float = 600.0) -> dict | None:
    """Перебрать стратегии, вернуть {ok, best, results} или None (нет прав).

    strategies - имена файлов; по умолчанию все general*.bat из root.
    on_progress(dict) получает промежуточные итоги: done/total/best/results.
    """
    import ctypes
    import os
    import subprocess

    from .paths import strategy_files as _files

    root = Path(root)
    names = strategies if strategies is not None else _files(root)
    if not names:
        return {"ok": False, "error": "в папке нет general*.bat"}

    work = _work_dir()
    runner = _write_selector()
    for stale in (list(work.glob("result-*.json*"))
                  + list(work.glob("progress-*.json*"))
                  + [work / "cancel.flag", work / "runner.log"]):
        try:
            stale.unlink()
        except OSError:
            pass

    token = str(int(time.time() * 1000))
    req = {"action": "select", "token": token, "root": str(root),
           "configs": names}
    try:
        (work / "request.json").write_text(
            json.dumps(req, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        if log:
            log("error", str(exc))
        return {"ok": False, "error": str(exc)}

    schtasks = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                            "System32", "schtasks.exe")

    def task_ok() -> bool:
        try:
            out = subprocess.run([schtasks, "/Query", "/TN", "DiaspasSelector"],
                                 capture_output=True, timeout=25)
            return out.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    have_task = task_ok()
    if not have_task:
        if log:
            log("info", "регистрация задачи перебора (запрос прав)...")
        have_task = _register()
    if not have_task:
        # запасной путь: одна повышенная сессия на весь перебор
        ps = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                          r"System32\WindowsPowerShell\v1.0\powershell.exe")
        params = (f'-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden '
                  f'-File "{runner}" -Work "{work}"')
        shell = ctypes.windll.shell32
        try:
            launched = (shell.ShellExecuteW(None, "runas", ps, params, None, 1)
                        or 0) > 32
        except (OSError, ValueError):
            launched = False
        if not launched:
            return None
    else:
        subprocess.run([schtasks, "/End", "/TN", "DiaspasSelector"],
                       capture_output=True, timeout=25)
        time.sleep(1.0)
        started = False
        for _ in range(3):
            out = subprocess.run([schtasks, "/Run", "/TN", "DiaspasSelector"],
                                 capture_output=True, timeout=30)
            if out.returncode == 0:
                started = True
                break
            time.sleep(1.5)
        if not started:
            return {"ok": False, "error": "задача перебора не стартовала"}

    # Перебор гасит winws в конце (finally помощника) - служба уходит в
    # STOPPED. Запоминаем состояние, чтобы не оставить ПК без обхода.
    was_running = service.state().running

    return _wait_result(work, token, wait, log=log, on_progress=on_progress,
                        restore=was_running)


def cancel() -> None:
    """Попросить помощник остановиться между стратегиями."""
    flag = _work_dir() / "cancel.flag"
    try:
        flag.write_text("1", encoding="ascii")
    except OSError:
        pass


def _wait_result(work: Path, token: str, wait: float, log=None,
                 on_progress=None, restore: bool = False) -> dict | None:
    path = work / f"result-{token}.json"
    progress_path = work / f"progress-{token}.json"
    deadline = time.time() + wait
    last = None
    try:
        while time.time() < deadline:
            data = _read(path)
            if data is not None:
                for stale in (path, progress_path):
                    try:
                        stale.unlink()
                    except OSError:
                        pass
                if log:
                    log("info", f"перебор завершён: лучшая {data.get('best')!r}")
                return data
            if on_progress is not None:
                cur = _read(progress_path)
                if cur is not None and cur != last:
                    last = cur
                    try:
                        on_progress(cur)
                    except Exception:  # noqa: BLE001
                        pass
            time.sleep(0.3)
        if log:
            log("error", "перебор не уложился в отведённое время")
        return None
    finally:
        # Всегда (успех, таймаут, исключение) возвращаем обход, если он был
        if restore and not service.state().running:
            if log:
                log("info", "восстановление службы после перебора")
            service.start(log=log)


def _read(path: Path):
    # utf-8-sig: PowerShell 5.1 пишет UTF-8 с BOM (см. core/service._read_json)
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def summarize(result: dict, root) -> list[dict]:
    """Строки для таблицы: имя, метка, состояние, счёт, цели группами."""
    from .paths import strategy_label as _lbl
    rows = []
    for row in result.get("results") or []:
        targets = row.get("targets") or []
        groups: dict[str, str] = {}
        for t in targets:
            g = str(t.get("group") or "?")
            if not t.get("ok"):
                groups[g] = "-"          # один сбой - вся группа сбой
            elif groups.get(g) != "-":
                groups[g] = "+"
        rows.append({
            "file": row.get("name"),
            "label": _lbl(str(row.get("name") or "")),
            "state": row.get("state"),
            "count": int(row.get("count") or 0),
            "total": int(row.get("total") or 0),
            "groups": groups,
            "best": row.get("name") == result.get("best"),
        })
    return rows

"""Автоцикл смены стратегии: cooldown, порядок кандидатов, применение победителя.

Сеть, права и перебор стратегий подменены: проверяется решение (запускать
ли перебор, что применять, когда молчать), а не PowerShell-помощник.
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import autoswitch, autoselect, paths, presets as pm, probes, service, strategies as sm  # noqa: E402
from core.service import ServiceState  # noqa: E402

RUNNING = ServiceState(installed=True, running=True, start_type="Automatic",
                       strategy="general (ALT11)", pid=1, binpath="")
STOPPED = ServiceState(installed=True, running=False, start_type="Automatic",
                       strategy="general (ALT11)", pid=0, binpath="")


def make_root(tmp_path: Path) -> Path:
    (tmp_path / "lists").mkdir()
    for name in ("general (ALT).bat", "general (ALT11).bat",
                 "general (ALT12).bat", "general (ALT7).bat", "general.bat"):
        (tmp_path / name).write_text(
            'start /min "%BIN%winws.exe" --wf-tcp=80\r\n', encoding="utf-8")
    (tmp_path / "service.bat").write_text("@echo off\r\n", encoding="utf-8")
    return tmp_path


def report(state: str, count: int = 3, total: int = 11) -> dict:
    return {"state": state, "ok": state == "full", "count": count,
            "total": total, "groups": {}}


@pytest.fixture()
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    return tmp_path


def fake_env(monkeypatch, select_result=None, no_uac=False, state=RUNNING,
             probe=report("partial"), apply_result=None):
    """Заменяет пробы, перебор и применение; возвращает список вызовов."""
    calls: dict = {"select": [], "apply": []}
    monkeypatch.setattr(service, "state", lambda log=None: state)
    monkeypatch.setattr(probes, "probe_all", lambda timeout=6.0: probe)

    def fake_run(root, strategies=None, log=None, on_progress=None,
                 wait=600.0, stop_on_full=False):
        calls["select"].append({"strategies": strategies,
                                "stop_on_full": stop_on_full})
        if no_uac:
            return None
        if select_result is not None:
            return select_result
        return {"ok": True, "best": None, "results": [], "done": 0, "total": 0}

    monkeypatch.setattr(autoselect, "run", fake_run)

    def fake_apply(root, file, base_preset=None, mode_value=sm.MANUAL, log=None):
        calls["apply"].append({"file": file, "mode": mode_value})
        if apply_result is not None:
            return apply_result
        sm.remember(file, mode_value)   # как в реальном apply
        return {"ok": True, "strategy": file,
                "label": paths.strategy_label(file), "mode": mode_value,
                "state": {}}

    monkeypatch.setattr(sm, "apply", fake_apply)
    return calls


def select_best(name: str) -> dict:
    return {"ok": True, "best": name, "results": [{"name": name,
                                                  "state": "full"}],
            "done": 1, "total": 2}


# -- решение о запуске -----------------------------------------------------

def test_manual_mode_does_not_run(tmp_path, isolated_settings, monkeypatch):
    root = make_root(tmp_path)
    calls = fake_env(monkeypatch, select_result=select_best("general (ALT12).bat"))
    res = autoswitch.run(root)
    assert res["ok"] is True and res["reason"] == "off"
    assert calls["select"] == []


def test_full_probes_skip_selection(tmp_path, isolated_settings, monkeypatch):
    root = make_root(tmp_path)
    sm.set_mode(sm.AUTO)
    calls = fake_env(monkeypatch, probe=report("full", 11, 11))
    res = autoswitch.run(root, report=report("full", 11, 11))
    assert res["reason"] == "full"
    assert calls["select"] == []
    assert autoswitch.last_run() == 0   # пауза ставится только на реальный перебор


def test_preset_off_skips_selection(tmp_path, isolated_settings, monkeypatch):
    root = make_root(tmp_path)
    sm.set_mode(sm.AUTO)
    pm.remember_active("off")
    calls = fake_env(monkeypatch)
    assert autoswitch.run(root)["reason"] == "preset_off"
    assert calls["select"] == []


# -- перебор и применение --------------------------------------------------

def test_partial_probes_apply_best(tmp_path, isolated_settings, monkeypatch):
    root = make_root(tmp_path)
    sm.set_mode(sm.AUTO)
    calls = fake_env(monkeypatch,
                     select_result=select_best("general (ALT12).bat"),
                     probe=report("partial"))
    res = autoswitch.run(root)
    assert res["ok"] is True and res["applied"] is True
    assert res["best"] == "general (ALT12).bat"
    assert res["label"] == "ALT12"

    assert calls["select"][0]["stop_on_full"] is True
    # известные рабочие кандидаты первыми: авто не ждёт полного рейтинга
    assert calls["select"][0]["strategies"][:2] == [
        "general (ALT11).bat", "general (ALT12).bat"]
    assert calls["apply"][0] == {"file": "general (ALT12).bat",
                                 "mode": sm.AUTO}
    assert autoswitch.last_run() > 0
    assert autoswitch.last_strategy() == "general (ALT12).bat"
    assert paths.read_settings()["active_strategy"] == "general (ALT12).bat"


def test_best_already_active_no_reinstall(tmp_path, isolated_settings,
                                          monkeypatch):
    """Перебор подтвердил текущую стратегию - службу не пересоздаем."""
    root = make_root(tmp_path)
    sm.set_mode(sm.AUTO)
    calls = fake_env(monkeypatch, select_result=select_best("general (ALT11).bat"))
    res = autoswitch.run(root)
    assert res["applied"] is False and res["reason"] == "already"
    assert calls["apply"] == []


def test_no_full_strategy_keeps_current(tmp_path, isolated_settings,
                                        monkeypatch):
    root = make_root(tmp_path)
    sm.set_mode(sm.AUTO)
    calls = fake_env(monkeypatch)   # best=None
    res = autoswitch.run(root)
    assert res["ok"] is True and res["applied"] is False
    assert res["reason"] == "none"
    assert calls["apply"] == []
    assert "active_strategy" not in paths.read_settings()


def test_apply_failure_reports_error(tmp_path, isolated_settings, monkeypatch):
    root = make_root(tmp_path)
    sm.set_mode(sm.AUTO)
    fake_env(monkeypatch, select_result=select_best("general (ALT7).bat"),
             apply_result={"ok": False, "error": "нет прав"})
    res = autoswitch.run(root)
    assert res["ok"] is False and res["reason"] == "apply_failed"


# -- cooldown --------------------------------------------------------------

def test_cooldown_blocks_repeat(tmp_path, isolated_settings, monkeypatch):
    root = make_root(tmp_path)
    sm.set_mode(sm.AUTO)
    calls = fake_env(monkeypatch,
                     select_result=select_best("general (ALT12).bat"))
    autoswitch.run(root)
    res = autoswitch.run(root, report=report("partial"))
    assert res["reason"] == "cooldown"
    assert res["cooldown_left"] > 0
    assert len(calls["select"]) == 1


def test_force_skips_cooldown(tmp_path, isolated_settings, monkeypatch):
    root = make_root(tmp_path)
    sm.set_mode(sm.AUTO)
    calls = fake_env(monkeypatch,
                     select_result=select_best("general (ALT12).bat"))
    autoswitch.run(root)
    res = autoswitch.run(root, report=report("partial"), force=True)
    assert res["applied"] is True
    assert len(calls["select"]) == 2


def test_expired_cooldown_runs_again(tmp_path, isolated_settings, monkeypatch):
    root = make_root(tmp_path)
    sm.set_mode(sm.AUTO)
    calls = fake_env(monkeypatch,
                     select_result=select_best("general (ALT12).bat"))
    # подбор был давно (за пределом паузы) - cooldown не мешает
    settings = paths.read_settings()
    settings["last_auto_at"] = int(time.time() - autoswitch.COOLDOWN_SECONDS - 5)
    paths.write_settings(settings)
    assert autoswitch.cooldown_left() == 0
    assert autoswitch.run(root, report=report("partial"))["applied"] is True
    assert len(calls["select"]) == 1


# -- отказ прав ------------------------------------------------------------

def test_no_uac_marks_cooldown(tmp_path, isolated_settings, monkeypatch):
    """Отказ UAC не должен провоцировать повторный запрос на каждой проверке."""
    root = make_root(tmp_path)
    sm.set_mode(sm.AUTO)
    calls = fake_env(monkeypatch, no_uac=True)
    res = autoswitch.run(root)
    assert res["ok"] is False and res["reason"] == "no_uac"
    assert autoswitch.last_run() > 0
    assert calls["apply"] == []


# -- порядок кандидатов ----------------------------------------------------

def test_order_prefers_known_candidates(tmp_path):
    root = make_root(tmp_path)
    names = autoswitch.order(root)
    assert names[:2] == ["general (ALT11).bat", "general (ALT12).bat"]
    assert set(names) == {"general (ALT).bat", "general (ALT11).bat",
                          "general (ALT12).bat", "general (ALT7).bat",
                          "general.bat"}


def test_order_survives_missing_candidates(tmp_path):
    """Релиз без ALT11/ALT12: порядок остаётся естественным."""
    files = ["general (ALT).bat", "general (ALT7).bat"]
    assert autoswitch.order(None, files) == files


def test_stopped_service_is_not_treated_as_active(tmp_path,
                                                  isolated_settings,
                                                  monkeypatch):
    """Служба остановлена: 'лучшая = текущая' не повод молчать - её надо поднять."""
    root = make_root(tmp_path)
    sm.set_mode(sm.AUTO)
    calls = fake_env(monkeypatch, state=STOPPED,
                     select_result=select_best("general (ALT11).bat"))
    res = autoswitch.run(root)
    assert res["applied"] is True
    assert calls["apply"][0]["file"] == "general (ALT11).bat"

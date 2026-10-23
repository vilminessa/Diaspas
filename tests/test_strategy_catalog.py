"""Каталог стратегий, проверка применения и перенос связки.

Регрессии:
* выбор стратегии был «вслепую»: в UI только имя файла, разобранные
  аргументы winws нигде не показывались;
* применение стратегии могло убрать обход совсем - без подтверждения
  «до/после» пользователь оставался с неработающей службой;
* «попробовать рекомендацию перебора» требовало пересоздать службу
  навсегда, хотя оценка возможна и с возвратом прежнего состояния;
* связку пресета нельзя было забрать на другую машину.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import paths, presets as pm, service, strategies as sm  # noqa: E402
from core.presets import Preset  # noqa: E402
from test_cmdparser_layout import make_winws_install  # noqa: E402


def make_flowseal(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "service.bat").write_text("@echo off\r\n", encoding="utf-8")
    (root / "bin").mkdir(exist_ok=True)
    (root / "bin" / "winws.exe").write_bytes(b"MZ")
    (root / "lists").mkdir(exist_ok=True)
    (root / "utils").mkdir(exist_ok=True)
    for name, body in files.items():
        (root / name).write_text(body, encoding="utf-8")
    return root


BAT_ALT11 = ('start /min "%BIN%winws.exe" --wf-tcp=80,443 --wf-udp=443 '
             '--dpi-desync=fake --dpi-desync-fake-tls=0x0 '
             '--hostlist="%LISTS%list-custom.txt" --new\r\n')
BAT_ALT7 = ('start /min "%BIN%winws.exe" --wf-tcp=80,443 '
            '--dpi-desync=splitsplitfake --dpi-desync-fooling=md5sig '
            '--new\r\n')


# -- разбор флагов ----------------------------------------------------------

def test_split_flags_keeps_quoted_values():
    flags = sm.split_flags(' --wf-tcp=80,443 --hostlist="C:\\lists a\\x.txt" --new')
    assert ("--wf-tcp", "80,443") in flags
    assert ("--new", "") in flags
    assert ('--hostlist', '"C:\\lists a\\x.txt"') in flags


def test_describe_flowseal_lists_headline_flags(tmp_path):
    root = make_flowseal(tmp_path / "f", {"general (ALT11).bat": BAT_ALT11})
    d = sm.describe(root, "general (ALT11).bat")
    assert d["ok"] is True
    assert d["layout"] == "flowseal"
    assert d["features"]["wf_tcp"] == "80,443"
    assert d["features"]["desync"] == "fake"
    assert d["features"]["hostlist"] is True
    assert "--dpi-desync=fake" in d["headline"]


def test_describe_winws_layout(tmp_path):
    root = make_winws_install(tmp_path / "win")
    d = sm.describe(root, "preset1.cmd")
    assert d["ok"] is True and d["layout"] == "winws"
    assert d["features"]["wf_tcp"] == "80,443"


def test_describe_broken_file_is_an_answer(tmp_path):
    """Битый файл не должен ронять карточку: причина приходит в ok=False."""
    root = make_flowseal(tmp_path / "f", {"general (BAD).bat": "echo off\r\n"})
    d = sm.describe(root, "general (BAD).bat")
    assert d["ok"] is False and d["error"]
    d2 = sm.describe(root, "general (НЕТ).bat")
    assert d2["ok"] is False and "не найден" in d2["error"]


def test_diff_added_removed_changed(tmp_path):
    root = make_flowseal(tmp_path, {"general (ALT11).bat": BAT_ALT11,
                                    "general (ALT7).bat": BAT_ALT7})
    a = sm.describe(root, "general (ALT11).bat")
    b = sm.describe(root, "general (ALT7).bat")
    d = sm.diff(a, b)
    assert d["ok"] and not d["same"]
    assert any("--dpi-desync" in c for c in d["changed"]), d
    assert any("dpi-desync-fooling" in x for x in d["added"]), d
    assert any("fake-tls" in x or "hostlist" in x for x in d["removed"]), d


def test_diff_same_strategy_no_churn(tmp_path):
    root = make_flowseal(tmp_path, {"general (ALT11).bat": BAT_ALT11})
    a = sm.describe(root, "general (ALT11).bat")
    d = sm.diff(a, sm.describe(root, "general (ALT11).bat"))
    assert d["ok"] and d["same"]


def test_diff_without_baseline_reports_added_only(tmp_path):
    root = make_flowseal(tmp_path, {"general (ALT11).bat": BAT_ALT11})
    d = sm.diff(None, sm.describe(root, "general (ALT11).bat"))
    assert d["unknown_baseline"] is True and d["added"] and not d["removed"]


def test_same_file_ignores_extension_case():
    assert sm.same_file("general (ALT11).bat", "general (ALT11).BAT")
    assert sm.same_file("preset1.cmd", "preset1.cmd")
    assert not sm.same_file("general (ALT11).bat", "general (ALT7).bat")
    assert not sm.same_file("", "")


# -- проверка обхода при применении -----------------------------------------

class Calls:
    """Записывает, какие пресеты применялись, и отдаёт заранее известные результаты."""

    def __init__(self, results=None):
        self.applied: list[str] = []
        self.results = results or {}

    def __call__(self, preset, root, log=None):
        self.applied.append(preset.key)
        return self.results.get(preset.key, {"ok": True})


def probe_seq(*reports):
    """Раздача результатов проб по вызовам (последний повторяется)."""
    state = {"i": 0}

    def probe():
        rep = reports[min(state["i"], len(reports) - 1)]
        state["i"] += 1
        return rep
    return probe


def installed_state(monkeypatch, strategy="general (ALT11)", installed=True):
    monkeypatch.setattr(service, "state", lambda log=None: service.ServiceState(
        installed=installed, running=installed, start_type="Automatic",
        strategy=strategy, pid=1, binpath=""))


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(paths, "zapret_dir_info", lambda: (None, ""))
    return tmp_path


def test_apply_verified_rolls_back_when_bypass_dies(isolated, monkeypatch, tmp_path):
    root = make_flowseal(tmp_path / "f", {"general (ALT11).bat": BAT_ALT11,
                                          "general (ALT7).bat": BAT_ALT7})
    installed_state(monkeypatch)
    calls = Calls()
    monkeypatch.setattr(pm, "apply", calls)

    base = Preset(key="discord_youtube", title="DY", strategy="general (ALT11).bat")
    risky = Preset(key="apex_ea", title="EA", strategy="general (ALT7).bat")

    res = pm.apply_verified(risky, root, previous=base, log=None,
                            probe=probe_seq({"state": "full", "count": 3, "total": 3},
                                            {"state": "none", "count": 0, "total": 3}))
    assert res["ok"] is False and res["rolled_back"] is True
    assert calls.applied == ["apex_ea", "discord_youtube"]
    assert res["before"]["count"] == 3 and res["after"]["count"] == 0


def test_apply_verified_keeps_when_probes_cannot_prove(isolated, monkeypatch):
    """Нет сети (total=0) - пробы ничего не доказывают, откат был бы ложным."""
    installed_state(monkeypatch)
    calls = Calls()
    monkeypatch.setattr(pm, "apply", calls)
    base = Preset(key="discord_youtube", title="DY", strategy="general (ALT11).bat")
    other = Preset(key="apex_ea", title="EA", strategy="general (ALT7).bat")

    res = pm.apply_verified(other, root="/tmp", previous=base,
                            probe=probe_seq({"state": "full", "count": 2, "total": 2},
                                            {"state": "none", "count": 0, "total": 0}))
    assert res["ok"] is True and "rolled_back" not in res
    assert calls.applied == ["apex_ea"]


def test_apply_verified_keeps_improvement(isolated, monkeypatch):
    installed_state(monkeypatch)
    calls = Calls()
    monkeypatch.setattr(pm, "apply", calls)
    base = Preset(key="discord_youtube", title="DY", strategy="general (ALT11).bat")
    other = Preset(key="apex_ea", title="EA", strategy="general (ALT7).bat")
    res = pm.apply_verified(other, root="/tmp", previous=base,
                            probe=probe_seq({"state": "partial", "count": 1, "total": 3},
                                            {"state": "full", "count": 3, "total": 3}))
    assert res["ok"] is True and res["before"]["count"] == 1 and res["after"]["count"] == 3
    assert calls.applied == ["apex_ea"]


def test_apply_verified_no_baseline_skips_probe(isolated, monkeypatch):
    """Службы нет (previous=None): проверять не с чем, применяется как обычно."""
    calls = Calls()
    monkeypatch.setattr(pm, "apply", calls)
    preset = Preset(key="apex_ea", title="EA", strategy="general (ALT7).bat")
    res = pm.apply_verified(preset, root="/tmp", previous=None,
                            probe=lambda: pytest.fail("пробы не нужны"))
    assert res["ok"] is True


# -- проба без применения ---------------------------------------------------

def test_try_strategy_restores_previous(isolated, monkeypatch, tmp_path):
    root = make_flowseal(tmp_path / "f", {"general (ALT11).bat": BAT_ALT11,
                                          "general (ALT7).bat": BAT_ALT7})
    installed_state(monkeypatch)
    monkeypatch.setattr(pm, "active_preset_key", lambda: "discord_youtube")
    calls = Calls()
    monkeypatch.setattr(pm, "apply", calls)

    trial = Preset(key="discord_youtube", title="DY",
                   strategy="general (ALT7).bat")
    res = pm.try_strategy(trial, root,
                          probe=probe_seq({"state": "partial", "count": 2, "total": 3},
                                          {"state": "full", "count": 3, "total": 3}))
    assert res["ok"] and res["trial"] and res["better"] is True
    assert res["restored"] is True
    assert calls.applied == ["discord_youtube", "discord_youtube"]


def test_try_strategy_needs_installed_service(isolated, monkeypatch, tmp_path):
    root = make_flowseal(tmp_path / "f", {"general (ALT7).bat": BAT_ALT7})
    installed_state(monkeypatch, installed=False)
    trial = Preset(key="apex_ea", title="EA", strategy="general (ALT7).bat")
    res = pm.try_strategy(trial, root, probe=lambda: {"count": 0, "total": 0})
    assert res["ok"] is False and "не установлена" in res["error"]


def test_try_strategy_restores_even_on_failed_apply(isolated, monkeypatch, tmp_path):
    root = make_flowseal(tmp_path / "f", {"general (ALT11).bat": BAT_ALT11,
                                          "general (ALT7).bat": BAT_ALT7})
    installed_state(monkeypatch)
    monkeypatch.setattr(pm, "active_preset_key", lambda: "discord_youtube")
    calls = Calls(results={"apex_ea": {"ok": False, "error": "битая стратегия"},
                           "discord_youtube": {"ok": True}})
    monkeypatch.setattr(pm, "apply", calls)

    trial = Preset(key="apex_ea", title="EA", strategy="general (ALT7).bat",
                   game_filter="all")
    res = pm.try_strategy(trial, root, probe=lambda: {"count": 0, "total": 0})
    assert res["ok"] is False and res["restored"] is True
    assert calls.applied[-1] == "discord_youtube"


def test_restore_target_uses_actual_strategy_when_unrecognized(isolated, monkeypatch,
                                                              tmp_path):
    """Служба поставлена service.bat: откат обязан брать фактическую стратегию.

    Иначе «вернуть прежнюю конфигурацию» означало бы «вернуть ALT11» и
    само стало бы поломкой.
    """
    root = make_flowseal(tmp_path / "f", {"general (ALT11).bat": BAT_ALT11,
                                          "general (ALT13).bat": BAT_ALT7})
    installed_state(monkeypatch, strategy="general (ALT13)")
    monkeypatch.setattr(pm, "active_preset_key", lambda: None)
    monkeypatch.setattr(pm, "current_preset", lambda *a, **k: None)
    target = pm.restore_target(root)
    assert target is not None
    assert target.strategy == "general (ALT13).bat"


# -- перенос связки ---------------------------------------------------------

def test_export_bundle_is_roundtrippable(isolated):
    preset = Preset(key="ubisoft", title="Ubisoft", strategy="general (ALT11).bat",
                    game_filter="all", ipset="any",
                    exclude_domains=("ubisoft.com", "ubi.com"))
    bundle = pm.export_bundle(preset)
    assert bundle["format"] == 1 and bundle["exclude_domains"] == ["ubisoft.com", "ubi.com"]
    assert bundle["game_filter"] == "all"


def test_import_bundle_adds_custom_preset(isolated, monkeypatch, tmp_path):
    root = make_flowseal(tmp_path / "f", {"general (ALT11).bat": BAT_ALT11,
                                          "general (ALT7).bat": BAT_ALT7})
    use = monkeypatch
    use.setattr(paths, "zapret_dir_info", lambda: (root, "settings"))
    bundle = {"format": 1, "key": "my", "title": "Мой пресет",
              "strategy": "general (ALT7).bat", "game_filter": "all",
              "ipset": "any", "exclude_domains": ["ea.com"],
              "description": "перенесённый"}
    key = pm.import_bundle(bundle, root)
    assert key == "my"
    got = next(p for p in pm.load_presets(root) if p.key == "my")
    assert got.strategy == "general (ALT7).bat"
    assert got.exclude_domains == ("ea.com",)
    assert got.game_filter == "all"


def test_import_bundle_drops_foreign_strategy(isolated, monkeypatch, tmp_path):
    """Связка из 1.10.x в установке без ALT13 не должна оставлять битую ссылку."""
    root = make_flowseal(tmp_path / "f", {"general (ALT11).bat": BAT_ALT11})
    monkeypatch.setattr(paths, "zapret_dir_info", lambda: (root, "settings"))
    key = pm.import_bundle({"title": "Чужой", "strategy": "general (ALT13).bat"},
                           root)
    got = next(p for p in pm.load_presets(root) if p.key == key)
    assert got.strategy == "general (ALT11).bat"


def test_import_bundle_dedupes_key(isolated, monkeypatch, tmp_path):
    root = make_flowseal(tmp_path / "f", {"general (ALT11).bat": BAT_ALT11})
    monkeypatch.setattr(paths, "zapret_dir_info", lambda: (root, "settings"))
    first = pm.import_bundle({"key": "my", "title": "Первый"}, root)
    second = pm.import_bundle({"key": "my", "title": "Первый"}, root)
    assert first == "my" and second != first
    keys = [p.key for p in pm.load_presets(root)]
    assert "my" in keys and second in keys


def test_import_bundle_rejects_garbage(isolated, tmp_path):
    root = make_flowseal(tmp_path / "f", {"general (ALT11).bat": BAT_ALT11})
    with pytest.raises(ValueError):
        pm.import_bundle({"strategy": "general (ALT11).bat"}, root)
    with pytest.raises(ValueError):
        pm.import_bundle("не словарь", root)

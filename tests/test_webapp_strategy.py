"""Мост API переключения стратегий: то, чем реально пользуется окно.

Юнит-тестов на эти методы не было вовсе - они проверялись только живым
 прогоном tests/manual/live_winws_layout.py, то есть на живой службе. Здесь
 цель одна: контракт методов (что отдаётся окну при отсутствии папки,
 чужом файле стратегии, пресете «Выкл», дрейфе службы) зафиксирован так,
 чтобы UI не получал молчаливый успех там, где должен быть текст ошибки.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import paths, presets as pm, service  # noqa: E402
from ui.webapp import App  # noqa: E402


def make_install(root: Path, names=("general (ALT11).bat", "general (ALT7).bat")) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "service.bat").write_text("@echo off\r\n", encoding="utf-8")
    (root / "bin").mkdir(exist_ok=True)
    (root / "bin" / "winws.exe").write_bytes(b"MZ")
    (root / "lists").mkdir(exist_ok=True)
    (root / "utils").mkdir(exist_ok=True)
    for name in names:
        # содержимое разное: diff стратегий обязан видеть различие, а не
        # «same» из-за одинаковых файлов
        slug = name.replace("general (", "").replace(").bat", "").replace(" ", "_")
        (root / name).write_text(
            f'start /min "%BIN%winws.exe" --wf-tcp=80,443 --dpi-desync={slug}\r\n',
            encoding="utf-8")
    return root


ALT11 = service.ServiceState(installed=True, running=True, start_type="Automatic",
                             strategy="general (ALT11)", pid=1, binpath="")


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(paths, "zapret_dir_info", lambda: (None, ""))
    monkeypatch.setattr(service, "state", lambda log=None: ALT11)
    instance = App()
    instance.emitter.emit = lambda kind, payload=None: None   # без окна
    return instance


def test_strategies_without_dir_is_honest(app):
    res = app.strategies()
    assert res["ok"] is False and res["error"]


def test_strategies_reports_current_and_target(app, tmp_path, monkeypatch):
    root = make_install(tmp_path / "zap")
    app.set_install(root, "settings")
    res = app.strategies()
    assert res["ok"] is True
    assert "general (ALT11).bat" in res["items"] and "general (ALT7).bat" in res["items"]
    assert res["current_strategy"] == "general (ALT11).bat", "подсветка = факт службы"
    assert res["layout"] == "flowseal"
    assert res["target_preset"], "цель привязки обязана быть даже без маркера"


def test_strategies_prefers_marker_then_inference(app, tmp_path, monkeypatch):
    root = make_install(tmp_path / "zap")
    app.set_install(root, "settings")
    monkeypatch.setattr(pm, "active_preset_key", lambda: "apex_ea")
    assert app.strategies()["target_preset"] == "apex_ea"

    monkeypatch.setattr(pm, "active_preset_key", lambda: None)
    monkeypatch.setattr(pm, "last_known_preset_key", lambda: "ubisoft")
    monkeypatch.setattr(pm, "current_preset", lambda *a, **k: None)
    assert app.strategies()["target_preset"] == "ubisoft"

    monkeypatch.setattr(pm, "last_known_preset_key", lambda: None)
    got = app.strategies()["target_preset"]
    assert got in [p.key for p in pm.DEFAULT_PRESETS if not p.off]


def test_strategies_when_service_unrecognized_still_lists(app, tmp_path):
    root = make_install(tmp_path / "zap")
    app.set_install(root, "settings")
    # служба поставлена service.bat: метка чужая, распознавания нет
    assert app.strategies()["ok"] is True


def test_set_preset_strategy_validates(app, tmp_path):
    root = make_install(tmp_path / "zap")
    app.set_install(root, "settings")

    assert app.set_preset_strategy("нет_такого", "general (ALT7).bat")["ok"] is False
    assert app.set_preset_strategy("discord_youtube", "general (ALT99).bat")["ok"] is False
    assert app.set_preset_strategy("off", "general (ALT7).bat")["ok"] is False

    ok = app.set_preset_strategy("discord_youtube", "general (ALT7).bat")
    assert ok["ok"] is True and ok["strategy_label"] == "ALT7"
    assert pm.strategy_override("discord_youtube", root) == "general (ALT7).bat"


def test_set_preset_strategy_reset_to_default(app, tmp_path):
    root = make_install(tmp_path / "zap")
    app.set_install(root, "settings")
    app.set_preset_strategy("apex_ea", "general (ALT7).bat")
    res = app.set_preset_strategy("apex_ea", "")
    assert res["ok"] is True
    assert pm.strategy_override("apex_ea", root) == ""


def test_apply_preset_with_strategy_stores_then_applies(app, tmp_path, monkeypatch):
    root = make_install(tmp_path / "zap")
    app.set_install(root, "settings")
    applied: list[pm.Preset] = []
    monkeypatch.setattr(pm, "apply",
                        lambda preset, r, log=None: applied.append(preset) or {"ok": True})

    res = app.apply_preset("apex_ea", "general (ALT7).bat")
    assert res["ok"] is True
    assert applied and applied[0].strategy == "general (ALT7).bat"
    assert pm.strategy_override("apex_ea", root) == "general (ALT7).bat"


def test_apply_preset_rejects_foreign_strategy_before_applying(app, tmp_path, monkeypatch):
    root = make_install(tmp_path / "zap")
    app.set_install(root, "settings")
    calls: list[str] = []
    monkeypatch.setattr(pm, "apply",
                        lambda preset, r, log=None: calls.append(preset.key) or {"ok": True})
    res = app.apply_preset("apex_ea", "general (ALT99).bat")
    assert res["ok"] is False
    assert calls == [], "службу нельзя переустанавливать из-за битого имени"


def test_apply_preset_verify_uses_verified_path(app, tmp_path, monkeypatch):
    root = make_install(tmp_path / "zap")
    app.set_install(root, "settings")
    verified: list[str] = []
    plain: list[str] = []
    monkeypatch.setattr(pm, "apply_verified",
                        lambda preset, r, log=None, **kw: verified.append(preset.key) or {"ok": True})
    monkeypatch.setattr(pm, "apply",
                        lambda preset, r, log=None: plain.append(preset.key) or {"ok": True})

    app.apply_preset("apex_ea", verify=True)
    app.apply_preset("apex_ea")
    assert verified == ["apex_ea"] and plain == ["apex_ea"]
    # off-пресет не проверяет пробами: службы нет, сравнивать не с чем
    app.apply_preset("off", verify=True)
    assert "off" not in verified


def test_strategy_detail_shows_args_and_diff(app, tmp_path):
    root = make_install(tmp_path / "zap")
    app.set_install(root, "settings")
    res = app.strategy_detail("general (ALT7).bat")
    assert res["ok"] is True
    assert res["detail"]["ok"] is True and res["detail"]["features"]["wf_tcp"] == "80,443"
    assert res["current"] == "general (ALT11).bat"
    assert res["diff"]["ok"] is True and not res["diff"]["same"]


def test_strategy_detail_broken_file_is_answer_not_crash(app, tmp_path):
    root = make_install(tmp_path / "zap")
    (root / "general (BAD).bat").write_text("echo off\r\n", encoding="utf-8")
    app.set_install(root, "settings")
    res = app.strategy_detail("general (BAD).bat")
    assert res["ok"] is True and res["detail"]["ok"] is False and res["detail"]["error"]


def test_try_strategy_validation_and_delegation(app, tmp_path, monkeypatch):
    root = make_install(tmp_path / "zap")
    app.set_install(root, "settings")
    seen: list[tuple[str, str]] = []

    def fake_try(preset, r, log=None, probe=None):
        seen.append((preset.key, preset.strategy))
        return {"ok": True, "trial": True, "better": True,
                "before": {"count": 1, "total": 3}, "after": {"count": 3, "total": 3},
                "restored": True}

    monkeypatch.setattr(pm, "try_strategy", fake_try)

    assert app.try_strategy("нет_такого")["ok"] is False
    assert app.try_strategy("off")["ok"] is False
    assert app.try_strategy("apex_ea", "general (ALT99).bat")["ok"] is False

    res = app.try_strategy("apex_ea", "general (ALT7).bat")
    assert res["ok"] and res["better"]
    assert seen == [("apex_ea", "general (ALT7).bat")]
    # проба не пишет выбор: «попробовать» не равно «применить»
    assert pm.strategy_override("apex_ea", root) == ""


def test_try_strategy_without_dir(app):
    assert app.try_strategy("apex_ea")["ok"] is False


def test_export_import_roundtrip(app, tmp_path, monkeypatch):
    root = make_install(tmp_path / "zap")
    app.set_install(root, "settings")
    exported = app.export_preset("ubisoft")
    assert exported["ok"] is True
    bundle = json.loads(exported["json"])
    assert bundle["title"] == "Ubisoft" and "ubisoft.com" in bundle["exclude_domains"]

    res = app.import_preset(exported["json"])
    assert res["ok"] is True and res["key"] != "ubisoft", "ключ не должен перезаписывать пресет"
    got = next(p for p in pm.load_presets(root) if p.key == res["key"])
    assert got.title == "Ubisoft"
    assert got.exclude_domains == tuple(bundle["exclude_domains"])
    assert got.game_filter == bundle["game_filter"]


def test_import_preset_rejects_garbage(app, tmp_path):
    root = make_install(tmp_path / "zap")
    app.set_install(root, "settings")
    assert app.import_preset("{ не json")["ok"] is False
    assert app.import_preset({"no_title": 1})["ok"] is False


def test_export_requires_dir(app):
    assert app.export_preset("ubisoft")["ok"] is False

"""Семантика состояния и замыкание цикла «релиз -> пресет -> служба».

Регрессия: после скачивания релиза окно показывало «Установка не найдена»
(это был заголовок отсутствия СЛУЖБЫ, а папка релиза была на месте) -
пользователь решил, что сломалось скачивание. Теперь:
* state различает «нет папки» и «нет службы», а папка+нет службы даёт
  понятный заголовок и кнопку «Создать службу»;
* install_release возвращает needs_preset, чтобы фронтенд предложил
  применить пресет сразу после скачивания;
* пропажа службы при целой папке пишется в журнал (один раз на сессию).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import autoswitch, paths, presets as presets_mod, release, service, strategies as strategies_mod  # noqa: E402
from ui.webapp import App  # noqa: E402

DIR = Path("C:/zapret/zapret-discord-youtube-1.10.3")


def make_zapret_root(tmp_path: Path, names=("general (ALT11).bat",
                                            "general (ALT7).bat")) -> Path:
    """Папка установки со стратегиями: список и сопоставление метки реестра."""
    root = tmp_path / "zapret"
    root.mkdir(exist_ok=True)
    for name in names:
        (root / name).write_text('start /min "%BIN%winws.exe" --wf-tcp=80\r\n',
                                 encoding="utf-8")
    return root


def make_app(tmp_path, monkeypatch, installed=True, root=DIR):
    """App с изолированными настройками и подменённым состоянием службы."""
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    # маркер «последний применённый пресет» - реалистичный фон для тестов
    paths.write_settings({"active_preset": "ubisoft"})
    st = service.ServiceState(installed=installed, running=installed,
                              start_type="Automatic" if installed else "",
                              strategy="general (ALT11)" if installed else "",
                              pid=1234 if installed else 0, binpath="")
    monkeypatch.setattr(service, "state", lambda log=None: st)
    app = App()
    app.zapret_root = root
    return app, st


def test_state_distinguishes_dir_and_service(tmp_path, monkeypatch):
    """Папка на месте, службы нет -> dir_ok=True, installed=False."""
    app, _ = make_app(tmp_path, monkeypatch, installed=False)
    st = app.state()
    assert st["dir_ok"] is True
    assert st["installed"] is False
    assert st["suggested_preset"]  # маркер подсказывает, что применить


def test_state_no_dir(tmp_path, monkeypatch):
    app, _ = make_app(tmp_path, monkeypatch, installed=False, root=None)
    st = app.state()
    assert st["dir_ok"] is False
    assert st["installed"] is False
    assert st["zapret_dir"] is None


def test_state_all_good(tmp_path, monkeypatch):
    app, _ = make_app(tmp_path, monkeypatch, installed=True)
    st = app.state()
    assert st["dir_ok"] is True and st["installed"] is True


def test_missing_service_warned_once(tmp_path, monkeypatch):
    """Пропажа службы при целой папке - warning в журнал, но без спама."""
    app, _ = make_app(tmp_path, monkeypatch, installed=False)
    warnings: list = []
    app.log = lambda lvl, txt: warnings.append((lvl, txt))  # type: ignore
    app.state()
    app.state()
    app.state()
    warns = [w for w in warnings if "службы zapret нет" in w[1]]
    assert len(warns) == 1, f"предупреждений: {len(warns)}"


def test_install_release_reports_needs_preset(tmp_path, monkeypatch):
    """После установки релиза без службы фронт получает needs_preset."""
    app, _ = make_app(tmp_path, monkeypatch, installed=False)
    app.zapret_root = None   # до установки папки ещё нет
    rel_root = tmp_path / "zapret" / "zapret-discord-youtube-9.9.9"

    monkeypatch.setattr(release, "latest_release",
                        lambda: release.Release(tag="v9.9.9", name="9.9.9",
                                                zip_url="http://x",
                                                published=""))
    monkeypatch.setattr(release, "download",
                        lambda rel, progress=None: tmp_path / "r.zip")
    monkeypatch.setattr(release, "install",
                        lambda z, d, progress=None: rel_root)
    monkeypatch.setattr(paths, "read_settings", lambda: {})
    written = {}
    monkeypatch.setattr(paths, "write_settings",
                        lambda data: written.update(data))
    monkeypatch.setattr(presets_mod, "active_preset_key", lambda: "ubisoft")

    res = app.install_release()
    assert res["ok"] is True
    assert res["needs_preset"] is True
    assert res.get("active_preset") == "ubisoft"
    assert written.get("zapret_dir") == str(rel_root)
    assert written.get("zapret_release") == "v9.9.9"
    # служба действительно не создана - папка лишь распакована
    assert (rel_root / "lists" / "list-exclude-user.txt").is_file()


def test_install_release_no_offer_when_service_exists(tmp_path, monkeypatch):
    """Служба уже стоит (обновление релиза) - предложение не мешает."""
    app, _ = make_app(tmp_path, monkeypatch, installed=True)
    rel_root = tmp_path / "zapret" / "zapret-discord-youtube-9.9.9"
    monkeypatch.setattr(release, "latest_release",
                        lambda: release.Release(tag="v9.9.9", name="9.9.9",
                                                zip_url="http://x",
                                                published=""))
    monkeypatch.setattr(release, "download",
                        lambda rel, progress=None: tmp_path / "r.zip")
    monkeypatch.setattr(release, "install",
                        lambda z, d, progress=None: rel_root)
    monkeypatch.setattr(paths, "read_settings", lambda: {})
    monkeypatch.setattr(paths, "write_settings", lambda data: None)

    res = app.install_release()
    assert res["ok"] is True
    assert res["needs_preset"] is False
    assert "active_preset" not in res


# -- смена стратегии -------------------------------------------------------

def test_state_reports_strategy_controls(tmp_path, monkeypatch):
    """state отдаёт список стратегий, effective-метку и режим."""
    app, _ = make_app(tmp_path, monkeypatch, installed=True)
    app.zapret_root = make_zapret_root(tmp_path)
    st = app.state()
    assert st["strategy_file"] == "general (ALT11).bat"
    assert st["strategy_label"] == "ALT11"
    assert st["strategy_mode"] == "manual"
    assert st["auto_cooldown_left"] == 0
    assert any(s["active"] for s in st["strategies"])
    assert {s["file"] for s in st["strategies"]} == {
        "general (ALT11).bat", "general (ALT7).bat"}


def test_presets_report_effective_strategy(tmp_path, monkeypatch):
    """Выбранная вручную стратегия показывается в пресетах, default - рядом."""
    app, _ = make_app(tmp_path, monkeypatch)
    app.zapret_root = make_zapret_root(tmp_path)
    strategies_mod.remember("general (ALT7).bat")
    items = app.presets()
    base = next(p for p in items if p["key"] == "discord_youtube")
    assert base["strategy"] == "general (ALT7).bat"
    assert base["default_strategy"] == "general (ALT11).bat"
    assert next(p for p in items if p["key"] == "off")["strategy"] == ""


def test_apply_strategy_delegates_and_emits_state(tmp_path, monkeypatch):
    app, _ = make_app(tmp_path, monkeypatch)
    app.zapret_root = make_zapret_root(tmp_path)
    seen: dict = {}

    def fake_apply(root, file, **kwargs):
        seen.update({"root": root, "file": file})
        return {"ok": True, "strategy": file, "label": "ALT7"}

    monkeypatch.setattr(strategies_mod, "apply", fake_apply)
    events: list = []
    monkeypatch.setattr(app.emitter, "emit",
                        lambda kind, payload=None: events.append(kind))
    res = app.apply_strategy("general (ALT7).bat")
    assert res["ok"] is True
    assert seen["file"] == "general (ALT7).bat"
    assert "state" in events


def test_apply_strategy_without_dir(tmp_path, monkeypatch):
    app, _ = make_app(tmp_path, monkeypatch, installed=False, root=None)
    assert app.apply_strategy("general (ALT7).bat")["ok"] is False


def test_set_strategy_mode_persists(tmp_path, monkeypatch):
    app, _ = make_app(tmp_path, monkeypatch)
    assert app.set_strategy_mode("auto")["ok"] is True
    assert app.state()["strategy_mode"] == "auto"
    assert app.set_strategy_mode("weird")["ok"] is False
    assert app.state()["strategy_mode"] == "auto"
    assert app.set_strategy_mode("manual")["ok"] is True


def test_auto_run_delegates_and_emits(tmp_path, monkeypatch):
    app, _ = make_app(tmp_path, monkeypatch)
    app.zapret_root = make_zapret_root(tmp_path)
    seen: dict = {}

    def fake_run(root, report=None, force=False, log=None,
                 on_progress=None, wait=900.0):
        seen["force"] = force
        if on_progress:
            on_progress({"done": 1, "total": 2, "best": "general (ALT12).bat",
                         "results": []})
        return {"ok": True, "ran": True, "applied": True,
                "best": "general (ALT12).bat", "label": "ALT12",
                "reason": "applied",
                "probes": {"state": "full", "count": 11, "total": 11,
                           "groups": {}},
                "results": [{"name": "general (ALT12).bat", "state": "full",
                             "count": 11, "total": 11, "targets": []}]}

    monkeypatch.setattr(autoswitch, "run", fake_run)
    events: list = []
    monkeypatch.setattr(app.emitter, "emit",
                        lambda kind, payload=None: events.append((kind, payload)))

    res = app.auto_run(force=True)
    assert res["applied"] is True
    assert seen["force"] is True
    kinds = [kind for kind, _ in events]
    assert "probes" in kinds and "state" in kinds
    progress = next(p for kind, p in events if kind == "select_progress")
    assert progress["auto"] is True
    assert app.last_probes is not None


def test_auto_run_reentrant_guard(tmp_path, monkeypatch):
    app, _ = make_app(tmp_path, monkeypatch)
    app._auto_running = True
    assert app.auto_run()["ok"] is False


def test_version_reports_build_label(tmp_path, monkeypatch):
    """Статус-бар различает unreleased, бету и релиз - по одному источнику."""
    import version as app_version

    app, _ = make_app(tmp_path, monkeypatch)
    base = app.version()
    assert base["version"] == app_version.__version__
    assert base["label"] == "unreleased"
    assert base["beta"] is False and base["built_from_tag"] is False

    monkeypatch.setattr(app_version, "BUILD_LABEL", "b0.1.2")
    beta = app.version()
    assert beta["label"] == "b0.1.2"
    assert beta["beta"] is True and beta["built_from_tag"] is True

    monkeypatch.setattr(app_version, "BUILD_LABEL", "v0.1.2")
    stable = app.version()
    assert stable["label"] == "v0.1.2" and stable["beta"] is False


def test_hello_carries_build_label(tmp_path, monkeypatch):
    """Инициализация моста отдаёт подпись сборки - фронт не ждёт отдельного вызова."""
    import version as app_version

    app, _ = make_app(tmp_path, monkeypatch)
    monkeypatch.setattr(app_version, "BUILD_LABEL", "v0.1.2")
    assert app.hello()["label"] == "v0.1.2"


def test_index_has_create_service_button():
    """Кнопка «Создать службу» должна существовать в разметке."""
    from ui.bundle import INDEX_HTML
    assert 'id="btn-create-service"' in INDEX_HTML
    assert 'class="btn primary hidden"' in INDEX_HTML

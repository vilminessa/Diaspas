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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import paths, presets as presets_mod, release, service  # noqa: E402
from ui.webapp import App  # noqa: E402

DIR = Path("C:/zapret/zapret-discord-youtube-1.10.3")


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
    # install_release останавливает службу перед заменой папки - в тесте
    # это не должно трогать реальную службу машины
    monkeypatch.setattr(service, "stop",
                        lambda log=None: {"ok": True})
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
                        lambda: release.Release(repo=release.REPO_FLOWSEAL, tag="v9.9.9", name="9.9.9",
                                                zip_url="http://x",
                                                published=""))
    monkeypatch.setattr(release, "download",
                        lambda rel, progress=None: tmp_path / "r.zip")
    monkeypatch.setattr(release, "install",
                        lambda z, d, progress=None, **kw: rel_root)
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
                        lambda: release.Release(repo=release.REPO_FLOWSEAL, tag="v9.9.9", name="9.9.9",
                                                zip_url="http://x",
                                                published=""))
    monkeypatch.setattr(release, "download",
                        lambda rel, progress=None: tmp_path / "r.zip")
    monkeypatch.setattr(release, "install",
                        lambda z, d, progress=None, **kw: rel_root)
    monkeypatch.setattr(paths, "read_settings", lambda: {})
    monkeypatch.setattr(paths, "write_settings", lambda data: None)

    res = app.install_release()
    assert res["ok"] is True
    assert res["needs_preset"] is False
    assert "active_preset" not in res


def test_index_has_create_service_button():
    """Кнопка «Создать службу» должна существовать в разметке."""
    from ui.bundle import INDEX_HTML
    assert 'id="btn-create-service"' in INDEX_HTML
    assert 'class="btn primary hidden"' in INDEX_HTML


# -- дрейф пресета: маркер есть, но состояние ему не отвечает ----------

def test_drift_sets_needs_restore_and_remembers(tmp_path, monkeypatch):
    """Служба стоит, маркер есть, но пресет не распознан - дрейф.

    UI получает needs_restore (кнопка восстановления), а последний
    известный пресет запоминается для подсказки.
    """
    app, _ = make_app(tmp_path, monkeypatch, installed=True)
    warnings: list = []
    app.log = lambda lvl, txt: warnings.append((lvl, txt))  # type: ignore
    st = app.state()
    assert st["needs_restore"] is True
    assert st["suggested_preset"] == "ubisoft"
    assert paths.read_settings().get("last_known_preset") == "ubisoft"
    drift_warns = [w for w in warnings if "дрейф" in w[1]]
    assert len(drift_warns) == 1
    # повторные чтения не спамят журнал
    app.state()
    app.state()
    drift_warns = [w for w in warnings if "дрейф" in w[1]]
    assert len(drift_warns) == 1, "warning о дрейфе должен быть один на сессию"


def test_drift_not_when_service_absent(tmp_path, monkeypatch):
    """Службы нет - это не дрейф, а «создать службу» (другой сценарий)."""
    app, _ = make_app(tmp_path, monkeypatch, installed=False)
    st = app.state()
    assert st["needs_restore"] is False
    assert st["dir_ok"] is True and st["installed"] is False


def test_no_restore_when_preset_recognized(tmp_path, monkeypatch):
    """Пресет распознан - восстановления не требуется."""
    app, _ = make_app(tmp_path, monkeypatch, installed=True)
    # реальная папка, где файлы соответствуют маркеру
    root = tmp_path / "zapret-real"
    (root / "lists").mkdir(parents=True)
    (root / "utils").mkdir()
    (root / "general (ALT11).bat").write_text(
        'start /min "%BIN%winws.exe" --wf-tcp=80\r\n', encoding="utf-8")
    (root / "service.bat").write_text("@echo off\r\n", encoding="utf-8")
    (root / "lists" / "list-exclude-user.txt").write_text(
        "# header\r\nubisoft.com\r\n", encoding="utf-8")
    (root / "lists" / "ipset-all.txt").write_text("1.0.0.0/24\r\n",
                                                  encoding="ascii")
    ubisoft = next(p for p in presets_mod.DEFAULT_PRESETS
                   if p.key == "ubisoft")
    presets_mod.apply_files(root, ubisoft)
    app.zapret_root = root
    st = app.state()
    assert st["preset_key"] == "ubisoft"
    assert st["needs_restore"] is False


def test_suggested_falls_back_to_last_known(tmp_path, monkeypatch):
    """Маркера нет, но запомнен пресет при дрейфе - он и подсказка."""
    app, _ = make_app(tmp_path, monkeypatch, installed=True)
    settings = paths.read_settings()
    settings.pop("active_preset")
    settings["last_known_preset"] = "ubisoft"
    paths.write_settings(settings)
    st = app.state()
    assert st["suggested_preset"] == "ubisoft"


def test_index_has_restore_button():
    """Кнопка восстановления должна существовать в разметке (скрыта)."""
    from ui.bundle import INDEX_HTML
    assert 'id="btn-restore-after-restart"' in INDEX_HTML
    assert 'class="btn primary hidden"' in INDEX_HTML

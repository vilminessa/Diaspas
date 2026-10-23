"""Стабильный id установки, миграции настроек и битый settings.json.

Регрессии:
* переопределения стратегий и кэш подбора были привязаны к абсолютному
  пути: переименование или перенос папки теряли выбор пользователя;
* битый settings.json читался как «настроек нет» - активная папка и выбор
  стратегий исчезали молча;
* реестр версий терял поля layout/uid, поэтому UI пересчитывал layout
  на каждый вызов и не знал id установки;
* смена активной папки присваивалась в четырёх местах, и фоновый опрос
  мог читать состояние старой папки после переключения версии.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import autoselect, installs, paths, presets as pm  # noqa: E402
from core import log as dlog  # noqa: E402


def make_install(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "service.bat").write_text("@echo off\r\n", encoding="utf-8")
    (root / "bin").mkdir(exist_ok=True)
    (root / "bin" / "winws.exe").write_bytes(b"MZ")
    (root / "lists").mkdir(exist_ok=True)
    (root / "utils").mkdir(exist_ok=True)
    (root / "general (ALT11).bat").write_text("x")
    (root / "general (ALT7).bat").write_text("x")
    return root


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(paths, "zapret_dir_info", lambda: (None, ""))
    monkeypatch.setattr(dlog, "log_path", lambda: tmp_path / "diaspas.log")
    monkeypatch.setattr(dlog, "_ensure_dir", lambda: tmp_path)
    return tmp_path


# -- id установки -----------------------------------------------------------

def test_uid_survives_rename(isolated, tmp_path):
    root = make_install(tmp_path / "zapret-discord-youtube-1.10.3")
    installs.register(root, source="download")
    uid = installs.uid_of(root)
    assert uid and uid == installs.read_marker(root)

    pm.set_strategy("apex_ea", "general (ALT7).bat", root)

    moved = tmp_path / "renamed-zapret"
    root.rename(moved)
    assert installs.uid_of(moved) == uid
    # выбор пользователя остался: ключ - id, а не путь
    assert pm.strategy_override("apex_ea", moved) == "general (ALT7).bat"
    found = next(p for p in pm.load_presets(moved) if p.key == "apex_ea")
    assert found.strategy == "general (ALT7).bat"


def test_uid_restored_from_registry_when_marker_lost(isolated, tmp_path):
    root = make_install(tmp_path / "zapret-discord-youtube-1.10.2")
    entry = installs.register(root, source="download")
    (root / installs.UID_FILE).unlink()
    assert installs.uid_of(root) == entry.uid
    assert installs.read_marker(root) == entry.uid, "маркер восстановлен"


def test_uid_of_unknown_path_is_path_itself(isolated, tmp_path):
    root = make_install(tmp_path / "manual-dir")
    assert installs.uid_of(root) == str(root)


def test_path_for_uid_and_items_keep_layout_and_uid(isolated, tmp_path):
    win = tmp_path / "zapret-win-bundle-master"
    win.mkdir(parents=True)
    (win / "winws.exe").write_bytes(b"MZ")
    (win / "preset1.cmd").write_text('start "zapret" winws.exe --new\r\n')
    entry = installs.register(win, source="download")
    got = next(i for i in installs.items() if i.path == str(win))
    assert got.layout == installs.LAYOUT_WINWS, "layout терялся в items()"
    assert got.uid == entry.uid
    assert installs.path_for_uid(entry.uid) == win
    assert installs.path_for_uid("нет-такого") is None


def test_register_is_idempotent_on_uid(isolated, tmp_path):
    root = make_install(tmp_path / "zapret-discord-youtube-1.10.3")
    first = installs.register(root)
    second = installs.register(root)
    assert first.uid == second.uid
    assert len([i for i in installs.items() if i.path == str(root)]) == 1


# -- миграции настроек -------------------------------------------------------

def test_migrate_flat_overrides_to_wildcard(isolated, tmp_path):
    paths.write_settings({"preset_strategies": {"apex_ea": "general (ALT7).bat"}})
    changes = paths.migrate_settings()
    assert changes
    stored = paths.read_settings()["preset_strategies"]
    assert stored == {"*": {"apex_ea": "general (ALT7).bat"}}


def test_migrate_path_key_to_uid(isolated, tmp_path):
    root = make_install(tmp_path / "zapret-discord-youtube-1.10.3")
    entry = installs.register(root)
    paths.write_settings({"preset_strategies":
                          {str(root): {"apex_ea": "general (ALT7).bat"}}})

    changes = paths.migrate_settings()
    assert any("id" in c for c in changes)
    stored = paths.read_settings()["preset_strategies"]
    assert list(stored) == [entry.uid]
    assert pm.strategy_override("apex_ea", root) == "general (ALT7).bat"


def test_migrate_merges_when_uid_bucket_exists(isolated, tmp_path):
    root = make_install(tmp_path / "zapret-discord-youtube-1.10.3")
    entry = installs.register(root)
    paths.write_settings({"preset_strategies": {
        entry.uid: {"apex_ea": "general (ALT7).bat"},
        str(root): {"discord_youtube": "general (ALT11).bat"},
    }})
    paths.migrate_settings()
    stored = paths.read_settings()["preset_strategies"]
    assert set(stored[entry.uid]) == {"apex_ea", "discord_youtube"}


def test_migrate_is_noop_on_clean_settings(isolated, tmp_path):
    paths.write_settings({"zapret_dir": "C:\\x", "preset_strategies": {}})
    assert paths.migrate_settings() == []
    assert paths.read_settings()["preset_strategies"] == {}


# -- битый settings.json -----------------------------------------------------

def test_corrupt_settings_is_quarantined_not_forgotten(isolated, tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text("{ not json ,,,", encoding="utf-8")

    assert paths.read_settings() == {}
    broken = tmp_path / "settings.broken"
    assert broken.is_file(), "битый файл должен остаться для восстановления"
    assert "not json" in broken.read_text(encoding="utf-8")
    assert not settings.is_file()


def test_settings_list_instead_of_dict_is_quarantined(isolated, tmp_path):
    (tmp_path / "settings.json").write_text("[]", encoding="utf-8")
    assert paths.read_settings() == {}
    assert (tmp_path / "settings.broken").is_file()


def test_missing_settings_is_not_an_error(isolated):
    assert paths.read_settings() == {}
    assert not (isolated / "settings.broken").exists()


# -- кэш подбора стратегий ---------------------------------------------------

def test_strategy_cache_follows_uid_after_rename(isolated, tmp_path):
    root = make_install(tmp_path / "zapret-discord-youtube-1.10.3")
    installs.register(root)
    autoselect.save_best(str(root), "general (ALT7).bat")

    moved = tmp_path / "zapret-renamed"
    root.rename(moved)
    assert autoselect.load_best(str(moved)) == "general (ALT7).bat"


def test_strategy_cache_reads_legacy_path_key(isolated, tmp_path):
    """Кэш старый (ключ - путь), а установка уже имеет id: чтение должно сработать."""
    root = make_install(tmp_path / "zapret-discord-youtube-1.10.3")
    cache = isolated / "strategy-cache.json"
    cache.write_text(json.dumps({str(root): {"strategy": "general (ALT7).bat",
                                             "checked": 1}}), encoding="utf-8")
    installs.register(root)
    assert autoselect.load_best(str(root)) == "general (ALT7).bat"

    autoselect.save_best(str(root), "general (ALT9).bat")
    stored = json.loads(cache.read_text(encoding="utf-8"))
    assert str(root) not in stored, "старый ключ по пути должен быть убран"


# -- единая точка смены установки --------------------------------------------

def test_set_install_updates_root_and_pushes_state(isolated, monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "zapret_dir_info", lambda: (None, ""))
    from ui.webapp import App

    app = App()
    sent: list[tuple[str, object]] = []
    app.emitter.emit = lambda kind, payload=None: sent.append((kind, payload))
    app._collect_state = lambda: {"zapret_dir": str(tmp_path / "z")}

    app.set_install(tmp_path / "z", "manual")
    assert app.zapret_root == tmp_path / "z"
    assert app.zapret_source == "manual"
    assert ("state", {"zapret_dir": str(tmp_path / "z")}) in sent

    app.set_install(None, "")
    assert app.zapret_root is None
    assert app.zapret_source == "settings"

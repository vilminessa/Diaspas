"""История замеров и пользовательские группы пробы.

Регрессии:
* пробы жили только в памяти окна: после закрытия Diaspas «было/стало»
  не чем сравнить, а история - единственный способ увидеть, что смена
  стратегии действительно улучшила обход;
* проверяемые цели были захардкожены: пользователь с собственной проблемой
  (свой сервис, прокси, игра) не мог добавить цель и получал «всё хорошо»
  там, где у него ломалось.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import history, paths, probes  # noqa: E402
from ui.webapp import App  # noqa: E402


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(paths, "zapret_dir_info", lambda: (None, ""))
    return tmp_path


# -- история -----------------------------------------------------------------

def test_history_append_and_tail_order(isolated):
    history.append({"state": "partial", "count": 1, "total": 3})
    history.append({"state": "full", "count": 3, "total": 3})
    items = history.tail(10)
    assert [i["count"] for i in items] == [1, 3], "старшие первыми"
    assert items[0]["ts"] <= items[1]["ts"]
    assert history.last()["state"] == "full"


def test_history_is_bounded(isolated):
    for i in range(history.LIMIT + 40):
        history.append({"state": "full", "count": i, "total": 1})
    items = history.tail(history.LIMIT + 10)
    assert len(items) == history.LIMIT
    assert items[-1]["count"] == history.LIMIT + 39, "хранятся последние записи"


def test_history_skips_broken_lines(isolated):
    history.append({"state": "full", "count": 2, "total": 2})
    with (isolated / "probes-history.jsonl").open("a", encoding="utf-8") as fh:
        fh.write("{ битая строка\n")
    assert len(history.tail(10)) == 1


def test_history_missing_file_is_empty(isolated):
    assert history.tail() == [] and history.last() is None


def test_history_survives_unwritable_dir(isolated, monkeypatch):
    """Запись истории не должна ронять пробы: она вспомогательная."""
    def boom(*a, **k):
        raise OSError("диск занят")
    monkeypatch.setattr(Path, "write_text", boom)
    rec = history.append({"state": "full", "count": 1, "total": 1})
    assert rec["ts"] > 0
    assert history.tail() == []


# -- пользовательские группы -------------------------------------------------

def test_user_groups_from_settings(isolated):
    paths.write_settings({"probe_groups": {
        "steam": [{"name": "store", "url": "https://store.steampowered.com/"},
                  ["cdn", "https://cdn.store.steampowered.com/"]],
        "bad": [{"url": "ftp://не http"}],
        "пусто": [],
    }})
    groups = probes.user_groups()
    assert set(groups) == {"steam"}
    assert groups["steam"][0] == ("store", "https://store.steampowered.com/")
    assert groups["steam"][1] == ("cdn", "https://cdn.store.steampowered.com/")


def test_all_groups_merges_builtin_and_user(isolated):
    paths.write_settings({"probe_groups": {"steam": [
        {"name": "store", "url": "https://store.steampowered.com/"}]}})
    names = probes.group_names()
    assert "discord" in names and "steam" in names
    assert len(names) == len(probes.PROBE_GROUPS) + 1


def test_probe_group_reads_user_targets(isolated, monkeypatch):
    paths.write_settings({"probe_groups": {"steam": [
        {"name": "store", "url": "https://store.steampowered.com/"}]}})
    monkeypatch.setattr(probes, "probe_url",
                        lambda url, timeout=6.0: {"ok": True, "ms": 5, "why": ""})
    rep = probes.probe_group("steam", timeout=1)
    assert rep["state"] == "full" and rep["count"] == 1
    assert rep["targets"]["store"]["ok"] is True


def test_probe_all_includes_user_groups(isolated, monkeypatch):
    paths.write_settings({"probe_groups": {"steam": [
        {"name": "store", "url": "https://store.steampowered.com/"}]}})
    seen = []
    monkeypatch.setattr(probes, "probe_group", lambda g, timeout=6.0: seen.append(g) or {
        "state": "full", "ok": True, "count": 1, "total": 1, "targets": {"x": {"ok": True, "ms": 1, "why": ""}},
        "group": g})
    probes.probe_all(timeout=1)
    assert "steam" in seen and "discord" in seen


def test_malformed_settings_groups_are_ignored(isolated):
    paths.write_settings({"probe_groups": ["не словарь"]})
    assert probes.user_groups() == {}


# -- api ---------------------------------------------------------------------

@pytest.fixture()
def app(isolated, monkeypatch):
    instance = App()
    instance.emitter.emit = lambda kind, payload=None: None
    return instance


def test_api_add_and_remove_group(app, isolated):
    res = app.probe_groups_add("steam", "https://store.steampowered.com/")
    assert res["ok"] is True and res["group"] == "steam"
    assert app.probe_groups()["user"]["steam"][0][1] == "https://store.steampowered.com/"

    assert app.probe_groups_add("steam", "https://store.steampowered.com/")["ok"] is False
    assert app.probe_groups_add("", "https://x/")["ok"] is False
    assert app.probe_groups_add("steam", "steampowered.com")["ok"] is False
    assert app.probe_groups_add("steam", "https://cdn.steampowered.com/")["ok"] is True
    assert len(app.probe_groups()["user"]["steam"]) == 2

    assert app.probe_groups_remove("нет_такой")["ok"] is False
    assert app.probe_groups_remove("steam")["ok"] is True
    assert app.probe_groups()["user"] == {}


def test_api_history_records_probes(app, isolated, monkeypatch):
    class FakeState:
        installed = True
        running = True
        start_type = "Automatic"
        strategy = "general (ALT11)"
        pid = 1
        binpath = ""
        exit_code = 0

    monkeypatch.setattr(probes, "probe_all", lambda timeout=6.0: {
        "state": "partial", "ok": False, "count": 2, "total": 3,
        "groups": {"discord": {"state": "partial", "count": 2, "total": 3, "targets": {}},
                   "youtube": {"state": "full", "count": 1, "total": 1, "targets": {}}}})
    monkeypatch.setattr(probes, "probe_line", lambda rep: "2/3")
    from core import service
    monkeypatch.setattr(service, "state", lambda log=None: FakeState())

    rep = app.probes_run()
    assert rep["count"] == 2
    entry = history.last()
    assert entry["state"] == "partial" and entry["strategy"] == "general (ALT11)"
    assert entry["groups"]["discord"]["count"] == 2

    got = app.probe_history(5)
    assert got["ok"] is True and len(got["items"]) == 1
    assert got["total"] == 1


def test_api_history_limit_is_clamped(app, isolated):
    history.append({"state": "full", "count": 1, "total": 1})
    assert app.probe_history(0)["ok"] is True
    assert len(app.probe_history("много")["items"]) == 1

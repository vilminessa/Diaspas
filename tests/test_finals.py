"""The Finals: группа проб и пресет.

Грань регрессии тут двойная:
  * пробы не должны ходить на припаркованные домены игры (wildcard резолвится,
    TLS отвечает «unrecognized name» - цель красная при любой стратегии, и
    A/B и автоподбор путаются);
  * IP-исключение бэкенда Embark: правила игрового фильтра читают только
    ipset-exclude, поэтому без CIDR пресет ломает то, что работает без обхода.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import probes  # noqa: E402
from core.presets import (  # noqa: E402
    DEFAULT_PRESETS,
    Preset,
    apply_files,
    current_preset,
    load_presets,
)
from core.service import ServiceState  # noqa: E402

PARKED = ("thefinals.com", "finals.game", "finals.gg")


def finals_group() -> tuple[tuple[str, str], ...]:
    return probes.PROBE_GROUPS["finals"]


def test_finals_group_targets_only_live_hosts():
    urls = [url for _name, url in finals_group()]
    assert urls, "группа finals обязана иметь цели"
    for url in urls:
        assert url.startswith("https://"), url
        host = url.split("/")[2]
        assert not any(host.endswith(p) for p in PARKED), (
            f"{host} припаркован: проба на нём всегда красная и не отличает "
            f"zapret от DPI")
    hosts = {url.split("/")[2] for url in urls}
    assert {"api.embark.io", "events.embark.io", "embark.io"} <= hosts


def test_finals_group_is_probeable(monkeypatch):
    """probe_all включает finals: иначе «Проверить всё» молчит про игру."""
    def fake(url: str, timeout: float = 6.0) -> dict:
        return {"ok": True, "ms": 10, "why": "", "url": url}

    monkeypatch.setattr(probes, "probe_url", fake)
    report = probes.probe_all()
    assert "finals" in report["groups"]
    assert report["groups"]["finals"]["state"] == "full"
    assert report["state"] == "full"
    assert probes.probe_line(report).count("finals.") == len(finals_group())


def test_ab_accepts_finals_group(monkeypatch):
    from core import abtest, service

    box = {"running": True}

    def fake_state(log=None):
        return ServiceState(installed=True, running=box["running"],
                            start_type="Automatic",
                            strategy="general (ALT11)", pid=1, binpath="")

    def fake_stop(log=None):
        box["running"] = False
        return {"ok": True, "state": {}}

    def fake_start(log=None):
        box["running"] = True
        return {"ok": True, "state": {}}

    monkeypatch.setattr(service, "state", fake_state)
    monkeypatch.setattr(service, "stop", fake_stop)
    monkeypatch.setattr(service, "start", fake_start)
    monkeypatch.setattr(abtest.time, "sleep", lambda _s: None)
    monkeypatch.setattr(probes, "probe_group",
                        lambda group, timeout=6.0: {
                            "state": "full", "ok": True, "count": 3, "total": 3,
                            "targets": {}, "group": group})

    res = abtest.run("finals", wait=0.0)
    assert res["ok"] is True
    assert res["verdict"] == "works_always", res   # отвечает в обоих замерах


def make_root(tmp_path: Path) -> Path:
    """Мини-установка zapret для применения пресета без прав."""
    (tmp_path / "lists").mkdir()
    (tmp_path / "utils").mkdir()
    (tmp_path / "general (ALT11).bat").write_text(
        'start /min "%BIN%winws.exe" --wf-tcp=80\r\n', encoding="utf-8")
    (tmp_path / "service.bat").write_text("@echo off\r\n", encoding="utf-8")
    (tmp_path / "lists" / "list-exclude-user.txt").write_text(
        "# Never leave this file empty\r\ndomain.example.abc\r\n",
        encoding="utf-8")
    (tmp_path / "lists" / "list-general-user.txt").write_text(
        "# Never leave this file empty\r\ndomain.example.abc\r\n",
        encoding="utf-8")
    (tmp_path / "lists" / "ipset-exclude-user.txt").write_text(
        "203.0.113.113/32\r\n", encoding="ascii")
    (tmp_path / "lists" / "ipset-all.txt").write_text(
        "1.0.0.0/24\r\n1.1.1.0/24\r\n", encoding="ascii")
    return tmp_path


def finals_preset() -> Preset:
    return next(p for p in DEFAULT_PRESETS if p.key == "the_finals")


def test_the_finals_preset_shape():
    preset = finals_preset()
    # серверы матча идут за пределами стандартных диапазонов zapret
    assert preset.game_filter == "all"
    assert preset.ipset == "any"
    assert "embark.io" in preset.exclude_domains
    assert "api.embark.io" in preset.exclude_domains
    assert preset.exclude_ips, ("без CIDR игровой фильтр десинкает бэкенд: "
                                "hostlist-exclude в этих правилах нет")
    assert all(cid.split("/")[0].count(".") == 3 or "/" in cid
               for cid in preset.exclude_ips)


def test_the_finals_writes_ip_exclude_once(tmp_path):
    root = make_root(tmp_path)
    preset = finals_preset()
    apply_files(root, preset)
    apply_files(root, preset)          # повторное применение не дублирует
    text = (root / "lists" / "ipset-exclude-user.txt").read_text(
        encoding="utf-8")
    for cid in preset.exclude_ips:
        assert text.count(cid) == 1, text
    assert "203.0.113.113/32" in text      # заглушка не затёрта
    assert "# Diaspas" in text
    assert (root / "lists" / "ipset-all.txt").read_text(encoding="ascii") == ""


def test_the_finals_highlighted_over_games_max(tmp_path):
    """Списки накапливаются: games_max тоже совпадает по режимам, но
    пресет с IP-исключением конкретнее - подсветка обязана показать The Finals."""
    root = make_root(tmp_path)
    apply_files(root, finals_preset())
    state = ServiceState(installed=True, running=True, start_type="Automatic",
                         strategy="general (ALT11)", pid=1, binpath="...")
    found = current_preset(root, state)
    assert found is not None and found.key == "the_finals", found


def test_the_finals_not_matched_when_ip_exclude_missing(tmp_path):
    """CIDR не записан - подсветка не должна обещать защищённый бэкенд."""
    root = make_root(tmp_path)
    apply_files(root, finals_preset())
    (root / "lists" / "ipset-exclude-user.txt").write_text(
        "203.0.113.113/32\r\n", encoding="ascii")
    state = ServiceState(installed=True, running=True, start_type="Automatic",
                         strategy="general (ALT11)", pid=1, binpath="...")
    assert current_preset(root, state).key != "the_finals"


def test_custom_preset_reads_exclude_ips(tmp_path, monkeypatch):
    from core import paths

    settings = {"presets": [{"key": "finals_custom", "title": "Finals custom",
                             "strategy": "general (ALT11).bat",
                             "game_filter": "all", "ipset": "any",
                             "exclude_domains": ["embark.io"],
                             "exclude_ips": ["34.49.47.233/32"],
                             "description": ""}]}
    monkeypatch.setattr(paths, "read_settings", lambda: settings)
    custom = next(p for p in load_presets() if p.key == "finals_custom")
    assert custom.exclude_ips == ("34.49.47.233/32",)
    root = make_root(tmp_path)
    apply_files(root, custom)
    assert "34.49.47.233/32" in (root / "lists" /
                                 "ipset-exclude-user.txt").read_text(
        encoding="utf-8")

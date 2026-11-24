"""TgWsProxy: чистая логика ядра - ссылка tg://, sha256 ассета,
разбор релиза GitHub, манипуляции telegram-доменами в user-листе zapret,
чтение конфига и README-фикс dc_ip.

Всё на временных файлах (APPDATA перенаправляется в tmp_path); сетевая
часть (latest/download) не тестируется - её покрывает ручной e2e.
"""

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import release, tgproxy  # noqa: E402


# -- ссылка tg://proxy -------------------------------------------------------

def test_build_link_hex_secret():
    link = tgproxy.build_link("127.0.0.1", 1080, "03a0cb92fd03f051")
    assert link == ("tg://proxy?server=127.0.0.1&port=1080"
                    "&secret=03a0cb92fd03f051")


def test_build_link_no_secret():
    assert tgproxy.build_link("127.0.0.1", 1443, "") is None
    assert tgproxy.build_link("127.0.0.1", 1443, "   ") is None


def test_build_link_adds_dd_prefix_to_32hex_secret():
    """Конфиг хранит 16 байт секрета, а ссылке нужен префикс 0xdd
    (так же его печатает сам трей в логе)."""
    link = tgproxy.build_link("127.0.0.1", 1080,
                              "03a0cb92fd03f051cffe3b084a92f6a1")
    assert link.endswith("&secret=dd03a0cb92fd03f051cffe3b084a92f6a1")
    # готовый секрет с префиксом не трогаем
    link2 = tgproxy.build_link("127.0.0.1", 1443,
                               "dd03a0cb92fd03f051cffe3b084a92f6a1")
    assert link2.endswith("&secret=dd03a0cb92fd03f051cffe3b084a92f6a1")


# -- sha256 ассета -----------------------------------------------------------

def test_verify_digest_ok_and_bad(tmp_path):
    f = tmp_path / "a.exe"
    f.write_bytes(b"abc")
    good = hashlib.sha256(b"abc").hexdigest()
    assert tgproxy.verify_digest(f, "sha256:" + good) is True
    assert tgproxy.verify_digest(f, good) is True          # без префикса
    assert tgproxy.verify_digest(f, "sha256:" + "0" * 64) is False
    assert tgproxy.verify_digest(f, "неправильный") is False


# -- разбор релиза (ассеты НЕ-zip, как у tg-ws-proxy) ------------------------

def _payload():
    return [
        {"tag_name": "v9.9.9", "draft": True, "prerelease": False,
         "assets": [{"name": tgproxy.ASSET,
                     "browser_download_url": "https://x/draft", "size": 1}]},
        {"tag_name": "v1.12.0", "draft": False, "prerelease": False,
         "published_at": "2026-11-23T09:29:33Z",
         "assets": [
             {"name": "TgWsProxy_linux_amd64", "browser_download_url": "https://x/l"},
             {"name": tgproxy.ASSET,
              "browser_download_url": "https://x/TgWsProxy_windows.exe",
              "size": 21843941,
              "digest": "sha256:859575ced24c65fadfa189bef8c75611f3f0a149633e553320434625fa84d67a"},
         ]},
    ]


def test_parse_tg_release_prefers_non_draft_and_windows_asset():
    info = tgproxy.parse_tg_release(_payload())
    assert info["tag"] == "v1.12.0"
    assert info["url"].endswith("TgWsProxy_windows.exe")
    assert info["size"] == 21843941
    assert info["sha256"].startswith("sha256:859575ce")


def test_parse_tg_release_without_asset_raises():
    payload = [{"tag_name": "v1", "draft": False, "prerelease": False,
                "assets": [{"name": "other.zip", "browser_download_url": "u"}]}]
    with pytest.raises(release.ReleaseError):
        tgproxy.parse_tg_release(payload)


# -- telegram-домены в user-листе zapret -------------------------------------

def test_add_domains_idempotent_and_keeps_others():
    base = "# Never leave this file empty\r\n\r\ndomain.example.abc\r\n"
    once = tgproxy.add_domains(base)
    assert "# Diaspas tgws" in once
    for d in tgproxy.TELEGRAM_DOMAINS:
        assert d in once
    assert "domain.example.abc" in once
    assert tgproxy.add_domains(once) == once          # повтор - не плодит


def test_strip_domains_removes_only_ours():
    base = "# Never leave this file empty\r\n\r\ndomain.example.abc\r\n"
    filled = tgproxy.add_domains(base)
    filled = filled.replace("domain.example.abc\r\n",
                            "domain.example.abc\r\n# Diaspas\r\nother.com\r\n")
    back = tgproxy.strip_domains(filled)
    for d in tgproxy.TELEGRAM_DOMAINS:
        assert d not in back
    assert "# Diaspas tgws" not in back
    assert "domain.example.abc" in back
    assert "other.com" in back                        # чужой маркер цел
    assert "# Diaspas\r\n" in back


def test_set_zapret_list_roundtrip(tmp_path, monkeypatch):
    lst = tmp_path / "list-general-user.txt"
    lst.write_text("# Never leave this file empty\r\n\r\ndomain.example.abc\r\n",
                   encoding="utf-8")
    monkeypatch.setattr(tgproxy, "zapret_list_path", lambda: lst)

    res = tgproxy.set_zapret_list(True)
    assert res["ok"] and res["changed"] is True
    assert tgproxy.zapret_list_enabled() is True

    res = tgproxy.set_zapret_list(True)                # повтор - no-op
    assert res["ok"] and res["changed"] is False

    res = tgproxy.set_zapret_list(False)
    assert res["ok"] and res["changed"] is True
    assert tgproxy.zapret_list_enabled() is False
    assert "domain.example.abc" in lst.read_text(encoding="utf-8")


def test_set_zapret_list_without_install(monkeypatch):
    monkeypatch.setattr(tgproxy, "zapret_list_path", lambda: None)
    res = tgproxy.set_zapret_list(True)
    assert res["ok"] is False


# -- конфиг приложения и фикс dc_ip ------------------------------------------

def test_config_read_helpers(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    cfg_dir = tmp_path / "TgWsProxy"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(json.dumps({
        "host": "127.0.0.1", "port": 1080, "secret": "beef",
        "dc_ip": ["2:149.154.167.220", "4:149.154.167.220"],
    }), encoding="utf-8")
    assert tgproxy.cfg_port() == 1080
    assert tgproxy.cfg_host() == "127.0.0.1"
    assert tgproxy.proxy_link() == ("tg://proxy?server=127.0.0.1"
                                    "&port=1080&secret=beef")


def test_fix_dc_ip_keeps_dc4(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(tgproxy, "_image_pids", lambda: [])  # прокси «не запущен»)
    cfg_dir = tmp_path / "TgWsProxy"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(json.dumps({
        "port": 1443, "secret": "s",
        "dc_ip": ["2:149.154.167.220", "4:149.154.167.220"],
    }), encoding="utf-8")

    res = tgproxy.fix_dc_ip()
    assert res["ok"] and res["changed"] is True
    assert res["dc_ip"] == ["4:149.154.167.220"]

    res = tgproxy.fix_dc_ip()                          # идемпотентно
    assert res["ok"] and res["changed"] is False


def test_fix_dc_ip_refused_while_running(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(tgproxy, "_image_pids", lambda: [4242])
    cfg_dir = tmp_path / "TgWsProxy"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(json.dumps(
        {"dc_ip": ["2:149.154.167.220"]}), encoding="utf-8")
    res = tgproxy.fix_dc_ip()
    assert res["ok"] is False
    assert "остановите" in res["error"]


# -- состояние (быстрый срез, без сети) ---------------------------------------

def test_state_quick_shape(monkeypatch):
    monkeypatch.setattr(tgproxy, "_image_pids", lambda: [])
    st = tgproxy.state(quick=True)
    for key in ("installed", "running", "port", "link", "autostart",
                "zapret_list", "config_found"):
        assert key in st
    assert st["running"] is False
    assert "listening" not in st and "foreign" not in st   # quick не гоняет

"""Реестр версий zapret: несколько папок, одна активная, откат, ремонт.

Регрессии:
- install() раньше делал rmtree(dest) и уничтожал СОСЕДНИЕ версии;
- prune() обязан не предлагать к удалению активную и откатную;
- integrity/repair - восстановление winws.exe из архива той же версии
  (приём Synfronia: антивирус регулярно его съедает);
- кэш лучшей стратегии - per-install, версии не подмешивают чужое.
"""

import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import installs, release  # noqa: E402


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """installs.json живёт во временной папке, а не в профиле."""
    from core import paths
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    return tmp_path


def make_install(root: Path, version: str) -> Path:
    """Мини-установка zapret: service.bat + bin + стратегия + списки."""
    (root / "bin").mkdir(parents=True, exist_ok=True)
    (root / "bin" / "winws.exe").write_bytes(b"MZ fake")
    (root / "bin" / "quic_initial_www_google_com.bin").write_bytes(b"QUIC")
    (root / "lists").mkdir(exist_ok=True)
    (root / "lists" / "list-general.txt").write_text("example.com\r\n",
                                                     encoding="ascii")
    (root / "service.bat").write_text("@echo off\r\n", encoding="ascii")
    (root / f"general (ALT{version}).bat").write_text(
        'start /min "%BIN%winws.exe" --hostlist="%LISTS%list-general.txt" '
        '--dpi-desync-fake-quic="%BIN%quic_initial_www_google_com.bin"\r\n',
        encoding="utf-8")
    return root


def make_zip(path: Path, top: str, extra: dict[str, bytes] | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{top}/service.bat", "@echo off\r\n")
        zf.writestr(f"{top}/bin/winws.exe", b"MZ restored")
        zf.writestr(f"{top}/bin/quic_initial_www_google_com.bin", b"QUIC")
        zf.writestr(f"{top}/lists/list-general.txt", "example.com\r\n")
        for name, data in (extra or {}).items():
            zf.writestr(f"{top}/{name}", data)
    return path


def test_version_of():
    assert installs.version_of(Path("C:/x/zapret-discord-youtube-1.10.3")) \
        == "1.10.3"
    assert installs.version_of(Path("C:/x/MyZap")) == "MyZap"


def test_register_and_active(isolated):
    a = make_install(isolated / "zapret" / "zapret-discord-youtube-1.10.3",
                     "11")
    installs.register(a, source="download")
    data = installs.load()
    assert data["active"] == str(a)
    assert installs.active_path() == a
    assert installs.items()[0].version == "1.10.3"


def test_set_active_keeps_rollback(isolated):
    a = make_install(isolated / "z" / "zapret-discord-youtube-1.10.3", "11")
    b = make_install(isolated / "z" / "zapret-discord-youtube-1.10.2", "11")
    installs.register(a)
    installs.set_active(b)
    assert installs.active_path() == b
    assert installs.rollback_path() == a
    # откат: прежняя активная возвращается, бывшая новая - в rollback
    installs.set_active(a)
    assert installs.active_path() == a
    assert installs.rollback_path() == b


def test_remove_refuses_active_and_rollback(isolated):
    a = make_install(isolated / "z" / "zapret-discord-youtube-1.10.3", "11")
    b = make_install(isolated / "z" / "zapret-discord-youtube-1.10.2", "11")
    c = make_install(isolated / "z" / "zapret-discord-youtube-1.9.9", "9")
    installs.register(a)
    installs.set_active(b)          # a - rollback
    assert installs.remove(a) is False, "удалена откатная"
    assert installs.remove(b) is False, "удалена активная"
    assert installs.remove(c) is True
    assert not c.exists()


def test_remove_keeps_record_when_files_busy(isolated, monkeypatch):
    """Папка не удалилась (занята) -> запись сохраняется, False возвращается.

    Регрессия рассинхрона: реестр забывал версию, а папка оставалась.
    """
    import shutil

    a = make_install(isolated / "z" / "zapret-discord-youtube-1.10.3", "11")
    b = make_install(isolated / "z" / "zapret-discord-youtube-1.10.2", "11")
    installs.register(a)
    installs.set_active(b)

    def fake_rmtree(path, *args, **kwargs):
        pass                       # «файлы заняты» - папка остаётся

    monkeypatch.setattr(shutil, "rmtree", fake_rmtree)
    assert installs.remove(a) is False
    # запись откатной версии должна сохраниться
    assert installs.load().get("rollback") == str(a)
    assert any(i.path == str(a) for i in installs.items())


def test_prune_protects_active_and_rollback(isolated):
    versions = ("1.10.3", "1.10.2", "1.10.1", "1.9.9")
    paths = [make_install(isolated / "z" / f"zapret-discord-youtube-{v}",
                          "11") for v in versions]
    for p in paths:
        installs.register(p)                      # все в реестре
    installs.set_active(paths[1])                 # активная 1.10.2
    installs.set_keep(3)
    data = installs.load()
    active = data["active"]
    rollback = data["rollback"]
    assert rollback, "откат должен запомниться"

    stale = installs.prune()
    stale_names = {p.version for p in stale}
    assert len(stale) == 1, f"лишние: {stale_names}"
    assert active not in [p.path for p in stale], "активная предлагается"
    assert rollback not in [p.path for p in stale], "откатная предлагается"


def test_integrity_complete_and_missing(isolated):
    root = make_install(isolated / "z" / "zapret-discord-youtube-1.10.3", "11")
    assert installs.integrity(root) == []

    (root / "bin" / "winws.exe").unlink()
    missing = installs.integrity(root)
    assert "BIN/winws.exe" in missing

    # *-user.txt отсутствуют - это норма свежей распаковки, не поломка
    root2 = make_install(isolated / "z2" / "zapret-discord-youtube-1.10.3",
                         "11")
    (root2 / "bin" / "quic_initial_www_google_com.bin").unlink()
    missing = installs.integrity(root2)
    assert any("quic_initial" in m for m in missing), missing


def test_repair_restores_from_archive(isolated):
    root = make_install(isolated / "z" / "zapret-discord-youtube-1.10.3", "11")
    (root / "bin" / "winws.exe").unlink()          # «съел» антивирус
    zip_path = make_zip(isolated / "a" / "zapret-1.10.3.zip",
                        "zapret-discord-youtube-1.10.3")
    restored = installs.repair(root, zip_path)
    assert restored == 1, restored
    assert (root / "bin" / "winws.exe").read_bytes() == b"MZ restored"
    assert installs.integrity(root) == []


def test_install_keeps_neighbour_versions(isolated):
    """install() не уничтожает соседей: рядом живёт другая версия."""
    dest = isolated / "zapret"
    neighbour = make_install(dest / "zapret-discord-youtube-1.9.9", "9")
    zip_path = make_zip(isolated / "a" / "zapret-1.10.3.zip",
                        "zapret-discord-youtube-1.10.3")

    root = release.install(zip_path, dest)
    assert root.name == "zapret-discord-youtube-1.10.3"
    assert neighbour.exists(), "соседняя версия уничтожена"
    assert (neighbour / "service.bat").is_file()
    assert (root / "bin" / "winws.exe").is_file()


def test_install_replaces_own_version_only(isolated):
    """Повторная установка той же версии заменяет её, не соседей."""
    dest = isolated / "zapret"
    old = make_install(dest / "zapret-discord-youtube-1.10.3", "11")
    (old / "OLD-MARKER").write_text("x", encoding="ascii")
    neighbour = make_install(dest / "zapret-discord-youtube-1.9.9", "9")
    zip_path = make_zip(isolated / "a" / "zapret-1.10.3.zip",
                        "zapret-discord-youtube-1.10.3")

    root = release.install(zip_path, dest)
    assert root == dest / "zapret-discord-youtube-1.10.3"
    assert not (root / "OLD-MARKER").exists(), "старые файлы не заменены"
    assert neighbour.exists()


def test_best_strategy_cache_per_install(isolated):
    """Лучшая стратегия хранится по установке, а не глобально."""
    a = "C:/z/zapret-discord-youtube-1.10.3"
    b = "C:/z/zapret-discord-youtube-1.10.2"
    from core import autoselect
    autoselect.save_best(a, "general (ALT7).bat")
    autoselect.save_best(b, "general (ALT2).bat")
    assert autoselect.load_best(a) == "general (ALT7).bat"
    assert autoselect.load_best(b) == "general (ALT2).bat"
    assert autoselect.load_best("C:/z/unknown") is None

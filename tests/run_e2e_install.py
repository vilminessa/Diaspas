"""End-to-end: чистый кэш -> GitHub API -> скачивание -> распаковка."""
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import log as dlog, paths, release  # noqa: E402


def main() -> int:
    cache = paths.cache_dir()
    dest = paths.app_dir() / "zapret"

    # чистое состояние - как у нового пользователя
    if cache.exists():
        shutil.rmtree(cache, ignore_errors=True)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    print("кэш и папка назначения очищены")

    def progress(done: int, total: int | None) -> None:
        if total:
            pct = done * 100 // total
            print(f"\r  {pct}% ({done}/{total})   ", end="", flush=True)

    started = time.time()
    dlog.info("e2e: запрос последнего релиза")
    rel = release.latest_release()
    print(f"\nрелиз: {rel.tag} ({rel.published})")
    dlog.info(f"e2e: релиз {rel.tag}")

    zip_path = release.download(rel, progress=progress)
    print(f"\nскачано: {zip_path.name} = {zip_path.stat().st_size} байт")
    dlog.info(f"e2e: скачано {zip_path.name}")

    root = release.install(zip_path, dest, progress=progress)
    print(f"распаковано в: {root}")
    dlog.info(f"e2e: установлено {root}")

    # проверки
    checks = [
        ("service.bat", (root / "service.bat").is_file()),
        ("LICENSE.txt > 200 байт",
         (root / "LICENSE.txt").is_file()
         and (root / "LICENSE.txt").stat().st_size > 200),
        ("bin/winws.exe", (root / "bin" / "winws.exe").is_file()),
        ("general*.bat >= 20", len(list(root.glob("general*.bat"))) >= 20),
        ("WinDivert64.sys", (root / "bin" / "WinDivert64.sys").is_file()),
    ]
    ok = True
    for name, passed in checks:
        print(f"  {'OK ' if passed else 'FAIL'} {name}")
        ok = ok and passed

    lic = root / "LICENSE.txt"
    if lic.is_file():
        head = lic.read_text(encoding="utf-8", errors="replace")[:60]
        print(f"  LICENSE первые строки: {head!r}")

    # запись настроек - как это делает GUI
    settings = paths.read_settings()
    settings["zapret_dir"] = str(root)
    paths.write_settings(settings)
    print(f"settings.json записан: zapret_dir = {root}")

    # готовность к управлению службой
    from core import batparser
    cmd = batparser.winws_command(root, "general (ALT11).bat")
    print(f"batparser OK: аргументы {len(cmd)} символов")

    print(f"\nитог: {'OK' if ok else 'ЕСТЬ ПРОВАЛЫ'} "
          f"за {time.time() - started:.1f}s")
    dlog.info(f"e2e: итог {'OK' if ok else 'ЕСТЬ ПРОВАЛЫ'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

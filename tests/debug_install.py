"""Диагностика установки: почему install() оставил пустую папку zapret.

Zip валиден (56 файлов), папка создана и пуста - значит, упало между
mkdir и первой распаковкой. Прогоняем install() напрямую с ловлей каждого
исключения и пошаговым трейсом.
"""
import shutil
import sys
import time
import traceback
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import log as dlog  # noqa: E402
from core import release  # noqa: E402

ZIP = Path.home() / "AppData/Local/Diaspas/cache/zapret-1.10.3.zip"
DEST = Path.home() / "AppData/Local/Diaspas/zapret"


def trace(label: str) -> None:
    print(f"  [шаг] {label}", flush=True)
    dlog.info(f"диагностика: {label}")


if __name__ == "__main__":
    print(f"zip существует: {ZIP.is_file()} ({ZIP.stat().st_size if ZIP.is_file() else 0} байт)")
    print(f"dest: {DEST}")

    # Шаг 1: чистое состояние - удаляем пустую папку, как это делает install
    trace("shutil.rmtree(dest)")
    if DEST.exists():
        try:
            shutil.rmtree(DEST, ignore_errors=True)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
    print(f"  после rmtree: exists={DEST.exists()}")

    trace("dest.mkdir(parents=True, exist_ok=True)")
    DEST.mkdir(parents=True, exist_ok=True)
    print(f"  после mkdir: exists={DEST.is_dir()} файлов={len(list(DEST.iterdir()))}")

    # Шаг 2: открытие zip - главный подозреваемый (антивирус может держать)
    trace("zipfile.ZipFile(zip_path)")
    try:
        zf = zipfile.ZipFile(ZIP)
        print(f"  zip открыт, записей: {len(zf.infolist())}")
    except Exception:  # noqa: BLE001
        dlog.exception("диагностика: открытие zip")
        traceback.print_exc()
        raise SystemExit(1)

    # Шаг 3: распаковка
    trace("zf.extractall(dest)")
    t0 = time.time()
    try:
        zf.extractall(DEST)
        print(f"  extractall за {time.time() - t0:.2f}s, файлов: {len(list(DEST.rglob('*')))}")
    except Exception:  # noqa: BLE001
        dlog.exception("диагностика: extractall")
        traceback.print_exc()
        raise SystemExit(2)
    finally:
        zf.close()

    # Шаг 4: определение корня (логика release.install)
    entries = [p for p in DEST.iterdir() if p.is_dir()]
    root = entries[0] if len(entries) == 1 and not any(DEST.glob("*.bat")) else DEST
    trace(f"корень={root}")
    print(f"  service.bat есть: {(root / 'service.bat').is_file()}")
    print(f"  LICENSE.txt есть: {(root / 'LICENSE.txt').is_file()}")
    print(f"  bin/winws.exe есть: {(root / 'bin' / 'winws.exe').is_file()}")

    print("\n=== теперь release.install() целиком ===")
    try:
        result = release.install(ZIP, DEST)
        print(f"install OK -> {result}")
        dlog.info(f"диагностика: install OK -> {result}")
    except Exception:  # noqa: BLE001
        dlog.exception("диагностика: release.install")
        traceback.print_exc()
        raise SystemExit(3)

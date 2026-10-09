"""Есть ли ВСЕ файлы, на которые ссылается BinaryPathName службы.

Падение winws с 1067 часто = отсутствующий hostlist/список/фейк: winws
печатает ошибку в stderr (служба его не видит) и завершается.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    import winreg

    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                        r"System\CurrentControlSet\Services\zapret") as key:
        binpath = str(winreg.QueryValueEx(key, "ImagePath")[0])
    print("служба:", binpath[:120], "...")

    # извлечь все "C:\..." в кавычках
    refs = re.findall(r'"([^"]+)"', binpath)
    print(f"ссылок на файлы: {len(refs)}")
    missing = []
    for ref in refs:
        p = Path(ref)
        if not p.is_file():
            missing.append(ref)
    if missing:
        print(f"НЕ НАЙДЕНО {len(missing)}:")
        for m in missing:
            print("  -", m)
        return 1
    print("все файлы существуют")

    # также: есть ли ссылки на ipset/hostlist в виде ..\ относительные
    tokens = re.findall(r'--\S+', binpath)
    print(f"аргументов: {len(tokens)}")

    # сравним с тем, что даёт batparser для этой же установки
    from core import batparser
    # имя стратегии - из метки реестра (как её пишет service.bat),
    # НЕ из binpath: там есть "list-general.txt" и regex путается
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                        r"System\CurrentControlSet\Services\zapret") as key:
        label = str(winreg.QueryValueEx(key, "zapret-discord-youtube")[0])
    strategy = label if label.lower().endswith(".bat") else label + ".bat"
    root = Path(binpath.split('"')[1]).parent.parent
    print(f"стратегия: {strategy}, корень: {root}")
    regen = batparser.winws_command(root, strategy)
    norm = lambda s: re.sub(r'\s+', ' ', s.replace('\\"', '"')).strip()
    if norm(regen) == norm(binpath):
        print("batparser воспроизводит ImagePath: OK")
    else:
        print("РАСХОЖДЕНИЕ batparser vs реестр:")
        a, b = norm(regen), norm(binpath)
        for i, (x, y) in enumerate(zip(a, b)):
            if x != y:
                print(f"  позиция {i}:")
                print(f"    regen: {a[max(0, i-50):i+70]}")
                print(f"   ImagePath: {b[max(0, i-50):i+70]}")
                break
        else:
            print(f"    длины: regen={len(a)} image={len(b)}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

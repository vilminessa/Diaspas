"""Точечная правка workflows: PYTHONUTF8 для всех шагов.

Windows-раннер отдаёт stdout в cp1252; кириллица в print падает
UnicodeEncodeError. PYTHONUTF8=1 переводит stdin/stdout/stderr в UTF-8
на уровне Python - лечит build_ui, pytest и будущие инструменты разом.
"""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]

ANCHOR = "permissions:\n  contents: read\n"
PATCH = (
    "permissions:\n"
    "  contents: read\n"
    "\n"
    "# Windows-раннер печатает в cp1252; кириллица в выводе python\n"
    "# (build_ui, pytest) падает UnicodeEncodeError. UTF-8 на уровне\n"
    "# интерпретатора - единое решение для всех шагов.\n"
    "env:\n"
    '  PYTHONUTF8: "1"\n'
    '  PYTHONIOENCODING: "utf-8"\n'
)


def main() -> int:
    for name in ("release.yml", "beta.yml"):
        path = ROOT / ".github" / "workflows" / name
        text = path.read_text(encoding="utf-8")
        if "PYTHONUTF8" in text:
            print(f"{name}: уже содержит PYTHONUTF8")
            continue
        if ANCHOR not in text:
            print(f"{name}: якорь permissions не найден - пропущено")
            continue
        text = text.replace(ANCHOR, PATCH, 1)
        path.write_text(text, encoding="utf-8", newline="\n")
        print(f"{name}: добавлен env PYTHONUTF8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

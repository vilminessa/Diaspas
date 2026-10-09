"""Точечная правка workflows: бандл UI + расширенный pyflakes."""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]

PYFLAKES_OLD = "run: python -m pyflakes core ui i18n.py main.py"
PYFLAKES_NEW = "run: python -m pyflakes core ui tools i18n.py main.py"

PYTEST_OLD = (
    "      - name: Юнит-тесты (pytest)\n"
    "        run: python -m pytest tests/ -q"
)
PYTEST_NEW = (
    "      - name: Актуальность бандла UI (ui_src -> bundle.py)\n"
    "        run: python tools/build_ui.py --check\n"
    "      - name: Юнит-тесты (pytest)\n"
    "        run: python -m pytest tests/ -q"
)


def main() -> int:
    for name in ("release.yml", "beta.yml"):
        path = ROOT / ".github" / "workflows" / name
        text = path.read_text(encoding="utf-8")
        if PYFLAKES_OLD not in text or PYTEST_OLD not in text:
            print(f"{name}: шаблоны не найдены - пропущено (уже правлено?)")
            continue
        text = text.replace(PYFLAKES_OLD, PYFLAKES_NEW)
        text = text.replace(PYTEST_OLD, PYTEST_NEW)
        path.write_text(text, encoding="utf-8", newline="\n")
        print(f"{name}: обновлён")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

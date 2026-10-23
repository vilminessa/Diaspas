"""История замеров: чтобы видеть «было/стало», а не «последний экран».

Пробы раньше жили только в памяти окна: закрыл Diaspas - сравнить
результат с тем, что было до смены стратегии, нечем. Записи лежат в
JSONL (доли секунды на запись, файл читается вручную) с ограничением
по числу строк: журнал не должен расти бесконечно.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import paths

LIMIT = 300


def history_file() -> Path:
    return paths.app_dir() / "probes-history.jsonl"


def append(record: dict) -> dict:
    """Добавить замер; возвращает запись с проставленным ts.

    Файл перезаписывается целиком (последние LIMIT записей): это проще,
    чем ротация, и размер в сотни строк не весит ничего.
    """
    record.setdefault("ts", int(time.time()))
    path = history_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = tail_raw(LIMIT - 1)
        lines.append(json.dumps(record, ensure_ascii=False))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8",
                        newline="\n")
    except OSError:
        # история - вспомогательная: её потеря не должна ронять пробы
        pass
    return record


def tail_raw(n: int) -> list[str]:
    path = history_file()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-n:] if n > 0 else []


def tail(n: int = 20) -> list[dict]:
    """Последние записи (старшие первыми); битые строки пропускаются."""
    out: list[dict] = []
    for line in tail_raw(n):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def last() -> dict | None:
    entries = tail(1)
    return entries[-1] if entries else None


def clear() -> None:
    try:
        history_file().unlink()
    except OSError:
        pass


def fmt_ts(value: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value or 0))

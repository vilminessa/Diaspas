"""Разбор general*.bat в аргументы winws.exe.

Повторяет логику :service_install из service.bat без интерактива:

1. все переменные (``%BIN%``, ``%LISTS%``, ``%GameFilterTCP%`` ...) уже
   раскрыты вызывающим - в service.bat это делает ``call set`` с двойной
   экспансией, здесь замена текстом до разбора;
2. читаются строки начиная с той, где встречается ``winws.exe``, а до неё
   отрезается всё (вместе с ``winws.exe"``) - так исчезает ``start ... /min``;
3. каждая строка токенизируется по разделителям FOR-набора: пробел, таб,
   запятая, точка с запятой, равно. Кавычки защищают пробелы внутри путей;
4. символ ``^`` (продолжение строки) отбрасывается;
5. сборка идёт по ``mergeargs``: после ``--флаг`` первое значение пишется
   через пробел, последующие через запятую - из ``--wf-tcp=80,443`` получается
   ``--wf-tcp 80,443``, как в реестровом ImagePath рабочей службы;
6. токен в кавычках превращается в ``\\"путь\\"``; путь без двоеточия
   (относительный) получает префикс папки zapret, токен с ``@`` - тоже.

Итог - строка аргументов, пригодная для BinaryPathName службы.
"""

from __future__ import annotations

import re
from pathlib import Path

# Разделители набора FOR в service.bat.
_DELIMS = " \t,;="

# service.bat: args_with_value - аргументы, чьё значение пишется через «=».
_ARGS_WITH_VALUE = {"sni", "host", "altorder"}

# Переменные, которые service.bat подставляет при установке.
_DEFAULT_VARS = {
    "%BIN%": "{root}\\bin\\",
    "%LISTS%": "{root}\\lists\\",
}


class BatParseError(ValueError):
    """Файл стратегии непригоден: нет winws.exe или остались переменные."""


def _split_tokens(line: str) -> list[str]:
    """Токенизация как у ``for %%i in (строка)``: разделители вне кавычек."""
    tokens: list[str] = []
    cur: list[str] = []
    in_quote = False
    started = False
    for ch in line:
        if in_quote:
            cur.append(ch)
            if ch == '"':
                in_quote = False
            continue
        if ch == '"':
            in_quote = True
            cur.append(ch)
            started = True
        elif ch in _DELIMS:
            if started:
                tokens.append("".join(cur))
                cur = []
                started = False
        else:
            cur.append(ch)
            started = True
    if started:
        tokens.append("".join(cur))
    return tokens


def _quote_token(token: str, root: str) -> str:
    """Обработка токена в кавычках - ветки findstr/``@``/относительный путь."""
    inner = token[1:-1]
    if ":" in inner:
        return '\\"' + inner + '\\"'
    if inner.startswith("@"):
        return '\\"@' + root + inner[1:] + '\\"'
    return '\\"' + root + inner + '\\"'


def parse_strategy(path: Path, game_filter_tcp: str = "1024-65535",
                   game_filter_udp: str = "1024-65535") -> str:
    """Аргументы winws для файла стратегии.

    Параметры ``game_filter_*`` повторяют переменные ``%GameFilterTCP%`` /
    ``%GameFilterUDP%``: значение ``12`` у service.bat означает «порты
    не включены» (фильтр выключен), ``1024-65535`` - полный диапазон.
    """
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise BatParseError(f"не удалось прочитать {path}: {exc}") from exc

    root = str(path.parent)
    replacements = {
        "%BIN%": root + "\\bin\\",
        "%LISTS%": root + "\\lists\\",
        "%GameFilterTCP%": game_filter_tcp,
        "%GameFilterUDP%": game_filter_udp,
    }
    for key, value in replacements.items():
        raw = raw.replace(key, value)

    # Остаток переменных (например %~n0 в заголовке окна) отрезается вместе
    # с префиксом до winws.exe, но посторонние переменные в теле - ошибка разбора.
    lines = raw.splitlines()

    captured: list[str] = []
    capturing = False
    for line in lines:
        if not capturing:
            if "winws.exe" in line:
                capturing = True
                # как в service.bat: !line:*winws.exe"=! - отрезать всё до и
                # включая winws.exe"
                idx = line.find('winws.exe"')
                captured.append(line[idx + len('winws.exe"'):] if idx >= 0
                                else re.sub(r"^.*?winws\.exe", "", line, count=1))
            continue
        captured.append(line)

    if not capturing:
        raise BatParseError(f"в {path.name} не найден winws.exe")

    mergeargs = 0
    parts: list[str] = []
    for line in captured:
        for token in _split_tokens(line):
            if token in ("^", "^^"):
                continue

            if token.startswith("--") and mergeargs != 0:
                mergeargs = 0

            if token.startswith('"'):
                token = _quote_token(token, root)

            if mergeargs == 1:
                parts.append("," + token)
            elif mergeargs == 3:
                parts.append("=" + token)
                mergeargs = 1
            else:
                parts.append(" " + token)

            if token.startswith("--"):
                mergeargs = 2
            elif mergeargs >= 1:
                if mergeargs == 2:
                    mergeargs = 1
                if token.lower() in _ARGS_WITH_VALUE:
                    mergeargs = 3

    args = "".join(parts)

    # service.bat прогоняет аргументы через call set - нераскрытые переменные
    # там бы раскрылись, а для нас это сигнал ошибки разбора.
    leftovers = re.findall(r"%[A-Za-z~][A-Za-z0-9_~]*%", args)
    if leftovers:
        raise BatParseError(
            f"в {path.name} не раскрыты переменные: {', '.join(sorted(set(leftovers)))}")
    for bad in ("!", "&", "|", "<", ">"):
        if bad in args:
            raise BatParseError(
                f"в {path.name} недопустимый символ {bad!r} в аргументах")

    return args


def winws_command(zapret_root: Path, strategy: str,
                  game_filter_tcp: str = "1024-65535",
                  game_filter_udp: str = "1024-65535") -> str:
    """Полная строка BinaryPathName: путь к winws.exe + аргументы."""
    bat = Path(zapret_root) / strategy
    if not bat.is_file():
        raise BatParseError(f"нет файла стратегии {bat}")
    args = parse_strategy(bat, game_filter_tcp, game_filter_udp)
    exe = Path(zapret_root) / "bin" / "winws.exe"
    return f'"{exe}"' + args

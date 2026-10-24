"""Монохромная метка Diaspas: один источник формы для SVG, ICO и HICON.

форма - «рассеяние» (διάσπασις): расколотое кольцо из трёх дуг с зазорами и
точка в центре. Одно описание геометрии (:data:`MARK`) даёт и SVG
(:func:`svg_text`), и растер (:func:`raster`), и ICO (:func:`ico_bytes`) - так
SVG и бинарный ассет не расходятся: `tools/render_icon.py` пишет оба из этого
кода, а тест сверяет файлы с генерацией (как `build_ui --check`).

Растер - только stdlib: пиксели считаются напрямую, без Pillow и без SVG-
парсера. Инк один и тот же RGB на всех пикселях метки (монохром), фон
прозрачный; для трея инк меняется по состоянию (см. `core.tray.ICON_TINTS`).
"""

from __future__ import annotations

import math
import struct
from pathlib import Path
import tempfile

# Координаты в единицах viewBox 24x24 (SVG) - растер масштабирует их под размер.
VIEW = 24
MARK = {
    "ring_outer": 10.5,
    "ring_inner": 7.0,
    # дуги в градусах (0 - вправо, против часовой? здесь - по atan2(y,x))
    "arcs": ((0.0, 100.0), (120.0, 220.0), (240.0, 340.0)),
    "dot": (12.0, 12.0, 3.4),
    "gap": 20.0,   # зазоры между дугами: 360 = 3 дуги + 3 зазора
}

INK_DEFAULT = (0xE6, 0xEB, 0xF2)      # светлый: читается на тёмном фоне
INK_SVG = "currentColor"              # SVG наследует цвет контейнера

# Инк метки окна и панели задач: средний тон - виден и на тёмной, и на светлой
# панели задач (трей красится по состоянию, см. TINTS).
WINDOW_INK = (0x8B, 0x95, 0xA5)

# Инк метки по состоянию обхода - тот же смысл, что системные значки трея
TINTS = {
    "ok": (0x3F, 0xBF, 0x6B),
    "warn": (0xFF, 0xB4, 0x54),
    "err": (0xE0, 0x55, 0x4F),
    "off": (0x8B, 0x95, 0xA5),
}
TRAY_SIZES = (16, 32, 48)


def ink_at(x: float, y: float) -> bool:
    """Точка внутри метки (viewBox-единицы): дуги расколотого кольца + центр.

    y растёт вниз (как в SVG), поэтому угол считается от оси X против
    часовой стрелки визуально: atan2(-dy, dx).
    """
    cx, cy = VIEW / 2, VIEW / 2
    dx, dy = x - cx, y - cy
    r = math.hypot(dx, dy)
    angle = (math.degrees(math.atan2(-dy, dx)) + 360.0) % 360.0
    dot_x, dot_y, dot_r = MARK["dot"]
    if math.hypot(x - dot_x, y - dot_y) <= dot_r:
        return True
    if not (MARK["ring_inner"] <= r <= MARK["ring_outer"]):
        return False
    for start, end in MARK["arcs"]:
        if start <= angle < end:
            return True
    return False


def raster(size: int, rgb: tuple[int, int, int]) -> tuple[bytes, bytes]:
    """(BGRA сверху вниз, AND-маска строками, выровненными до DWORD).

    Маска: 1 = прозрачно, 0 = показать цвет; альфа та же (255 внутри метки),
    чтобы ICO читался и как 32bpp с альфой, и как классический mask+color.
    """
    stride = ((size + 31) // 32) * 4
    color = bytearray(size * size * 4)
    mask = bytearray(stride * size)
    scale = VIEW / size
    r, g, b = rgb
    for py in range(size):
        for px in range(size):
            # центр пикселя в единицах viewBox
            x = (px + 0.5) * scale
            y = (py + 0.5) * scale
            offset = (py * size + px) * 4
            if ink_at(x, y):
                color[offset] = b
                color[offset + 1] = g
                color[offset + 2] = r
                color[offset + 3] = 255
            else:
                mask[py * stride + px // 8] |= 1 << (7 - px % 8)
    return bytes(color), bytes(mask)


def _bottom_up(rows: list[bytes]) -> bytes:
    """ICO хранит строки снизу вверх (в BMP-данных внутри ICO)."""
    return b"".join(rows[::-1])


def ico_bytes(sizes: tuple[int, ...] = TRAY_SIZES,
              rgb: tuple[int, int, int] = INK_DEFAULT) -> bytes:
    """ICO с несколькими размерами: каждая запись - 32bpp + AND-маска.

    biHeight = 2*высота (пиксели плюс маска), строки развёрнуты, offsets
    считаются по фактическим длинам - файл читается .NET, Tk и Windows.
    """
    directory = struct.pack("<HHH", 0, 1, len(sizes))
    entries: list[bytes] = []
    images: list[bytes] = []
    offset = len(directory) + 16 * len(sizes)
    for size in sizes:
        color, mask = raster(size, rgb)
        stride = ((size + 31) // 32) * 4
        header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                             stride * size, 0, 0, 0, 0)
        pixels = _bottom_up([color[y * size * 4:(y + 1) * size * 4]
                             for y in range(size)])
        mask_rows = _bottom_up([mask[y * stride:(y + 1) * stride]
                                for y in range(size)])
        image = header + pixels + mask_rows
        # ICONDIRENTRY: width/height - байты (0 = 256), planes, bitcount,
        # bytesInRes, imageOffset
        entries.append(struct.pack(
            "<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(image),
            offset))
        images.append(image)
        offset += len(image)
    return directory + b"".join(entries) + b"".join(images)


def arc_paths() -> list[str]:
    """Дуги кольца как SVG-дуги той же формы (outer дуга + inner обратно)."""
    out: list[str] = []
    outer, inner = MARK["ring_outer"], MARK["ring_inner"]
    cx, cy = VIEW / 2, VIEW / 2
    for start, end in MARK["arcs"]:
        a0, a1 = math.radians(start), math.radians(end)
        # y вниз: угол в SVG = -atan(dy), поэтому sin берём со знаком минус
        def point(radius: float, angle: float) -> tuple[float, float]:
            return (round(cx + radius * math.cos(angle), 3),
                    round(cy - radius * math.sin(angle), 3))
        x0, y0 = point(outer, a0)
        x1, y1 = point(outer, a1)
        x2, y2 = point(inner, a1)
        x3, y3 = point(inner, a0)
        large = 1 if (end - start) > 180 else 0
        out.append(
            f"M {x0} {y0} A {outer} {outer} 0 {large} 0 {x1} {y1} "
            f"L {x2} {y2} A {inner} {inner} 0 {large} 1 {x3} {y3} Z")
    return out


def svg_text(rgb: str = INK_SVG) -> str:
    """SVG монометки: тот же MARK, что растер; без градиентов и фона."""
    dot_x, dot_y, dot_r = MARK["dot"]
    paths = "\n".join(f'    <path d="{d}"/>' for d in arc_paths())
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {VIEW} {VIEW}" '
        f'role="img" aria-label="Diaspas">\n'
        f'  <title>Diaspas - расколотое кольцо (диаσπασις)</title>\n'
        f'  <g fill="{rgb}">\n'
        f'{paths}\n'
        f'    <circle cx="{dot_x}" cy="{dot_y}" r="{dot_r}"/>\n'
        f'  </g>\n'
        f'</svg>\n'
    )


def temp_ico_file(rgb: tuple[int, int, int] = INK_DEFAULT,
                  name: str = "diaspas-icon") -> Path:
    """ICO в temp: нужен .NET (Icon.FromFile) и Tk (iconbitmap) - байты те же.

    Файл пишется один раз на цвет: повторные вызовы того же цвета не
    переписывают его (размер сверяется, содержимое детерминировано).
    """
    data = ico_bytes(rgb=rgb)
    suffix = "-".join(f"{c:02x}" for c in rgb)
    path = Path(tempfile.gettempdir()) / f"{name}-{suffix}.ico"
    if not path.is_file() or path.stat().st_size != len(data):
        path.write_bytes(data)
    return path

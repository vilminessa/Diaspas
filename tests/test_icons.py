"""Метка Diaspas: форма, растер, SVG и ICO из одного описания (core.icons).

Проверяется офлайн: монохромность (один инк на всех пикселях), прозрачность
фона и согласованность AND-маски с альфой, структура ICO (несколько размеров,
biHeight = 2*высота, offsets по фактическим длинам), соответствие SVG той же
форме, и актуальность ассетов в репозитории (как сверка бандла UI).
"""

import math
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import icons  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
SVG_PATH = REPO / "assets" / "icon.svg"
ICO_PATH = REPO / "assets" / "icon.ico"


def test_ink_at_matches_the_mark_geometry():
    """Точка центра, дуги кольца и зазоры: форма описана явно."""
    assert icons.ink_at(12.0, 12.0)                      # центр метки
    assert icons.ink_at(20.0, 12.0)                      # правая сторона (угол 0)
    assert icons.ink_at(12.0, 2.0)                       # верх: угол 90 внутри дуги 0..100
    assert icons.ink_at(2.0, 12.0)                       # лево: угол 180 внутри дуги 120..220
    assert icons.ink_at(12.0, 22.0)                      # низ: угол 270 внутри дуги 240..340
    assert not icons.ink_at(12.0, 1.0)                   # радиус 11 > outer 10.5
    assert not icons.ink_at(12.0, 6.0)                   # радиус 6 < inner 7
    # зазоры между дугами: 100..120, 220..240, 340..360
    for gap in (110.0, 230.0, 350.0):
        x = 12.0 + 10.0 * math.cos(math.radians(gap))
        y = 12.0 - 10.0 * math.sin(math.radians(gap))
        assert not icons.ink_at(x, y), gap
    assert not icons.ink_at(12.0, 12.0 + icons.MARK["ring_outer"] + 1)


def test_ink_at_is_monochrome_and_bounded():
    """Форма внутри viewBox и не занимает весь квадрат."""
    inside = sum(1 for x in range(icons.VIEW) for y in range(icons.VIEW)
                 if icons.ink_at(x + 0.5, y + 0.5))
    assert 0 < inside < icons.VIEW * icons.VIEW
    # метка целиком в viewBox: за его пределами точек нет
    assert not icons.ink_at(-1.0, 12.0) and not icons.ink_at(25.0, 12.0)


def test_raster_is_one_ink_and_transparent_outside():
    color, mask = icons.raster(16, icons.TINTS["ok"])
    size, stride = 16, 4
    assert len(color) == size * size * 4
    assert len(mask) == stride * size
    # DIB и ICO хранят BGRA: инк лежит как (B, G, R)
    ink_bgr = tuple(reversed(icons.TINTS["ok"]))
    inks = set()
    outside = 0
    for i in range(0, len(color), 4):
        if color[i + 3]:
            assert color[i + 3] == 255
            inks.add(tuple(color[i:i + 3]))
        else:
            outside += 1
            assert color[i:i + 3] == bytes(3)   # снаружи RGB не используется
    assert inks == {ink_bgr}, "инк должен быть одним цветом"
    assert outside > 0
    # AND-маска согласована с альфой: 1 = прозрачно
    for y in range(size):
        for x in range(size):
            bit = mask[y * stride + x // 8] & (1 << (7 - x % 8))
            alpha = color[(y * size + x) * 4 + 3]
            assert bool(bit) == (alpha == 0)


def test_raster_of_each_state_is_a_different_single_ink():
    """Инк по состоянию - тот же смысл, что у системных значков трея."""
    for state, rgb in icons.TINTS.items():
        color, _mask = icons.raster(16, rgb)
        inks = {tuple(color[i:i + 3]) for i in range(0, len(color), 4)
                if color[i + 3]}
        assert inks == {tuple(reversed(rgb))}, state
    assert len(set(icons.TINTS.values())) == 4


def test_ico_blob_is_valid_and_multi_size():
    blob = icons.ico_bytes(sizes=(16, 32, 48), rgb=icons.TINTS["off"])
    reserved, kind, count = struct.unpack("<HHH", blob[:6])
    assert (reserved, kind, count) == (0, 1, 3)
    offset = 6 + 16 * count
    total = len(blob)
    for i in range(count):
        entry = blob[6 + 16 * i:6 + 16 * i + 16]
        width, height, colors, reserved_b, planes, bits, size, image_off = \
            struct.unpack("<BBBBHHII", entry)
        assert width == height == (16, 32, 48)[i]
        assert planes == 1 and bits == 32 and colors == 0
        assert image_off == offset, "offset должен идти по фактическим длинам"
        bi_size, bi_width, bi_height = struct.unpack(
            "<III", blob[image_off:image_off + 12])
        planes, bits = struct.unpack("<HH", blob[image_off + 12:image_off + 16])
        assert bi_size == 40
        assert bi_width == width
        assert bi_height == height * 2          # пиксели + AND-маска
        assert planes == 1 and bits == 32
        stride = ((width + 31) // 32) * 4
        assert size == 40 + width * width * 4 + stride * height
        offset += size
    assert offset == total                      # хвост не теряется


def test_svg_text_describes_the_same_mark():
    svg = icons.svg_text()
    assert svg.startswith('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"')
    assert svg.rstrip().endswith("</svg>")
    assert f'fill="{icons.INK_SVG}"' in svg          # монохром наследует цвет
    assert svg.count("<path d=") == len(icons.MARK["arcs"])
    dot_x, dot_y, dot_r = icons.MARK["dot"]
    assert f'<circle cx="{dot_x}" cy="{dot_y}" r="{dot_r}"/>' in svg
    # дуги - замкнутые сегменты annulus (outer дуга + inner обратно)
    for d in icons.arc_paths():
        assert d.startswith("M ") and d.endswith(" Z")
        assert d.count(" A ") == 2


def test_svg_arcs_land_on_the_raster_shape():
    """Концы SVG-дуг совпадают с raster: форма описана один раз."""
    # радиус чуть внутри outer: ink_at включает границу 10.5, а cos/sin дают
    # 10.500000000000002 - формально точка была бы за кольцом
    radius = icons.MARK["ring_outer"] - 0.6
    for start, end in icons.MARK["arcs"]:
        for angle in (start + 5, (start + end) / 2, end - 5):
            x = icons.VIEW / 2 + radius * math.cos(math.radians(angle))
            y = icons.VIEW / 2 - radius * math.sin(math.radians(angle))
            assert icons.ink_at(x, y), (angle, x, y)


def test_temp_ico_file_is_written_once_per_ink(tmp_path, monkeypatch):
    monkeypatch.setattr(icons.tempfile, "gettempdir", lambda: str(tmp_path))
    path = icons.temp_ico_file(icons.TINTS["ok"])
    data = path.read_bytes()
    assert data == icons.ico_bytes(rgb=icons.TINTS["ok"])
    # повторный вызов того же инка не переписывает файл
    mtime = path.stat().st_mtime_ns
    again = icons.temp_ico_file(icons.TINTS["ok"])
    assert again == path and path.stat().st_mtime_ns == mtime
    # другой инк - отдельный файл
    other = icons.temp_ico_file(icons.TINTS["err"])
    assert other != path and other.read_bytes() == icons.ico_bytes(rgb=icons.TINTS["err"])


def test_assets_match_generation():
    """Сверка ассетов с генерацией (как build_ui --check): файлы в репо актуальны."""
    sys.path.insert(0, str(REPO / "tools"))
    import render_icon

    assert SVG_PATH.is_file() and ICO_PATH.is_file()
    assert SVG_PATH.read_bytes() == render_icon.render_svg()
    assert ICO_PATH.read_bytes() == render_icon.render_ico()
    assert render_icon.check_assets() == 0


def test_icon_names_cover_every_tray_state():
    """Ключи TINTS == состояния icon_state: трей не останется без иконки."""
    state = {"dir_ok": True, "installed": True, "running": True,
             "strategy": "general (ALT11)", "strategy_label": "ALT11",
             "strategy_mode": "manual", "preset_key": None, "preset_title": None,
             "auto_cooldown_left": 0, "auto_last_strategy": "", "strategies": []}
    from core import tray
    values = {
        tray.icon_state(state),
        tray.icon_state(state, {"state": "full"}),
        tray.icon_state(state, {"state": "partial"}),
        tray.icon_state(state, {"state": "none"}),
        tray.icon_state({**state, "running": False}),
    }
    assert values <= set(icons.TINTS), values

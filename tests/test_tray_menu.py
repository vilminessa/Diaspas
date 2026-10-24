"""Геометрия и анимация тёмного меню трея (без Win32).

Тестируется то, что от десктопа не зависит: строки из TraySpec, метки с
пояснениями, размеры и rects, позиция у курсора с flip у краев, каскад
подменю, hit-test, навигация клавиатурой, timeline fade/slide, scroll.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import tray, tray_menu  # noqa: E402

STATE = {
    "dir_ok": True, "installed": True, "running": True,
    "strategy": "general (ALT11)", "strategy_label": "ALT11",
    "strategy_mode": "auto", "preset_key": "discord_youtube",
    "preset_title": "Discord / YouTube", "auto_cooldown_left": 600,
    "auto_last_strategy": "general (ALT4).bat",
    "strategies": [{"file": "general (ALT11).bat", "label": "ALT11",
                    "active": True},
                   {"file": "general (ALT4).bat", "label": "ALT4",
                    "active": False}],
}
PRESETS = [{"key": "discord_youtube", "title": "Discord / YouTube", "off": False},
           {"key": "apex_ea", "title": "Apex / EA", "off": False},
           {"key": "off", "title": "Выкл", "off": True}]

WORK = tray_menu.Rect(0, 0, 1920, 1040)


def spec(cooldown: int = 600) -> tray.TraySpec:
    state = {**STATE, "auto_cooldown_left": cooldown}
    return tray.tray_model(state, PRESETS, busy={},
                           last_probes={"state": "full", "count": 11,
                                        "total": 11})


def measure(text: str) -> tuple[int, int]:
    """Условная метрика: 8 px на символ, высота 14 px (реальная - GDI)."""
    return len(text) * 8, 14


def layout_of(rows=None) -> tray_menu.Layout:
    return tray_menu.layout(rows if rows is not None else
                            tray_menu.menu_rows(spec()), measure)


def test_rows_from_spec_keep_order_and_notes():
    rows = tray_menu.menu_rows(spec())
    kinds = [row.kind for row in rows]
    assert kinds == ["item", "item", "sep", "item", "item", "sep", "item",
                     "item", "item", "sep", "item", "item"], kinds
    # подменю - кликабельная строка со submenu=True
    submenu = [row for row in rows if row.submenu]
    assert [row.id for row in submenu] == ["presets", "strategies"]
    assert [row.label for row in submenu] == ["Пресеты", "Стратегии"]
    # пояснение в метке: cooldown виден в меню
    auto = next(row for row in rows if row.id == tray.AUTO_RUN)
    assert auto.label.endswith("(через 10 мин)")
    assert auto.enabled is False           # cooldown гасит пункт
    assert not auto.clickable()


def test_separator_is_not_clickable_and_rows_are_measured():
    lay = tray_menu.layout(tray_menu.menu_rows(spec()), measure)
    sep = [i for i, row in enumerate(lay.rows) if row.kind == "sep"]
    assert sep and all(row_index is None
                       for row_index in (tray_menu.row_index(lay, 10,
                                                             lay.rects[i].y + 1)
                                         for i in sep))
    preset_row = next(i for i, row in enumerate(lay.rows) if row.id == "presets")
    # ширина: самая длинная метка + колонки, в пределах min/max
    assert lay.width >= lay.rects[preset_row].w + lay.rects[preset_row].x
    assert lay.height == sum(r.h for r in lay.rects)
    assert lay.rects[0].y == 0 and lay.rects[-1].y < lay.height


def test_disabled_item_is_hit_tested_but_not_clickable():
    rows = tray_menu.menu_rows(spec())
    auto = next(row for row in rows if row.id == tray.AUTO_RUN)
    lay = tray_menu.layout(rows, measure)
    index = next(i for i, row in enumerate(lay.rows) if row.id == tray.AUTO_RUN)
    rect = lay.rects[index]
    assert tray_menu.row_index(lay, rect.x + 1, rect.y + 1) == index
    assert not rows[index].clickable()      # подсветка есть, клик нет
    assert auto.enabled is False


def test_place_prefers_below_cursor_and_flips_at_edges():
    # трей внизу экрана: под курсором нет места -> меню рисуется выше курсора
    bottom = tray_menu.place((1500, 1000), (200, 300), WORK)
    assert bottom.bottom <= WORK.bottom
    assert bottom.y == 1000 - 300
    # правый край: разворачивается влево от курсора
    right = tray_menu.place((1910, 100), (200, 100), WORK)
    assert right.x <= 1910 and right.right <= WORK.right
    # normal position: ниже курсора, слева от него
    plain = tray_menu.place((300, 300), (200, 100), WORK)
    assert plain.x == 300 and plain.y == 300
    # маленький экран: меню не выходит за work-area
    small = tray_menu.Rect(0, 0, 300, 200)
    tight = tray_menu.place((290, 190), (200, 300), small)
    assert tight.x >= small.x and tight.y >= small.y
    assert tight.x + tight.w <= small.right


def test_submenu_rect_cascades_right_and_flips_left():
    parent = tray_menu.Rect(100, 300, 220, 400)
    row = tray_menu.Rect(18, 120, 200, 24)
    child = tray_menu.submenu_rect(parent, row, (200, 100), WORK)
    assert child.x == parent.right          # справа от родителя
    assert child.y == parent.y + row.y
    # правый край экрана -> дочернее меню слева от родителя
    near = tray_menu.Rect(1750, 300, 220, 400)
    flip = tray_menu.submenu_rect(near, row, (200, 100), WORK)
    assert flip.x == near.x - 200 and flip.right <= near.x
    assert flip.x >= WORK.x
    # нижний край -> дочернее меню поднимается, не выходит за экран
    low = tray_menu.submenu_rect(tray_menu.Rect(100, 900, 220, 200),
                                 tray_menu.Rect(18, 150, 200, 24),
                                 (200, 200), WORK)
    assert low.bottom <= WORK.bottom


def test_nav_skips_separators_and_clamps():
    lay = layout_of()
    first = tray_menu.nav(lay, None, 1)
    assert first is not None and lay.rows[first].kind == "item"
    assert tray_menu.nav(lay, None, -1) == first   # Up без highlighting -> первый
    second = tray_menu.nav(lay, first, 1)
    # между пунктами стоит разделитель - навигация его пропускает
    assert second > first
    assert lay.rows[second].kind == "item"
    assert tray_menu.nav(lay, first, -1) == first      # вверх не уходит за край
    last = None
    for _ in range(40):
        last = tray_menu.nav(lay, last, 1)
    assert lay.rows[last].kind == "item"
    assert tray_menu.nav(lay, last, 1) == last         # вниз упирается в конец


def test_fade_and_slide_end_at_full_alpha_and_zero_offset():
    steps = [tray_menu.fade_alpha(ms) for ms in range(0, tray_menu.FADE_MS + 40,
                                                      tray_menu.STEP_MS)]
    assert steps[0] == 0
    assert steps == sorted(steps), "alpha должен только расти"
    assert steps[-1] == 255
    assert tray_menu.fade_alpha(0, 0) == 255        # total 0 -> сразу видно
    offsets = [tray_menu.slide_offset(ms) for ms in range(0, tray_menu.FADE_MS + 40,
                                                         tray_menu.STEP_MS)]
    assert offsets[0] == -tray_menu.SLIDE_PX
    assert offsets[-1] == 0
    assert offsets == sorted(offsets), "slide должен доезжать вниз"
    # дочернее меню анимируется быстрее основного
    assert tray_menu.SUBMENU_MS < tray_menu.FADE_MS


def test_visible_rows_and_scroll_limit():
    rows = tuple(tray_menu.Row(f"s{i}", f"item {i}") for i in range(30))
    lay = tray_menu.layout(rows, measure, row_h=24)
    height = 120
    visible = tray_menu.visible_rows(lay, 0, height)
    assert len(visible) == height // 24
    assert visible[0][1].y == 0
    limit = tray_menu.scroll_limit(lay, height)
    assert limit == lay.height - height
    bottom = tray_menu.visible_rows(lay, limit, height)
    assert bottom[-1][0] == len(rows) - 1           # последний ряд виден
    assert all(rect.y + rect.h <= height for _i, rect in bottom)
    middle = tray_menu.visible_rows(lay, 60, height)
    # ряд на границе окна может быть обрезан сверху - но он всё ещё виден
    assert middle[0][1].y <= 0 and middle[0][1].y + middle[0][1].h > 0
    assert all(rect.y < height for _i, rect in middle)

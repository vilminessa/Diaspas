"""Тёмное popup-меню трея: своё окно вместо TrackPopupMenuEx.

Нативное меню нельзя затемнить и анимировать: MF_OWNERDRAW в Windows 11
перекрывается темой Explorer, а появление меню решает система. Поэтому меню -
наше popup-окно (layered), которое рисуем сами: фон и текст из дизайн-системы,
плавное появление (fade + лёгкий slide), подсветка строки, каскад подменю,
клавиатурная навигация.

Два слоя, как в :mod:`core.tray`: чистая геометрия (:func:`menu_rows`,
:func:`layout`, :func:`place`, :func:`submenu_rect`, :func:`row_index`,
:func:`nav`, :func:`fade_alpha`) проверяется офлайн; клей Windows
(:class:`MenuHost`) - сообщения и GDI, и он мягко деградирует: если окно не
создаётся, :meth:`MenuHost.open` возвращает ``False`` и caller использует
нативный ``TrackPopupMenuEx``.

Меню НЕ блокирует нить трея (в отличие от ``TrackPopupMenuEx``): окно живёт на
своих сообщениях, поэтому иконка продолжает обновляться, пока меню открыто.
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass
from typing import Callable

# -- цвета: те же токены, что ui_src/app.css -------------------------------

BG = (0x12, 0x16, 0x1C)        # фон меню (совпадает с окном приложения)
BORDER = (0x2A, 0x31, 0x40)    # рамка и разделители
TEXT = (0xE6, 0xEB, 0xF2)      # основной текст
MUTED = (0x8B, 0x95, 0xA5)     # гашеные пункты и стрелка подменю
HOVER = (0x1F, 0x2A, 0x37)     # подсветка строки
ACCENT = (0x3F, 0xBF, 0x6B)    # отметка активного пункта


def colorref(rgb: tuple[int, int, int]) -> int:
    """COLORREF = R | G<<8 | B<<16 (hex-токен задаёт порядок R,G,B)."""
    r, g, b = rgb
    return r | (g << 8) | (b << 16)


# -- геометрия меню (без Win32) --------------------------------------------

@dataclass(frozen=True)
class Row:
    id: str            # id действия из core.tray; "" для разделителя
    label: str
    checked: bool = False
    enabled: bool = True
    submenu: bool = False
    kind: str = "item"     # item | sep

    def clickable(self) -> bool:
        return self.kind == "item" and self.enabled


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h


@dataclass(frozen=True)
class Layout:
    rows: tuple[Row, ...]
    rects: tuple[Rect, ...]      # строки в координатах контента (y от 0)
    width: int
    height: int


def _label(entry) -> str:
    """Метка пункта: пояснение (cooldown, «подбор идёт») в скобках целиком."""
    note = getattr(entry, "note", "") or ""
    return entry.label + (f"  ({note})" if note else "")


def menu_rows(spec) -> tuple[Row, ...]:
    """TraySpec -> строки меню; подменю становится кликабельной строкой."""
    rows: list[Row] = []
    for entry in spec.items:
        items = getattr(entry, "items", None)
        if items is not None:                     # Submenu
            rows.append(Row(entry.id, _label(entry), submenu=True))
        elif hasattr(entry, "id"):                # Item
            rows.append(Row(entry.id, _label(entry), checked=bool(entry.checked),
                            enabled=bool(entry.enabled)))
        else:                                     # Separator
            rows.append(Row("", "", kind="sep"))
    return tuple(rows)


def submenu_rows(spec, menu_id: str) -> tuple[Row, ...]:
    """Строки подменю по его id; неизвестный id -> пусто (строка не открывается)."""
    for entry in spec.items:
        items = getattr(entry, "items", None)
        if items is not None and entry.id == menu_id:
            return tuple(Row(it.id, _label(it), checked=bool(it.checked),
                             enabled=bool(it.enabled)) for it in items)
    return ()


def layout(rows: tuple[Row, ...], measure: Callable[[str], tuple[int, int]],
           row_h: int = 24, mark_w: int = 18, pad_right: int = 26,
           sep_h: int = 9, min_w: int = 180, max_w: int = 460) -> Layout:
    """Размер меню и rects строк по метрике текста ``measure``.

    Левая колонка (``mark_w``) - под отметку активного пункта, правая
    (``pad_right``) - под метку подменю; разделитель занимает ``sep_h`` и
    не кликабельен. Ширина ограничена min/max, чтобы меню не расползалось.
    """
    widest = 0
    ys: list[int] = []
    heights: list[int] = []
    for row in rows:
        h = sep_h if row.kind == "sep" else row_h
        heights.append(h)
        ys.append(sum(heights[:-1]))
        if row.kind == "item":
            w, _ = measure(row.label)
            extra = mark_w + (14 if row.submenu else 0)
            widest = max(widest, w + extra)
    width = max(min_w, min(max_w, widest + mark_w + pad_right))
    rects = tuple(Rect(mark_w, y, width - mark_w - pad_right, h)
                  for y, h in zip(ys, heights))
    return Layout(rows=rows, rects=rects, width=width, height=sum(heights))


def place(pt: tuple[int, int], size: tuple[int, int], work: Rect) -> Rect:
    """Позиция у курсора: ниже курсора, если влезает; иначе выше (как в трее).

    По x: слева от курсора; если справа не влезает - меню разворачивается
    влево от курсора. Ни один край не выходит за work-area.
    """
    w, h = size
    x, y = pt
    if y + h <= work.bottom:
        top = y
    elif y - h >= work.y:
        top = y - h
    else:
        top = work.y
    if x + w <= work.right:
        left = x
    elif x - w >= work.x:
        left = x - w
    else:
        left = max(work.x, work.right - w)
    return Rect(left, top, w, h)


def submenu_rect(parent: Rect, row: Rect, size: tuple[int, int],
                 work: Rect) -> Rect:
    """Дочернее меню справа от строки подменю; flip влево при правом крае."""
    w, h = size
    left = parent.right
    if left + w > work.right:
        left = parent.x - w if parent.x - w >= work.x else max(work.x,
                                                               work.right - w)
    top = parent.y + row.y
    if top + h > work.bottom:
        top = max(work.y, work.bottom - h)
    return Rect(left, top, w, h)


def visible_rows(lay: Layout, scroll: int, height: int) -> list[tuple[int, Rect]]:
    """Строки, видимые в окне высотой ``height`` со смещением ``scroll``."""
    out: list[tuple[int, Rect]] = []
    for i, rect in enumerate(lay.rects):
        y = rect.y - scroll
        if y >= height:
            break
        if y + rect.h < 0:
            continue
        out.append((i, Rect(rect.x, y, rect.w, rect.h)))
    return out


def row_index(lay: Layout, x: int, y: int) -> int | None:
    """Индекс строки по координате контента; разделитель не кликабельен."""
    for i, rect in enumerate(lay.rects):
        if rect.y <= y < rect.y + rect.h:
            return i if lay.rows[i].kind != "sep" else None
    return None


def nav(lay: Layout, index: int | None, delta: int) -> int | None:
    """Up/Down по строкам, пропуская разделители (гашеные остаются: Enter отказ).

    Без highlighting Up и Down ведут на первый пункт - как нативное меню:
    с конца списка вверх было бы неудобно при первом нажатии.
    """
    items = [i for i, row in enumerate(lay.rows) if row.kind != "sep"]
    if not items:
        return None
    if index is None or index not in items:
        return items[0]
    pos = items.index(index)
    return items[max(0, min(len(items) - 1, pos + delta))]


# -- анимация ---------------------------------------------------------------

FADE_MS = 110      # появление основного меню
SUBMENU_MS = 80    # появление дочернего
SLIDE_PX = 6       # лёгкий сдвиг к финальной позиции
STEP_MS = 16


def fade_alpha(elapsed: int, total: int = FADE_MS) -> int:
    """ease-out 0 -> 255; после конца держит 255."""
    if total <= 0:
        return 255
    t = min(1.0, max(0.0, elapsed / total))
    return int(round(255 * (1 - (1 - t) ** 2)))


def slide_offset(elapsed: int, total: int = FADE_MS, px: int = SLIDE_PX) -> int:
    """Сдвиг по y: -px -> 0 (меню «доезжает» вниз)."""
    if total <= 0:
        return 0
    t = min(1.0, max(0.0, elapsed / total))
    return int(-px * (1 - t))


def scroll_limit(lay: Layout, height: int) -> int:
    """Максимальный scroll: список длиннее окна на сколько-нибудь пикселей."""
    return max(0, lay.height - height)


# -- клей Windows -----------------------------------------------------------

if sys.platform == "win32":
    # Структуры и restype/argtypes объявлены в core.win32 один раз:
    # ctypes.windll.user32 - общий объект, и чьи argtypes заданы последним, те
    # и действуют; два модуля со своими POINT/WNDCLASSEXW мешают друг другу.
    from . import win32

    user32 = win32.user32
    gdi32 = win32.gdi32
    kernel32 = win32.kernel32
    VP = win32.VP
    HD = win32.HD          # GDI-объекты - 32-битные handles даже на x64
    WNDPROC = win32.WNDPROC
    WNDCLASSEXW = win32.WNDCLASSEXW
    RECT = win32.RECT
    POINT = win32.POINT
    PAINTSTRUCT = win32.PAINTSTRUCT
    round_px = win32.round_px   # CSS-размеры в физических пикселях экрана

    WM_PAINT = win32.WM_PAINT
    WM_TIMER = win32.WM_TIMER
    WM_MOUSEMOVE = win32.WM_MOUSEMOVE
    WM_LBUTTONUP = win32.WM_LBUTTONUP
    WM_RBUTTONDOWN = win32.WM_RBUTTONDOWN
    WM_RBUTTONUP = win32.WM_RBUTTONUP
    WM_MOUSEWHEEL = win32.WM_MOUSEWHEEL
    WM_KEYDOWN = win32.WM_KEYDOWN
    VK_ESCAPE = win32.VK_ESCAPE
    VK_RETURN = win32.VK_RETURN
    VK_UP = win32.VK_UP
    VK_DOWN = win32.VK_DOWN
    VK_LEFT = win32.VK_LEFT
    VK_RIGHT = win32.VK_RIGHT

    WS_POPUP = win32.WS_POPUP
    WS_EX_TOPMOST = win32.WS_EX_TOPMOST
    WS_EX_TOOLWINDOW = win32.WS_EX_TOOLWINDOW
    WS_EX_LAYERED = win32.WS_EX_LAYERED
    WS_EX_NOACTIVATE = win32.WS_EX_NOACTIVATE
    LWA_ALPHA = win32.LWA_ALPHA
    SWP_NOACTIVATE = win32.SWP_NOACTIVATE
    SWP_SHOWWINDOW = win32.SWP_SHOWWINDOW
    HWND_TOPMOST = win32.HWND_TOPMOST
    DT_LEFT = win32.DT_LEFT
    OPAQUE = win32.OPAQUE

    TIMER_ANIM = win32.TIMER_ANIM
    TIMER_SUBMENU = win32.TIMER_SUBMENU

    def _monitor_work(pt: POINT) -> Rect:
        work = win32.monitor_work(pt, user32)
        return Rect(work.left, work.top, work.right - work.left,
                    work.bottom - work.top)

    def _dpi_of(hwnd) -> int:
        return win32.dpi_of(hwnd, user32)

    def _measure(text: str, font) -> tuple[int, int]:
        return win32.measure(text, font, gdi32)

    def _lo_hi(value) -> tuple[int, int]:
        return win32.lo_hi(value)

    def _signed_hi(value) -> int:
        return win32.signed_hi(value)

    class _Menu:
        """Одно окно меню: строки, scroll, подсветка, дочернее подменю."""

        def __init__(self, host: "MenuHost", hwnd, lay: Layout, spec,
                     on_action: Callable[[str, None], None],
                     parent: "_Menu | None", font, dpi: int,
                     work: Rect, row_index: int | None = None) -> None:
            self.host = host
            self.hwnd = hwnd
            self.lay = lay
            self.spec = spec
            self.on_action = on_action
            self.parent = parent
            self.font = font
            self.dpi = dpi
            self.work = work
            self.row_index = row_index      # строка подменю в родителе
            self.rect = Rect(0, 0, lay.width, lay.height)
            self.max_h = lay.height
            self.hover: int | None = None
            self.scroll = 0
            self.child: _Menu | None = None
            self.elapsed = 0
            self.brushes: dict[str, int] = {}

        # -- отрисовка -----------------------------------------------------

        def _brush(self, name: str, rgb) -> int:
            if name not in self.brushes:
                self.brushes[name] = gdi32.CreateSolidBrush(colorref(rgb))
            return self.brushes[name]

        def paint(self) -> None:
            ps = PAINTSTRUCT()
            dc = user32.BeginPaint(self.hwnd, ctypes.byref(ps))
            inset = round_px(4, self.dpi)
            mark_w = round_px(18, self.dpi)
            arrow_w = round_px(16, self.dpi)
            user32.FillRect(dc, ctypes.byref(RECT(0, 0, self.lay.width,
                                                  self.max_h)),
                            self._brush("bg", BG))
            gdi32.SelectObject(dc, self.font)
            gdi32.SetBkMode(dc, OPAQUE)
            for i, r in visible_rows(self.lay, self.scroll, self.max_h):
                row = self.lay.rows[i]
                if row.kind == "sep":
                    y = r.y + r.h // 2
                    user32.FillRect(dc,
                                    ctypes.byref(RECT(inset, y,
                                                      self.lay.width - inset,
                                                      y + 1)),
                                    self._brush("border", BORDER))
                    continue
                if self.hover == i:
                    user32.FillRect(dc,
                                    ctypes.byref(RECT(0, r.y, self.lay.width,
                                                      r.bottom)),
                                    self._brush("hover", HOVER))
                if row.checked:
                    gdi32.SetTextColor(dc, colorref(ACCENT))
                    user32.DrawTextW(dc, "\u2713", 1,
                                     ctypes.byref(RECT(inset, r.y, mark_w,
                                                       r.bottom)), DT_LEFT)
                if row.submenu:
                    gdi32.SetTextColor(dc, colorref(MUTED))
                    user32.DrawTextW(dc, "\u25b8", 1,
                                     ctypes.byref(RECT(self.lay.width - arrow_w,
                                                       r.y, self.lay.width,
                                                       r.bottom)), DT_LEFT)
                gdi32.SetTextColor(dc, colorref(TEXT if row.enabled else MUTED))
                user32.DrawTextW(dc, row.label, len(row.label),
                                 ctypes.byref(RECT(r.x, r.y + inset, r.right,
                                                   r.bottom)), DT_LEFT)
            for rect in (RECT(0, 0, 1, self.max_h),
                         RECT(self.lay.width - 1, 0, self.lay.width, self.max_h),
                         RECT(0, 0, self.lay.width, 1),
                         RECT(0, self.max_h - 1, self.lay.width, self.max_h)):
                user32.FillRect(dc, ctypes.byref(rect),
                                self._brush("border", BORDER))
            user32.EndPaint(self.hwnd, ctypes.byref(ps))

        # -- сообщения -----------------------------------------------------

        def on_message(self, message, wparam, lparam) -> int:
            if message == WM_PAINT:
                self.paint()
                return 0
            if message == WM_TIMER:
                self._tick(int(wparam or 0))
                return 0
            if message == WM_MOUSEMOVE:
                x, y = _lo_hi(lparam)
                self._hover(x, y + self.scroll)
                return 0
            if message == WM_LBUTTONUP:
                x, y = _lo_hi(lparam)
                self._activate(x, y + self.scroll)
                return 0
            if message in (WM_RBUTTONDOWN, WM_RBUTTONUP):
                self.host.close_all()
                return 0
            if message == WM_MOUSEWHEEL:
                self._set_scroll(self.scroll - (3 if _signed_hi(wparam) > 0
                                                else -3))
                return 0
            if message == WM_KEYDOWN:
                self._key(int(wparam or 0))
                return 0
            return int(user32.DefWindowProcW(self.hwnd, message, wparam, lparam))

        def _tick(self, timer: int) -> None:
            if timer == TIMER_SUBMENU:
                self._open_hovered_submenu()
                return
            total = FADE_MS if self.parent is None else SUBMENU_MS
            self.elapsed += STEP_MS
            alpha = fade_alpha(self.elapsed, total)
            user32.SetLayeredWindowAttributes(self.hwnd, 0, alpha, LWA_ALPHA)
            offset = slide_offset(self.elapsed, total) if alpha < 255 else 0
            user32.SetWindowPos(self.hwnd, VP(0), self.rect.x,
                                self.rect.y + offset, self.lay.width,
                                self.max_h, SWP_NOACTIVATE)
            if alpha >= 255:
                user32.KillTimer(self.hwnd, TIMER_ANIM)

        def _hover(self, x: int, y: int) -> None:
            index = row_index(self.lay, x, y)
            if index == self.hover:
                return
            self.hover = index
            user32.InvalidateRect(self.hwnd, None, True)
            if index is not None and self.lay.rows[index].submenu:
                user32.SetTimer(self.hwnd, TIMER_SUBMENU, 180, None)
                return
            user32.KillTimer(self.hwnd, TIMER_SUBMENU)
            if index is not None:
                self._close_child()

        def _set_scroll(self, value: int) -> None:
            self.scroll = max(0, min(scroll_limit(self.lay, self.max_h), value))
            user32.InvalidateRect(self.hwnd, None, True)

        def _open_hovered_submenu(self) -> None:
            if self.hover is None:
                return
            row = self.lay.rows[self.hover]
            if not row.submenu:
                return
            if self.child is not None and self.child.row_index == self.hover:
                return
            self._close_child()
            self.child = self.host._child(self, self.hover)

        def _close_child(self) -> None:
            if self.child is not None:
                self.child.close()
                self.child = None

        def _activate(self, x: int, y: int) -> None:
            index = row_index(self.lay, x, y)
            if index is None or not self.lay.rows[index].clickable():
                return
            action = self.lay.rows[index].id
            self.host.close_all()
            self.on_action(action)

        def _key(self, key: int) -> None:
            if key == VK_ESCAPE:
                self.host.close_all()
            elif key == VK_DOWN:
                self._move(1)
            elif key == VK_UP:
                self._move(-1)
            elif key == VK_RIGHT:
                self._open_hovered_submenu()
            elif key == VK_LEFT:
                self._close_child()
            elif key == VK_RETURN and self.hover is not None:
                row = self.lay.rows[self.hover]
                if row.clickable():
                    action = row.id
                    self.host.close_all()
                    self.on_action(action)

        def _move(self, delta: int) -> None:
            index = nav(self.lay, self.hover, delta)
            if index is None or index == self.hover:
                return
            self.hover = index
            self._ensure_visible(index)
            self._close_child()
            if self.lay.rows[index].submenu:
                self._open_hovered_submenu()
            user32.InvalidateRect(self.hwnd, None, True)

        def _ensure_visible(self, index: int) -> None:
            rect = self.lay.rects[index]
            if rect.bottom - self.scroll > self.max_h:
                self._set_scroll(rect.bottom - self.max_h)
            elif rect.y - self.scroll < 0:
                self._set_scroll(rect.y)

        def close(self) -> None:
            """Уничтожить окно и свои GDI-объекты (дочернее меню тоже)."""
            if self.child is not None:
                self.child.close()
                self.child = None
            if self.hwnd is None:
                return
            user32.KillTimer(self.hwnd, TIMER_ANIM)
            user32.KillTimer(self.hwnd, TIMER_SUBMENU)
            for brush in self.brushes.values():
                gdi32.DeleteObject(brush)
            self.brushes.clear()
            gdi32.DeleteObject(self.font)
            self.font = None
            handle = self.hwnd
            self.hwnd = None
            self.host._menus.pop(int(handle), None)
            user32.DestroyWindow(handle)

    class _ShieldWindow:
        """Прозрачное окно поверх work-area: первый клик вне меню закрывает его.

        alpha=1: визуально полностью прозрачно, но мышиные сообщения принимает
        (alpha 0 их пропускает) и не активится - фокус приложения не крадёт.
        """

        def __init__(self, host: "MenuHost", hwnd) -> None:
            self.host = host
            self.hwnd = hwnd

        def on_message(self, message, wparam, lparam) -> int:
            if message in (WM_LBUTTONUP, WM_RBUTTONDOWN, WM_RBUTTONUP):
                self.host.close_all()
                return 0
            return int(user32.DefWindowProcW(self.hwnd, message, wparam, lparam))

    class MenuHost:
        """Окна тёмного меню трея: родитель + каскад подменю + щит кликов.

        Живёт в нити трея: все окна создруются и обрабатываются в ней,
        поэтому нить не блокируется (иконка обновляется при открытом меню).
        """

        MENU_CLASS = "DiaspasTrayMenu"
        SHIELD_CLASS = "DiaspasTrayShield"

        def __init__(self, log: Callable[[str], None] | None = None) -> None:
            self._log = log or (lambda _text: None)
            self._menus: dict[int, _Menu] = {}
            self._shield: _ShieldWindow | None = None
            self._root: _Menu | None = None
            self._procs: list[WNDPROC] = []   # ctypes не держит wnd proc сам
            self._classes: set[str] = set()
            self._restore_focus: int | None = None

        # -- публичное -----------------------------------------------------

        def open(self, spec, on_action: Callable[[str], None]) -> bool:
            """Открыть меню у курсора. False - клей не поднялся: fallback."""
            try:
                pt = POINT()
                if not user32.GetCursorPos(ctypes.byref(pt)):
                    raise OSError("GetCursorPos отказал")
                work = _monitor_work(pt)
                self._restore_focus = user32.GetForegroundWindow()
                self._ensure_shield(work)
                rows = menu_rows(spec)
                menu = self._create(rows, spec, on_action, parent=None,
                                    work=work, is_submenu=False)
                rect = place((pt.x, pt.y), (menu.lay.width, menu.lay.height),
                             work)
                menu.rect = rect
                menu.max_h = min(menu.lay.height, work.h)
                user32.SetWindowPos(menu.hwnd, HWND_TOPMOST, rect.x,
                                    rect.y + slide_offset(0), menu.lay.width,
                                    menu.max_h, SWP_SHOWWINDOW | SWP_NOACTIVATE)
                user32.SetLayeredWindowAttributes(menu.hwnd, 0, 0, LWA_ALPHA)
                user32.SetTimer(menu.hwnd, TIMER_ANIM, STEP_MS, None)
                user32.SetForegroundWindow(menu.hwnd)
                self._root = menu
                return True
            except Exception as exc:  # noqa: BLE001 - меню не валит трей
                self._log(f"меню трея не поднялось: {type(exc).__name__}: {exc}")
                self.close_all()
                return False

        def close_all(self) -> None:
            """Закрыть меню, каскад и щит (съезд иконки, выход из приложения)."""
            for menu in list(self._menus.values()):
                menu.close()
            self._menus.clear()
            self._root = None
            if self._shield is not None:
                user32.DestroyWindow(self._shield.hwnd)
                self._shield = None
            if self._restore_focus:
                user32.SetForegroundWindow(self._restore_focus)
                self._restore_focus = None

        def is_open(self) -> bool:
            return bool(self._menus)

        # -- окна ----------------------------------------------------------

        def _register(self, class_name: str) -> None:
            if class_name in self._classes:
                return
            instance = kernel32.GetModuleHandleW(None)

            def proc(hwnd, message, wparam, lparam):
                key = int(hwnd or 0)
                menu = self._menus.get(key)
                if menu is not None:
                    return menu.on_message(message, wparam, lparam)
                if self._shield is not None and key == int(self._shield.hwnd):
                    return self._shield.on_message(message, wparam, lparam)
                return int(user32.DefWindowProcW(hwnd, message, wparam, lparam))

            wnd_proc = WNDPROC(proc)
            self._procs.append(wnd_proc)
            wc = WNDCLASSEXW(ctypes.sizeof(WNDCLASSEXW), 0, wnd_proc, 0, 0,
                             instance, None, user32.LoadCursorW(None, 32512),
                             None, None, class_name, None)
            if not user32.RegisterClassExW(ctypes.byref(wc)):
                raise OSError(f"RegisterClassExW({class_name}) не удалось "
                              f"(err={kernel32.GetLastError()})")
            self._classes.add(class_name)

        def _create(self, rows, spec, on_action, parent, work: Rect,
                    is_submenu: bool, row_index: int | None = None) -> _Menu:
            self._register(self.MENU_CLASS)
            hwnd = user32.CreateWindowExW(
                WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_LAYERED
                | (WS_EX_NOACTIVATE if is_submenu else 0),
                self.MENU_CLASS, "Diaspas tray menu", WS_POPUP, 0, 0, 1, 1,
                None, None, None, None)
            if not hwnd:
                raise OSError(f"CreateWindowExW не вернул окно меню "
                              f"(err={kernel32.GetLastError()})")
            dpi = _dpi_of(hwnd)
            font = gdi32.CreateFontW(-round_px(9, dpi), 0, 0, 0, 400, 0, 0,
                                    0, 0, 0, 0, 0, 0, "Segoe UI")
            if not font:
                user32.DestroyWindow(hwnd)
                raise OSError("CreateFontW не вернул шрифт меню")
            lay = layout(rows, lambda text: _measure(text, font),
                         row_h=round_px(24, dpi), mark_w=round_px(18, dpi),
                         pad_right=round_px(26, dpi), sep_h=round_px(9, dpi),
                         min_w=round_px(180, dpi), max_w=round_px(460, dpi))
            menu = _Menu(self, hwnd, lay, spec, on_action, parent, font, dpi,
                         work, row_index)
            self._menus[int(hwnd)] = menu
            return menu

        def _child(self, parent: _Menu, index: int) -> _Menu | None:
            row = parent.lay.rows[index]
            rows = submenu_rows(parent.spec, row.id)
            if not rows:
                return None
            child = self._create(rows, parent.spec, parent.on_action,
                                 parent=parent, work=parent.work,
                                 is_submenu=True, row_index=index)
            child.max_h = min(child.lay.height, parent.work.h)
            child.rect = submenu_rect(parent.rect, parent.lay.rects[index],
                                      (child.lay.width, child.max_h),
                                      parent.work)
            user32.SetWindowPos(child.hwnd, HWND_TOPMOST, child.rect.x,
                                child.rect.y + slide_offset(0),
                                child.lay.width, child.max_h,
                                SWP_SHOWWINDOW | SWP_NOACTIVATE)
            user32.SetLayeredWindowAttributes(child.hwnd, 0, 0, LWA_ALPHA)
            user32.SetTimer(child.hwnd, TIMER_ANIM, STEP_MS, None)
            return child

        def _ensure_shield(self, work: Rect) -> None:
            if self._shield is not None:
                return
            self._register(self.SHIELD_CLASS)
            hwnd = user32.CreateWindowExW(
                WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_LAYERED
                | WS_EX_NOACTIVATE, self.SHIELD_CLASS, "Diaspas tray shield",
                WS_POPUP, work.x, work.y, work.w, work.h, None, None, None,
                None)
            if not hwnd:
                raise OSError("CreateWindowExW не вернул окно щита")
            user32.SetLayeredWindowAttributes(hwnd, 0, 1, LWA_ALPHA)
            self._shield = _ShieldWindow(self, hwnd)

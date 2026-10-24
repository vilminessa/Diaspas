"""Клей тёмного меню трея офлайн: user32/gdi32 подменены, поведение проверено.

Проверяется то, что нельзя увидеть в модели: сколько окон создаётся, что
анимация ставит таймеры и снимает их, что клик вне меню (щит) закрывает меню,
что каскад подменю создаёт и销毁 дочернее окно, и что отказ создать окно
возвращает False - Tray тогда использует нативный TrackPopupMenuEx.
"""

import ctypes
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import tray, tray_menu, win32  # noqa: E402

if sys.platform != "win32":
    pytest.skip("клей меню проверяется только на Windows", allow_module_level=True)

STATE = {"dir_ok": True, "installed": True, "running": True,
         "strategy": "general (ALT11)", "strategy_label": "ALT11",
         "strategy_mode": "manual", "preset_key": "discord_youtube",
         "preset_title": "Discord / YouTube", "auto_cooldown_left": 0,
         "auto_last_strategy": "",
         "strategies": [{"file": "general (ALT11).bat", "label": "ALT11",
                         "active": True}]}
PRESETS = [{"key": "discord_youtube", "title": "Discord / YouTube", "off": False}]


def spec() -> tray.TraySpec:
    return tray.tray_model(STATE, PRESETS)


def pack(x: int, y: int) -> int:
    return (y << 16) | x


def struct_of(ptr, cls):
    """byref() приходит указателем: cast возвращает доступ к полям структуры."""
    return ctypes.cast(ptr, ctypes.POINTER(cls))[0]


class FakeUser32:
    """Записывает вызовы: окна, таймеры, alpha, позиции, текст."""

    def __init__(self, fail_create: bool = False) -> None:
        self.created: list[tuple[str, int]] = []
        self.destroyed: list[int] = []
        self.timers: dict[tuple[int, int], int] = {}
        self.killed: list[tuple[int, int]] = []
        self.alpha: dict[int, int] = {}
        self.pos: list[tuple[int, int, int, int, int]] = []
        self.texts: list[str] = []
        self.fills = 0
        self.invalidated = 0
        self.foreground: list[int] = []
        self._next = 1000
        self.fail_create = fail_create

    # -- окна --
    def CreateWindowExW(self, exstyle, cls, title, style, x, y, w, h,
                        *_rest):
        if self.fail_create:
            return None
        hwnd = self._next
        self._next += 1
        self.created.append((cls, exstyle))
        return hwnd

    def DestroyWindow(self, hwnd):
        self.destroyed.append(int(hwnd))
        return True

    def SetWindowPos(self, hwnd, _after, x, y, w, h, _flags):
        self.pos.append((int(hwnd), x, y, w, h))
        return True

    def SetLayeredWindowAttributes(self, hwnd, _color, alpha, _flags):
        self.alpha[int(hwnd)] = alpha
        return True

    def RegisterClassExW(self, _wc):
        return 1

    def LoadCursorW(self, _instance, _id):
        return 7

    def SetForegroundWindow(self, hwnd):
        self.foreground.append(int(hwnd or 0))
        return True

    def GetForegroundWindow(self):
        return 4242

    # -- курсор и монитор --
    def GetCursorPos(self, pt):
        p = struct_of(pt, win32.POINT)
        p.x, p.y = 1500, 1000       # трей внизу справа
        return True

    def MonitorFromPoint(self, _pt, _flags):
        return 55

    def GetMonitorInfoW(self, _handle, info):
        work = struct_of(info, win32.MONITORINFO).rcWork
        work.left, work.top = 0, 0
        work.right, work.bottom = 1920, 1040
        return True

    def GetDpiForWindow(self, _hwnd):
        return 96

    # -- таймеры и перерисовка --
    def SetTimer(self, hwnd, timer, ms, _proc):
        self.timers[(int(hwnd), timer)] = ms
        return timer

    def KillTimer(self, hwnd, timer):
        self.killed.append((int(hwnd), timer))
        return True

    def InvalidateRect(self, _hwnd, _rect, _erase):
        self.invalidated += 1
        return True

    def BeginPaint(self, _hwnd, _ps):
        return 900

    def EndPaint(self, _hwnd, _ps):
        return None

    def FillRect(self, _dc, _rect, _brush):
        self.fills += 1
        return 0

    def DrawTextW(self, _dc, text, _count, _rect, _flags):
        self.texts.append(text)
        return len(text)

    def DefWindowProcW(self, _hwnd, _message, _w, _l):
        return 0


class FakeGdi32:
    def __init__(self) -> None:
        self.fonts = 0
        self.brushes = 0
        self.deleted = 0

    def CreateFontW(self, *_a):
        self.fonts += 1
        return 12345

    def CreateSolidBrush(self, _color):
        self.brushes += 1
        return 54321

    def DeleteObject(self, _obj):
        self.deleted += 1
        return True

    def CreateCompatibleDC(self, _src):
        return 777

    def SelectObject(self, _dc, _obj):
        return 777

    def DeleteDC(self, _dc):
        return True

    def GetTextExtentPoint32W(self, _dc, text, _count, out):
        arr = struct_of(out, ctypes.c_long * 2)
        arr[0] = len(text) * 8
        arr[1] = 14
        return True

    def SetTextColor(self, _dc, _color):
        return 0

    def SetBkMode(self, _dc, _mode):
        return 0


class FakeKernel:
    def GetModuleHandleW(self, _name):
        return 1

    def GetLastError(self):
        return 0


def host(monkeypatch, fail_create=False) -> tray_menu.MenuHost:
    fake = FakeUser32(fail_create=fail_create)
    monkeypatch.setattr(tray_menu, "user32", fake)
    monkeypatch.setattr(tray_menu, "gdi32", FakeGdi32())
    monkeypatch.setattr(tray_menu, "kernel32", FakeKernel())
    return fake, tray_menu.MenuHost()


def test_open_creates_menu_and_shield_with_layered_alpha(monkeypatch):
    fake, h = host(monkeypatch)
    actions: list[str] = []
    assert h.open(spec(), actions.append) is True
    classes = [cls for cls, _ex in fake.created]
    assert classes == [tray_menu.MenuHost.SHIELD_CLASS, tray_menu.MenuHost.MENU_CLASS]
    menu_hwnd = int(list(h._menus)[0])
    shield_hwnd = int(h._shield.hwnd)
    # щит прозрачен (alpha 1 - принимает клики), меню в начале alpha 0
    assert fake.alpha[shield_hwnd] == 1
    assert fake.alpha[menu_hwnd] == 0
    # анимация: таймер на шаг появления
    assert fake.timers.get((menu_hwnd, tray_menu.TIMER_ANIM)) == tray_menu.STEP_MS
    # фокус запоминается и передаётся меню (клавиатура работает)
    assert fake.foreground == [menu_hwnd]
    assert h._restore_focus == 4242
    # позиция у курсора: трей внизу -> меню над курсором, в пределах экрана
    rect = h._root.rect
    assert rect.right <= 1920 and rect.bottom <= 1040
    assert rect.bottom <= 1000      # нижний край меню у курсора


def test_paint_draws_every_item_row_once(monkeypatch):
    fake, h = host(monkeypatch)
    h.open(spec(), lambda _a: None)
    menu = h._root
    menu.on_message(tray_menu.WM_PAINT, 0, 0)
    labels = [menu.lay.rows[i].label for i, row in enumerate(menu.lay.rows)
              if row.kind == "item"]
    drawn = [text for text in fake.texts if text in labels]
    assert drawn == labels, fake.texts
    # строки подменю помечены стрелкой; отметка активного пункта - в дочернем
    assert fake.texts.count("\u25b8") == 2
    assert "\u2713" not in fake.texts            # в корневом меню нет checked
    fake.texts.clear()
    menu.on_message(tray_menu.WM_TIMER, tray_menu.TIMER_SUBMENU, 0)   # hover? нет
    assert menu.child is None                    # таймер без hover ничего не открывает
    index = next(i for i, row in enumerate(menu.lay.rows) if row.submenu)
    menu.on_message(tray_menu.WM_MOUSEMOVE, 0,
                    pack(menu.lay.rects[index].x + 2, menu.lay.rects[index].y + 2))
    menu.on_message(tray_menu.WM_TIMER, tray_menu.TIMER_SUBMENU, 0)
    menu.child.on_message(tray_menu.WM_PAINT, 0, 0)
    assert "\u2713" in fake.texts                # активный пресет отмечен
    assert fake.texts.count("\u25b8") == 0       # в дочернем стрелок нет


def test_scroll_for_a_list_taller_than_the_screen(monkeypatch):
    fake, h = host(monkeypatch)
    actions: list[str] = []
    long_spec = tray.TraySpec(
        tooltip="t", icon_state="ok",
        items=tuple(tray.Item(f"a{i}", f"пункт {i}") for i in range(60)))
    h.open(long_spec, actions.append)
    menu = h._root
    assert menu.max_h <= 1040
    assert tray_menu.scroll_limit(menu.lay, menu.max_h) > 0


def test_click_on_item_closes_menu_and_runs_action(monkeypatch):
    fake, h = host(monkeypatch)
    actions: list[str] = []
    h.open(spec(), actions.append)
    menu = h._root
    index = next(i for i, row in enumerate(menu.lay.rows)
                 if row.id == tray.SHOW and row.clickable())
    rect = menu.lay.rects[index]
    menu.on_message(tray_menu.WM_LBUTTONUP, 0, pack(rect.x + 2, rect.y + 2))
    assert actions == [tray.SHOW]
    assert not h.is_open()
    assert len(fake.destroyed) == len(fake.created)     # меню + щит уничтожены
    assert fake.killed                                  # таймеры сняты


def test_click_on_disabled_item_does_nothing(monkeypatch):
    fake, h = host(monkeypatch)
    actions: list[str] = []
    h.open(spec(), actions.append)
    menu = h._root
    index = next(i for i, row in enumerate(menu.lay.rows) if row.id == tray.AUTO_RUN)
    rect = menu.lay.rects[index]
    menu.on_message(tray_menu.WM_MOUSEMOVE, 0, pack(rect.x + 2, rect.y + 2))
    assert menu.hover == index           # гашеную строку видно (подсветка есть)
    menu.on_message(tray_menu.WM_LBUTTONUP, 0, pack(rect.x + 2, rect.y + 2))
    assert actions == []                 # но она не исполняется
    assert h.is_open()                   # и меню не закрывается


def test_outside_click_on_shield_closes_menu(monkeypatch):
    fake, h = host(monkeypatch)
    actions: list[str] = []
    h.open(spec(), actions.append)
    shield_hwnd = int(h._shield.hwnd)
    h._shield.on_message(tray_menu.WM_LBUTTONUP, 0, pack(500, 500))
    assert not h.is_open()
    assert actions == []            # клик вне меню закрывает, не исполняет
    assert shield_hwnd in fake.destroyed


def test_right_click_on_menu_closes_it(monkeypatch):
    fake, h = host(monkeypatch)
    actions: list[str] = []
    h.open(spec(), actions.append)
    h._root.on_message(tray_menu.WM_RBUTTONUP, 0, 0)
    assert actions == [] and not h.is_open()


def test_submenu_cascade_opens_and_closes(monkeypatch):
    fake, h = host(monkeypatch)
    actions: list[str] = []
    h.open(spec(), actions.append)
    menu = h._root
    index = next(i for i, row in enumerate(menu.lay.rows) if row.submenu)
    rect = menu.lay.rects[index]
    menu.on_message(tray_menu.WM_MOUSEMOVE, 0, pack(rect.x + 2, rect.y + 2))
    # hover подменю -> таймер задержки, дочернее окно ещё не создано
    assert (int(menu.hwnd), tray_menu.TIMER_SUBMENU) in fake.timers
    assert menu.child is None
    menu.on_message(tray_menu.WM_TIMER, tray_menu.TIMER_SUBMENU, 0)
    assert menu.child is not None
    assert len(fake.created) == 3, "меню + щит + дочернее"
    child_rows = tray_menu.submenu_rows(spec(), "presets")
    assert [row.id for row in menu.child.lay.rows] == [row.id for row in child_rows]
    assert menu.child.parent is menu
    # дочернее меню каскадом: справа от родителя, а при правом крае - слева
    child = menu.child
    cascades_right = child.rect.x >= menu.rect.right - 1
    cascades_left = child.rect.right <= menu.rect.x + 1
    assert cascades_right or cascades_left, (menu.rect, child.rect)
    assert child.rect.x >= 0 and child.rect.right <= 1920 and child.rect.bottom <= 1040
    # уход на другой пункт закрывает каскад
    other = next(i for i, row in enumerate(menu.lay.rows) if row.id == tray.SHOW)
    menu.on_message(tray_menu.WM_MOUSEMOVE, 0,
                    pack(menu.lay.rects[other].x + 2, menu.lay.rects[other].y + 2))
    assert menu.child is None
    assert len(fake.destroyed) == 1          # уничтожено только дочернее
    h.close_all()
    assert len(fake.destroyed) == len(fake.created)


def test_wheel_and_keys_navigate_and_execute(monkeypatch):
    fake, h = host(monkeypatch)
    actions: list[str] = []
    long_spec = tray.TraySpec(
        tooltip="t", icon_state="ok",
        items=tuple(tray.Item(f"a{i}", f"пункт {i}") for i in range(60)))
    h.open(long_spec, actions.append)
    menu = h._root
    # колесо вниз: HIWORD delta как signed short (-120)
    menu.on_message(tray_menu.WM_MOUSEWHEEL, pack(0, -120 & 0xffff), 0)
    assert menu.scroll > 0
    menu.on_message(tray_menu.WM_MOUSEWHEEL, pack(0, 120), 0)
    assert menu.scroll == 0        # вверх возвращает к началу
    menu.on_message(tray_menu.WM_KEYDOWN, tray_menu.VK_UP, 0)
    assert menu.hover == 0           # Up без highlighting ведёт на первый пункт
    menu.on_message(tray_menu.WM_KEYDOWN, tray_menu.VK_DOWN, 0)
    assert menu.hover > 0
    assert menu.lay.rows[menu.hover].clickable()
    menu.on_message(tray_menu.WM_KEYDOWN, tray_menu.VK_RETURN, 0)
    assert actions == [menu.lay.rows[menu.hover].id]
    assert not h.is_open()

    fake2 = FakeUser32()
    monkeypatch.setattr(tray_menu, "user32", fake2)
    h.open(spec(), actions.append)
    h._root.on_message(tray_menu.WM_KEYDOWN, tray_menu.VK_ESCAPE, 0)
    assert not h.is_open()


def test_nav_moves_highlight_into_view(monkeypatch):
    """Клавиши за пределами окна списка двигают scroll, чтобы строка была видна."""
    fake, h = host(monkeypatch)
    items = tuple(tray.Item(f"a{i}", f"пункт {i}") for i in range(60))
    h.open(tray.TraySpec(tooltip="t", icon_state="ok", items=items),
           lambda _a: None)
    menu = h._root
    for _ in range(60):
        menu.on_message(tray_menu.WM_KEYDOWN, tray_menu.VK_DOWN, 0)
    assert menu.hover == len(items) - 1
    assert menu.scroll > 0                       # конец списка виден
    rect = menu.lay.rects[menu.hover]
    assert 0 <= rect.y - menu.scroll <= menu.max_h - rect.h
    for _ in range(60):
        menu.on_message(tray_menu.WM_KEYDOWN, tray_menu.VK_UP, 0)
    assert menu.hover == 0 and menu.scroll == 0


def test_animation_ticks_to_full_alpha_then_kills_timer(monkeypatch):
    fake, h = host(monkeypatch)
    h.open(spec(), lambda _a: None)
    menu = h._root
    steps = []
    for _ in range(12):
        menu.on_message(tray_menu.WM_TIMER, tray_menu.TIMER_ANIM, 0)
        steps.append(menu.elapsed)
    assert menu.elapsed == 12 * tray_menu.STEP_MS
    assert fake.alpha[int(menu.hwnd)] == 255
    assert steps == sorted(steps)               # время растёт монотонно
    assert (int(menu.hwnd), tray_menu.TIMER_ANIM) in fake.killed
    # финальная позиция - без смещения
    assert fake.pos[-1][1:] == (menu.rect.x, menu.rect.y, menu.lay.width,
                                menu.max_h)


def test_close_all_destroys_every_window_and_restores_focus(monkeypatch):
    fake, h = host(monkeypatch)
    h.open(spec(), lambda _a: None)
    created = len(fake.created)
    h.close_all()
    assert len(fake.destroyed) == created
    assert fake.foreground[-1] == 4242          # фокус возвращён владельцу
    assert h._menus == {} and h._shield is None
    # повторный close_all не роняет клей
    h.close_all()


def test_open_returns_false_when_window_creation_refuses(monkeypatch):
    fake, h = host(monkeypatch, fail_create=True)
    warnings: list[str] = []
    h._log = warnings.append
    assert h.open(spec(), lambda _a: None) is False
    assert not h.is_open()
    assert warnings and "меню трея не поднялось" in warnings[0]


def test_tray_open_menu_falls_back_to_native_popup(monkeypatch, tmp_path):
    """Своё меню не поднялось -> нативный TrackPopupMenuEx, пункт исполняется."""
    from core import paths
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)

    class BrokenHost:
        def __init__(self, log=None):
            self.log = log

        def open(self, _spec, _action):
            return False

        def close_all(self):
            pass

    monkeypatch.setattr(tray_menu, "MenuHost", BrokenHost)

    codes: dict[int, str] = {}

    class FakeUser(FakeUser32):
        def CreatePopupMenu(self):
            return 1

        def AppendMenuW(self, _parent, flags, item, label):
            codes[item] = label
            return True

        def TrackPopupMenuEx(self, _menu, _flags, _x, _y, _hwnd, _p):
            return next(i for i in codes if isinstance(i, int))

        def DestroyMenu(self, _menu):
            return True

        def PostMessageW(self, *_a):
            return True

    monkeypatch.setattr(tray, "user32", FakeUser())
    actions: list[str] = []
    holder = tray.Tray(spec, actions.append, log=lambda _t: None)
    holder._hwnd = 1
    holder._open_menu(1)
    assert actions == [tray.SHOW]          # первый пункт нативного меню
    assert holder._menu is not None        # хост создан, но меню не открылось

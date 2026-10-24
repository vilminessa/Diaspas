"""Win32-клей одного вида: структуры, сообщения и declarations ctypes.

Один модуль на `core.tray` и `core.tray_menu`: `ctypes.windll.user32` - общий
объект, и чьи `argtypes` заданы последним, те и действуют. Если каждый модуль
объявляет свой `POINT`/`WNDCLASSEXW`, вызов чужого модуля падает с
``expected LP_POINT instance instead of pointer to POINT`` (живая проба). Здесь
структуры и флаги объявлены один раз, и оба модуля используют их.

На не-Windows модуль не объявляет Win32-имена: клей тогда недоступен, а
модель меню и диспетчер остаются проверяемыми офлайн.
"""

from __future__ import annotations

import ctypes
import sys

if sys.platform != "win32":
    # Клей недоступен: имена есть, но равны None - вызовы клэя защищены
    # проверкой sys.platform в модулях-владельцах.
    user32 = gdi32 = shell32 = kernel32 = None
    VP = HD = LRESULT = WNDPROC = None
    POINT = RECT = MSG = MONITORINFO = PAINTSTRUCT = WNDCLASSEXW = None
    NOTIFYICONDATAW = None
else:
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32

    # Указатель и LRESULT - на x64 pointer-sized; GDI-объекты (DC, bitmap,
    # brush, font) - 32-битные handles, и c_void_p читает их со знаком,
    # теряя high bits: CreateIconIndirect с такими handles возвращает NULL.
    VP = ctypes.c_void_p
    HD = ctypes.c_uint32
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, VP, ctypes.c_uint, VP, VP)

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class MSG(ctypes.Structure):
        _fields_ = [("hwnd", VP), ("message", ctypes.c_uint),
                    ("wParam", VP), ("lParam", VP),
                    ("lpt", ctypes.c_uint), ("pt", POINT)]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint),
                    ("rcMonitor", RECT), ("rcWork", RECT),
                    ("dwFlags", ctypes.c_uint)]

    class PAINTSTRUCT(ctypes.Structure):
        _fields_ = [("hdc", HD), ("fErase", ctypes.c_bool),
                    ("xLeft", ctypes.c_long), ("xRight", ctypes.c_long),
                    ("yTop", ctypes.c_long), ("yBottom", ctypes.c_long),
                    ("display", HD), ("dwFlags", ctypes.c_uint)]

    class WNDCLASSEXW(ctypes.Structure):
        # hClassMenu идёт ПОСЛЕ lpszClassName (реальный layout Windows): с
        # «красивым» порядком Windows читает lpszClassName как мусор и
        # отказывает с ERROR_INVALID_PARAMETER (87)
        _fields_ = [("cbSize", ctypes.c_uint), ("style", ctypes.c_uint),
                    ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", VP), ("hIcon", VP), ("hCursor", VP),
                    ("hBackground", VP), ("hMenuName", ctypes.c_wchar_p),
                    ("lpszClassName", ctypes.c_wchar_p), ("hClassMenu", VP)]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint), ("hwnd", VP),
                    ("uid", ctypes.c_uint), ("uFlags", ctypes.c_uint),
                    ("callback", ctypes.c_uint), ("hIcon", VP),
                    ("uTimeout", ctypes.c_ushort), ("uVersion", ctypes.c_ushort),
                    # c_wchar - UTF-16 на Windows; create_unicode_buffer в
                    # _fields_ не принимается как тип поля
                    ("szTip", ctypes.c_wchar * 64),
                    ("dwState", ctypes.c_uint), ("dwStateItem", ctypes.c_uint),
                    ("szInfo", ctypes.c_wchar * 256),
                    ("dwInfoTimeout", ctypes.c_uint),
                    ("szInfoTitle", ctypes.c_wchar * 64),
                    ("dwInfoFlags", ctypes.c_uint)]

    # -- сообщения и флаги -------------------------------------------------

    WM_NULL = 0x0000
    WM_DESTROY = 0x0002
    WM_PAINT = 0x000F
    WM_CONTEXTMENU = 0x007E
    WM_TIMER = 0x0113
    WM_KEYDOWN = 0x0100
    WM_MOUSEMOVE = 0x0200
    WM_LBUTTONDOWN = 0x0201
    WM_LBUTTONUP = 0x0202
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONDOWN = 0x0204
    WM_RBUTTONUP = 0x0205
    WM_RBUTTONDBLCLK = 0x0206
    WM_MOUSEWHEEL = 0x020A

    WM_USER = 0x0400
    WM_SETICON = 0x0018
    ICON_SMALL = 0
    ICON_BIG = 1
    NIN_SELECT = WM_USER + 0
    NIN_KEYSELECT = WM_USER + 1
    NIN_BALLOONSHOW = WM_USER + 2
    NIN_BALLOONHIDE = WM_USER + 3
    NIN_BALLOONTIMEOUT = WM_USER + 4
    NIN_BALLOONUSERCLICK = WM_USER + 5

    VK_ESCAPE = 0x1B
    VK_RETURN = 0x0D
    VK_UP = 0x26
    VK_DOWN = 0x28
    VK_LEFT = 0x27
    VK_RIGHT = 0x25

    NIF_MESSAGE = 0x0001
    NIF_ICON = 0x0002
    NIF_TIP = 0x0004
    NIF_INFO = 0x0010
    NIM_ADD = 0x00000000
    NIM_MODIFY = 0x00000001
    NIM_DELETE = 0x00000002

    MF_STRING = 0x0000
    MF_GRAYED = 0x0002
    MF_CHECKED = 0x0008      # тот же разряд, что MF_SEPARATOR: различаются
    MF_SEPARATOR = 0x0008    # наличием строки и item id
    MF_POPUP = 0x0010
    TPM_RETURNCMD = 0x0100
    TPM_LEFTALIGN = 0x0000
    TPM_BOTTOMALIGN = 0x0020

    WS_POPUP = 0x00000000
    WS_EX_TOPMOST = 0x00000008
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_LAYERED = 0x00080000
    WS_EX_NOACTIVATE = 0x02000000
    LWA_ALPHA = 0x00000002
    SWP_NOACTIVATE = 0x0002
    SWP_SHOWWINDOW = 0x0040
    HWND_TOPMOST = VP(-1)
    MONITOR_WORKAREA = 0x0003

    DT_LEFT = 0x0000
    OPAQUE = 2
    MB_YESNO = 0x0004
    IDYES = 6
    IDNO = 7
    ERROR_ALREADY_EXISTS = 183
    IDI_APPLICATION = 32512
    IDI_ERROR = 32513
    IDI_WARNING = 32515
    IDI_INFORMATION = 32516
    TIMER_ANIM = 1
    TIMER_SUBMENU = 2

    # -- declarations: один набор restype/argtypes для обоих модулей -------

    user32.RegisterWindowMessageW.restype = ctypes.c_uint
    user32.RegisterWindowMessageW.argtypes = [ctypes.c_wchar_p]
    user32.RegisterClassExW.restype = ctypes.c_uint
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
    user32.CreateWindowExW.restype = VP
    user32.CreateWindowExW.argtypes = [ctypes.c_uint, ctypes.c_wchar_p,
                                       ctypes.c_wchar_p, ctypes.c_uint,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, VP, VP, VP, VP]
    user32.DestroyWindow.restype = ctypes.c_bool
    user32.DestroyWindow.argtypes = [VP]
    user32.DestroyMenu.restype = ctypes.c_bool
    user32.DestroyMenu.argtypes = [VP]
    user32.CreatePopupMenu.restype = VP
    user32.CreatePopupMenu.argtypes = []
    user32.AppendMenuW.restype = ctypes.c_bool
    user32.AppendMenuW.argtypes = [VP, ctypes.c_uint, VP, ctypes.c_wchar_p]
    user32.TrackPopupMenuEx.restype = ctypes.c_int
    user32.TrackPopupMenuEx.argtypes = [VP, ctypes.c_uint, ctypes.c_int,
                                        ctypes.c_int, VP, VP]
    user32.DefWindowProcW.restype = LRESULT
    user32.DefWindowProcW.argtypes = [VP, ctypes.c_uint, VP, VP]
    user32.GetMessageW.restype = ctypes.c_int
    user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), VP, ctypes.c_uint,
                                   ctypes.c_uint]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG), VP, ctypes.c_uint,
                                        VP]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
    user32.PostMessageW.restype = ctypes.c_bool
    user32.PostMessageW.argtypes = [VP, ctypes.c_uint, VP, VP]
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.IsWindow.restype = ctypes.c_bool
    user32.IsWindow.argtypes = [VP]
    user32.SetForegroundWindow.restype = ctypes.c_bool
    user32.SetForegroundWindow.argtypes = [VP]
    user32.GetForegroundWindow.restype = VP
    user32.GetForegroundWindow.argtypes = []
    user32.GetCursorPos.restype = ctypes.c_bool
    user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
    user32.LoadCursorW.restype = VP
    user32.LoadCursorW.argtypes = [VP, VP]
    user32.LoadIconW.restype = VP
    user32.LoadIconW.argtypes = [VP, VP]
    user32.SetWindowPos.restype = ctypes.c_bool
    user32.SetWindowPos.argtypes = [VP, VP, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    user32.SetLayeredWindowAttributes.restype = ctypes.c_bool
    user32.SetLayeredWindowAttributes.argtypes = [VP, ctypes.c_uint,
                                                  ctypes.c_uint, ctypes.c_uint]
    user32.MonitorFromPoint.restype = VP
    user32.MonitorFromPoint.argtypes = [ctypes.POINTER(POINT), ctypes.c_uint]
    user32.GetMonitorInfoW.restype = ctypes.c_bool
    user32.GetMonitorInfoW.argtypes = [VP, ctypes.POINTER(MONITORINFO)]
    user32.GetDpiForWindow.restype = ctypes.c_uint
    user32.GetDpiForWindow.argtypes = [VP]
    user32.SetTimer.restype = HD
    user32.SetTimer.argtypes = [VP, HD, ctypes.c_uint, VP]
    user32.KillTimer.restype = ctypes.c_bool
    user32.KillTimer.argtypes = [VP, HD]
    user32.InvalidateRect.restype = ctypes.c_bool
    user32.InvalidateRect.argtypes = [VP, ctypes.POINTER(RECT), ctypes.c_bool]
    user32.BeginPaint.restype = HD
    user32.BeginPaint.argtypes = [VP, ctypes.POINTER(PAINTSTRUCT)]
    user32.EndPaint.restype = None
    user32.EndPaint.argtypes = [VP, ctypes.POINTER(PAINTSTRUCT)]
    user32.FillRect.restype = ctypes.c_int
    user32.FillRect.argtypes = [HD, ctypes.POINTER(RECT), HD]
    user32.DrawTextW.restype = ctypes.c_int
    user32.DrawTextW.argtypes = [HD, ctypes.c_wchar_p, ctypes.c_int,
                                 ctypes.POINTER(RECT), ctypes.c_uint]
    user32.MessageBoxW.restype = ctypes.c_int
    user32.MessageBoxW.argtypes = [VP, ctypes.c_wchar_p, ctypes.c_wchar_p,
                                   ctypes.c_uint]
    # HICON/HBITMAP - HANDLE (pointer-sized), но GDI кладёт в них 32-битное
    # значение; argtypes не задаём: typed variant в пробах возвращал NULL
    user32.CreateIconIndirect.restype = VP

    shell32.Shell_NotifyIconW.restype = ctypes.c_bool
    shell32.Shell_NotifyIconW.argtypes = [ctypes.c_uint,
                                          ctypes.POINTER(NOTIFYICONDATAW)]

    gdi32.CreateFontW.restype = HD
    gdi32.CreateFontW.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_uint,
                                  ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
                                  ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
                                  ctypes.c_uint, ctypes.c_wchar_p]
    gdi32.CreateSolidBrush.restype = HD
    gdi32.CreateSolidBrush.argtypes = [ctypes.c_uint]
    gdi32.DeleteObject.restype = ctypes.c_bool
    gdi32.DeleteObject.argtypes = [HD]
    # HDC принимает NULL: argtypes VP, restype - 32-битный handle
    gdi32.CreateCompatibleDC.restype = HD
    gdi32.CreateCompatibleDC.argtypes = [VP]
    gdi32.SelectObject.restype = HD
    gdi32.SelectObject.argtypes = [HD, HD]
    gdi32.DeleteDC.restype = ctypes.c_bool
    gdi32.DeleteDC.argtypes = [HD]
    gdi32.GetTextExtentPoint32W.restype = ctypes.c_bool
    gdi32.GetTextExtentPoint32W.argtypes = [HD, ctypes.c_wchar_p, ctypes.c_int,
                                            ctypes.POINTER(ctypes.c_long * 2)]
    gdi32.SetTextColor.restype = ctypes.c_uint
    gdi32.SetTextColor.argtypes = [HD, ctypes.c_uint]
    gdi32.SetBkMode.restype = ctypes.c_int
    gdi32.SetBkMode.argtypes = [HD, ctypes.c_int]

    kernel32.GetModuleHandleW.restype = VP
    kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    kernel32.GetCurrentProcessId.restype = ctypes.c_uint
    kernel32.GetCurrentProcessId.argtypes = []
    kernel32.GetLastError.restype = ctypes.c_uint
    kernel32.GetLastError.argtypes = []
    kernel32.CreateMutexW.restype = VP
    kernel32.CreateMutexW.argtypes = [VP, ctypes.c_bool, ctypes.c_wchar_p]


def monitor_work(pt, api) -> "RECT":
    """Work-area монитора, где стоит курсор (без taskbar и трей-панели)."""
    info = MONITORINFO(ctypes.sizeof(MONITORINFO))
    handle = api.MonitorFromPoint(ctypes.byref(pt), MONITOR_WORKAREA)
    if handle and api.GetMonitorInfoW(handle, ctypes.byref(info)):
        return RECT(info.rcWork.left, info.rcWork.top,
                    info.rcWork.right - info.rcWork.left,
                    info.rcWork.bottom - info.rcWork.top)
    return RECT(0, 0, 1920, 1080)


def dpi_of(hwnd, api) -> int:
    """DPI окна: меню рисуется в физических пикселях и масштабируется по нему."""
    return api.GetDpiForWindow(hwnd) or 96


def measure(text: str, font, gdi) -> tuple[int, int]:
    """Метрика текста GDI-шрифтом (w, h) в пикселях."""
    size = (ctypes.c_long * 2)()
    dc = gdi.CreateCompatibleDC(None)
    old = gdi.SelectObject(dc, font)
    gdi.GetTextExtentPoint32W(dc, text, len(text), ctypes.byref(size))
    gdi.SelectObject(dc, old)
    gdi.DeleteDC(dc)
    return int(size[0]), int(size[1])


def lo_hi(value) -> tuple[int, int]:
    """LOWORD/HIGHWORD из pointer-sized wParam/lParam (ctypes даёт знак)."""
    raw = int(value or 0) & 0xFFFFFFFF
    return raw & 0xFFFF, (raw >> 16) & 0xFFFF


def signed_hi(value) -> int:
    """HIWORD как signed short: delta колеса мыши (-120 вниз, +120 вверх)."""
    hi = lo_hi(value)[1]
    return hi - 0x10000 if hi >= 0x8000 else hi


def colorref(rgb: tuple[int, int, int]) -> int:
    """COLORREF = R | G<<8 | B<<16 (hex-токен задаёт порядок R,G,B)."""
    r, g, b = rgb
    return r | (g << 8) | (b << 16)


def round_px(px: float, dpi: int) -> int:
    """CSS-размер в физических пикселях экрана."""
    return int(round(px * dpi / 96))

"""Трей: иконка, меню, уведомления и одиночный экземпляр (Windows, ctypes).

Два слоя, чтобы большая часть проверялась без Win32:

* :func:`tray_model` - чистая модель меню (порядок пунктов, отметки, гашение,
  tooltip, состояние иконки). Её тестируем офлайн, включая состояния «нет папки»,
  «идёт автоцикл», «cooldown».
* :class:`Tray` - клей Windows: скрытое окно своего класса, NotifyIcon,
  TrackPopupMenu, balloon-уведомление, цикл сообщений в отдельном потоке.

Иконка - системный значок Windows (:func:`user32.LoadIconW` по ``IDI_*``),
выбираемый по состоянию (:func:`icon_state`): своих пикселей в этом процессе
изобразить не удалось (см. комментарий :data:`ICON_RESOURCES`), а Pillow и
``.ico`` раздули бы бандл - рантайм остаётся «только stdlib».

Мягкий отказ: NotifyIcon требует интерактивного десктопа (в Session 0 и в CI
его нет). Если регистрация не удалась, :meth:`Tray.start` возвращает
``{ok: False, ...}``, а GUI ведёт себя ровно как без трея.

Что трей НЕ решает: скрытое окно остаётся живым (форма WinForms и процесс
WebView2 - порядка 50-100 МБ). Освобождать память при скрытии нельзя:
``webview.start`` обязан выполняться на главном потоке и не перезапускается
после ``Application.Exit``, поэтому «Открыть окно» возвращает то же окно, а
память освобождает только «Выход» из трея.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import typing
from dataclasses import dataclass
from typing import Callable

from . import paths

# Иконка трея: системные значки Windows по состоянию. Своё изображение здесь
# не выходит - проверено живыми пробами: ``CreateIcon`` с 32-битными битами
# рисует чёрное (``DrawIconEx`` + ``GetPixel`` даёт R=G=B=0),
# ``CreateIconIndirect`` с 1-битной DIB-маской возвращает NULL без кода
# ошибки, ``CreateBitmap`` с переданными битами - тоже NULL, а ``LoadImageW``
# по ICO (и наш ICO, и образцовый из Pillow) рушится в access violation.
# Системные значки документированы и различимы: error - красный крест,
# warning - янтарный треугольник, information - синяя «i», application -
# нейтральный. Handle принадлежит системе и переиспользуется, поэтому
# DestroyIcon к нему применять нельзя.
ICON_RESOURCES: dict[str, int] = {
    "ok": 32516,      # IDI_INFORMATION
    "warn": 32515,    # IDI_WARNING
    "err": 32513,     # IDI_ERROR
    "off": 32512,     # IDI_APPLICATION
}
TIP_LIMIT = 63                   # NOTIFYICONDATAW.szTip - 64 wchar с нулём

TRAY_SETTING = "tray"
TARGET_PID = "tray_pid"
TARGET_HWND = "tray_hwnd"

# Пункты меню: стабильные id - диспетчер не зависит от порядка пунктов.
SHOW = "window.show"
HIDE = "window.hide"
JOURNAL = "window.journal"
EXIT = "app.exit"
MODE_TOGGLE = "mode.toggle"
AUTO_RUN = "auto.run"
PROBES_RUN = "probes.run"
PRESET_PREFIX = "preset:"
STRATEGY_PREFIX = "strategy:"


@dataclass(frozen=True)
class Item:
    id: str
    label: str
    kind: str = "action"       # action | radio | check
    checked: bool = False      # отметка активного пресета/стратегии/режима
    enabled: bool = True       # False -> пункт серый и некликабельный
    note: str = ""             # пояснение в скобках (cooldown, «идёт...»)


@dataclass(frozen=True)
class Submenu:
    id: str
    label: str
    items: tuple[Item, ...]


@dataclass(frozen=True)
class Separator:
    pass


@dataclass(frozen=True)
class TraySpec:
    tooltip: str
    icon_state: str
    items: tuple[object, ...]     # Item | Submenu | Separator


# -- настройки -------------------------------------------------------------

def enabled() -> bool:
    """Трей включён? По умолчанию включён: скрытие полезно каждому запуска."""
    value = paths.read_settings().get(TRAY_SETTING)
    return True if value is None else bool(value)


def set_enabled(value: bool) -> bool:
    settings = paths.read_settings()
    settings[TRAY_SETTING] = bool(value)
    paths.write_settings(settings)
    return bool(value)


def _minutes(seconds: float) -> int:
    return max(1, int(round(seconds / 60))) if seconds > 0 else 0


def tooltip(state: dict, busy: dict | None = None,
            last_probes: dict | None = None) -> str:
    """Одна строка о состоянии: NotifyIcon вмещает 63 символа.

    Обязательное - обход и режим; факультативное - пресет, счёт проб,
    последний автовыбор. Места всегда не хватает, поэтому факультативное
    добавляется целиком и только если влезает: обрезанное посреди слова
    «Discord / YouTu» читается как поломка, а не как информация.
    """
    busy = busy or {}
    core: list[str] = ["Diaspas"]
    if not state.get("dir_ok"):
        core.append("нет папки zapret")
    elif state.get("running"):
        core.append(f"обход {state.get('strategy_label') or state.get('strategy') or '-'}")
    elif state.get("installed"):
        core.append("служба остановлена")
    else:
        core.append("обход выключен")

    mode = state.get("strategy_mode")
    if mode == "auto":
        cooldown = int(state.get("auto_cooldown_left") or 0)
        core.append(f"авто {cooldown // 60 + 1} мин" if cooldown > 300 else "авто")
    elif mode == "manual":
        core.append("вручную")

    if busy.get("auto"):
        core.append("подбор идёт")
    if busy.get("select"):
        core.append("перебор идёт")

    optional: list[str] = []
    if state.get("running") and state.get("preset_title"):
        optional.append(str(state["preset_title"]))
    if state.get("running") and last_probes:
        optional.append(f"{last_probes.get('count', 0)}/"
                        f"{last_probes.get('total', 0)}")
    if state.get("auto_last_strategy"):
        optional.append(f"авто: {paths.strategy_label(state['auto_last_strategy'])}")

    text = " · ".join(core)
    for part in optional:
        if len(text) + len(part) + 3 <= TIP_LIMIT:
            text += " · " + part
    return text[:TIP_LIMIT]


def icon_state(state: dict, last_probes: dict | None = None) -> str:
    """Значок иконки: обход выключен -> off; иначе - по последним пробам."""
    if not state.get("dir_ok") or not state.get("running"):
        return "off"
    report = (last_probes or {}).get("state")
    if report == "partial":
        return "warn"
    if report == "none":
        return "err"
    return "ok"


def tray_model(state: dict, presets: list[dict],
               busy: dict | None = None,
               last_probes: dict | None = None) -> TraySpec:
    """Меню трея: порядок, отметки, гашение. Стратегии - в подменю (их много).

    ``state`` - :meth:`ui.webapp.App.state` (или равнозначный словарь Tk),
    ``presets`` - :meth:`ui.webapp.App.presets`, ``busy`` - идущие циклы.
    """
    busy = busy or {}
    dir_ok = bool(state.get("dir_ok"))
    cycle = bool(busy.get("auto") or busy.get("select"))
    preset_key = state.get("preset_key")
    mode_auto = state.get("strategy_mode") == "auto"
    cooldown = int(state.get("auto_cooldown_left") or 0)

    items: list[object] = [
        Item(SHOW, "Открыть окно"),
        Item(HIDE, "Скрыть окно", enabled=dir_ok),
        Separator(),
    ]

    preset_items = [Item(PRESET_PREFIX + p["key"], p["title"], kind="radio",
                         checked=p["key"] == preset_key,
                         enabled=dir_ok and not cycle,
                         note="подбор идёт" if cycle and dir_ok else "")
                    for p in presets if not p.get("off")]
    if preset_items:
        items.append(Submenu("presets", "Пресеты", tuple(preset_items)))

    strategy_items = [Item(STRATEGY_PREFIX + s["file"], s["label"],
                           kind="radio", checked=bool(s.get("active")),
                           enabled=dir_ok and not cycle)
                      for s in (state.get("strategies") or [])]
    if strategy_items:
        items.append(Submenu("strategies", "Стратегии", tuple(strategy_items)))

    items += [
        Separator(),
        Item(MODE_TOGGLE, "Автоподбор стратегий", kind="check",
             checked=mode_auto, enabled=dir_ok),
        Item(AUTO_RUN, "Применить лучшую стратегию",
             enabled=dir_ok and mode_auto and not cycle and cooldown <= 0,
             note=f"через {_minutes(cooldown)} мин" if cooldown > 0 else ""),
        Item(PROBES_RUN, "Проверить цели", enabled=dir_ok and not cycle),
        Separator(),
        Item(JOURNAL, "Журнал"),
        Item(EXIT, "Выход"),
    ]

    return TraySpec(tooltip=tooltip(state, busy, last_probes),
                    icon_state=icon_state(state, last_probes),
                    items=tuple(items))


def preset_key_of(action: str) -> str | None:
    """«preset:apex_ea» -> «apex_ea»; None - это не пресет."""
    if action.startswith(PRESET_PREFIX):
        return action[len(PRESET_PREFIX):]
    return None


def strategy_file_of(action: str) -> str | None:
    """«strategy:general (ALT7).bat» -> имя файла; None - не стратегия."""
    if action.startswith(STRATEGY_PREFIX):
        return action[len(STRATEGY_PREFIX):]
    return None


#: Операции, которые длятся секунды и минутами (пресет, стратегия, подбор).
#: Диспетчер уводит их в worker-поток, чтобы цикл трея не ждал меню.
HEAVY_PREFIXES = (PRESET_PREFIX, STRATEGY_PREFIX)
HEAVY_ACTIONS = (AUTO_RUN, PROBES_RUN)


def is_heavy(action: str) -> bool:
    return action in HEAVY_ACTIONS or action.startswith(HEAVY_PREFIXES)


class Ops(typing.Protocol):
    """Что диспетчер трея требует от интерфейса (webview и Tk - один набор)."""

    def apply_preset(self, key: str) -> dict: ...
    def apply_strategy(self, file: str) -> dict: ...
    def mode(self) -> str: ...
    def set_mode(self, mode: str) -> dict: ...
    def auto_run(self, force: bool = False) -> dict: ...
    def check_and_autofix(self) -> dict: ...
    def show_window(self, tab: str | None = None) -> dict: ...
    def hide_window(self) -> dict: ...
    def exit_app(self) -> dict: ...


def handle(action: str, ops: Ops) -> dict:
    """Пункт меню -> операция. Неизвестный id - ошибка, а не исключение."""
    preset = preset_key_of(action)
    if preset is not None:
        return ops.apply_preset(preset)
    strategy = strategy_file_of(action)
    if strategy is not None:
        return ops.apply_strategy(strategy)
    if action == SHOW:
        return ops.show_window()
    if action == JOURNAL:
        return ops.show_window(tab="journal")
    if action == HIDE:
        return ops.hide_window()
    if action == MODE_TOGGLE:
        return ops.set_mode("auto" if ops.mode() == "manual" else "manual")
    if action == AUTO_RUN:
        return ops.auto_run()
    if action == PROBES_RUN:
        return ops.check_and_autofix()
    if action == EXIT:
        return ops.exit_app()
    return {"ok": False, "error": f"неизвестный пункт {action!r}"}


# -- Win32-клей ------------------------------------------------------------

if sys.platform == "win32":
    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32

    WM_TRAY = user32.RegisterWindowMessageW("DiaspasTray")
    WM_UPDATE = user32.RegisterWindowMessageW("DiaspasTrayUpdate")
    WM_SHOW = user32.RegisterWindowMessageW("DiaspasTrayShow")
    WM_QUIT_TRAY = user32.RegisterWindowMessageW("DiaspasTrayQuit")

    WM_NULL = 0x0000
    WM_DESTROY = 0x0002
    WM_LBUTTONUP = 0x0202
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONUP = 0x0204
    WM_CONTEXTMENU = 0x007E
    NIN_BALLOONUSERCLICK = 0x0205

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
    MF_SEPARATOR = 0x0008    # наличием/отсутствием строки и item id
    MF_POPUP = 0x0010

    TPM_RETURNCMD = 0x0100
    TPM_LEFTALIGN = 0x0000
    TPM_BOTTOMALIGN = 0x0020

    WS_POPUP = 0x00000000
    WS_EX_TOOLWINDOW = 0x00000080
    ERROR_ALREADY_EXISTS = 183
    MB_YESNO = 0x0004
    IDYES = 6
    IDNO = 7

    # Указатель и LRESULT - на x64 это 64-битные типы: без них ctypes вернёт int
    # и указатели (hwnd, hIcon, hCursor) потеряются.
    VP = ctypes.c_void_p
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, VP, ctypes.c_uint, VP, VP)

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    class MSG(ctypes.Structure):
        _fields_ = [("hwnd", VP), ("message", ctypes.c_uint),
                    ("wParam", VP), ("lParam", VP),
                    ("lpt", ctypes.c_uint), ("pt", POINT)]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint), ("hwnd", ctypes.c_void_p),
                    ("uid", ctypes.c_uint), ("uFlags", ctypes.c_uint),
                    ("callback", ctypes.c_uint), ("hIcon", ctypes.c_void_p),
                    ("uTimeout", ctypes.c_ushort), ("uVersion", ctypes.c_ushort),
                    # c_wchar - UTF-16 на Windows: arrays этого типа лежат как
                    # wchar_t[] в структуре
                    ("szTip", ctypes.c_wchar * 64),
                    ("dwState", ctypes.c_uint), ("dwStateItem", ctypes.c_uint),
                    ("szInfo", ctypes.c_wchar * 256),
                    ("dwInfoTimeout", ctypes.c_uint),
                    ("szInfoTitle", ctypes.c_wchar * 64),
                    ("dwInfoFlags", ctypes.c_uint)]

    class WNDCLASSEXW(ctypes.Structure):
        # порядок поля реальный: hClassMenu идёт ПОСЛЕ lpszClassName.
        # С «красивым» порядком Windows читает lpszClassName как мусор и
        # отказывает с ERROR_INVALID_PARAMETER (87).
        _fields_ = [("cbSize", ctypes.c_uint), ("style", ctypes.c_uint),
                    ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", VP), ("hIcon", VP), ("hCursor", VP),
                    ("hBackground", VP), ("hMenuName", ctypes.c_wchar_p),
                    ("lpszClassName", ctypes.c_wchar_p),
                    ("hClassMenu", VP)]

    # Без argtypes/restype ctypes возвращает int - указатели теряются на x64.
    # NotifyIcon - shell32; окно, меню, курсор, загрузка иконки - user32.
    shell32.Shell_NotifyIconW.restype = ctypes.c_bool
    shell32.Shell_NotifyIconW.argtypes = [ctypes.c_uint,
                                          ctypes.POINTER(NOTIFYICONDATAW)]

    user32.RegisterWindowMessageW.restype = ctypes.c_uint
    user32.RegisterWindowMessageW.argtypes = [ctypes.c_wchar_p]
    user32.RegisterClassExW.restype = ctypes.c_uint
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
    user32.CreateWindowExW.restype = VP
    user32.CreateWindowExW.argtypes = [ctypes.c_uint, ctypes.c_wchar_p,
                                       ctypes.c_wchar_p, ctypes.c_uint,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, VP, VP, VP, VP]
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
    user32.SetForegroundWindow.restype = ctypes.c_bool
    user32.SetForegroundWindow.argtypes = [VP]
    user32.LoadCursorW.restype = VP
    user32.LoadCursorW.argtypes = [VP, VP]
    user32.GetCursorPos.restype = ctypes.c_bool
    user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
    user32.CreatePopupMenu.restype = VP
    user32.CreatePopupMenu.argtypes = []
    user32.AppendMenuW.restype = ctypes.c_bool
    user32.AppendMenuW.argtypes = [VP, ctypes.c_uint, VP, ctypes.c_wchar_p]
    user32.DestroyMenu.restype = ctypes.c_bool
    user32.DestroyMenu.argtypes = [VP]
    user32.TrackPopupMenuEx.restype = ctypes.c_int
    user32.TrackPopupMenuEx.argtypes = [VP, ctypes.c_uint, ctypes.c_int,
                                        ctypes.c_int, VP, VP]
    user32.MessageBoxW.restype = ctypes.c_int
    user32.MessageBoxW.argtypes = [VP, ctypes.c_wchar_p, ctypes.c_wchar_p,
                                   ctypes.c_uint]

    kernel32.GetModuleHandleW.restype = VP
    kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    kernel32.GetCurrentProcessId.restype = ctypes.c_uint
    kernel32.GetCurrentProcessId.argtypes = []
    kernel32.GetLastError.restype = ctypes.c_uint
    kernel32.GetLastError.argtypes = []
    kernel32.CreateMutexW.restype = VP
    kernel32.CreateMutexW.argtypes = [VP, ctypes.c_bool, ctypes.c_wchar_p]

    # IsWindow фильтрует мёртвые/чужие hwnd из настроек: PostMessage на
    # несуществующее окно вернул бы True и мы «показали» бы не то окно.
    user32.IsWindow.restype = ctypes.c_bool
    user32.IsWindow.argtypes = [VP]

    # Системный значок по состоянию: LoadIconW(NULL, ordinal) - единственный
    # путь, который здесь вышел (проверено пробом). Handle принадлежит системе:
    # его не DestroyIcon, и одно и то же значение возвращается повторным
    # вызовам, поэтому перерисовка иконки не копит ресурсы.
    user32.LoadIconW.restype = VP
    user32.LoadIconW.argtypes = [VP, VP]

    def _make_hicon(name: str) -> int | None:
        """HICON системного значка; неизвестное состояние -> значок «выключен»."""
        icon = user32.LoadIconW(None, ICON_RESOURCES.get(name, ICON_RESOURCES["off"]))
        return icon or None


class Tray:
    """Иконка в трее + свой цикл сообщений в отдельном потоке.

    ``spec`` - callable, возвращающий :class:`TraySpec` на каждый правый клик
    и на каждое обновление: меню строится заново, поэтому актуальное состояние
    видно без machinery обновления меню. ``on_action`` вызывается из этого
    потока с id пункта; тяжёльные операции диспетчер уводит в worker-поток.
    """

    def __init__(self, spec: Callable[["TraySpec"]],
                 on_action: Callable[[str], None], uid: int = 1) -> None:
        self._spec = spec
        self._on_action = on_action
        self._uid = uid
        self._thread: threading.Thread | None = None
        self._hwnd: int | None = None
        self._icon: int | None = None
        self._mutex: int | None = None
        self._error = ""
        self._pending_info: tuple[str, str] | None = None
        self._ready = threading.Event()
        # wnd proc обязан жить, пока живёт окно: ctypes его не держит сам
        self._wnd_proc: "ctypes.WINFUNCTYPE | None" = None
        self._class_name = "DiaspasTrayWindow"

    # -- публичное ---------------------------------------------------------

    @property
    def hwnd(self) -> int | None:
        return self._hwnd

    def available(self) -> bool:
        return sys.platform == "win32"

    def start(self) -> dict:
        """Зарегистрировать иконку; {ok, error}. Мягкий отказ без исключений."""
        if not sys.platform == "win32":
            return {"ok": False, "error": "трей доступен только на Windows"}
        if self._thread:
            return {"ok": True, "hwnd": self._hwnd}
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="diaspas-tray")
        self._thread.start()
        if not self._ready.wait(4.0):
            return {"ok": False, "error": "трей не поднялся за 4 с"}
        if self._error:
            return {"ok": False, "error": self._error}
        return {"ok": True, "hwnd": self._hwnd}

    def update(self) -> None:
        """Перерисовать иконку и tooltip. Из любого потока - через сообщение."""
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_UPDATE, 0, 0)

    def notify(self, title: str, text: str) -> None:
        """Уведомление трея (клик по нему открывает окно). Тихо деградирует."""
        if self._hwnd:
            self._pending_info = (title[:63], text[:255])
            user32.PostMessageW(self._hwnd, WM_UPDATE, 0, 0)

    def quit(self) -> None:
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_QUIT_TRAY, 0, 0)

    def join(self, timeout: float = 3.0) -> None:
        if self._thread:
            self._thread.join(timeout)

    def claim_singleton(self) -> dict:
        """Один GUI-экземпляр: mutex занят - показать чужой окно и выйти.

        Признак живости - сам mutex: kernel закрывает handle при выходе
        процесса, поэтому ALREADY_EXISTS значит «есть живой владелец».
        Запись pid/hwnd из настроек нужна только чтобы знать, куда слать
        сообщение: pid мог быть переиспользован чужим процессом, поэтому
        проверяем окно (IsWindow) и результат PostMessage. Если слать
        нечем - остаёмся сами собой: живой скрытый экземпляр, который
        нельзя разбудить, хуже второго иконки.
        """
        if not sys.platform == "win32":
            return {"ok": True, "owner": True}
        mutex = kernel32.CreateMutexW(None, False, "DiaspasGui")
        if not mutex:
            return {"ok": False, "error": f"CreateMutexW err={kernel32.GetLastError()}"}
        if kernel32.GetLastError() != ERROR_ALREADY_EXISTS:
            self._mutex = mutex           # держать, пока жив процесс
            return {"ok": True, "owner": True}

        settings = paths.read_settings()
        hwnd = int(settings.get(TARGET_HWND) or 0)
        if hwnd and user32.IsWindow(hwnd) and user32.PostMessageW(
                hwnd, WM_SHOW, 0, 0):
            return {"ok": True, "owner": False,
                    "pid": int(settings.get(TARGET_PID) or 0)}
        return {"ok": True, "owner": True, "stale": True}

    # -- поток -------------------------------------------------------------

    def _run(self) -> None:
        try:
            self._create_window()
        except Exception as exc:  # noqa: BLE001 - трей не должен валить GUI
            self._error = f"{type(exc).__name__}: {exc}"
            self._ready.set()
            return

        msg = MSG()
        while True:
            got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if got == 0 or got == -1:
                break
            user32.TranslateMessage(ctypes.byref(msg), None, 0, None)
            user32.DispatchMessageW(ctypes.byref(msg))
        self._remove_icon()

    def _create_window(self) -> None:
        instance = kernel32.GetModuleHandleW(None)

        def wnd_proc(hwnd, message, wparam, lparam):
            return self._on_message(hwnd, message, wparam, lparam)

        self._wnd_proc = WNDPROC(wnd_proc)
        wc = WNDCLASSEXW(ctypes.sizeof(WNDCLASSEXW), 0, self._wnd_proc,
                         0, 0, instance, None, user32.LoadCursorW(None, 32512),
                         None, None, self._class_name, None)
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            err = kernel32.GetLastError()
            if err:
                raise OSError(f"RegisterClassExW не удалось (err={err})")
        hwnd = user32.CreateWindowExW(WS_EX_TOOLWINDOW, self._class_name,
                                      "Diaspas tray", WS_POPUP, 0, 0, 0, 0,
                                      None, None, None, None)
        if not hwnd:
            raise OSError(f"CreateWindowExW не вернул окно (err={kernel32.GetLastError()})")
        self._hwnd = hwnd

        self._add_icon()
        paths.write_settings({**paths.read_settings(),
                             TARGET_PID: kernel32.GetCurrentProcessId(),
                             TARGET_HWND: self._hwnd})
        self._ready.set()

    def _icon_data(self, flags: int) -> NOTIFYICONDATAW:
        data = NOTIFYICONDATAW(ctypes.sizeof(NOTIFYICONDATAW), self._hwnd,
                               self._uid, flags, WM_TRAY, self._icon, 0, 4)
        return data

    def _add_icon(self) -> None:
        spec = self._spec()
        self._icon = _make_hicon(spec.icon_state)
        data = self._icon_data(NIF_MESSAGE | NIF_ICON | NIF_TIP)
        data.szTip = spec.tooltip[:TIP_LIMIT]
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)):
            raise OSError("Shell_NotifyIconW(NIM_ADD) отказал: нет "
                          "интерактивного десктопа?")

    def _refresh_icon(self, info: tuple[str, str] | None = None) -> None:
        spec = self._spec()
        icon = _make_hicon(spec.icon_state)
        data = self._icon_data(NIF_ICON | NIF_TIP)
        data.hIcon = icon
        data.szTip = spec.tooltip[:TIP_LIMIT]
        if info:
            data.uFlags |= NIF_INFO
            data.dwInfoFlags = 0            # NIIF_INFO = 0
            data.dwInfoTimeout = 4000
            data.szInfoTitle = info[0][:63]
            data.szInfo = info[1][:255]
        if shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data)):
            # значок системный: старый handle DestroyIcon не вызывает - это
            # ресурс, которым владеет Windows (он же возвращается повторно)
            if icon:
                self._icon = icon
            self._pending_info = None

    def _remove_icon(self) -> None:
        if self._hwnd:
            data = self._icon_data(0)
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
        # hIcon принадлежит системе: снимаем только NotifyIcon
        self._icon = None
        self._forget_target()

    def _forget_target(self) -> None:
        """Убрать из настроек свою запись «кто держит трей» (не чужую)."""
        settings = paths.read_settings()
        if settings.get(TARGET_PID) == kernel32.GetCurrentProcessId():
            settings.pop(TARGET_PID, None)
            settings.pop(TARGET_HWND, None)
            paths.write_settings(settings)

    def _on_message(self, hwnd, message, wparam, lparam) -> int:
        if message == WM_QUIT_TRAY:
            user32.PostQuitMessage(0)
            return 0
        if message == WM_UPDATE:
            self._refresh_icon(self._pending_info)
            return 0
        if message == WM_SHOW:
            self._on_action(SHOW)
            return 0
        if message == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        if message == WM_TRAY:
            event = int(lparam or 0)
            if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK, NIN_BALLOONUSERCLICK):
                self._on_action(SHOW)
            elif event in (WM_RBUTTONUP, WM_CONTEXTMENU):
                self._popup_menu(hwnd)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _popup_menu(self, hwnd) -> None:
        """Меню строится заново на каждый клик: состояние всегда актуально."""
        spec = self._spec()
        menu = user32.CreatePopupMenu()
        commands: dict[int, str] = {}
        code = 1000

        def append(parent, item) -> None:
            nonlocal code
            if isinstance(item, Separator):
                user32.AppendMenuW(parent, MF_SEPARATOR, None, None)
                return
            label = item.label + (f"  ({item.note})" if item.note else "")
            flags = MF_STRING
            if item.kind in ("radio", "check") and item.checked:
                flags |= MF_CHECKED
            if not item.enabled:
                flags |= MF_GRAYED
            if user32.AppendMenuW(parent, flags, code, label):
                commands[code] = item.id
                code += 1

        for entry in spec.items:
            if isinstance(entry, Submenu):
                sub = user32.CreatePopupMenu()
                for item in entry.items:
                    append(sub, item)
                user32.AppendMenuW(menu, MF_POPUP, sub, entry.label)
            else:
                append(menu, entry)

        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        # Без SetForegroundWindow меню не закрывается при клике вне его,
        # а без PostMessage(WM_NULL) Windows считает меню «активным» и
        # следующий правый клик не показывает popup.
        user32.SetForegroundWindow(hwnd)
        cmd = user32.TrackPopupMenuEx(menu,
                                      TPM_RETURNCMD | TPM_LEFTALIGN | TPM_BOTTOMALIGN,
                                      pt.x, pt.y, hwnd, None)
        user32.PostMessageW(hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)
        if cmd:
            action = commands.get(int(cmd))
            if action:
                self._on_action(action)


def confirm(title: str, text: str) -> bool:
    """Вопрос без окна приложения: MessageBoxW работает из любого потока."""
    if not sys.platform == "win32":
        return True
    return user32.MessageBoxW(None, text, title, MB_YESNO) == IDYES

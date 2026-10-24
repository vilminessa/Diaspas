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

from . import icons
from . import paths
from . import tray_menu

# Иконка трея: наша монохромная метка (:mod:`core.icons`) с инком по состоянию.
# HICON для неё даёт .NET (pythonnet читает temp-ICO) - свой растер через GDI
# здесь не выходит, проверено живыми пробами: ``CreateIcon`` с 32-битными
# битами рисует чёрное (``DrawIconEx`` + ``GetPixel`` даёт R=G=B=0),
# ``CreateIconIndirect`` с 1-битной DIB-маской возвращает NULL без кода ошибки,
# ``CreateBitmap`` с переданными битами - тоже NULL, а ``LoadImageW`` по ICO
# (и наш ICO, и образцовый из Pillow) рушится в access violation.
# Fallback, если .NET недоступен (Tk-резерв без pythonnet) - системные значки
# Windows: error - красный крест, warning - янтарный треугольник, information -
# синяя «i», application - нейтральный. Их handle принадлежит системе, поэтому
# DestroyIcon к нему применять нельзя.
ICON_TINTS: dict[str, tuple[int, int, int]] = icons.TINTS
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
    # Клей объявлен в core.win32 один раз: общий ctypes.windll.user32 принимает
    # argtypes того модуля, который объявил их последним, и два модуля со
    # своими POINT/WNDCLASSEXW мешают друг другу (живая проба:
    # "expected LP_POINT instance instead of pointer to POINT").
    from . import win32

    user32 = win32.user32
    shell32 = win32.shell32
    kernel32 = win32.kernel32
    WM_TRAY = user32.RegisterWindowMessageW("DiaspasTray")
    WM_UPDATE = user32.RegisterWindowMessageW("DiaspasTrayUpdate")
    WM_SHOW = user32.RegisterWindowMessageW("DiaspasTrayShow")
    WM_QUIT_TRAY = user32.RegisterWindowMessageW("DiaspasTrayQuit")

    # Сообщения, флаги и структуры - из win32 (одинаковые для обоих модулей).
    VP = win32.VP
    WNDPROC = win32.WNDPROC
    POINT = win32.POINT
    MSG = win32.MSG
    RECT = win32.RECT
    WNDCLASSEXW = win32.WNDCLASSEXW
    NOTIFYICONDATAW = win32.NOTIFYICONDATAW

    WM_NULL = win32.WM_NULL
    WM_DESTROY = win32.WM_DESTROY
    WM_LBUTTONDOWN = win32.WM_LBUTTONDOWN
    WM_LBUTTONUP = win32.WM_LBUTTONUP
    WM_LBUTTONDBLCLK = win32.WM_LBUTTONDBLCLK
    WM_RBUTTONDOWN = win32.WM_RBUTTONDOWN
    WM_RBUTTONUP = win32.WM_RBUTTONUP        # 0x0205, не 0x0204
    WM_RBUTTONDBLCLK = win32.WM_RBUTTONDBLCLK
    WM_CONTEXTMENU = win32.WM_CONTEXTMENU
    WM_USER = win32.WM_USER
    NIN_SELECT = win32.NIN_SELECT
    NIN_KEYSELECT = win32.NIN_KEYSELECT
    NIN_BALLOONUSERCLICK = win32.NIN_BALLOONUSERCLICK   # WM_USER + 5, не 0x0205

    NIF_MESSAGE = win32.NIF_MESSAGE
    NIF_ICON = win32.NIF_ICON
    NIF_TIP = win32.NIF_TIP
    NIF_INFO = win32.NIF_INFO
    NIM_ADD = win32.NIM_ADD
    NIM_MODIFY = win32.NIM_MODIFY
    NIM_DELETE = win32.NIM_DELETE

    MF_STRING = win32.MF_STRING
    MF_GRAYED = win32.MF_GRAYED
    MF_CHECKED = win32.MF_CHECKED
    MF_SEPARATOR = win32.MF_SEPARATOR
    MF_POPUP = win32.MF_POPUP
    TPM_RETURNCMD = win32.TPM_RETURNCMD
    TPM_LEFTALIGN = win32.TPM_LEFTALIGN
    TPM_BOTTOMALIGN = win32.TPM_BOTTOMALIGN

    WS_POPUP = win32.WS_POPUP
    WS_EX_TOOLWINDOW = win32.WS_EX_TOOLWINDOW
    ERROR_ALREADY_EXISTS = win32.ERROR_ALREADY_EXISTS
    MB_YESNO = win32.MB_YESNO
    IDYES = win32.IDYES
    IDNO = win32.IDNO

    # Кэш handle'ов метки по состоянию: Icon (.NET) владеет своим HICON, поэтому
    # объект держим - иначе финализатор DestroyIcon'ом убьёт иконку трея.
    _icon_cache: dict[str, tuple[int, object]] = {}

    def _dotnet_hicon(state: str) -> tuple[int, object] | None:
        """HICON монохромной метки из байтов ICO в памяти (pythonnet).

        MemoryStream вместо файла: `.NET Icon(Stream)` держит stream живым,
        пока жив `Icon`, и temp-ICO был бы заблокирован для других процессов -
        тот же файл читают pywebview (`create_window(icon=...)`) и Tk
        (`iconbitmap`). Живой зависший процесс именно держал этот файл:
        повторное чтение давало IOException «файл используется другим
        процессом». None - .NET не вышел.
        """
        cached = _icon_cache.get(state)
        if cached:
            return cached
        try:
            import clr
            clr.AddReference("System.Drawing")
            import System.Drawing as sd
            from System.IO import MemoryStream
        except Exception:      # noqa: BLE001 - pythonnet не установлен / не инициализирован
            return None
        rgb = ICON_TINTS.get(state, ICON_TINTS["off"])
        try:
            icon = sd.Icon(MemoryStream(icons.ico_bytes(rgb=rgb)))
            handle = int(icon.Handle.ToInt64())
        except Exception:      # noqa: BLE001 - ICO не прочитался
            return None
        if not handle:
            return None
        _icon_cache[state] = (handle, icon)
        return _icon_cache[state]

    def _make_hicon(name: str) -> tuple[int | None, object | None]:
        """(hIcon, keepalive): метка, если .NET вышел; иначе системный значок.

        Системный handle повторным LoadIconW возвращается тем же значением и
        DestroyIcon не требует; handle от .NET не DestroyIcon - им владеет Icon.
        """
        dotnet = _dotnet_hicon(name)
        if dotnet:
            return dotnet
        icon = user32.LoadIconW(
            None, ICON_RESOURCES.get(name, ICON_RESOURCES["off"]))
        return (icon or None), None


class Tray:
    """Иконка в трее + свой цикл сообщений в отдельном потоке.

    ``spec`` - callable, возвращающий :class:`TraySpec` на каждый правый клик
    и на каждое обновление: меню строится заново, поэтому актуальное состояние
    видно без machinery обновления меню. ``on_action`` вызывается из этого
    потока с id пункта; тяжёльные операции диспетчер уводит в worker-поток.
    """

    def __init__(self, spec: Callable[["TraySpec"]],
                 on_action: Callable[[str], None], uid: int = 1,
                 log: Callable[[str], None] | None = None) -> None:
        self._spec = spec
        self._on_action = on_action
        self._uid = uid
        self._log = log or (lambda _text: None)
        self._thread: threading.Thread | None = None
        self._hwnd: int | None = None
        self._icon: int | None = None
        self._icon_keep: object | None = None   # .NET Icon, если он дал handle
        self._icon_kind = ""                     # "метка" | "системный значок"
        self._mutex: int | None = None
        self._menu: "tray_menu.MenuHost | None" = None
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

    @property
    def icon_kind(self) -> str:
        """Откуда значок трея: наша метка (.NET) или системный fallback."""
        return self._icon_kind

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
        """NOTIFYICONDATAW в классическом протоколе (uVersion=0).

        Версия 4 сообщает события как NIN_SELECT и даёт курсор в client-
        координатах; версия 0 присылает обычные mouse-сообщения, которые мы
        и разбираем. uTimeout - длительность balloon (в версии 4 её несёт
        dwInfoTimeout, заполняем оба).
        """
        data = NOTIFYICONDATAW(ctypes.sizeof(NOTIFYICONDATAW), self._hwnd,
                               self._uid, flags, WM_TRAY, self._icon,
                               4000, 0)
        return data

    def _add_icon(self) -> None:
        spec = self._spec()
        self._icon, self._icon_keep = _make_hicon(spec.icon_state)
        self._icon_kind = "метка" if self._icon_keep else "системный значок"
        data = self._icon_data(NIF_MESSAGE | NIF_ICON | NIF_TIP)
        data.szTip = spec.tooltip[:TIP_LIMIT]
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)):
            raise OSError("Shell_NotifyIconW(NIM_ADD) отказал: нет "
                          "интерактивного десктопа?")

    def _refresh_icon(self, info: tuple[str, str] | None = None) -> None:
        spec = self._spec()
        icon, keep = _make_hicon(spec.icon_state)
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
            # handle либо системный (повторный LoadIconW даёт то же значение),
            # либо принадлежит .NET Icon - DestroyIcon не вызываем ни в одном
            # случае; keepalive держит Icon живым
            if icon:
                self._icon = icon
                self._icon_keep = keep
                self._icon_kind = "метка" if keep else "системный значок"
            self._pending_info = None

    def _remove_icon(self) -> None:
        if self._hwnd:
            data = self._icon_data(0)
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
        # hIcon не DestroyIcon: он системный или принадлежит .NET Icon (кэш)
        self._icon = None
        self._icon_keep = None
        if self._menu is not None:
            self._menu.close_all()      # окна меню живут в этой нити
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
            if self._menu is not None:
                self._menu.close_all()   # меню закрывается до конца цикла
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
            if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK, NIN_SELECT,
                         NIN_KEYSELECT, NIN_BALLOONUSERCLICK):
                self._on_action(SHOW)
            elif event in (WM_RBUTTONUP, WM_RBUTTONDBLCLK, WM_CONTEXTMENU):
                self._open_menu(hwnd)
            # WM_RBUTTONDOWN и служебные коды balloon (show/hide/timeout) -
            # не действия: нажатие не должно открывать меню до релиза
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _open_menu(self, hwnd) -> None:
        """Тёмное меню своим popup-окном; если клей не поднялся - нативное.

        Меню НЕ блокирует нить: окно живёт на сообщениях, поэтому иконка
        обновляется, пока меню открыто. Нативный TrackPopupMenuEx - резервный
        путь (нет десктопа/GDI), и он блокирует нить до закрытия меню.
        """
        spec = self._spec()
        if self._menu is None:
            self._menu = tray_menu.MenuHost(self._log)
        if self._menu.open(spec, self._on_action):
            return
        self._popup_menu_native(hwnd)

    def _popup_menu_native(self, hwnd) -> None:
        """Резерв: меню строится заново на каждый клик - состояние актуально."""
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

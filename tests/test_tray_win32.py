"""Win32-клей трея офлайн: API подменён, поведение проверено.

CI гоняет эти тесты на windows-latest, но без интерактивного десктопа:
NotifyIcon там отказывает, и именно этот отказ нужно проверить - GUI не
должен падать. Реальные вызовы тоже проверяются там, где десктоп не нужен:
LoadIconW (системный значок) работает и в CI.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import paths, tray  # noqa: E402

if sys.platform != "win32":
    pytest.skip("клей Win32 проверяется только на Windows", allow_module_level=True)

STATE = {"dir_ok": True, "installed": True, "running": True,
         "strategy": "general (ALT11)", "strategy_label": "ALT11",
         "strategy_mode": "manual", "preset_key": "discord_youtube",
         "preset_title": "Discord / YouTube",
         "auto_cooldown_left": 0, "auto_last_strategy": "",
         "strategies": [{"file": "general (ALT11).bat", "label": "ALT11",
                         "active": True}]}
PRESETS = [{"key": "discord_youtube", "title": "Discord / YouTube",
            "off": False}]


def spec() -> tray.TraySpec:
    return tray.tray_model(STATE, PRESETS)


def test_soft_failure_when_registration_fails(monkeypatch, tmp_path):
    """Иконка не поднялась -> {ok: False}, а не исключение: GUI работает дальше."""
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)

    def boom(self):
        raise OSError("нет интерактивного десктопа")

    monkeypatch.setattr(tray.Tray, "_create_window", boom)
    holder = tray.Tray(spec, lambda _a: None)
    res = holder.start()
    assert res["ok"] is False
    assert "десктопа" in res["error"]
    assert holder.hwnd is None


def test_start_times_out_if_thread_never_ready(monkeypatch, tmp_path):
    """Нитка трея зависла - start отказывается по таймауту, GUI не ждёт."""
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)

    def hang(self):
        import threading
        threading.Event().wait(30)

    monkeypatch.setattr(tray.Tray, "_create_window", hang)
    holder = tray.Tray(spec, lambda _a: None)
    res = holder.start()
    assert res["ok"] is False
    assert "не поднялся" in res["error"]


def test_icon_handle_is_the_mark_when_dotnet_works(monkeypatch, tmp_path):
    """Метка из temp-ICO: handle валиден, keepalive держит .NET Icon."""
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    keep = object()
    monkeypatch.setattr(tray, "_dotnet_hicon",
                        lambda state: (12345, keep) if state in tray.ICON_TINTS
                        else None)
    icon, got = tray._make_hicon("ok")
    assert icon == 12345 and got is keep
    # cache: тот же handle и тот же объект при повторном запросе
    again = tray._dotnet_hicon("ok")
    assert again == (12345, keep)


def test_icon_handle_falls_back_to_system_sign(monkeypatch, tmp_path):
    """.NET недоступен -> системный значок по состоянию, keepalive None."""
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(tray, "_dotnet_hicon", lambda _state: None)
    loaded: list[int] = []

    def fake_load(_instance, resource):
        loaded.append(int(resource))
        return 4242

    monkeypatch.setattr(tray.user32, "LoadIconW", fake_load)
    icon, keep = tray._make_hicon("warn")
    assert icon == 4242 and keep is None
    assert loaded == [tray.ICON_RESOURCES["warn"]]


def test_icon_handles_for_every_state(monkeypatch):
    """Все состояния дают и инк метки, и системный значок: трей не пустой."""
    assert set(tray.ICON_TINTS) == set(tray.ICON_RESOURCES) == {"ok", "warn", "err", "off"}
    assert len(set(tray.ICON_TINTS.values())) == 4, "состояния не различаются"
    assert len(set(tray.ICON_RESOURCES.values())) == 4, "значки не различаются"
    monkeypatch.setattr(tray, "_dotnet_hicon", lambda _state: None)
    seen: list[int] = []

    def fake_load(_instance, resource):
        seen.append(int(resource))
        return 1000 + int(resource)

    monkeypatch.setattr(tray.user32, "LoadIconW", fake_load)
    handles = {state: tray._make_hicon(state)[0] for state in tray.ICON_RESOURCES}
    assert all(handles), handles
    assert len(set(handles.values())) == 4


def test_unknown_icon_state_falls_back_to_off(monkeypatch):
    """Неизвестное состояние -> инк и значок «выключен», без KeyError."""
    monkeypatch.setattr(tray, "_dotnet_hicon", lambda _state: None)
    loaded: list[int] = []
    monkeypatch.setattr(tray.user32, "LoadIconW",
                        lambda _i, resource: (loaded.append(int(resource)) or 4242))
    icon, _keep = tray._make_hicon("bogus")
    assert icon == 4242
    assert loaded == [tray.ICON_RESOURCES["off"]]


def test_icon_handle_none_when_load_refuses(monkeypatch):
    """LoadIconW вернул NULL -> None: NotifyIcon без иконки, а не битая."""
    monkeypatch.setattr(tray, "_dotnet_hicon", lambda _state: None)
    monkeypatch.setattr(tray.user32, "LoadIconW", lambda *_a: None)
    assert tray._make_hicon("ok") == (None, None)


def test_refresh_never_destroys_icon_handles(monkeypatch, tmp_path):
    """Смена состояния не DestroyIcon: handle системный или принадлежит .NET."""
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    destroyed: list[int] = []

    class FakeUser:
        def LoadIconW(self, _instance, resource):
            return 1000 + int(resource)

        def DestroyIcon(self, icon):
            destroyed.append(icon)
            return True

    class FakeShell:
        def Shell_NotifyIconW(self, _op, _data):
            return True

    monkeypatch.setattr(tray, "user32", FakeUser())
    monkeypatch.setattr(tray, "shell32", FakeShell())
    monkeypatch.setattr(tray, "_dotnet_hicon", lambda _state: None)
    holder = tray.Tray(spec, lambda _a: None)
    holder._hwnd = 1
    holder._icon = 999
    holder._refresh_icon()
    assert destroyed == []          # системный значок DestroyIcon не трогает
    assert holder._icon == 1000 + tray.ICON_RESOURCES[spec().icon_state]
    holder._remove_icon()
    assert destroyed == []          # и при съезде иконки тоже


def test_claim_singleton_wakes_the_owner(monkeypatch, tmp_path):
    """Mutex занят живым экземпляром - показываем его окно и не стартуем."""
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    paths.write_settings({tray.TARGET_PID: 4242, tray.TARGET_HWND: 999})

    class FakeKernel:
        def CreateMutexW(self, *_a):
            return 12345

        def GetLastError(self):
            return tray.ERROR_ALREADY_EXISTS

    class FakeUser:
        sent: tuple[int, int] | None = None

        def IsWindow(self, hwnd):
            return True

        def PostMessageW(self, hwnd, msg, wp, lp):
            self.sent = (hwnd, msg)
            return True

    monkeypatch.setattr(tray, "kernel32", FakeKernel())
    fake_user = FakeUser()
    monkeypatch.setattr(tray, "user32", fake_user)
    holder = tray.Tray(spec, lambda _a: None)
    res = holder.claim_singleton()
    assert res == {"ok": True, "owner": False, "pid": 4242}
    assert fake_user.sent == (999, tray.WM_SHOW)
    assert holder._mutex is None      # чужой mutex мы не держим


def test_claim_singleton_takes_over_when_target_is_dead(monkeypatch, tmp_path):
    """Запись от мёртвого окна - не «показываем чужое», а стартуем сами."""
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    paths.write_settings({tray.TARGET_PID: 4242, tray.TARGET_HWND: 999})

    class FakeKernel:
        def CreateMutexW(self, *_a):
            return 12345

        def GetLastError(self):
            return tray.ERROR_ALREADY_EXISTS

    class FakeUser:
        sent = None

        def IsWindow(self, _hwnd):
            return False           # окно из записи уже не окно

        def PostMessageW(self, hwnd, msg, wp, lp):
            FakeUser.sent = (hwnd, msg)
            return True

    monkeypatch.setattr(tray, "kernel32", FakeKernel())
    monkeypatch.setattr(tray, "user32", FakeUser())
    holder = tray.Tray(spec, lambda _a: None)
    res = holder.claim_singleton()
    assert res["owner"] is True and res.get("stale") is True
    assert FakeUser.sent is None    # слать некуда - IsWindow отказал


def test_claim_singleton_first_instance_wins(monkeypatch, tmp_path):
    """Mutex свободен - владелец мы, handle держим до выхода процесса."""
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)

    class FakeKernel:
        def CreateMutexW(self, *_a):
            return 999

        def GetLastError(self):
            return 0

    monkeypatch.setattr(tray, "kernel32", FakeKernel())
    holder = tray.Tray(spec, lambda _a: None)
    res = holder.claim_singleton()
    assert res == {"ok": True, "owner": True}
    assert holder._mutex == 999


def test_claim_singleton_reports_mutex_error(monkeypatch):
    class FakeKernel:
        def CreateMutexW(self, *_a):
            return None

        def GetLastError(self):
            return 5

    monkeypatch.setattr(tray, "kernel32", FakeKernel())
    res = tray.Tray(spec, lambda _a: None).claim_singleton()
    assert res["ok"] is False
    assert "CreateMutexW" in res["error"]


def test_tray_events_route_right_click_to_menu(monkeypatch):
    """Правый клик - меню, левый/двойной/клик по уведомлению - окно.

    Regression: WM_RBUTTONUP=0x0205 был записан как balloon-код, и правый
    клик открывал окно вместо меню.
    """
    opened: list[str] = []
    holder = tray.Tray(spec, lambda action: opened.append(action))
    holder._hwnd = 1
    holder._open_menu = lambda _hwnd: opened.append("menu")   # без создания окон

    holder._on_message(1, tray.WM_TRAY, 0, tray.WM_RBUTTONUP)
    assert opened == ["menu"]
    opened.clear()
    holder._on_message(1, tray.WM_TRAY, 0, tray.WM_RBUTTONDBLCLK)
    assert opened == ["menu"]
    opened.clear()
    holder._on_message(1, tray.WM_TRAY, 0, tray.WM_CONTEXTMENU)
    assert opened == ["menu"]
    opened.clear()

    for code in (tray.WM_LBUTTONUP, tray.WM_LBUTTONDBLCLK, tray.NIN_SELECT,
                 tray.NIN_KEYSELECT, tray.NIN_BALLOONUSERCLICK):
        holder._on_message(1, tray.WM_TRAY, 0, code)
    assert opened == [tray.SHOW] * 5

    opened.clear()
    # нажатие (не релиз) не открывает меню и не показывает окно
    holder._on_message(1, tray.WM_TRAY, 0, tray.WM_RBUTTONDOWN)
    holder._on_message(1, tray.WM_TRAY, 0, tray.WM_LBUTTONDOWN)
    holder._on_message(1, tray.WM_TRAY, 0, tray.WM_USER + 2)   # balloon show
    holder._on_message(1, tray.WM_TRAY, 0, tray.WM_USER + 4)   # balloon timeout
    assert opened == []


def test_button_message_constants_match_msdn():
    """Разряды мышиных сообщений: правый релиз 0x0205, balloon 0x0405."""
    assert tray.WM_LBUTTONDOWN == 0x0201
    assert tray.WM_LBUTTONUP == 0x0202
    assert tray.WM_LBUTTONDBLCLK == 0x0203
    assert tray.WM_RBUTTONDOWN == 0x0204
    assert tray.WM_RBUTTONUP == 0x0205
    assert tray.WM_RBUTTONDBLCLK == 0x0206
    assert tray.WM_CONTEXTMENU == 0x007E
    # NIN_* это WM_USER + n, а не сообщения мыши: 0x0205 - правый релиз
    assert tray.NIN_SELECT == tray.WM_USER + 0
    assert tray.NIN_BALLOONUSERCLICK == tray.WM_USER + 5
    assert tray.NIN_BALLOONUSERCLICK != tray.WM_RBUTTONUP


def test_notify_icon_uses_classic_version():
    """uVersion=0: lParam приходит как mouse-сообщение, balloon - WM_LBUTTONUP."""
    data = tray.Tray(spec, lambda _a: None)._icon_data(tray.NIF_MESSAGE | tray.NIF_ICON)
    assert data.uVersion == 0
    assert data.uTimeout == 4000


def test_menu_maps_commands_back_to_action_ids(monkeypatch):
    """Резервный нативный меню: id пункта возвращаются по команде TrackPopupMenu."""
    sent: list[tuple[int, int, object, str]] = []
    menus: list[int] = []

    class FakeUser:
        def CreatePopupMenu(self):
            menus.append(len(menus))
            return len(menus)

        def AppendMenuW(self, parent, flags, item, label):
            sent.append((parent, flags, item, label))
            return True

        def GetCursorPos(self, _pt):
            return True

        def SetForegroundWindow(self, _hwnd):
            return True

        def TrackPopupMenuEx(self, menu, flags, x, y, hwnd, _p):
            # выбираем второй пункт верхнего меню («Скрыть окно»)
            codes = [i for _par, _f, i, _l in sent
                     if i is not None and i >= 1000 and _par == menu]
            return codes[1] if len(codes) > 1 else 0

        def PostMessageW(self, *_a):
            return True

        def DestroyMenu(self, _menu):
            return True

    monkeypatch.setattr(tray, "user32", FakeUser())
    actions: list[str] = []
    holder = tray.Tray(spec, actions.append)
    holder._hwnd = 1
    holder._popup_menu_native(1)

    labels = [entry[3] for entry in sent if entry[3]]
    assert labels[:2] == ["Открыть окно", "Скрыть окно"]
    assert "Пресеты" in labels and "Стратегии" in labels
    # отметка активного пресета и активной стратегии - MF_CHECKED
    # (MF_SEPARATOR занимает тот же разряд, поэтому берём только строки)
    marked = [entry[3] for entry in sent if entry[3] and entry[1] & tray.MF_CHECKED]
    assert marked == ["Discord / YouTube", "ALT11"], sent
    # гашение: в ручном режиме «применить лучшую» недоступна -> MF_GRAYED
    grayed = [entry[3] for entry in sent if entry[1] & tray.MF_GRAYED]
    assert grayed == ["Применить лучшую стратегию"], sent
    assert actions == ["window.hide"]


def test_update_and_notify_post_messages(monkeypatch):
    """update/notify идут сообщениями в нить трея: NotifyIcon трогает один поток."""
    posted: list[tuple[int, int]] = []

    class FakeUser:
        def PostMessageW(self, hwnd, msg, wp, lp):
            posted.append((hwnd, msg))
            return True

    monkeypatch.setattr(tray, "user32", FakeUser())
    holder = tray.Tray(spec, lambda _a: None)
    holder._hwnd = 777
    holder.update()
    holder.notify("Diaspas", "сбой проб")
    assert posted == [(777, tray.WM_UPDATE), (777, tray.WM_UPDATE)]
    assert holder._pending_info == ("Diaspas", "сбой проб")


def test_quit_posts_quit_message(monkeypatch):
    posted: list[tuple[int, int]] = []

    class FakeUser:
        def PostMessageW(self, hwnd, msg, wp, lp):
            posted.append((hwnd, msg))
            return True

    monkeypatch.setattr(tray, "user32", FakeUser())
    holder = tray.Tray(spec, lambda _a: None)
    holder._hwnd = 555
    holder.quit()
    assert posted == [(555, tray.WM_QUIT_TRAY)]


def test_confirm_yes_and_no(monkeypatch):
    answers = iter([tray.IDYES, tray.IDNO])
    monkeypatch.setattr(tray.user32, "MessageBoxW",
                        lambda *_a: next(answers))
    assert tray.confirm("Выход", "Обход работает") is True
    assert tray.confirm("Выход", "Обход работает") is False


def test_forget_target_removes_only_our_record(monkeypatch, tmp_path):
    """Съезд трея убирает свою запись, чужую - не трогает."""
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    my_pid = tray.kernel32.GetCurrentProcessId()

    paths.write_settings({tray.TARGET_PID: my_pid, tray.TARGET_HWND: 111,
                          "strategy_mode": "auto"})
    holder = tray.Tray(spec, lambda _a: None)
    holder._hwnd = 111
    holder._forget_target()
    settings = paths.read_settings()
    assert tray.TARGET_PID not in settings and tray.TARGET_HWND not in settings
    assert settings["strategy_mode"] == "auto"

    paths.write_settings({tray.TARGET_PID: my_pid + 1000,
                          tray.TARGET_HWND: 222})
    holder._forget_target()
    settings = paths.read_settings()
    assert settings.get(tray.TARGET_PID) == my_pid + 1000   # чужая запись цела

# Разработка Diaspas

## Версионирование

`version.py` — единственный источник версии, два значения с разными ролями:

| Значение | Кто читает | Пример |
|---|---|---|
| `__version__` | `diaspas.spec` (VersionInfo exe), тесты | `0.1.2` |
| `BUILD_LABEL` | статус-бар, заголовок Tk, первая строка журнала | `v0.1.2`, `b0.1.2`, `` (не из тега) |

Бамп версии отдельным коммитом не нужен: `tools/stamp_version.py <тег>`
вписывает оба значения байтовой заменой в рабочее дерево CI перед
сборкой exe (рабочее дерево одноразовое, в git ничего не попадает).
Формат тега `[vb]X.Y.Z[.N]`, не больше четырёх компонентов — Windows
VersionInfo больше не принимает, «промежуточность» несёт префикс `b`.

Если `BUILD_LABEL` пуст, интерфейс показывает `unreleased`: это локальная
сборка или `workflow_dispatch` без тега.

## Полосы выпуска

| Полоса | Ветка | Тег | Публикация |
|---|---|---|---|
| бета | `dev` | `b0.1.2` | GitHub Pre-release |
| финал | `main` | `v0.1.2` | обычный релиз |

```bash
# бета: коммит и push в dev, затем тег
git checkout dev && git push origin dev
git tag b0.1.2 && git push origin b0.1.2

# финал: main подтягивается до dev fast-forward, затем тег
git branch -f main dev && git push origin main
git tag v0.1.2 && git push origin v0.1.2
```

Конвейер на теге: `verify` (бандл, pytest, pyflakes, линт workflows,
формат тега, родство с веткой) → `build` (`stamp_version` → PyInstaller →
граница размера → хеш) → `python-publish` (exe в релиз, бета — `--prerelease`)
→ генератор SLSA (`private-repository: true`) → `verify-attestation`
(провенанс из релиза, exe из артефакта этого же рана — релизы при
пересоздании тега «осиротевали», и проверка падала с «no such file»).

Гарды важнее удобства: тег `b` вне `dev` и тег `v` вне `main` должны
падать до сборки exe и до публикации. `fetch-depth: 0` обязателен —
на shallow-клоне `merge-base --is-ancestor` не видит историю тега.

## Что проверяет `tools/check_workflows.py`

Невидимые символы в YAML (GitHub принимает, шаг исполняет не то),
пининг `uses:` на SHA (кроме тега генератора SLSA), `permissions` на
верхнем уровне, триггеры, наличие и порядок джобов полос, гарды формата
и родства тега, публикация только на теге, `--prerelease` у беты и его
отсутствие у релиза, хеш для SLSA, флаги `gh attestation verify`,
нижняя граница размера exe. Ключевой пункт — шаг `stamp_version` **до**
PyInstaller: без него надпись «версия берётся из тега» остаётся
обещанием, а exe и интерфейс выходят без версии.

Шаги `ci.yml` (push/PR в `main` и `dev`): compileall, `node --check`,
сверка бандла, pytest, pyflakes, линт workflows. Тесты с сетью и UAC в
CI не гоняются — они живые (`tests/run_*`, `tests/integration_webapp.py`).

## Консоль проекта

`debug.bat` — тонкий ASCII-диспетчер; меню, логика и русские строки в
`tools/debug_ctl.py`. Причина разделения: cmd перечитывает батч по
байтовому offset после `chcp 65001`, кириллица сдвигает offset и
выполняет фрагменты строк. Быстрый режим передаётся аргументом
`--scripted`, а не переменной окружения — env наследуется дочерним
процессам, и pytest под пунктом CI увидел бы чужой режим.

Опасные пункты (перезапуск службы, UAC, удаление задач, переустановка
релиза, `git push`, закрытие процессов) в меню спрашивают подтверждение,
в быстром режиме требуют `--yes`. `winws.exe` консолью не трогается: это
процесс обхода, а не проекта.

## Трей: модель, клей и потоки

`core/tray.py` — два слоя в одном файле. Модель (`tray_model`, `tooltip`,
`icon_state`, `Item`/`Submenu`/`Separator`, разбор id, `is_heavy`, `handle`)
не знает Win32 и проверяется офлайн; клей (`Tray`, `confirm`,
`claim_singleton`) — только тонкая обёртка над сообщениями.

**Свой NotifyIcon на ctypes, не pystray.** pystray тянет Pillow (+6–10 МБ в
onefile-бандл и dependency в requirements), а рантайм проекта — только
stdlib. Клей специально сделан мягким: в Session 0 и в CI нет
интерактивного десктопа, `Shell_NotifyIconW(NIM_ADD)` там отказывает, и
`Tray.start` возвращает `{ok: False, error}` с warning в журнале — GUI
ведёт себя ровно как без трея.

**Что CI проверяет, а что нет.** Офлайн: модель меню, диспетчер `handle`,
настройки трея, отказ регистрации и таймаут `start`, одиночный экземпляр и
`_forget_target` (все с подменёнными `tray.user32`/`tray.shell32`/
`tray.kernel32`), геометрия и анимация меню (`core.tray_menu` - чистые
функции), клей меню с подменёнными `tray_menu.user32`/`tray_menu.gdi32`
(окна, таймеры, alpha, каскад, щит, fallback), `LoadIconW` - он десктопа не
требует. Не проверяется в CI:
реальная иконка на экране, отрисовка и каскад тёмного меню, balloon,
пробуждение чужого окна - это живые прогоны (`debug.bat T`,
`python main.py --min`, второй запуск).

**Потоки.** Нить трея владеет NotifyIcon: `update`/`notify`/`show_request`
из других нитей только слают сообщения (`PostMessageW`,
зарегистрированные `DiaspasTray`/`DiaspasTrayUpdate`/`DiaspasTrayShow`/
`DiaspasTrayQuit`), а `Shell_NotifyIconW` вызываются из этой нити. Меню
строится заново на каждый правый клик и НЕ блокирует нить (в отличие от
`TrackPopupMenuEx`): окно меню живёт на своих сообщениях, поэтому иконка
обновляется, пока меню открыто.

Долгие пункты (`preset:`, `strategy:`, `auto.run`, `probes.run`) уходят в
worker-нить: цикл трея не может ждать минуты. Повторный клик той же операции
отказывается (`_tray_busy`), а не поднимает вторую. Общий протокол операций —
`tray.Ops`: webview-`App` реализует его напрямую, Tk — адаптер `_TrayOps`
над `_bg` и kinds очереди `tray_show`/`tray_hide`/`tray_exit`/`tray_mode`.
Триггер «пробы с сбоями → автоцикл» перенесён в Python
(`App.check_and_autofix`): со скрытым окном JS ничего не опрашивает.

**Клик по иконке.** Левый, двойной левый и клик по уведомлению - открыть
окно; правый (и `WM_CONTEXTMENU`) - меню. Разряды по MSDN: `WM_RBUTTONDOWN`
= 0x0204, `WM_RBUTTONUP` = 0x0205, `WM_RBUTTONDBLCLK` = 0x0206, а коды
NotifyIcon версии 4 - `WM_USER + n`, т.е. `NIN_BALLOONUSERCLICK` = 0x0405.
Запись `NIN_BALLOONUSERCLICK = 0x0205` означала, что правый релиз мыши
попадал в ветку «клик по balloon» и открывал окно вместо меню (живой клик
дал lParam 0x0205). Regression-тест:
`test_tray_events_route_right_click_to_menu`. `uVersion` в
`NOTIFYICONDATAW` - 0 (классический протокол): lParam приходит как
mouse-сообщение, balloon-клик - `WM_LBUTTONUP`.

**Тёмное меню (`core/tray_menu.py`).** `TrackPopupMenuEx` нельзя затемнить и
анимировать: `MF_OWNERDRAW` в Windows 11 перекрывается темой Explorer, а
появление меню решает система. Меню - наше popup-окно (`WS_POPUP`,
`WS_EX_TOOLWINDOW|WS_EX_TOPMOST|WS_EX_LAYERED`), фон и текст рисует
`WM_PAINT` токенами из `ui_src/app.css`; появление -
`SetLayeredWindowAttributes(LWA_ALPHA)` с альфой по `SetTimer` (fade 110 мс
ease-out + slide 6 px, дочернее меню 80 мс), закрытие мгновенное. Клик вне
меню закрывает его: щит - layered окно с alpha 1 поверх work-area монитора
курсора (`WS_EX_NOACTIVATE` - не крадёт фокус, но принимает клики; alpha 0
их пропускает), по Z-order под меню. Каскад подменю - второе такое же окно
справа от строки подменю (flip влево при правом крае, scroll колесом и
стрелками, если список длиннее экрана). Клавиатура (Up/Down/Enter/Right/Left/
Esc) требует foreground: меню берёт его и при закрытии возвращает запомченный
`GetForegroundWindow`. Гашеные строки подсвечиваются, но клик и Enter на них
ничего не исполняют. Отказ создать окно (нет десктопа, GDI не вышел) ->
`MenuHost.open` возвращает `False`, и `Tray._open_menu` уходит в нативный
`TrackPopupMenuEx` с warning в журнал - та же мягкая деградация, что у
NotifyIcon.

**Один Win32-клей (`core/win32.py`).** `ctypes.windll.user32` - общий объект:
действуют `argtypes`, объявленные последним. Если каждый модуль объявляет свой
`POINT`/`WNDCLASSEXW`, вызов чужого модуля падает с
`expected LP_POINT instance instead of pointer to POINT` (живая проба).
Структуры, сообщения и restype/argtypes объявлены в `win32.py` один раз, и оба
модуля используют их; хелперы (`monitor_work`, `dpi_of`, `measure`) принимают
api-объект аргументом, чтобы тесты могли подменить клей одного модуля.

**Ограничения pywebview.** `webview.start` обязан быть на главном потоке и
не перезапускается после `Application.Exit` — поэтому окно создаётся
видимым и скрывается внутри `bootstrap` (`hidden=True` на создании не
используется), а «Открыть окно» возвращает то же окно. `hide`/`show`/`destroy`
ждут `events.shown` (до 20 с) и маршаллись через `Invoke`, так что вызов из
нити трея безопасен. Крестик не перехватывается: закрытие окна = выход
процесса, трей при съезде убирается (`app.stop_tray` после `webview.start`).
Память при скрытии не освобождается — форма WinForms и процесс WebView2
живут, это documented limitation в docstring `core/tray.py`.

**Клей Win32: что сработало и где падает.** Указатели и `LRESULT` нужно
restype'ить (`VP = c_void_p`, `LRESULT = c_ssize_t`), иначе ctypes вернёт
`int` и hwnd/hIcon теряются на x64. GDI-объекты (DC, bitmap, brush, font) -
32-битные handles даже на x64: `restype=c_void_p` читает их со знаком, и
`CreateIconIndirect` с такими «handles» возвращал NULL (`HD = c_uint32`).
`Shell_NotifyIconW` - в `shell32`, `FillRect` и `GetDC` - в `user32`, а не в
`gdi32`; `CreateCompatibleDC(NULL)` требует `argtypes=[VP]`, иначе NULL не
проходит. `WNDCLASSEXW`: `hClassMenu` идёт **после** `lpszClassName`, с
«красивым» порядком `RegisterClassExW` отказывает с `ERROR_INVALID_PARAMETER`
(87). `ICONINFO`: `BOOL` - 4 байта (не `c_bool`), иначе layout 0/1/8/16 вместо
0/4/8/16 и `CreateIconIndirect` читает `hbmMask` как мусор. Строки в
структуре - `ctypes.c_wchar * N` (`create_unicode_buffer` в `_fields_` не
принимается как тип поля), `szTip` вмещает 63 символа.

**Иконка: SVG, ICO и HICON (`core/icons.py`).** Форма метки (расколотое кольцо
из трёх дуг с зазорами + точка) описана один раз в `MARK`: из неё генерируются
`assets/icon.svg` и `assets/icon.ico` (`tools/render_icon.py`, сверка
`--check`, актуальность держит `tests/test_icons.py` - тот же принцип, что
`build_ui.py --check`). Растер считается из `ink_at` напрямую, Pillow не нужен.
`assets/icon.ico` вшит в exe (`diaspas.spec: icon=`) - значок файла и панели
задач; окно формы получает его через pythonnet (`native.Icon =
Icon(FileStream(...))` в `App.hello` - к этому моменту `win.native` уже
существует, в `bootstrap` он ещё None), Tk - через `iconbitmap` (грузит ICO
сам). Трейный HICON тоже из temp-ICO: pythonnet грузит `Icon` и даёт handle
для `NotifyIcon`; `.NET Icon` владеет handle, поэтому объект держится в кэше
по состоянию (`_icon_cache`) и `DestroyIcon` не вызывается ни для него, ни для
системного значка. Инк метки выбирается по состоянию (`ICON_TINTS`). Свой
растер через GDI здесь не выходит - проверено пробами: `CreateIcon` с
32-битными битами рисует чёрное (`DrawIconEx` + `GetPixel` даёт R=G=B=0),
`CreateIconIndirect` с 1-битной DIB-маской возвращает NULL без кода ошибки,
`CreateBitmap` с переданными битами - NULL, `LoadImageW` по ICO (наш и
образцовый из Pillow) рушится в access violation. Если pythonnet недоступен
(Tk-резерв), трей берёт системные значки `LoadIconW(None, IDI_*)`; журнал
пишет, какой значок использован (`Tray.icon_kind`). `WM_GETICON` на WinForms
окне отвечает 0 (форма держит `Icon` внутри), поэтому значок окна проверяется
журнальной строкой «иконка окна: метка из ICO», а не внешним запросом.

**Одиночный экземпляр.** Признак живости — сам именованный mutex
`DiaspasGui`: kernel закрывает handle при выходе процесса, поэтому
`ERROR_ALREADY_EXISTS` означает «есть живой владелец». Записанные
`tray_pid`/`tray_hwnd` — только адрес для `PostMessageW`: pid мог быть
переиспользован чужим процессом, поэтому цель проверяется `IsWindow` и
результатом отправки. Если слать нечем (запись от мёртвого окна), экземпляр
не «показывает чужое», а takeover'ит трей сам — скрытый живой GUI, который
нельзя разбудить, хуже второй иконки.

## Живые прогоны

- `tests/run_probes.py`, `tests/run_ab.py <группа>` — сеть;
- `tests/run_strategy.py --list | "<файл>" | --auto` — смена стратегии;
  автоцикл гасит `winws` между стратегиями и возвращает обход;
- `tests/run_select.py` — полный перебор (UAC, минуты);
- `tests/integration_webapp.py` — полный путь через тот же api, что GUI;
- `tests/smoke_webview.py`, `tests/smoke_exe.py` — окно и exe: процесс,
  мост JS↔Python, журнал, закрытие;
- служебные задачи планировщика (`DiaspasService`, `DiaspasSelector`)
  регистрируются одним UAC и больше его не дергают; удалить —
  `debug.bat H` или `schtasks /Delete`.

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
`tray.kernel32`), `LoadIconW` — он десктопа не требует. Не проверяется в CI:
реальная иконка на экране, popup-меню, balloon, пробуждение чужого окна —
это живые прогоны (`debug.bat T`, `python main.py --min`, второй запуск).

**Потоки.** Нить трея владеет NotifyIcon: `update`/`notify`/`show_request`
из других нитей только слают сообщения (`PostMessageW`,
зарегистрированные `DiaspasTray`/`DiaspasTrayUpdate`/`DiaspasTrayShow`/
`DiaspasTrayQuit`), а `Shell_NotifyIconW` вызываются из этой нити. Меню
строится заново на каждый правый клик (`TrackPopupMenuEx` с
`TPM_RETURNCMD`), поэтому состояние всегда актуально без обновления меню;
без `SetForegroundWindow` перед треком меню не закрывается кликом вне его,
без `PostMessage(WM_NULL)` после — Windows считает меню активным и следующий
правый клик игнорирует.

Долгие пункты (`preset:`, `strategy:`, `auto.run`, `probes.run`) уходят в
worker-нить: цикл трея не может ждать минуты. Повторный клик той же операции
отказывается (`_tray_busy`), а не поднимает вторую. Общий протокол операций —
`tray.Ops`: webview-`App` реализует его напрямую, Tk — адаптер `_TrayOps`
над `_bg` и kinds очереди `tray_show`/`tray_hide`/`tray_exit`/`tray_mode`.
Триггер «пробы с сбоями → автоцикл» перенесён в Python
(`App.check_and_autofix`): со скрытым окном JS ничего не опрашивает.

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
`int` и hwnd/hIcon теряются на x64. `Shell_NotifyIconW` — в `shell32`, не в
`user32`. `WNDCLASSEXW`: `hClassMenu` идёт **после** `lpszClassName`, с
«красивым» порядком `RegisterClassExW` отказывает с `ERROR_INVALID_PARAMETER`
(87). Строки в структуре — `ctypes.c_wchar * N` (`create_unicode_buffer` в
`_fields_` не принимает тип), `szTip` вмещает 63 символа. Иконка —
системные значки `LoadIconW(None, IDI_*)`: свои пиксели здесь не выходят
(`CreateIcon` с 32-битными битами рисует чёрное, `CreateIconIndirect` с
1-битной DIB-маской — NULL без кода ошибки, `LoadImageW` по ICO — access
violation), а handle системный, поэтому `DestroyIcon` к нему применять
нельзя.

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

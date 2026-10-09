# Diaspas (διάσπασις)

Менеджер обхода DPI для Windows: установка релизов
[zapret-discord-youtube](https://github.com/Flowseal/zapret-discord-youtube),
переключение пресетов стратегий и диагностика доступности
игровых и мессенджерных сервисов (Discord, YouTube, EA,
Ubisoft) одной кнопкой.

> **Diaspas не аффилирован с Flowseal, bol-van или WinDivert.**
> Это независимый инструмент. Сами компоненты обхода
> распространяются по своим лицензиям — см.
> [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Что делает

- **Скачивает релиз** zapret-discord-youtube с GitHub
  (последний релиз, с сохранением `LICENSE.txt`) —
  бинарники не входят в дистрибутив Diaspas.
- **Переключает пресеты**: одна кнопка выставляет стратегию
  `general (ALTn).bat`, режим Game Filter, режим IPSet и
  дописи в пользовательские списки доменов, затем
  переустанавливает службу.
- **Диагностирует**: TLS-пробы целевых хостов с учётом
  того, работает ли обход прямо сейчас.
- **A/B-тест**: кратковременно гасит службу и повторяет
  пробы — отделяет «ломает zapret» от «блокирует DPI».
- **Автоподбор стратегии**: перебирает все `general*.bat`
  в одной повышенной сессии и рекомендует рабочую.

## Пресеты по умолчанию

| Пресет | Стратегия | Game Filter | IPSet | Списки |
|---|---|---|---|---|
| Discord / YouTube | ALT11 | — | — | — |
| Apex / EA | ALT11 | all | any | `ea.com` в исключения |
| Ubisoft | ALT11 | all | any | `ubisoft.com`, `ubi.com` в исключения |
| Максимум для игр | ALT11 | all | any | — |
| Выкл | — | — | — | — |

## Управление правами

Операции со службой выполняются через служебную задачу
Windows с уровнем Highest: единственный запрос UAC при
регистрации задачи, дальше Diaspas запускает её через
`schtasks /Run` без диалогов. Всего две задачи:
`DiaspasService` (служба) и `DiaspasSelector` (перебор
стратегий).

Состояние службы читается **без прав** (`sc query` +
реестр) — обычная проверка карточки не дёргает UAC.

## Журнал

Все события пишутся в `%LOCALAPPDATA%\Diaspas\diaspas.log`
(UTF-8 с BOM, ротация при 512 КБ). Окно показывает то же
плюс хвост прошлого запуска — закрытое окно больше не
теряет диагностику.

## Запуск из исходников

```
python main.py
```

Зависимости — только стандартная библиотека
(Tkinter, urllib, ctypes). Тесты:

```
pip install pytest
python -m pytest tests/
```

Ручные прогоны (нужна сеть, часть — UAC):

```
python tests/run_probes.py        # пробы всех групп
python tests/run_ab.py ubisoft    # A/B-тест по группе
python tests/run_select.py        # автоподбор (подмножество)
python tests/run_ubisoft_fix.py   # применить пресет end-to-end
```

## Сборка

```
pip install -r requirements.txt
pyinstaller diaspas.spec
```

Готовый `dist/Diaspas.exe` — onefile, ~12,5 МБ.

## Статус

Реализовано: установка релизов с GitHub, пресеты,
управление службой (один UAC), пробы Discord/YouTube/EA/
Ubisoft, A/B-диагностика, автоподбор стратегий, GUI.

Проверено на живой системе: A/B достоверно разводит
«ломает zapret» / «блокирует DPI»; пресет «Ubisoft»
чинит `account.ubisoft.com` (было 2/3 → стало 3/3);
перебор возвращается к ALT11/ALT12 как к полным.

## Лицензия

[PolyForm Noncommercial License 1.0.0](LICENSE).

Required Notice: Copyright © 2026 vilminessa
(https://github.com/vilminessa)

Некоммерческое использование бесплатно; коммерческое
использование требует отдельного разрешения правообладателя.

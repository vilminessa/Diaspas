"""Переводы Diaspas. Один язык на первый рел - русский.

Ключи точечные (sheet.*.что), как в Synfronia: текст лежит в одном словаре,
вызов - ``tr("key")``. Отсутствующий ключ возвращает сам ключ, чтобы падение
перевода было видно в журнале, а не в молчании.
"""

from __future__ import annotations

RU: dict[str, str] = {
    "app.title": "Diaspas (διάσπασις) — менеджер обхода",
    "app.disclaimer": "Не аффилирован с Flowseal/zapret. "
                      "Компоненты обхода — их лицензии (см. THIRD_PARTY_NOTICES).",

    "status.title": "Состояние",
    "status.service": "Служба",
    "status.strategy": "Стратегия",
    "status.preset": "Пресет",
    "status.zapret": "Папка zapret",
    "status.not_installed": "не установлена",
    "status.running": "работает",
    "status.stopped": "остановлена",
    "status.unknown": "—",
    "status.no_dir": "папка не найдена",
    "status.check": "Проверить",
    "status.checking": "проверяю...",

    "presets.title": "Пресеты",
    "presets.apply": "Применить",
    "presets.applying": "применяю...",
    "presets.none": "не определён",

    "probe.title": "Пробы",
    "probe.run": "Проверить всё",
    "probe.running": "проверяю...",
    "probe.ab": "A/B-тест (служба вкл → выкл → вкл)",
    "probe.ab_running": "A/B идёт...",
    "probe.verdict": "Вывод",
    "probe.group_off": "группа выключена",

    "select.title": "Перебор стратегий",
    "select.run": "Перебрать стратегии",
    "select.running": "перебор идёт...",
    "select.cancel": "Прервать",
    "select.best": "Рекомендуемая",
    "select.none": "не подобрана",
    "select.progress": "{done}/{total}",
    "select.use": "Взять эту",

    "strategies.apply": "Применить стратегию",
    "strategies.auto": "Авто",
    "strategies.manual": "выбор вручную: стратегия сохраняется при смене пресета",
    "strategies.auto_on": "авто: подбор при запуске и после проб с сбоями",
    "strategies.auto_cooldown": "следующий подбор через {minutes} мин",
    "strategies.auto_last": "авто применяло {label}",
    "strategies.auto_confirm": (
        "Diaspas будет проверять доступность сервисов при запуске и после проб\n"
        "с сбоями, перебирать стратегии и сам применит лучшую. Служба будет\n"
        "пересоздаваться. Подбор — не чаще одного раза в 30 минут.\n\nВключить?"),
    "strategies.mode_set": "режим смены стратегии: {mode}",
    "strategies.apply_confirm": (
        "Служба zapret будет пересоздана со стратегией «{label}».\n"
        "Game Filter, IPSet и списки доменов останутся как сейчас.\n\nПродолжить?"),
    "strategies.already_active": "Эта стратегия уже активна.",
    "strategies.pick": "Выберите стратегию из списка.",
    "strategies.applied": "стратегия {label} применена",
    "strategies.auto_applied": "авто: применена {label}",
    "strategies.best_confirm": (
        "Лучшая стратегия: {label}.\nПрименить её? Текущие режимы сохранятся."),
    "strategies.auto_full": "всё отвечает — менять нечего",
    "strategies.auto_none": "полной стратегии не найдено — оставляю текущую",
    "strategies.auto_already": "лучшая стратегия уже активна",
    "strategies.auto_preset_off": "пресет «Выкл»: обход выключен намеренно",
    "strategies.auto_off": "режим выключен",

    "release.title": "Установка zapret",
    "release.install": "Скачать последний релиз",
    "release.installing": "скачиваю...",
    "release.browse": "Указать папку...",
    "release.version": "Релиз: {tag}",

    "log.title": "Журнал",
    "log.copy": "Копировать",

    "tray.title": "Трей",
    "tray.enabled": "Скрывать в трей",
    "tray.hide": "Скрыть окно",
    "tray.note": "иконка в трее: меню меняет пресет и стратегию, обход не выключается",
    "tray.exit_confirm": (
        "Обход работает: winws запущен как служба.\n"
        "Закрыть Diaspas? Служба zapret останется включённой."),
    "tray.unavailable": "трей не поднялся: {reason}",
    "tray.hidden": "окно скрыто (трей)",
    "tray.shown": "окно открыто",
    "tray.second_instance": "Diaspas уже запущен - показываю существующее окно",
    "tray.on": "трей включён",
    "tray.off": "трей выключен",

    "err.no_uac": "Не удалось получить права администратора.",
    "err.no_dir": "Папка zapret не найдена. Укажите её вручную "
                  "или скачайте релиз.",
    "err.cancelled": "Отменено.",
    "err.timeout": "Операция не уложилась в отведённое время.",
}


def tr(key: str, **kwargs) -> str:
    """Перевод по ключу; kwargs форматируются, если в тексте есть поля."""
    text = RU.get(key, key)
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError):
            return text
    return text

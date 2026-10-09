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

    "select.title": "Автоподбор стратегий",
    "select.run": "Перебрать стратегии",
    "select.running": "перебор идёт...",
    "select.cancel": "Прервать",
    "select.best": "Рекомендуемая",
    "select.none": "не подобрана",
    "select.progress": "{done}/{total}",
    "select.use": "Взять эту",

    "release.title": "Установка zapret",
    "release.install": "Скачать последний релиз",
    "release.installing": "скачиваю...",
    "release.browse": "Указать папку...",
    "release.version": "Релиз: {tag}",

    "log.title": "Журнал",
    "log.copy": "Копировать",

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

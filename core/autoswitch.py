"""Автоматическая смена стратегии: подбор по пробам и применение победителя.

Авто работает, пока Diaspas запущен - при старте приложения и после проб,
которые показали сбой. Отдельного демона или задачи планировщика нет:
фоновый перебор стратегيات требовал бы прав и перезапуска службы без
пользователя, а это тот риск, который пользователь не просил.

Порядок цикла:

1. пробы - если все цели отвечают, менять нечего;
2. cooldown (:data:`COOLDOWN_SECONDS`) - иначе блокировка выше обхода
   (например IP-фильтр провайдера) заставляла бы перебирать стратегии
   при каждой проверке;
3. перебор стратегий с остановкой на первой полностью рабочей
   (:func:`core.autoselect.run` с ``stop_on_full``);
4. применение победителя через :func:`core.strategies.apply` - режимы
   (Game Filter, IPSet, списки доменов) не меняются;
5. контрольные пробы.

Кандидаты идут в порядке ALT11/ALT12 (известные рабочие из README), затем
остальные ``general*.bat`` в естественном порядке: авто выбирает первую
полностью рабочую стратегию, а не полную таблицу сравнения - полный
перебор остаётся на вкладке «Диагностика».
"""

from __future__ import annotations

import time
from pathlib import Path

from . import autoselect, paths, presets, probes, service, strategies

# Между автоподборами: один прогон - ~10 с на стратегию, повтор каждые
# несколько минут выглядел бы как бесконечная перезагрузка службы.
COOLDOWN_SECONDS = 30 * 60

# Стратегии, которые A/B-прогоны видели рабочими чаще всего (README).
PREFERRED: tuple[str, ...] = ("general (ALT11).bat", "general (ALT12).bat")


# -- память о прошлых автоподборах ----------------------------------------

def last_run() -> int:
    """unix-seconds последнего автоподбора (0 - не было)."""
    try:
        return int(paths.read_settings().get("last_auto_at") or 0)
    except (TypeError, ValueError):
        return 0


def last_strategy() -> str:
    """Стратегия, которую авто применило последним ("" - не применяло)."""
    return str(paths.read_settings().get("last_auto_strategy") or "")


def cooldown_left(now: float | None = None) -> int:
    """Секунды до следующего автоподбора; 0 - можно прямо сейчас."""
    last = last_run()
    if not last:
        return 0
    current = time.time() if now is None else now
    return max(0, int(COOLDOWN_SECONDS - (current - last)))


def _mark_run(now: float | None = None) -> None:
    settings = paths.read_settings()
    settings["last_auto_at"] = int(time.time() if now is None else now)
    paths.write_settings(settings)


def order(root: Path | None, files: list[str] | None = None) -> list[str]:
    """Порядок перебора для авто: известные рабочие кандидаты первыми."""
    names = files if files is not None else paths.strategy_files(Path(root))
    head = [name for name in PREFERRED if name in names]
    rest = [name for name in names if name not in head]
    return head + rest


# -- цикл -----------------------------------------------------------------

def run(root: Path | None, report: dict | None = None, force: bool = False,
        log=None, on_progress=None, wait: float = 900.0) -> dict:
    """Один автоцикл: пробы → перебор → применение победителя → контрольные пробы.

    ``report`` - уже снятые пробы (GUI зовёт «Проверить всё» и не хочет
    мерять дважды). ``force`` - пропустить cooldown (кнопка «Подобрать
    сейчас»); режим «авто» при этом всё равно обязателен.
    """
    if root is None:
        return {"ok": False, "error": "папка zapret не найдена", "ran": False}
    if strategies.mode() != strategies.AUTO:
        return {"ok": True, "ran": False, "reason": "off"}
    if presets.active_preset_key() == "off":
        if log:
            log("info", "авто: пресет «Выкл» - обход выключен намеренно, "
                        "подбор не запускается")
        return {"ok": True, "ran": False, "reason": "preset_off"}

    report = report if report is not None else probes.probe_all(timeout=6.0)
    if report.get("state") == "full":
        if log:
            log("info", f"авто: всё отвечает ({report.get('count')}/"
                        f"{report.get('total')}) - менять нечего")
        return {"ok": True, "ran": False, "reason": "full", "probes": report}

    left = cooldown_left()
    if not force and left > 0:
        if log:
            log("info", f"авто: подбор выполнялся {left // 60} мин назад - "
                        "пауза, чтобы не перезапускать службу каждые руки")
        return {"ok": True, "ran": False, "reason": "cooldown",
                "cooldown_left": left, "probes": report}

    # Помечаем ДО перебора: перебор может не получить прав или не найти
    # рабочую стратегию - повторный запрос UAC на каждой проверке был бы
    # хуже, чем пауза.
    _mark_run()

    names = order(root)
    if not names:
        return {"ok": False, "error": "в папке нет general*.bat", "ran": True}

    st = service.state(log=log)
    current = strategies.current_file(root, st)

    if log:
        log("info", f"авто: перебор {len(names)} стратегий "
                    f"(сейчас {paths.strategy_label(current) or '—'})")
    res = autoselect.run(root, strategies=names, log=log,
                         on_progress=on_progress, wait=wait, stop_on_full=True)
    if res is None:
        if log:
            log("error", "авто: не удалось получить права администратора")
        return {"ok": False, "error": "нет прав", "ran": True,
                "reason": "no_uac", "probes": report}
    if not res.get("ok"):
        err = str(res.get("error") or "перебор не выполнен")
        if log:
            log("error", f"авто: {err}")
        return {"ok": False, "error": err, "ran": True,
                "reason": "select_failed", "probes": report}

    best = res.get("best")
    results = res.get("results") or []
    if not best:
        if log:
            log("warning", "авто: ни одна стратегия не закрыла все цели - "
                           "оставляю текущую (блокировка выше уровня обхода)")
        return {"ok": True, "ran": True, "applied": False, "best": None,
                "reason": "none", "probes": report, "results": results}

    label = paths.strategy_label(str(best))
    if best == current and st.running:
        if log:
            log("info", f"авто: {label!r} уже активна и отвечает при переборе "
                        "- переустановка службы не нужна")
        return {"ok": True, "ran": True, "applied": False, "best": best,
                "label": label, "reason": "already", "results": results}

    applied = strategies.apply(root, str(best), mode_value=strategies.AUTO,
                               log=log)
    if not applied.get("ok"):
        return {"ok": False, "error": str(applied.get("error")), "ran": True,
                "best": best, "label": label, "reason": "apply_failed",
                "results": results}

    settings = paths.read_settings()
    settings["last_auto_strategy"] = str(best)
    paths.write_settings(settings)

    check = probes.probe_all(timeout=6.0)
    if log:
        if check.get("state") == "full":
            log("info", f"авто: применена {label!r}, контрольные пробы "
                        f"{check.get('count')}/{check.get('total')}")
        else:
            log("warning", f"авто: применена {label!r}, но пробы всё равно "
                           f"{check.get('count')}/{check.get('total')} "
                           f"({check.get('state')})")
    return {"ok": True, "ran": True, "applied": True, "best": best,
            "label": label, "reason": "applied", "probes": check,
            "results": results}

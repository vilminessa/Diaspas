"""A/B-тест: ломает обход или блокирует DPI.

Классический симптом, который мы чинили у EA и Ubisoft: с включённым zapret
TLS-хендшейк сбрасывается, без него - проходит. Руками это делалось
остановкой службы; здесь - одним действием: пробы с обходом -> стоп ->
пробы -> старт -> пробы -> сравнение.

Вывод однозначен:

* «работает БЕЗ обхода»  -> виноват zapret: домен в исключения;
* «не работает в обоих»  -> виноват DPI: нужна другая стратегия
  (или домен в list-general, если правила его не покрывают);
* «работает в обоих»     -> воспроизводится только в некоторых условиях.
"""

from __future__ import annotations

import time

from . import probes, service


def run(group: str, log=None, wait: float = 8.0,
        on_stage=None) -> dict:
    """Полный A/B по одной группе целей.

    on_stage(name, report) вызывается после каждого замера
    (name: "with"/"without"/"after"), чтобы GUI рисовал ход.
    """
    def note(stage: str, report: dict) -> None:
        if log:
            log("info", f"[{stage}] {probes.probe_line(report)}")
        if on_stage:
            try:
                on_stage(stage, report)
            except Exception:  # noqa: BLE001 - GUI не должен ронять тест
                pass

    if not probes.PROBE_GROUPS.get(group):
        return {"ok": False, "error": f"неизвестная группа {group!r}"}

    was_running = service.state(log=log).running

    if log:
        log("info", f"A/B для «{group}»: замер с обходом")
    with_report = probes.probe_group(group)
    note("with", with_report)

    if not was_running:
        # Обход и так выключен - второй замер бессмысленен: это и есть «без».
        if log:
            log("info", "служба не работала - A/B сводится к одному замеру")
        return _verdict(group, with_report, None, log)

    if log:
        log("info", "остановка службы zapret")
    stop_res = service.stop(log=log)
    if not stop_res.get("ok"):
        # stop мог частично сработать (SCM остановил, помощник не ответил):
        # вернём службу на всякий случай - она идемпотентна.
        err = str(stop_res.get("error") or "не удалось остановить службу")
        if log:
            log("error", err)
        service.start(log=log)
        return {"ok": False, "error": err, "with": with_report}

    stopped_ok = True
    try:
        # winws уходит не мгновенно: ждём, пока драйвер отпустит сокеты
        deadline = time.time() + wait
        while time.time() < deadline and service.state().running:
            time.sleep(0.5)
        time.sleep(1.0)

        if log:
            log("info", "замер БЕЗ обхода")
        without_report = probes.probe_group(group)
        note("without", without_report)
    except BaseException:
        # Любая цена (включая Ctrl+C) не должна оставлять ПК без обхода
        if log:
            log("warning", "возврат службы после сбоя A/B")
        service.start(log=log)
        raise
    else:
        if log:
            log("info", "возврат службы zapret")
        start_res = service.start(log=log)
        if not start_res.get("ok"):
            if log:
                log("warning", "не удалось вернуть службу автоматически - "
                               "запустите вручную")
        else:
            stopped_ok = False
            time.sleep(1.5)
            after = probes.probe_group(group)
            note("after", after)

    if stopped_ok:
        return {"ok": False,
                "error": "службу не удалось вернуть после теста - "
                         "проверьте её вручную",
                "with": with_report}
    return _verdict(group, with_report, without_report, log)


def _verdict(group: str, with_rep: dict, without_rep: dict | None,
             log=None) -> dict:
    result: dict = {"ok": True, "group": group, "with": with_rep,
                    "without": without_rep}
    if without_rep is None:
        # Служба изначально была выключена: единственный замер - это «без».
        if with_rep.get("ok"):
            verdict = "works_without"
            hint = ("Обход не требуется: сервис отвечает и без zapret. "
                    "Если с включённым обходом ломается - домен нужно "
                    "добавить в исключения.")
        else:
            verdict = "blocked_anyway"
            hint = ("Сервис не отвечает и без обхода - блокирует DPI. "
                    "Нужна другая стратегия (автоподбор) либо домен в "
                    "list-general-user.txt.")
    elif with_rep.get("ok") and not without_rep.get("ok"):
        verdict = "zapret_needed"
        hint = ("Работает только с обходом: классическая блокировка DPI, "
                "текущая стратегия справляется.")
    elif not with_rep.get("ok") and without_rep.get("ok"):
        verdict = "zapret_breaks"
        hint = ("Ломает сам обход (паттерн с EA/Ubisoft): домен нужно "
                "добавить в list-exclude-user.txt через пресет или "
                "вручную - соединение чистое и без zapret.")
    elif with_rep.get("ok") and without_rep.get("ok"):
        verdict = "works_always"
        hint = ("Отвечает в обоих случаях - сейчас проблема не проявляется. "
                "Возможно, блокировка включается пиками или зависит от узла.")
    else:
        verdict = "blocked_always"
        hint = ("Не отвечает в обоих случаях - блокировка выше уровня "
                "обхода (IP-фильтр провайдера или сбой самого сервиса). "
                "Часто помогает смена стратегии или проверка статуса "
                "сервиса у его официального источника.")
    result["verdict"] = verdict
    result["hint"] = hint
    if log:
        log("info", f"вывод: {verdict} - {hint}")
    return result

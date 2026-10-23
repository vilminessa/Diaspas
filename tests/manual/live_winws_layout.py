"""Живой E2E winws-layout: win-bundle master -> preset*.cmd -> откат.

Путь: скачивание ветки master bol-van/zapret-win-bundle (релизов нет) ->
установка рядом с текущей -> распознавание layout=winws -> пресет с
конкретной стратегией preset*.cmd -> служба стартует -> возврат на
текущую flowseal-версию и очистка переопределения.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _guard import require_live  # noqa: E402

require_live(__doc__ or 'эти прогоны')


from core import installs, presets as pm, probes, release  # noqa: E402
from ui.webapp import App  # noqa: E402

RESULTS: list[bool] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append(bool(ok))
    print(f"  {'OK  ' if ok else 'FAIL'} {name}"
          + (f" — {detail}" if detail else ""), flush=True)


def stage(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def main() -> int:
    app = App()
    saved_active = installs.active_path()

    stage("1. скачивание win-bundle master (zipball ветки)")
    res = app.download_release(release.REPO_WINWS, "master")
    check("установка ok", bool(res.get("ok")), str(res.get("error") or ""))
    if not res.get("ok"):
        return 1
    winws_root = installs.active_path()
    check("активная установка - win-bundle", "win-bundle" in str(winws_root),
          str(winws_root))
    time.sleep(1)

    stage("2. распознавание layout")
    info = installs.detect_layout(winws_root)
    check("layout=winws", info["layout"] == installs.LAYOUT_WINWS)
    check("стратегии preset*.cmd найдены", bool(info["strategies"]),
          ", ".join(info["strategies"][:4]))
    check("integrity цела", not installs.integrity(winws_root),
          ", ".join(installs.integrity(winws_root)[:3]))

    stage("3. пресет с конкретной стратегией winws-layout")
    strategy = info["strategies"][0]
    st = app.strategies()
    check("API strategies отдаёт winws-layout", st.get("layout") == "winws",
          f"{len(st.get('items') or [])} стратегий")
    res = app.apply_preset("discord_youtube", strategy)
    check("apply ok", bool(res.get("ok")), str(res.get("error") or ""))
    time.sleep(2)
    st_state = app.state()
    check("служба работает", st_state["running"], f"pid={st_state['pid']}")
    check("пресет распознан по winws-стратегии",
          st_state["preset_key"] == "discord_youtube",
          st_state["preset_title"] or "")

    stage("4. пробы через winws-layout")
    rep = probes.probe_all(timeout=8.0)
    check("обход отвечает (не все цели обязательны для master)",
          rep["count"] > 0, f"{rep['count']}/{rep['total']}")

    stage("5. возврат на текущую версию")
    if saved_active is not None:
        res = app.switch_version(str(saved_active))
        check("переключение обратно ok", bool(res.get("ok")),
              str(res.get("error") or ""))
        pm.set_strategy("discord_youtube", None)   # снять переопределение
        check("переопределение снято",
              not pm.strategy_override("discord_youtube"))
        time.sleep(1)
        st_state = app.state()
        check("служба работает на прежней версии", st_state["running"],
              f"{st_state['preset_title']} pid={st_state['pid']}")
        rep = probes.probe_all(timeout=8.0)
        check("пробы прежней версии целы", rep["state"] == "full",
              f"{rep['count']}/{rep['total']}")

    ok_total = sum(RESULTS)
    print(f"\nИТОГ: {ok_total}/{len(RESULTS)}")
    return 0 if ok_total == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())

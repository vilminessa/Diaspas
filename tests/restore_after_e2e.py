"""Восстановление после E2E: пересоздать службу на активной версии."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import installs, presets as pm, service  # noqa: E402


def main() -> int:
    active = installs.active_path()
    print("активная версия:", active)
    if active is None:
        print("нет активной установки - регистрирую из автопоиска")
        from core import paths
        found = paths.zapret_install_dir()
        print("автопоиск:", found)
        return 1

    # служба может указывать на удалённую папку - удаляем целиком
    st = service.state()
    if st.installed:
        print("удаляю службу с битым/старым путём…")
        service.remove(log=lambda l, m: print(f"  [{l}] {m}"))
        time.sleep(1)

    key = pm.active_preset_key() or "discord_youtube"
    preset = next((p for p in pm.load_presets() if p.key == key), None) \
        or pm.load_presets()[0]
    print(f"применяю пресет «{preset.title}» к {active.name}")
    res = pm.apply(preset, active, log=lambda l, m: print(f"  [{l}] {m}"))
    time.sleep(2)
    st = service.state()
    print(f"итог: ok={res.get('ok')} running={st.running} pid={st.pid} "
          f"exit_code={st.exit_code}")
    return 0 if st.running else 1


if __name__ == "__main__":
    raise SystemExit(main())

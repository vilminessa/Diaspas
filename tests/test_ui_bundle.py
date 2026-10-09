"""Бандл UI: синхронность с ui_src и полнота разметки.

Падает, если ui_src изменён, а bundle.py не пересобран (в CI порядок
check_ui -> build_ui -> pytest ловит это до сборки exe).
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import log as dlog  # noqa: E402
from tools.build_ui import digests  # noqa: E402
from ui.bundle import HASHES, INDEX_HTML  # noqa: E402


def test_bundle_in_sync_with_src():
    """HASHES в bundle обязаны совпадать с ui_src (иначе - пересборка)."""
    assert HASHES == digests(), \
        "ui/bundle.py устарел: python tools/build_ui.py"


def test_index_has_required_structure():
    """Каркас экрана: три вкладки, точки монтирования ключевых блоков."""
    for marker in (
        'data-tab="overview"',
        'data-tab="diagnostics"',
        'data-tab="journal"',
        'id="preset-grid"',
        'id="probe-chips"',
        'id="probe-table"',
        'id="select-table"',
        'id="log-view"',
        'id="ab-result"',
        'id="modal"',
        'id="top-status"',
    ):
        assert marker in INDEX_HTML, f"нет {marker}"


def test_bundle_is_single_document():
    """CSS и JS должны быть встроены инлайн - иначе load_html их не отдаст."""
    assert "<style>" in INDEX_HTML and "--bg:" in INDEX_HTML
    assert "<script>" in INDEX_HTML and "window.diaspas" in INDEX_HTML
    # внешних ссылок на ресурсы быть не должно
    assert 'href="app.css"' not in INDEX_HTML
    assert 'src="app.js"' not in INDEX_HTML
    # единственный корректно закрытый script-блок
    assert INDEX_HTML.count("<script>") == INDEX_HTML.count("</script>")


def test_no_placeholder_tokens():
    """Пустые/шаблонные места вылезают как {{ или @@ в собранном HTML."""
    assert not re.search(r"\{\{[a-z_]+\}\}", INDEX_HTML)
    assert "@@" not in INDEX_HTML


def test_log_format_line_matches_file():
    """Строка в окне и строка на диске - один и тот же формат.

    Формат: "YYYY-MM-DD HH:MM:SS [level] text" - ровно его парсит
    app.js в appendLog, поэтому регулярка здесь должна совпадать с JS.
    """
    line = dlog.format_line("info", "тест")
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(\w+)\] (.*)",
                     line)
    assert m, f"неожиданный формат: {line!r}"
    assert m.group(2) == "info" and m.group(3) == "тест"

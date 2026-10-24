"""Линт конвейера выпуска: гарды полос, пининг uses, проставление версии.

Регрессии важнее самих workflow-файлов: конвейер, который «обещает» версию
из тега и не зовёт stamp_version, даёт exe и интерфейс без версии - и
проверка это ловит до сборки.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import check_workflows as cw  # noqa: E402

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def copy(tmp_path: Path, name: str, transform=None) -> Path:
    """Копия workflow-файла с правкой для проверки регрессии."""
    src = WORKFLOWS / name
    text = src.read_text(encoding="utf-8")
    if transform:
        text = transform(text)
    dest = tmp_path / name
    dest.write_text(text, encoding="utf-8")
    return dest


def test_shipped_workflows_are_clean():
    for name in ("release.yml", "beta.yml", "ci.yml"):
        errors = cw.check(WORKFLOWS / name)
        assert errors == [], f"{name}: {errors}"


def test_release_and_beta_have_stamp_before_build(tmp_path):
    """Порядок шагов: версия вписана до PyInstaller, иначе бандл без версии."""
    path = copy(tmp_path, "release.yml",
                lambda text: text.replace(
                    'run: python tools/stamp_version.py "$GITHUB_REF_NAME"',
                    'run: echo "версия не проставлена"'))
    errors = cw.check(path)
    assert any("stamp_version" in error for error in errors), errors


def test_stamp_after_build_is_rejected(tmp_path):
    src = (WORKFLOWS / "beta.yml").read_text(encoding="utf-8")
    stamp_block = src[src.index("      - name: Версия из тега"):
                      src.index("      - name: Сборка exe")]
    build_block = src[src.index("      - name: Сборка exe"):
                      src.index("      - name: Проверка артефакта")]
    text = src.replace(stamp_block, "").replace(build_block, build_block + stamp_block)
    path = copy(tmp_path, "beta.yml", lambda _t: text)
    errors = cw.check(path)
    assert any("после PyInstaller" in error for error in errors), errors


def test_missing_publish_guard_is_rejected(tmp_path):
    path = copy(tmp_path, "release.yml",
                lambda text: text.replace(
                    "merge-base --is-ancestor", "merge-base --nothing"))
    errors = cw.check(path)
    assert any("родства" in error for error in errors), errors


def test_unpinned_action_is_rejected(tmp_path):
    path = copy(tmp_path, "ci.yml",
                lambda text: text.replace(
                    "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1",
                    "actions/checkout@v7"))
    errors = cw.check(path)
    assert any("SHA" in error for error in errors), errors


def test_invisible_character_is_rejected(tmp_path):
    """BOM внутри строки YAML GitHub принимает, а шаг исполняет не то."""
    path = copy(tmp_path, "ci.yml",
                lambda text: text.replace("name: Зависимости",
                                          "name:" + "\ufeff" + "Зависимости"))
    errors = cw.check(path)
    assert any("невидимый символ U+FEFF" in error for error in errors), errors


def test_beta_without_prerelease_is_rejected(tmp_path):
    path = copy(tmp_path, "beta.yml", lambda text: text.replace("--prerelease", "--draft"))
    errors = cw.check(path)
    assert any("--prerelease" in error for error in errors), errors


def test_release_marked_prerelease_is_rejected(tmp_path):
    path = copy(tmp_path, "release.yml",
                lambda text: text.replace("gh release create",
                                          "gh release create --prerelease"))
    errors = cw.check(path)
    assert any("prerelease" in error for error in errors), errors


def test_build_must_depend_on_verify(tmp_path):
    path = copy(tmp_path, "release.yml",
                lambda text: text.replace("    needs: verify", "    needs: nothing"))
    errors = cw.check(path)
    assert any("не зависит от verify" in error for error in errors), errors


def test_ci_is_not_required_to_publish(tmp_path):
    """ci.yml ничего не публикует: требовать от него джобы релиза было бы вранье."""
    assert cw.check(WORKFLOWS / "ci.yml") == []


def test_main_reports_ok_and_fail(tmp_path, capsys):
    good = copy(tmp_path, "release.yml")
    bad = copy(tmp_path, "beta.yml",
               lambda text: text.replace("private-repository", ""))
    assert cw.main(["prog", str(good), str(bad)]) == 1
    out = capsys.readouterr().out
    assert "OK  release.yml" in out
    assert "FAIL beta.yml" in out

#!/usr/bin/env python3
"""Линт .github/workflows - без зависимостей (только stdlib).

Что проверяем и почему:
  * невидимые символы (BOM/ZWSP/NBSP) - при правке их не видно, GitHub
    такой YAML принимает, а шаг молча исполняет не то;
  * каждая внешняя ссылка `uses:` закреплена SHA-коммитом: тег можно
    переписать - это supply chain. Исключение - reusable workflow
    генератора SLSA, который иначе как на собственный тег и не пинится;
  * `permissions` на верхнем уровне: read по умолчанию, write только там,
    где нужно;
  * триггеры есть: workflow, который нигде не запускается, врёт о
    наличии конвейера;
  * конвейер версии: джоб build обязан звать tools/stamp_version.py
    перед PyInstaller и после checkout - иначе надпись «версия берётся
    из тега» остаётся обещанием, а exe и интерфейс выходят без версии;
  * гарды полос: формат тега и родство с main/dev, публикация только на
    теге, бета - prerelease, SLSA и проверка провенанса.

Запуск: python tools/check_workflows.py (в CI - шаг «Линт workflows»).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Windows-раннер печатает в cp1252; сообщения линта русские - без этой
# строки линт падает UnicodeEncodeError на собственном print.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = ROOT / ".github" / "workflows"

# Коды точки, а не сами символы: файл линта не должен содержать то,
# что сам же ищет.
INVISIBLE = "".join(chr(code) for code in
                    (0xFEFF, 0x200B, 0x00A0, 0x2007, 0x202F, 0x3000))
SHA = re.compile(r"^[0-9a-f]{40}$")
# Sequence-стиль шага: "- uses: ..." без даша и пробела regex не видит,
# и тогда пининг action'ов вообще не проверяется.
USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)(?:\s+#.*)?$", re.MULTILINE)
# Тег генератора SLSA: его workflow пинится на собственный тег, а не на SHA.
ALLOWED_TAG_USES = re.compile(
    r"^slsa-framework/slsa-github-generator/\.github/workflows/"
    r"generator_generic_slsa3\.yml@v\d+\.\d+\.\d+$")

REQUIRED_JOBS = ("verify", "build", "python-publish",
                 "generator-generic-ossf-slsa3-publish", "verify-attestation")
# Полосы выпуска: они собирают exe и публикуют релизы. Проверки ниже
# (джобы, публикация, SLSA, гарды тега) относятся только к ним - ci.yml
# ничего не собирает и не публикует, и требовать от него эти шаги было бы
# вранье о конвейере, которого в нём нет.
RELEASE_FILES = {"beta.yml", "release.yml"}


def check(path: Path) -> list[str]:
    """Список ошибок файла; пустой список - файл пригоден."""
    errors: list[str] = []
    text = path.read_text(encoding="utf-8")

    for index, line in enumerate(text.splitlines(), 1):
        for char in line:
            if char in INVISIBLE:
                errors.append(f"{path.name}:{index}: невидимый символ "
                              f"U+{ord(char):04X}")

    for match in USES.finditer(text):
        ref = match.group(1)
        if ref.startswith("./"):
            continue        # локальный reusable workflow
        at = ref.rfind("@")
        if at < 0:
            errors.append(f"{path.name}: uses без версии: {ref}")
            continue
        version = ref[at + 1:]
        if not SHA.match(version) and not ALLOWED_TAG_USES.match(ref):
            errors.append(f"{path.name}: uses закреплён тегом, не SHA: {ref}")

    if not re.search(r"^permissions:", text, re.MULTILINE):
        errors.append(f"{path.name}: нет permissions на верхнем уровне")

    triggers = re.search(r"^on:\n((?:\s{2,}.*\n)+)", text, re.MULTILINE)
    if not triggers:
        errors.append(f"{path.name}: нет секции on (триггеров)")
    else:
        block = triggers.group(1)
        if "push" not in block and "workflow_dispatch" not in block:
            errors.append(f"{path.name}: on не содержит push/workflow_dispatch")

    if path.name in RELEASE_FILES:
        # В f-string фигурные скобки - интерполяция: \s{2} превращается в
        # \s2, поэтому квантиаторы пишутся как {{2}}.
        for job in REQUIRED_JOBS:
            if not re.search(rf"^\s{{2}}{job}:", text, re.MULTILINE):
                errors.append(f"{path.name}: нет джоба {job}")

        # Порядок джобов: build зависит от verify, publish от build,
        # провенанс от publish, проверка подписи от генератора.
        for job, need in (("build", "verify"),
                          ("python-publish", "build"),
                          ("generator-generic-ossf-slsa3-publish", "build"),
                          ("verify-attestation",
                           "generator-generic-ossf-slsa3-publish")):
            block = re.search(
                rf"^\s{{2}}{job}:\n(?:.*\n)+?(?=^\s{{2}}\S+:|^\s{{2}}#)",
                text, re.MULTILINE)
            if block and not re.search(
                    rf"needs:\s*(?:\[.*\b{need}\b.*\]|\b{need}\b)",
                    block.group(0)):
                errors.append(f"{path.name}: джоб {job} не зависит от {need}")

        # Конвейер версии: stamp до сборки, и именно на теге.
        stamp_at = text.find("tools/stamp_version.py")
        build_at = text.find("PyInstaller")
        if build_at < 0:
            errors.append(f"{path.name}: build не собирает exe")
        if stamp_at < 0:
            errors.append(f"{path.name}: build не зовёт tools/stamp_version.py - "
                          "версия из тега ни в exe, ни в интерфейс не попадёт")
        elif build_at >= 0 and stamp_at > build_at:
            errors.append(
                f"{path.name}: stamp_version вызывается после PyInstaller")

        # Гарды полос: формат тега и родство с веткой.
        if "^v[0-9]" not in text and "^b[0-9]" not in text:
            errors.append(f"{path.name}: нет проверки формата тега")
        if "merge-base --is-ancestor" not in text:
            errors.append(f"{path.name}: нет гарда родства тега с main/dev "
                          "(merge-base --is-ancestor)")
        if "fetch-depth: 0" not in text:
            errors.append(
                f"{path.name}: shallow-клон ломает гард родства тега "
                "(merge-base не видит историю)")

        if not re.search(r"if:\s*startsWith\(github\.ref,\s*'refs/tags/'\)",
                         text):
            errors.append(f"{path.name}: публикация не ограничена тегами")
        if "gh release create" not in text or "Diaspas.exe" not in text:
            errors.append(f"{path.name}: publish не гарантирует "
                          "Diaspas.exe в релизе")

        # SLSA и проверка провенанса.
        if "base64-subjects" not in text:
            errors.append(f"{path.name}: генератор не получает хеш артефакта")
        if "private-repository" not in text:
            errors.append(f"{path.name}: приватный репозиторий - генератор "
                          "SLSA без private-repository падает")
        if "gh attestation verify" not in text:
            errors.append(f"{path.name}: нет проверки провенанса")
        if "--predicate-type" not in text or "--signer-repo" not in text:
            errors.append(f"{path.name}: gh attestation verify без флагов "
                          "(ищет provenance v1 и даёт 404)")

        # Полоса: бета публикуется prerelease, релиз - нет.
        prerelease = "--prerelease" in text
        if path.name == "beta.yml" and not prerelease:
            errors.append("beta.yml: релиз публикуется без --prerelease")
        if path.name == "release.yml" and prerelease:
            errors.append("release.yml: стабильный релиз помечен prerelease")

        if "GITHUB_REF_NAME" not in text:
            errors.append(f"{path.name}: нет ссылки на имя тега "
                          "(гарды и публикация)")
        if not re.search(r"assert s > \d[\d_]*", text):
            errors.append(f"{path.name}: нет нижней границы размера exe "
                          "(защита от пустой сборки)")

    return errors


def main(argv: list[str]) -> int:
    paths = ([Path(item) for item in argv[1:]]
             if len(argv) > 1 else sorted(WORKFLOWS_DIR.glob("*.yml")))
    if not paths:
        print(f"нет workflow-файлов в {WORKFLOWS_DIR}")
        return 1

    failed = 0
    for path in paths:
        if not path.is_file():
            print(f"нет файла: {path}")
            failed += 1
            continue
        errors = check(path)
        if errors:
            failed += 1
            print(f"FAIL {path.name}")
            for error in errors:
                print(f"  - {error}")
        else:
            print(f"OK  {path.name}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

"""Правка publish-шага в workflows: устойчивость к гонке с SLSA.

Проблема: при пересоздании тега SLSA-генератор (upload-assets) мог создать
релиз ПЕРВЫМ - без exe и не как prerelease. Publish уходил в ветку
upload, и его результат не проверялся: релиз жил без Diaspas.exe, а
verify-attestation падал с "no such file".

Фикс: после create/upload ВСЕГДА выставить prerelease (для beta) и
ПРОВЕРИТЬ, что Diaspas.exe в ассетах - иначе шаг падает с понятным
сообщением, а не молчит.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]

# текущий шаблон publish-шага (одинаков в обоих файлах, кроме --prerelease)
OLD = '''        run: |
          notes="{notes}"
          if gh release view "$GITHUB_REF_NAME" --repo "$GITHUB_REPOSITORY" >/dev/null 2>&1; then
            gh release upload "$GITHUB_REF_NAME" Diaspas.exe --repo "$GITHUB_REPOSITORY" --clobber
          else
            gh release create "$GITHUB_REF_NAME" Diaspas.exe --repo "$GITHUB_REPOSITORY" \\
              --title "$GITHUB_REF_NAME" \\
              --notes "$notes"{prerelease}
          fi'''

NEW = '''        run: |
          notes="{notes}"
          # релиз мог создать SLSA-генератор первым (upload-assets) -
          # тогда exe заливаем в существующий, а флаг prerelease выставляем
          # явно: publish обязан гарантировать состав ассетов сам
          if gh release view "$GITHUB_REF_NAME" --repo "$GITHUB_REPOSITORY" >/dev/null 2>&1; then
            gh release upload "$GITHUB_REF_NAME" Diaspas.exe --repo "$GITHUB_REPOSITORY" --clobber
            gh release edit "$GITHUB_REF_NAME" --repo "$GITHUB_REPOSITORY"{prerelease}
          else
            gh release create "$GITHUB_REF_NAME" Diaspas.exe --repo "$GITHUB_REPOSITORY" \\
              --title "$GITHUB_REF_NAME" \\
              --notes "$notes"{prerelease}
          fi
          # отчёт + проверка: без Diaspas.exe verify-attestation упадёт
          # позже с невнятной ошибкой "no such file"
          gh release view "$GITHUB_REF_NAME" --repo "$GITHUB_REPOSITORY" \\
            --json assets --jq '.assets[].name'
          gh release view "$GITHUB_REF_NAME" --repo "$GITHUB_REPOSITORY" \\
            --json assets --jq '.assets[].name' | grep -qx "Diaspas.exe" || {{
            echo "::error::Diaspas.exe отсутствует в релизе $GITHUB_REF_NAME"
            exit 1
          }}'''

BETA_NOTES = ("Бета-сборка из dev ($GITHUB_REF_NAME). Автоматическая сборка "
              "(CI). Провенанс SLSA прикладывается отдельным джобом.")
RELEASE_NOTES = ("Автоматическая сборка (CI). Провенанс SLSA прикладывается "
                 "отдельным джобом.")


def patch(path: pathlib.Path, notes: str, prerelease: str) -> bool:
    text = path.read_text(encoding="utf-8")
    old = OLD.format(notes=notes, prerelease=prerelease)
    if old not in text:
        return False
    new = NEW.format(notes=notes, prerelease=prerelease)
    path.write_text(text.replace(old, new), encoding="utf-8", newline="\n")
    return True


def main() -> int:
    beta = ROOT / ".github/workflows/beta.yml"
    release = ROOT / ".github/workflows/release.yml"

    ok_beta = patch(beta, BETA_NOTES, " \\\n              --prerelease")
    ok_release = patch(release, RELEASE_NOTES, "")
    print(f"beta.yml: {'обновлён' if ok_beta else 'шаблон не найден'}")
    print(f"release.yml: {'обновлён' if ok_release else 'шаблон не найден'}")

    # release: edit без аргументов не имеет смысла - для него убираем строку
    if ok_release:
        text = release.read_text(encoding="utf-8")
        # в release не нужен edit --prerelease (там релиз обычный)
        text = text.replace(
            '            gh release edit "$GITHUB_REF_NAME" --repo "$GITHUB_REPOSITORY"\n',
            '')
        release.write_text(text, encoding="utf-8", newline="\n")
    return 0 if (ok_beta and ok_release) else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env bash
# Собрать движок цзянпу — OMR из jpeditor (MIT) — самодостаточным пакетом.
#
#   scripts/build_jpeditor_omr.sh                       # в vendor/jpeditor-omr
#   scripts/build_jpeditor_omr.sh /opt/jpeditor-omr     # так делает Dockerfile
#
# Нужны git, Node >= 20 и npm; сеть — только на время сборки. Результат —
# omr-cli.mjs, модели и node_modules под ТЕКУЩУЮ платформу (sharp и
# onnxruntime-node нативные), запуск: node <каталог>/omr-cli.mjs картинка.png.
#
# Коммит закреплён: другая версия движка — другое распознавание. Обновлять
# только с замером: python -m omr.jianpu.bench tests/images/jianpu/synth
set -euo pipefail

REPO="${JPEDITOR_REPO:-https://github.com/lodebar2026/jpeditor.git}"
COMMIT="${JPEDITOR_COMMIT:-3e264a7b950d450ba920bb3989a5497de5fb8147}"
TARGET="${1:-vendor/jpeditor-omr}"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

git init -q "$work/src"
git -C "$work/src" fetch -q --depth 1 "$REPO" "$COMMIT"
git -C "$work/src" checkout -q FETCH_HEAD
(
  cd "$work/src"
  npm ci --no-audit --no-fund
  npm run build:cli
  node scripts/pack-omr.mjs
)

# В архиве один каталог jpeditor-omr-<платформа>/ — его содержимое и есть пакет.
mkdir -p "$work/pkg"
tar -xzf "$work"/src/dist-pkg/jpeditor-omr-*.tar.gz -C "$work/pkg" --strip-components=1
echo "$COMMIT" > "$work/pkg/COMMIT"
# --help импортирует omr.js, а с ним sharp и onnxruntime: пакет не под ту
# платформу упадёт здесь, а не на первой задаче в проде.
node "$work/pkg/omr-cli.mjs" --help >/dev/null 2>&1

rm -rf "$TARGET"
mkdir -p "$(dirname "$TARGET")"
mv "$work/pkg" "$TARGET"
echo "движок цзянпу: $TARGET ($COMMIT)"

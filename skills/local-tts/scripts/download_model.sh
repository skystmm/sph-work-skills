#!/usr/bin/env bash
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODEL="${1:-cosyvoice2}"

case "$MODEL" in
  cosyvoice2) DEFAULT_DEST="$ROOT/models/CosyVoice2" ;;
  cosyvoice3) DEFAULT_DEST="$ROOT/models/CosyVoice3" ;;
  *) printf '未知基座 %s，可选: cosyvoice2 cosyvoice3\n' "$MODEL" >&2; exit 2 ;;
esac

if [ "$#" -gt 2 ]; then
  printf '用法: bash scripts/download_model.sh [cosyvoice2|cosyvoice3] [新目标目录]\n' >&2
  exit 2
fi

DEST="${2:-$DEFAULT_DEST}"
if [ "$#" -lt 2 ]; then
  mkdir -p "$ROOT/models"
fi
if [ -x "$ROOT/.venv/bin/python" ]; then
  PYTHON="$ROOT/.venv/bin/python"
else
  PYTHON="python3"
fi
exec "$PYTHON" "$ROOT/scripts/model_install.py" install "$MODEL" "$DEST"

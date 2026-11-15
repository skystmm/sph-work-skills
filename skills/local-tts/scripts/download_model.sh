#!/usr/bin/env bash
# 从 ModelScope 下载 CosyVoice 系列推理权重（可断点续传）
# 用法: bash scripts/download_model.sh [cosyvoice2|cosyvoice3]   默认 cosyvoice2
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KEY="${1:-cosyvoice2}"

case "$KEY" in
  cosyvoice2)
    REPO_ID="iic/CosyVoice2-0.5B"
    DEST="$ROOT/models/CosyVoice2"
    FILES=(
      "cosyvoice2.yaml"
      "llm.pt" "flow.pt" "hift.pt"
      "campplus.onnx" "speech_tokenizer_v2.onnx"
      "CosyVoice-BlankEN/config.json"
      "CosyVoice-BlankEN/generation_config.json"
      "CosyVoice-BlankEN/merges.txt"
      "CosyVoice-BlankEN/model.safetensors"
      "CosyVoice-BlankEN/tokenizer_config.json"
      "CosyVoice-BlankEN/vocab.json"
    )
    ;;
  cosyvoice3)
    REPO_ID="FunAudioLLM/Fun-CosyVoice3-0.5B-2512"
    DEST="$ROOT/models/CosyVoice3"
    FILES=(
      "cosyvoice3.yaml"
      "llm.pt" "flow.pt" "hift.pt"
      "campplus.onnx" "speech_tokenizer_v3.onnx"
      "CosyVoice-BlankEN/config.json"
      "CosyVoice-BlankEN/generation_config.json"
      "CosyVoice-BlankEN/merges.txt"
      "CosyVoice-BlankEN/model.safetensors"
      "CosyVoice-BlankEN/tokenizer_config.json"
      "CosyVoice-BlankEN/vocab.json"
    )
    ;;
  *)
    echo "未知基座 '$KEY'，可选: cosyvoice2 cosyvoice3" >&2
    exit 2
    ;;
esac

BASE="https://www.modelscope.cn/api/v1/models/${REPO_ID}/repo?Revision=master&FilePath="
echo "==> $KEY  (${REPO_ID})  ->  $DEST"

mkdir -p "$DEST"
fail=0
for f in "${FILES[@]}"; do
  out="$DEST/$f"
  mkdir -p "$(dirname "$out")"
  if [ -s "$out" ]; then
    echo "[skip] $f  ($(du -h "$out" | cut -f1))"
    continue
  fi
  echo "[get ] $f"
  if ! curl -L --fail --retry 3 --retry-delay 2 -C - --connect-timeout 20 \
       "${BASE}${f}" -o "$out"; then
    echo "[FAIL] $f"
    fail=1
  fi
done

echo "---- 结果 ----"
du -sh "$DEST"
[ "$fail" -eq 0 ] && echo "ALL_OK" || echo "SOME_FAILED"

#!/usr/bin/env bash
# Загрузка моделей, которые не хранятся в репозитории (один раз перед первым запуском).
set -euo pipefail
cd "$(dirname "$0")"

# Tesseract: русская модель tessdata_best — для OCR сканов (ПВТР — скан без текстового слоя)
mkdir -p ocr
if [ ! -f ocr/rus.traineddata ]; then
  ocr_tmp=$(mktemp ocr/rus.traineddata.XXXXXX)
  trap 'rm -f "$ocr_tmp"' EXIT
  curl -fL -o "$ocr_tmp" \
    https://github.com/tesseract-ocr/tessdata_best/raw/main/rus.traineddata
  chmod 644 "$ocr_tmp" # Модель должна быть доступна для чтения внутри контейнера Docling.
  mv "$ocr_tmp" ocr/rus.traineddata
  trap - EXIT
fi

# GigaChat 3.1 Lightning, квантизация Q4_K_M (6.5 ГБ) — локальная LLM
MODELS_DIR="${MODELS_DIR:-$HOME/.cache/polyplastic-models/gigachat31}"
mkdir -p "$MODELS_DIR"
[ -f "$MODELS_DIR/GigaChat3.1-10B-A1.8B-q4_K_M.gguf" ] || uvx --from huggingface_hub hf download \
  ai-sage/GigaChat3.1-10B-A1.8B-GGUF GigaChat3.1-10B-A1.8B-q4_K_M.gguf --local-dir "$MODELS_DIR"

echo "Готово. Эмбеддинги и реранкер скачаются сами при первом запуске контейнеров."

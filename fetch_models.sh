#!/usr/bin/env bash
# Загрузка моделей, которые не хранятся в репозитории (один раз перед первым запуском).
set -euo pipefail
cd "$(dirname "$0")"

# Tesseract: русская модель tessdata_best — для OCR сканов (ПВТР — скан без текстового слоя)
mkdir -p ocr
[ -f ocr/rus.traineddata ] || curl -fL -o ocr/rus.traineddata \
  https://github.com/tesseract-ocr/tessdata_best/raw/main/rus.traineddata

# GigaChat 3.1 Lightning, квантизация Q4_K_M (6.5 ГБ) — локальная LLM
MODELS_DIR="${MODELS_DIR:-$HOME/.cache/polyplastic-models/gigachat31}"
mkdir -p "$MODELS_DIR"
[ -f "$MODELS_DIR/GigaChat3.1-10B-A1.8B-q4_K_M.gguf" ] || uvx --from huggingface_hub hf download \
  ai-sage/GigaChat3.1-10B-A1.8B-GGUF GigaChat3.1-10B-A1.8B-q4_K_M.gguf --local-dir "$MODELS_DIR"

echo "Готово. Эмбеддинги и реранкер скачаются сами при первом запуске контейнеров."

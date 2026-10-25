"""Оборванная загрузка OCR не оставляет готовый файл; повтор проходит без сети и настоящих моделей."""

import os
import shutil
import subprocess
from pathlib import Path


def test_ocr_download_is_atomic_and_retry_preserves_complete_file(tmp_path):
    source = Path(__file__).resolve().parents[2] / "fetch_models.sh"
    script = tmp_path / "fetch_models.sh"
    shutil.copy2(source, script)
    commands = tmp_path / "bin"
    commands.mkdir()
    curl = commands / "curl"
    curl.write_text(
        """#!/bin/bash
set -euo pipefail
printf 'curl\\n' >> "$MOCK_CALLS"
[ "$#" -eq 4 ] && [ "$1" = '-fL' ] && [ "$2" = '-o' ]
[ "$4" = 'https://github.com/tesseract-ocr/tessdata_best/raw/main/rus.traineddata' ]
if [ ! -f "$MOCK_INTERRUPTED" ]; then
    printf 'PARTIAL-OCR' > "$3"
    touch "$MOCK_INTERRUPTED"
    echo 'curl: (18) transfer closed with bytes remaining to read' >&2
    exit 18
fi
printf 'COMPLETE-OCR-PAYLOAD' > "$3"
"""
    )
    uvx = commands / "uvx"
    uvx.write_text(
        """#!/bin/bash
set -euo pipefail
printf 'uvx\\n' >> "$MOCK_CALLS"
[ "$#" -eq 8 ] && [ "$1" = '--from' ] && [ "$2" = 'huggingface_hub' ]
[ "$3" = 'hf' ] && [ "$4" = 'download' ]
[ "$5" = 'ai-sage/GigaChat3.1-10B-A1.8B-GGUF' ]
[ "$6" = 'GigaChat3.1-10B-A1.8B-q4_K_M.gguf' ]
[ "$7" = '--local-dir' ] && [ "$8" = "$MODELS_DIR" ]
printf 'MOCK-LLM-PAYLOAD' > "$8/$6"
"""
    )
    curl.chmod(0o755)
    uvx.chmod(0o755)
    calls = tmp_path / "calls.log"
    env = {
        **os.environ,
        "PATH": str(commands) + os.pathsep + os.defpath,
        "MODELS_DIR": str(tmp_path / "models"),
        "MOCK_CALLS": str(calls),
        "MOCK_INTERRUPTED": str(tmp_path / "interrupted"),
    }
    target = tmp_path / "ocr" / "rus.traineddata"

    failed = subprocess.run(
        ["/bin/bash", str(script)], env=env, capture_output=True, text=True, timeout=10, check=False
    )
    assert failed.returncode == 18, failed.stderr
    assert "Готово" not in failed.stdout
    assert not target.exists(), "Оборванная загрузка не должна оставлять конечный OCR-файл"
    assert list(target.parent.iterdir()) == []
    assert calls.read_text().splitlines() == ["curl"]

    retried = subprocess.run(
        ["/bin/bash", str(script)], env=env, capture_output=True, text=True, timeout=10, check=False
    )
    assert retried.returncode == 0, retried.stderr
    assert "Готово" in retried.stdout
    assert target.read_bytes() == b"COMPLETE-OCR-PAYLOAD"
    assert target.stat().st_mode & 0o777 == 0o644
    assert calls.read_text().splitlines() == ["curl", "curl", "uvx"]
    complete_mtime = target.stat().st_mtime_ns

    repeated = subprocess.run(
        ["/bin/bash", str(script)], env=env, capture_output=True, text=True, timeout=10, check=False
    )
    assert repeated.returncode == 0, repeated.stderr
    assert "Готово" in repeated.stdout
    assert target.read_bytes() == b"COMPLETE-OCR-PAYLOAD"
    assert target.stat().st_mtime_ns == complete_mtime
    assert calls.read_text().splitlines() == ["curl", "curl", "uvx"]

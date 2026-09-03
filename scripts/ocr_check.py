#!/usr/bin/env python3
"""Run just the extraction step (MarkItDown + one OCR backend) against a
local image file -- no LINE, no OneDrive. Useful for tuning a backend or
its prompt against real sample photos.

Requires a fully populated .env in the repo root (Settings validates every
field, including the LINE/Microsoft ones, even though this script never
uses them) plus credentials for whichever backend(s) you run.

Usage:
    python scripts/ocr_check.py photo.jpg
    python scripts/ocr_check.py photo.jpg --backend gemini
    python scripts/ocr_check.py photo.jpg --compare      # tries all three backends
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings, get_settings
from app.extract import build_markitdown, extract

BACKENDS = ("claude", "gemini", "tesseract")


def run_backend(image_path: str, backend: str, base_settings: Settings) -> None:
    settings = base_settings.model_copy(update={"ocr_backend": backend})
    settings.require_backend_credentials()
    md = build_markitdown(settings)
    result = extract(md, image_path, settings)

    print(f"\n=== backend: {backend} ===")
    print(f"opd_number:   {result.opd_number}")
    print(f"patient_name: {result.patient_name}")
    print(f"confidence:   {result.confidence}")
    print("--- markdown ---")
    print(result.markdown)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("image", help="Path to a local jpg/png of a lab report")
    parser.add_argument("--backend", choices=BACKENDS, help="Backend to run (default: OCR_BACKEND from .env)")
    parser.add_argument("--compare", action="store_true", help="Run all three backends and print each result")
    args = parser.parse_args()

    settings = get_settings()

    if args.compare:
        for backend in BACKENDS:
            try:
                run_backend(args.image, backend, settings)
            except Exception as exc:
                print(f"\n=== backend: {backend} FAILED: {exc} ===")
        return

    run_backend(args.image, args.backend or settings.ocr_backend, settings)


if __name__ == "__main__":
    main()

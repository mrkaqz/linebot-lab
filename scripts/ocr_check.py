#!/usr/bin/env python3
"""Run just the extraction step (MarkItDown + one OCR backend) against a
local image file -- no LINE, no OneDrive. Useful for tuning a backend or
its prompt against real sample photos.

No .env is required -- every setting is optional, so this runs against the
defaults (OCR_BACKEND=tesseract) out of the box. You only need credentials
for a backend you actually ask it to run; --backend claude/gemini read
their keys from .env or the environment exactly as the app does.

A backend that fails at RUNTIME does not surface as a crash here.
MarkItDown catches the exception and falls back to its built-in EXIF-only
converter, so the symptom is an empty transcript with opd_number: None.
The real cause is logged at ERROR by app.ocr.base. This script turns
logging on for exactly that reason, so an empty transcript here is always
accompanied by the traceback that explains it.

Usage:
    python scripts/ocr_check.py photo.jpg
    python scripts/ocr_check.py photo.jpg --backend gemini
    python scripts/ocr_check.py photo.jpg --compare      # tries all three backends
"""

from __future__ import annotations

import argparse
import logging
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

    # A backend that raises is swallowed by MarkItDown (it falls through to
    # the built-in EXIF-only converter), so without this the only symptom
    # would be an empty transcript. app.ocr.base logs the real cause.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

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

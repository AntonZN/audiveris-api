#!/usr/bin/env python3
"""
Test script for image preprocessing.

Usage:
    python test_image_preprocess.py input.png
    python test_image_preprocess.py input.png --output enhanced.png
    python test_image_preprocess.py input.png --factor 3.0 --contrast 1.5 --sharpness 2.0
"""

import argparse
from pathlib import Path

from PIL import Image, ImageEnhance


def preprocess_image(
    input_path: Path,
    output_path: Path,
    min_dimension: int = 2000,
    upscale_factor: float = 2.0,
    contrast_factor: float = 1.2,
    sharpness_factor: float = 1.5,
) -> dict:
    """Preprocess image and return stats."""
    stats = {
        "original_size": None,
        "new_size": None,
        "upscaled": False,
        "contrast_applied": contrast_factor != 1.0,
        "sharpness_applied": sharpness_factor != 1.0,
    }

    with Image.open(input_path) as img:
        stats["original_size"] = img.size
        needs_upscale = img.width < min_dimension or img.height < min_dimension

        if needs_upscale:
            new_size = (int(img.width * upscale_factor), int(img.height * upscale_factor))
            img = img.resize(new_size, Image.Resampling.LANCZOS)
            stats["upscaled"] = True
            stats["new_size"] = new_size
        else:
            stats["new_size"] = img.size

        # Enhance contrast
        if contrast_factor != 1.0:
            enhancer = ImageEnhance.Contrast(img)
            img = enhancer.enhance(contrast_factor)

        # Enhance sharpness
        if sharpness_factor != 1.0:
            enhancer = ImageEnhance.Sharpness(img)
            img = enhancer.enhance(sharpness_factor)

        img.save(output_path)

    return stats


def main():
    parser = argparse.ArgumentParser(description="Test image preprocessing for Audiveris")
    parser.add_argument("input", type=Path, help="Input image path")
    parser.add_argument("--output", "-o", type=Path, help="Output image path (default: input_enhanced.ext)")
    parser.add_argument("--min-dimension", type=int, default=2000, help="Min dimension to trigger upscale (default: 2000)")
    parser.add_argument("--factor", type=float, default=2.0, help="Upscale factor (default: 2.0)")
    parser.add_argument("--contrast", type=float, default=1.2, help="Contrast factor (default: 1.2)")
    parser.add_argument("--sharpness", type=float, default=1.5, help="Sharpness factor (default: 1.5)")

    args = parser.parse_args()

    if not args.input.exists():
        print(f"Error: Input file not found: {args.input}")
        return 1

    if args.output is None:
        args.output = args.input.with_stem(f"{args.input.stem}_enhanced")

    print(f"Input:  {args.input}")
    print(f"Output: {args.output}")
    print(f"Settings:")
    print(f"  min_dimension: {args.min_dimension}px")
    print(f"  upscale_factor: {args.factor}x")
    print(f"  contrast: {args.contrast}")
    print(f"  sharpness: {args.sharpness}")
    print()

    stats = preprocess_image(
        args.input,
        args.output,
        min_dimension=args.min_dimension,
        upscale_factor=args.factor,
        contrast_factor=args.contrast,
        sharpness_factor=args.sharpness,
    )

    print(f"Original size: {stats['original_size'][0]}x{stats['original_size'][1]}")
    print(f"New size:      {stats['new_size'][0]}x{stats['new_size'][1]}")
    print(f"Upscaled:      {'Yes' if stats['upscaled'] else 'No (already large enough)'}")
    print(f"Contrast:      {'Applied' if stats['contrast_applied'] else 'Skipped'}")
    print(f"Sharpness:     {'Applied' if stats['sharpness_applied'] else 'Skipped'}")
    print()
    print(f"Done! Check: {args.output}")

    return 0


if __name__ == "__main__":
    exit(main())

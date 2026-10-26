"""Send one sample receipt image through the model wrapper.

Run from the project folder:

    .venv/bin/python scripts/test_image.py

Exit code 0 means gpt-6-luna accepted the JPEG and returned JSON.
Check that transactions mentions Cafe Luna or Latte, and that amount_cents
is 450 for the latte or 450 for the total. A different reading still means
the image was accepted. An error that mentions the image format or mime type
means the image path is blocked.
"""

from __future__ import annotations

import asyncio
import base64
import sys
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw

from app.extract import EXTRACTION_SCHEMA, EXTRACT_INSTRUCTIONS
from app.llm import call_llm
from app.model import scrub


def sample_receipt() -> str:
    image = Image.new("RGB", (800, 500), "white")
    draw = ImageDraw.Draw(image)
    lines = [
        "CAFE LUNA",
        "2026-10-01",
        "Latte                 4.50",
        "Total                 4.50",
    ]
    y = 40
    for line in lines:
        draw.text((40, y), line, fill="black")
        y += 50
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def main() -> int:
    print("Sending a sample Cafe Luna receipt JPEG.")
    print("Check: exit 0, a transactions list, and a latte or total near 450 cents.")
    try:
        result = asyncio.run(
            call_llm(
                "image_probe",
                EXTRACT_INSTRUCTIONS,
                user_text="Extract every transaction from the attached receipt image.",
                image_b64=sample_receipt(),
                image_mime="image/jpeg",
                json_schema=EXTRACTION_SCHEMA,
            )
        )
    except Exception as exc:
        print(scrub(f"{type(exc).__name__}: {exc}"), file=sys.stderr)
        return 1
    print(f"Data: {result['data']}")
    usage = result["usage"]
    print(
        "Tokens: "
        f"input={usage['input_tokens']} "
        f"cached={usage['cached_tokens']} "
        f"output={usage['output_tokens']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

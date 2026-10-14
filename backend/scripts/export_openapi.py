"""Export the app's OpenAPI schema to docs/openapi.json.

Usage (from the backend/ directory):
    python -m scripts.export_openapi

Or directly:
    python backend/scripts/export_openapi.py

The internal routes (/internal/*) are already excluded because the
internal router uses include_in_schema=False on all its routes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Ensure the backend package is importable when run from the repo root
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# Compute output path relative to this script (docs/ at project root)
_DOCS_DIR = _BACKEND_DIR.parent / "docs"
_OUTPUT = _DOCS_DIR / "openapi.json"


def main() -> None:
    from app.main import app  # noqa: PLC0415

    schema = app.openapi()
    _DOCS_DIR.mkdir(parents=True, exist_ok=True)
    _OUTPUT.write_text(json.dumps(schema, indent=2), encoding="utf-8")
    paths = sorted(schema.get("paths", {}).keys())
    print(f"Wrote {_OUTPUT}")
    print(f"Paths ({len(paths)}):")
    for p in paths:
        print(f"  {p}")


if __name__ == "__main__":
    main()

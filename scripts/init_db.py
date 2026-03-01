#!/usr/bin/env python3
"""
Standalone DB initialiser — useful for running in CI or Docker before starting the server.

Usage:
    python scripts/init_db.py
"""

import sys
from pathlib import Path

# Allow running from the project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import init_db  # noqa: E402

if __name__ == "__main__":
    print("Initialising database…")
    init_db()
    print("Done.")

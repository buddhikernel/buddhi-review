"""``python -m buddhi_review`` → the CLI."""
from __future__ import annotations

import sys

from buddhi_review.cli import main

if __name__ == "__main__":
    sys.exit(main())

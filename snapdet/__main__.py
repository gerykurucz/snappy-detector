"""Entry point so ``python -m snapdet`` works."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())

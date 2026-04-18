"""Entrypoint CLI minimaliste — toute la logique boot vit dans `cli/runner.py`."""

import sys

from polycopy.cli.runner import main

if __name__ == "__main__":
    sys.exit(main())

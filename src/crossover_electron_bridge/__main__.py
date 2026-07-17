"""Module entrypoint for ``python -m crossover_electron_bridge``."""

from __future__ import annotations

import sys


def main() -> int:
    """Load and invoke the CLI only when execution is requested."""
    from .cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    sys.exit(main())

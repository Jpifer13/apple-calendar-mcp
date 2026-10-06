"""Console entry point: ``apple-calendar-mcp`` / ``python -m apple_calendar_mcp``."""

from __future__ import annotations

import sys


def main() -> None:
    if sys.platform != "darwin":
        sys.exit("apple-calendar-mcp only runs on macOS, which is where EventKit lives.")

    from .server import run

    run()


if __name__ == "__main__":
    main()

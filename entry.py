"""PyInstaller entry point - one-file exe wrapper around the CLI.

With no arguments (a double-click from Explorer) the CLI shows its menu, so this
does not need to invent a default action of its own.
"""
import sys

from activerosterupdate.cli import main

if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)

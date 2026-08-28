"""PyInstaller entry point - one-file exe wrapper around the CLI."""
import sys

from activerosterupdate.cli import main

if __name__ == "__main__":
    argv = sys.argv[1:]
    if not argv:
        # double-clicked from Explorer: do the obvious thing, then hold the window open
        argv = ["sync", "--apply"]
        print("AxisLiveRosters - syncing every club into the game.\n")
        code = main(argv)
        input("\nDone. Press Enter to close.")
        sys.exit(code)
    sys.exit(main(argv))

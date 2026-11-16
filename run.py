"""Entry point for running from source and for PyInstaller."""
import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()   # worker processes of the built .exe start here
    from dlpt.__main__ import main
    sys.exit(main())

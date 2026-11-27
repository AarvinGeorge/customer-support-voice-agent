"""What: module entry so `python -m evals run` works. Use when: the command line runs. Inputs: argv. Output: exit code.
Side effects: see evals.cli. Role: thin shim."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())

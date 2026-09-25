"""Make `researcher` (this workstream) importable regardless of how pytest is invoked."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

"""What: shared pytest helpers for the contracts package tests.
Use when: any test needs the package root or fixture directory.
Inputs: none.
Output: path constants.
Side effects: none.
Role: test support."""
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = PKG_ROOT / "fixtures"

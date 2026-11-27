"""What: pytest setup for the repo-level tests: registers the `planning_docs` marker.
Use when: pytest collects tests/.
Inputs: none. Output: the marker registration. Side effects: none.
Role: lets CI deselect (not skip) the one test that needs planning-docs, which lives outside this repository."""
import pytest


def pytest_configure(config: pytest.Config) -> None:
    """What: register the planning_docs marker. Use when: pytest starts. Inputs: the pytest config.
    Output: none. Side effects: adds a marker line to the config. Role: avoids unknown-marker warnings."""
    config.addinivalue_line("markers", "planning_docs: needs the planning-docs folder next to the repo (not in CI); CI runs -m 'not planning_docs'")

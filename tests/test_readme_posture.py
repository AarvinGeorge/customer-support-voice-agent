"""What: checks that the README "Production posture" table lists every item of the "Designed, not built" table of doc 09
section 1.2, each with a Designed-in cell and a trigger, and that the chunking slot (showcase 3.2) is filled.
Use when: CI (repo tests) and after editing README.md.
Inputs: README.md at the repo root. Output: pytest results. Side effects: reads one file.
Role: CS-E-08.S1.T2 guard; the planning-docs folder is not in this repo, so the expected item list is embedded here."""
from __future__ import annotations

import re
from pathlib import Path

README = (Path(__file__).resolve().parents[1] / "README.md").read_text()

# Expected items: one distinctive phrase per row of planning-docs/09_build_plan_thin_slice_2026-11-27.md section 1.2
# ("Designed, not built"), in that order, plus the 06 section 14 items (the last 1.2 row, "Everything in 06 s14", is
# expanded to one phrase each) and the items decided on 27 Nov 2026 that are designed only.
EXPECTED_ITEMS = [
    # doc 09 s1.2 rows
    "Approvals UI",
    "Ops router",
    "billing email change",
    "A4 grace extension",
    "Notification preferences",
    "Live Notion sync",
    "LangSmith detail tab",
    "LLM judge",
    "Mocked-model load test",
    "Mobile layouts",
    "Spoken verification",
    "Gate 2 re-ask",
    "first-token timeout",
    "Turn drop recovery",
    "page-refresh rehydrate",
    "Push-to-talk",
    "voice usage post",
    "Admission semaphore",
    "Worst-case timing",
    "restart proof",
    # doc 09 s1.2 last row, expanded from doc 06 s14
    "Reranker",
    "GraphRAG",
    "Sub-agents",
    "Circuit breakers",
    "Voice sideband",
    "Conversation summarization",
    "Chat turn resume",
    "Voice reconnect",
    "ticket stages",
    "SLA timers",
    "budget counter",
    "retention",
    "Approval withdrawal",
]


def _section(text: str, heading: str) -> str:
    """What: the text of one `## ` section. Use when: reading the posture section. Inputs: README text, heading.
    Output: the section body up to the next `## ` heading. Side effects: none. Role: scopes the table check."""
    m = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, flags=re.M | re.S)
    assert m, f"README has no '## {heading}' section"
    return m.group(1)


def _rows() -> list[list[str]]:
    """What: the data rows of the posture table. Use when: every test here. Inputs: none.
    Output: list of cell lists (Item, Designed in, Trigger). Side effects: none. Role: parser."""
    rows = []
    for line in _section(README, "Production posture").splitlines():
        if line.startswith("|") and not re.match(r"^\|[\s:|-]+\|$", line):
            rows.append([c.strip() for c in line.strip().strip("|").split("|")])
    assert rows and rows[0][:3] == ["Item", "Designed in", "Trigger"], "table header must be Item, Designed in, Trigger"
    return rows[1:]


def test_every_designed_not_built_item_is_in_the_table() -> None:
    """What: each expected item phrase appears in the Item column. Use when: pytest. Inputs: none. Output: assert.
    Side effects: none. Role: the Done-when of CS-E-08.S1.T2."""
    items = " \n".join(r[0].lower() for r in _rows())
    missing = [i for i in EXPECTED_ITEMS if i.lower() not in items]
    assert not missing, f"missing from the Production posture table: {missing}"


def test_every_row_has_a_designed_in_and_a_trigger() -> None:
    """What: no row has an empty or TODO cell. Use when: pytest. Inputs: none. Output: assert. Side effects: none.
    Role: a posture row without a trigger is not a decision."""
    for row in _rows():
        assert len(row) == 3, row
        assert all(c and "TODO" not in c for c in row), row


def test_posture_section_has_no_em_dash() -> None:
    """What: no long dash in the posture section or the chunking slot. Use when: pytest. Inputs: none. Output: assert.
    Side effects: none. Role: project writing rule."""
    chunk = re.search(r"^#### 3\.2 .*?(?=^#### )", README, flags=re.M | re.S)
    assert chunk
    assert "—" not in _section(README, "Production posture") + chunk.group(0)


def test_chunking_slot_is_filled_with_the_measured_numbers() -> None:
    """What: showcase 3.2 has no TODO and states the real counts (81 chunks, largest body 203 tokens).
    Use when: pytest. Inputs: none. Output: assert. Side effects: none. Role: README claims come from the chunker output."""
    m = re.search(r"^#### 3\.2 .*?(?=^#### )", README, flags=re.M | re.S)
    assert m
    body = m.group(0)
    assert "TODO" not in body
    assert "81 chunks" in body and "203 tokens" in body

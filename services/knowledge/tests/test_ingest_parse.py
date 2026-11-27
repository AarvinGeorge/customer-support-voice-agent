"""What: tests for the article front matter parser and the Markdown block parser (chunking rule 1).
Use when: CI. Inputs: synthetic articles. Output: pass or fail. Side effects: none.
Role: draft A s2.3 rule 1 and the loud-fail checks for front matter (doc 07 s16)."""
import pytest

from knowledge.ingest.errors import IngestError
from knowledge.ingest.parse import parse_article, parse_blocks

GOOD = """---
article_id: refund-policy
title: "Refund policy"
source_url: https://app.notion.com/p/4257c83d5896817b9a5fcc8230b8a559
last_edited_time: 2026-11-27T05:25:09.594Z
tiers: [all]
---

Intro text.

## Section
Body.
"""


def test_parse_front_matter():
    """What: fields are read and typed. Use when: CI. Inputs: GOOD. Output: assertion. Side effects: none.
    Role: happy path."""
    a = parse_article(GOOD, "refund-policy.md")
    assert (a.article_id, a.title, a.tiers) == ("refund-policy", "Refund policy", ["all"])
    assert a.last_updated == "2026-11-27"
    assert a.source_url.startswith("https://app.notion.com/p/")
    assert a.body.lstrip().startswith("Intro text.")
    assert a.body_first_line == 8


@pytest.mark.parametrize("mutate,needle", [
    (lambda t: t.replace("article_id: refund-policy\n", ""), "article_id"),
    (lambda t: t.replace('title: "Refund policy"\n', ""), "title"),
    (lambda t: t.replace("tiers: [all]", "tiers: [gold]"), "gold"),
    (lambda t: t.replace("tiers: [all]", "tiers: []"), "tiers"),
    (lambda t: t.replace("source_url: https://app.notion.com/p/4257c83d5896817b9a5fcc8230b8a559\n", ""), "source_url"),
    (lambda t: t.replace("---\n", "", 1), "front matter"),
    (lambda t: t.replace("\n## Section", "\n# Refund policy\n## Section"), "H1"),
    (lambda t: t.replace("article_id: refund-policy", "article_id: other"), "filename"),
])
def test_bad_front_matter_fails_naming_file(mutate, needle):
    """What: each missing or invalid field raises IngestError naming the file and the field.
    Use when: CI. Inputs: mutated GOOD. Output: assertion. Side effects: none. Role: ingest loud checks."""
    with pytest.raises(IngestError) as exc:
        parse_article(mutate(GOOD), "refund-policy.md")
    assert "refund-policy.md" in str(exc.value) and needle in str(exc.value)


BLOCKS = """Para one
continues.

- a
- b
  more

1. one
2. two

| h1 | h2 |
| --- | --- |
| x | y |

> quoted
> text

```
code
## not a heading

still code
```

### Heading three
"""


def test_blocks_kinds_and_text():
    """What: paragraph, list (bullets and numbers), table, blockquote, fenced code and heading blocks parse
    as single units. Use when: CI. Inputs: BLOCKS. Output: assertion. Side effects: none. Role: rule 1."""
    blocks = parse_blocks(BLOCKS)
    kinds = [b.kind for b in blocks]
    assert kinds == ["paragraph", "list", "list", "table", "blockquote", "code", "heading"]
    assert blocks[0].text == "Para one\ncontinues."
    assert blocks[1].text == "- a\n- b\n  more"
    assert blocks[3].text.splitlines()[0] == "| h1 | h2 |" and len(blocks[3].text.splitlines()) == 3
    assert "## not a heading" in blocks[5].text and "still code" in blocks[5].text
    assert (blocks[6].level, blocks[6].text) == (3, "Heading three")


def test_block_line_numbers():
    """What: each block records its first line (for error messages). Use when: CI. Inputs: BLOCKS
    starting at line 10. Output: assertion. Side effects: none. Role: 'file and line' errors."""
    blocks = parse_blocks(BLOCKS, first_line=10)
    assert blocks[0].line == 10 and blocks[1].line == 13


def test_tiers_marker_is_a_block():
    """What: the sentinel the chunker writes for '<!-- tiers: pro -->' parses as a tiers block.
    Use when: CI. Inputs: text. Output: assertion. Side effects: none. Role: per-section tier override."""
    blocks = parse_blocks("## Pro\n\x00TIERS pro,business\x00\nBody.\n")
    assert [b.kind for b in blocks] == ["heading", "tiers", "paragraph"]
    assert blocks[1].text == "pro,business"

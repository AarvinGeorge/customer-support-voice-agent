"""What: validates the data/articles snapshot: front matter fields, tiers, unique ids, policy refs resolve.
Use when: CI (run with the contracts environment: uv run --project packages/contracts --no-editable pytest tests).
Inputs: data/articles/*.md and data/policy.yaml. Output: pass or fail. Side effects: none.
Role: CS-E-09.S3.T5 Done-when (10 files, doc 07 s16 front matter) and the loud-fail checks of ingest."""
import re
from datetime import datetime
from pathlib import Path

import yaml

from contracts.policy import load_policy

ROOT = Path(__file__).resolve().parents[1]
ARTICLES = ROOT / "data" / "articles"
REQUIRED = {"article_id", "title", "source_url", "last_edited_time", "tiers"}
TIERS = {"all", "free", "pro", "business", "enterprise"}


def split_front_matter(text: str) -> tuple[dict, str]:
    """What: split a snapshot file into its YAML front matter and body.
    Use when: any test reads an article. Inputs: file text. Output: (dict, body).
    Side effects: none. Role: minimal parser; raises ValueError when the file has no front matter."""
    m = re.match(r"---\n(.*?)\n---\n(.*)\Z", text, re.S)
    if not m:
        raise ValueError("no front matter")
    return yaml.safe_load(m.group(1)), m.group(2)


def load_all() -> dict[str, tuple[dict, str]]:
    """What: read every article file. Use when: tests need all articles. Inputs: none.
    Output: {filename stem: (front matter, body)}. Side effects: reads files. Role: shared helper."""
    return {p.stem: split_front_matter(p.read_text()) for p in sorted(ARTICLES.glob("*.md"))}


def test_ten_files():
    """What: exactly 10 article files exist. Use when: CI. Inputs: none. Output: assertion.
    Side effects: none. Role: Done-when '10 files committed'."""
    assert len(list(ARTICLES.glob("*.md"))) == 10


def test_front_matter_complete_and_valid():
    """What: required fields only, valid tiers, unique ids equal to the filename, Notion URL shape.
    Use when: CI. Inputs: none. Output: assertion. Side effects: none. Role: doc 07 s16 front matter."""
    arts = load_all()
    ids = []
    for stem, (fm, body) in arts.items():
        assert set(fm) == REQUIRED, (stem, set(fm) ^ REQUIRED)
        assert fm["article_id"] == stem
        ids.append(fm["article_id"])
        assert isinstance(fm["title"], str) and fm["title"].strip()
        assert re.fullmatch(r"https://app\.notion\.com/p/[0-9a-f]{32}", fm["source_url"]), stem
        assert isinstance(fm["last_edited_time"], (datetime, str)) and str(fm["last_edited_time"]).startswith("2026-")
        assert fm["tiers"] and set(fm["tiers"]) <= TIERS, stem
        assert not re.search(r"^# ", body, re.M), f"{stem}: body must not contain an H1"
        assert chr(0x2014) not in body, stem
    assert len(set(ids)) == len(ids) == 10


def test_front_matter_checker_can_fail():
    """What: the parser rejects a file without front matter and a bad tier is detectable.
    Use when: CI. Inputs: synthetic text. Output: assertion. Side effects: none.
    Role: zero-proof for the checks above."""
    import pytest
    with pytest.raises(ValueError):
        split_front_matter("# no front matter\n")
    fm, _ = split_front_matter("---\narticle_id: x\ntiers: [gold]\n---\nbody\n")
    assert not set(fm["tiers"]) <= TIERS


def test_policy_refs_resolve_to_sections():
    """What: every reason-code article and section slug exists in data/articles.
    Use when: CI. Inputs: policy.yaml and the snapshot. Output: assertion. Side effects: none.
    Role: policy_ref citations always point at a real section (doc 07 s13.1)."""
    arts = load_all()
    cfg = load_policy(ROOT / "data" / "policy.yaml")
    for code, rc in cfg.reason_codes.items():
        assert rc.policy_ref.article in arts, (code, rc.policy_ref.article)
        heads = {re.sub(r"[^a-z0-9]+", "-", h.lower()).strip("-")
                 for h in re.findall(r"^#{2,3} (.+)$", arts[rc.policy_ref.article][1], re.M)}
        assert rc.policy_ref.section in heads, (code, rc.policy_ref)

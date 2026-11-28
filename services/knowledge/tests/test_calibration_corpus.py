"""What: the committed calibration file matches the corpus ingest would build from data/articles (review PR #86 I4).
Use when: CI (job python (services/knowledge)); any article edit without a recalibration turns it red. Inputs: data/articles,
data/policy.yaml, services/knowledge/config/retrieval_calibration.json. Output: pass or fail. Side effects: reads files only
(no database, no embedding call). Role: the systemic cause of the stale calibration found in rca-gate1-weak-band.md:
nothing tied the thresholds to the corpus they were measured on, so the D-070 article edits made them silently stale."""
import json
from pathlib import Path

from contracts.policy import load_policy

from knowledge.ingest.job import _load, corpus_version

ROOT = Path(__file__).resolve().parents[3]
CAL_PATH = ROOT / "services" / "knowledge" / "config" / "retrieval_calibration.json"


def live_corpus_version(articles_dir: Path) -> str:
    """What: the corpus_version ingest would record for these articles. Use when: the tests below. Inputs: the article
    directory. Output: hex digest. Side effects: reads files. Role: the same _load and corpus_version the ingest job runs."""
    loaded = _load(articles_dir, load_policy(ROOT / "data" / "policy.yaml"))
    return corpus_version([(c.chunk_id, c.content_hash) for _, chunks, _ in loaded for c in chunks])


def test_calibration_corpus_version_matches_the_articles():
    cal = json.loads(CAL_PATH.read_text())
    assert cal["corpus_version"] == live_corpus_version(ROOT / "data" / "articles"), (
        "data/articles changed since calibration: re-ingest, rerun services/knowledge/scripts/probe_retrieval.py and "
        "commit a new retrieval_calibration.json")


def test_an_article_edit_changes_the_version(tmp_path):
    # Positive control: the check can go red (one appended line in one article gives a different version).
    for src in (ROOT / "data" / "articles").glob("*.md"):
        (tmp_path / src.name).write_bytes(src.read_bytes())
    target = sorted(tmp_path.glob("*.md"))[0]
    before = live_corpus_version(tmp_path)
    target.write_text(target.read_text() + "\nOne more sentence.\n")
    assert live_corpus_version(tmp_path) != before

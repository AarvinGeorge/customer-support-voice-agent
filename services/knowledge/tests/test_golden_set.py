"""What: validates the golden set in evals/golden: schemas, cases, retrieval queries, gold chunk ids, coverage.
Use when: CI and before any golden run (CS-E-09.S5.T1 Done-when: every case validates against the schema; every gold chunk id resolves).
Inputs: evals/golden/{golden.yaml, retrieval.yaml, schema/*.json, fixtures/*.md}, data/articles, data/policy.yaml, data/seeds.
Output: pass or fail. Side effects: none (no network; chunk ids come from the knowledge chunker and the committed tiktoken cache).
Role: the data contract between the golden set and the runner (CS-E-06.S1.T1); a bad case fails here, never silently in a run."""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest
import yaml
from contracts.policy import load_policy
from jsonschema import Draft202012Validator

from knowledge.ingest.chunker import Chunk, chunk_article
from knowledge.ingest.parse import parse_article

ROOT = Path(__file__).resolve().parents[3]
GOLDEN = ROOT / "evals" / "golden"
ARTICLES = ROOT / "data" / "articles"
EM_DASH = chr(0x2014)  # built from its code point, so this file holds no literal em dash


def _load_yaml(path: Path):
    """What: read one YAML file. Use when: loading golden data. Inputs: path. Output: parsed object.
    Side effects: reads a file. Role: one loader so a missing file fails with its path."""
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _schema(name: str) -> dict:
    """What: read one JSON schema. Use when: building validators. Inputs: file name. Output: dict.
    Side effects: reads a file. Role: schema loader."""
    return json.loads((GOLDEN / "schema" / name).read_text(encoding="utf-8"))


def _chunks_of(path: Path) -> list[Chunk]:
    """What: chunk one article file with the real chunker. Use when: resolving chunk ids. Inputs: path.
    Output: list of Chunk. Side effects: reads files. Role: the same code ingest runs, no network."""
    policy = load_policy(ROOT / "data" / "policy.yaml")
    return chunk_article(parse_article(path.read_text(encoding="utf-8"), path.name), policy)


def _corpus_ids(fixtures: list[str] | None = None) -> set[str]:
    """What: every chunk id of the committed articles plus the named fixture articles. Use when: resolving gold ids.
    Inputs: fixture paths relative to evals/golden. Output: set of ids. Side effects: reads files.
    Role: gold chunk ids resolve against data/articles; a case's own fixtures extend that set for that case only."""
    ids = {c.chunk_id for p in sorted(ARTICLES.glob("*.md")) for c in _chunks_of(p)}
    for f in fixtures or []:
        ids |= {c.chunk_id for c in _chunks_of(GOLDEN / f)}
    return ids


def _cases() -> list[dict]:
    """What: the end-to-end cases. Use when: any case test. Inputs: none. Output: list of dicts.
    Side effects: reads golden.yaml. Role: loader."""
    return _load_yaml(GOLDEN / "golden.yaml")["cases"]


def _queries() -> list[dict]:
    """What: the retrieval-labelled queries. Use when: any retrieval test. Inputs: none. Output: list of dicts.
    Side effects: reads retrieval.yaml. Role: loader."""
    return _load_yaml(GOLDEN / "retrieval.yaml")["queries"]


def _personas() -> dict[str, dict]:
    """What: personas keyed by name. Use when: checking a case's persona. Inputs: none. Output: dict.
    Side effects: reads personas.yaml. Role: source of truth for persona facts."""
    return {p["name"]: p for p in _load_yaml(ROOT / "data" / "seeds" / "personas.yaml")}


def _all_golden_text() -> str:
    """What: concatenated text of every golden data file. Use when: scanning for forbidden characters or secrets.
    Inputs: none. Output: str. Side effects: reads files. Role: one scan surface."""
    files = [GOLDEN / "golden.yaml", GOLDEN / "retrieval.yaml", *sorted((GOLDEN / "schema").glob("*.json")),
             *sorted((GOLDEN / "fixtures").glob("*.md"))]
    return "\n".join(f.read_text(encoding="utf-8") for f in files)


def test_schemas_are_valid_json_schema():
    """What: both schema files are valid draft 2020-12 schemas. Use when: CI. Inputs: none. Output: pass or fail.
    Side effects: none. Role: a broken schema would validate nothing."""
    for name in ("case.schema.json", "retrieval.schema.json"):
        Draft202012Validator.check_schema(_schema(name))


def test_every_case_validates_against_the_schema():
    """What: each golden case validates; ids are unique. Use when: CI. Inputs: none. Output: pass or fail.
    Side effects: none. Role: Done-when part 1 (cases)."""
    validator = Draft202012Validator(_schema("case.schema.json"))
    cases = _cases()
    errors = [f"{c.get('id')}: {e.message} at {list(e.absolute_path)}" for c in cases for e in validator.iter_errors(c)]
    assert not errors, "\n".join(errors)
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids))


def test_every_retrieval_query_validates_against_the_schema():
    """What: each retrieval query validates; ids are unique. Use when: CI. Inputs: none. Output: pass or fail.
    Side effects: none. Role: Done-when part 1 (queries)."""
    validator = Draft202012Validator(_schema("retrieval.schema.json"))
    qs = _queries()
    errors = [f"{q.get('id')}: {e.message} at {list(e.absolute_path)}" for q in qs for e in validator.iter_errors(q)]
    assert not errors, "\n".join(errors)
    assert len({q["id"] for q in qs}) == len(qs)


def test_the_schema_can_fail():
    """What: prove validation is not vacuous: a broken case and a broken query are rejected. Use when: CI.
    Inputs: none. Output: pass or fail. Side effects: none. Role: a zero must be proven possible to be non-zero."""
    case_v = Draft202012Validator(_schema("case.schema.json"))
    query_v = Draft202012Validator(_schema("retrieval.schema.json"))
    assert list(case_v.iter_errors({"id": "bad"}))
    good = dict(_cases()[0])
    good["scenario"] = "S9"
    assert list(case_v.iter_errors(good))
    q = dict(_queries()[0])
    q["gold_chunk_ids"] = []
    assert list(query_v.iter_errors(q)), "an answerable query with no gold chunk must be rejected"


def test_every_gold_chunk_id_resolves():
    """What: every gold, forbidden and must-include chunk id exists in the chunker output of data/articles (plus the
    case's own fixtures). Use when: CI. Inputs: none. Output: pass or fail. Side effects: none.
    Role: Done-when part 2; a stale id would grade every run as a miss."""
    missing: list[str] = []
    for q in _queries():
        ids = _corpus_ids()
        for cid in q.get("gold_chunk_ids", []) + q.get("forbidden_chunk_ids", []):
            if cid not in ids:
                missing.append(f"{q['id']}: {cid}")
    for c in _cases():
        ids = _corpus_ids(c.get("fixtures", {}).get("articles"))
        exp = c["expected"]
        refs = exp.get("citations", {}).get("must_cite_any", []) + exp.get("citations", {}).get("must_not_cite", [])
        refs += exp.get("retrieval_must_include", [])
        for t in c["turns"]:
            refs += t.get("expect", {}).get("cites_any", [])
        for cid in refs:
            if cid not in ids:
                missing.append(f"{c['id']}: {cid}")
    assert not missing, "unresolved chunk ids:\n" + "\n".join(missing)


def test_chunk_id_resolution_can_fail():
    """What: prove resolution is not vacuous: a made-up id does not resolve and a real one does. Use when: CI.
    Inputs: none. Output: pass or fail. Side effects: none. Role: guards test_every_gold_chunk_id_resolves."""
    ids = _corpus_ids()
    assert "refund-policy#no-such-section" not in ids
    assert "refund-policy#how-refunds-work" in ids
    assert len(ids) > 50


def test_case_count_and_coverage():
    """What: about 12 cases; S1 to S5 each covered; a past_due case and the poisoned-article case exist. Use when: CI.
    Inputs: none. Output: pass or fail. Side effects: none. Role: the story's scope (doc 09 s5 row CS-E-09.S5.T1)."""
    cases = _cases()
    assert 12 <= len(cases) <= 16
    scenarios = Counter(c["scenario"] for c in cases)
    for s in ("S1", "S2", "S3", "S4", "S5"):
        assert scenarios[s] >= 1, f"no case for {s}"
    personas = _personas()
    assert any(personas[c["persona"]]["contract_status"] == "past_due" and "PAST_DUE" in c["expected"].get("reason_codes", [])
               for c in cases), "no past_due case"
    assert any(c.get("fixtures", {}).get("articles") for c in cases), "no poisoned-article case"


def test_retrieval_label_mix():
    """What: about 20 retrieval queries: answerable, not_in_docs (with near-misses), off_topic. Use when: CI.
    Inputs: none. Output: pass or fail. Side effects: none. Role: D-052 lesson: the not_in_docs set must include
    near-misses that sit close to tau_high, not only far-off questions."""
    qs = _queries()
    assert 18 <= len(qs) <= 28
    labels = Counter(q["label"] for q in qs)
    assert labels["answerable"] >= 10 and labels["off_topic"] >= 3
    assert sum(1 for q in qs if q["label"] == "not_in_docs" and q.get("near_miss")) >= 4
    assert sum(1 for q in qs if q["label"] == "off_topic" and q.get("near_miss")) >= 1


def test_train_test_split():
    """What: both splits are non-empty for cases and for every retrieval label; no test-split query is a calibration probe. Use when: CI. Inputs: none. Output: pass or fail. Side effects: none.
    Role: doc G s2.3: train and test reported separately; the test split is never tuned on (no test query is a calibration probe)."""
    cases = _cases()
    assert {c["split"] for c in cases} == {"train", "test"}
    qs = _queries()
    for label in ("answerable", "not_in_docs", "off_topic"):
        assert {q["split"] for q in qs if q["label"] == label} == {"train", "test"}, label


def test_personas_exist_and_credentials_are_never_written():
    """What: each case's persona is in personas.yaml and no PIN value appears in any golden file. Use when: CI.
    Inputs: none. Output: pass or fail. Side effects: none. Role: PIN placeholders only ({persona.pin});
    the runner resolves them, so the dataset never holds a PIN (doc 07 s11)."""
    personas = _personas()
    for c in _cases():
        assert c["persona"] in personas, f"{c['id']}: unknown persona {c['persona']}"
    text = _all_golden_text()
    for p in personas.values():
        assert re.search(rf"(?<!\d){re.escape(str(p['pin']))}(?!\d)", text) is None, f"PIN of {p['name']} written in golden files"


def test_reason_codes_exist_and_match_seeded_triggers():
    """What: expected reason codes are in policy.yaml; for a refund trigger code the persona and charge are the seeded
    trigger (data/seeds/triggers.yaml). Use when: CI. Inputs: none. Output: pass or fail. Side effects: none.
    Role: stops a case from expecting a code its persona can never produce."""
    policy = yaml.safe_load((ROOT / "data" / "policy.yaml").read_text(encoding="utf-8"))
    triggers = _load_yaml(ROOT / "data" / "seeds" / "triggers.yaml")["refund_triggers"]
    for c in _cases():
        codes = c["expected"].get("reason_codes", [])
        for code in codes:
            assert code in policy["reason_codes"], f"{c['id']}: {code} not in policy.yaml"
        if codes and codes[0] in triggers:
            assert triggers[codes[0]]["persona"] == c["persona"], f"{c['id']}: {codes[0]} is not seeded for {c['persona']}"
            charge = triggers[codes[0]]["charge"]
            charges = [a["charge_id"] for a in c["expected"]["final_state"].get("pending_actions", []) if "charge_id" in a]
            if charge and charges:
                assert charges == [charge], f"{c['id']}: charge {charges} is not the seeded {charge}"


def test_turn_sequences_follow_the_state_machine():
    """What: a confirm turn comes after a turn that ends in a read-back; a committed or submitted expectation needs a
    confirm turn; faults only on turn kinds that can carry them. Use when: CI. Inputs: none. Output: pass or fail.
    Side effects: none. Role: prepare-then-commit in a later turn (L1 s5, doc 07 s3.3) holds in the data too."""
    for c in _cases():
        turns = c["turns"]
        confirm_at = [i for i, t in enumerate(turns) if t["kind"] == "confirm"]
        for i in confirm_at:
            assert i > 0 and any(t.get("expect", {}).get("end_reason") == "readback" for t in turns[:i]), \
                f"{c['id']}: confirm turn {i} has no earlier read-back turn"
        ends = {t.get("expect", {}).get("end_reason") for t in turns}
        if ends & {"committed", "submitted"}:
            assert confirm_at, f"{c['id']}: expects commit or submit without a confirm turn"
        for t in turns:
            if "fault" in t:
                assert t["kind"] in ("text", "confirm"), f"{c['id']}: fault on a {t['kind']} turn"


def test_tool_trajectory_is_consistent():
    """What: required is inside allowed, forbidden does not meet allowed, max_calls names known tools. Use when: CI.
    Inputs: none. Output: pass or fail. Side effects: none. Role: a trajectory that contradicts itself grades every run wrong."""
    for c in _cases():
        t = c["expected"]["tool_trajectory"]
        allowed, required, forbidden = set(t["allowed"]), set(t["required"]), set(t.get("forbidden", []))
        assert required <= allowed, f"{c['id']}: required not in allowed: {required - allowed}"
        assert not (allowed & forbidden), f"{c['id']}: allowed and forbidden overlap: {allowed & forbidden}"
        for o in t.get("ordering", []):
            assert o["before"] in allowed and o["after"] in allowed, f"{c['id']}: ordering names a tool not allowed"


def test_committed_refund_needs_the_tool_chain():
    """What: a case expecting a committed or awaiting_approval refund requires prepare_refund and a confirm turn, and
    forbids commit-without-confirm and same-turn commit. Use when: CI. Inputs: none. Output: pass or fail.
    Side effects: none. Role: the code-enforced chain is the project's headline guarantee (D-031)."""
    for c in _cases():
        states = {a["state"] for a in c["expected"]["final_state"].get("pending_actions", [])}
        if states & {"committed", "awaiting_approval"} or c["expected"]["final_state"]["committed_refunds"] > 0:
            assert "prepare_refund" in c["expected"]["tool_trajectory"]["required"], c["id"]
            assert any(t["kind"] == "confirm" for t in c["turns"]), c["id"]
            forbidden = set(c["expected"].get("forbidden", []))
            assert {"commit_without_confirm", "commit_same_turn"} <= forbidden, f"{c['id']}: missing commit guards"


def test_poisoned_article_fixture_is_test_only_and_live():
    """What: the X14 fixture (the injection case, G-14; its id is neutral on purpose, see test_fixture_ids_do_not_label_the_attack) is outside data/articles, has an id no committed article has, its chunks are flagged
    injection_suspect, it carries the canary the case forbids, and the case requires the fixture chunk to be retrieved.
    Use when: CI. Inputs: none. Output: pass or fail. Side effects: none. Role: doc 06 X14: a fixture corpus, never an
    11th production article; a case that cannot retrieve its poison tests nothing."""
    poisoned = [c for c in _cases() if c.get("fixtures", {}).get("articles")]
    assert poisoned
    committed = {p.stem for p in ARTICLES.glob("*.md")}
    for c in poisoned:
        for f in c["fixtures"]["articles"]:
            path = GOLDEN / f
            assert path.is_file() and ARTICLES not in path.parents
            article = parse_article(path.read_text(encoding="utf-8"), path.name)
            assert article.article_id not in committed
            chunks = _chunks_of(path)
            assert any(ch.injection_suspect for ch in chunks), "fixture is not flagged by the sanitizer"
            canaries = c["expected"]["forbidden_strings"]
            assert canaries and all(s in path.read_text(encoding="utf-8") for s in canaries)
            must = c["expected"]["retrieval_must_include"]
            assert any(m.startswith(article.article_id + "#") for m in must)
        assert {"injection_followed"} <= set(c["expected"].get("forbidden", []))
        assert "prepare_refund" in c["expected"]["tool_trajectory"].get("forbidden", [])


def test_not_in_docs_labels_are_true_of_the_corpus():
    """What: every absent_terms entry of a not_in_docs or off_topic query really is absent from data/articles, and the
    check can find a present term. Use when: CI. Inputs: none. Output: pass or fail. Side effects: none.
    Role: a near-miss whose key word appears in the docs would be mislabelled (a zero must be provable non-zero)."""
    corpus = "\n".join(p.read_text(encoding="utf-8").lower() for p in ARTICLES.glob("*.md"))
    assert "refund" in corpus
    checked = 0
    for q in _queries():
        for term in q.get("absent_terms", []):
            assert term.lower() not in corpus, f"{q['id']}: '{term}' appears in the articles"
            checked += 1
    assert checked >= 6


def test_tier_filter_query_forbids_chunks_the_tier_cannot_see():
    """What: a query whose forbidden chunk ids are tier-restricted in the corpus names a tier that really cannot see them.
    Use when: CI. Inputs: none. Output: pass or fail. Side effects: none. Role: keeps the tier-leak label honest."""
    chunks = {c.chunk_id: c for p in ARTICLES.glob("*.md") for c in _chunks_of(p)}
    found = 0
    for q in _queries():
        for cid in q.get("forbidden_chunk_ids", []):
            allowed = ["all"] if q["tier"] == "public" else ["all", q["tier"]]
            assert not set(chunks[cid].tiers) & set(allowed), f"{q['id']}: {cid} is visible to tier {q['tier']}"
            found += 1
    assert found >= 1


def test_no_em_dashes_anywhere():
    """What: no em dash in any golden file. Use when: CI. Inputs: none. Output: pass or fail. Side effects: none.
    Role: project rule (no em dashes in any artifact)."""
    assert EM_DASH not in _all_golden_text()


@pytest.mark.parametrize("name", ["golden.yaml", "retrieval.yaml"])
def test_dataset_files_state_their_purpose(name: str):
    """What: each dataset file opens with a comment naming what, use when and role. Use when: CI. Inputs: file name.
    Output: pass or fail. Side effects: none. Role: code descriptors (D-015) for data files."""
    head = (GOLDEN / name).read_text(encoding="utf-8")[:1500]
    for label in ("What:", "Use when:", "Role:"):
        assert label in head


def test_verification_turn_continues_the_earlier_request():
    """What: a verification turn that answers a verification_requested turn must not expect end_reason verified, and no
    text turn may restate the request right after it. Use when: CI. Inputs: none. Output: pass or fail. Side effects: none.
    Role: doc 07 s6.3 step 5: on success the agent loop runs once so the model continues the earlier request."""
    for c in _cases():
        turns = c["turns"]
        for i, t in enumerate(turns):
            if t["kind"] == "verification" and i > 0 and turns[i - 1].get("expect", {}).get("end_reason") == "verification_requested":
                assert t.get("expect", {}).get("end_reason") not in (None, "verified"), f"{c['id']}: turn {i} must end where the request ends"
                assert "07 s6.3" in c.get("notes", ""), f"{c['id']}: notes must cite 07 s6.3 step 5"
                nxt = turns[i + 1] if i + 1 < len(turns) else None
                assert nxt is None or nxt["kind"] != "text", f"{c['id']}: a text turn after the card turn restates the request"


def test_open_outcome_cases_use_a_deterministic_ticket_rule():
    """What: no count_any_of; count_by_outcome keys are exactly the allowed outcomes. Use when: CI. Inputs: none.
    Output: pass or fail. Side effects: none. Role: a ticket count is paired with the observed outcome, so
    'answered and opened a ticket' cannot pass."""
    for c in _cases():
        tickets = c["expected"]["final_state"]["tickets"]
        assert "count_any_of" not in tickets
        if "count_by_outcome" in tickets:
            assert set(tickets["count_by_outcome"]) == set(c["expected"]["outcome"]), c["id"]
            assert "count" not in tickets
        else:
            assert len(c["expected"]["outcome"]) == 1 or c["id"] == "G-03", f"{c['id']}: open outcome needs count_by_outcome"


def test_fixture_reads_like_a_plain_article():
    """What: the visible text of the poisoned fixture does not say it is a test, fixture or attack. Use when: CI.
    Inputs: none. Output: pass or fail. Side effects: none. Role: a self-labelled attack proves little; the test-only
    marking lives in the file name, source_url and the dataset (doc 06 X14, doc G M3)."""
    for c in _cases():
        for f in c.get("fixtures", {}).get("articles", []):
            article = parse_article((GOLDEN / f).read_text(encoding="utf-8"), Path(f).name)
            visible = (article.title + "\n" + article.body).lower()
            for word in ("fixture", "attack", "poison", "test ", "imitat"):
                assert word not in visible, f"{f}: visible text says '{word}'"
            assert "example.invalid" in article.source_url


def test_no_test_split_query_is_a_calibration_probe():
    """What: no test-split retrieval query equals a probe in scripts/probe_retrieval.py. Use when: CI. Inputs: none.
    Output: pass or fail. Side effects: none. Role: probes set tau_high (D-052); the test split is never tuned on."""
    src = (Path(__file__).resolve().parents[1] / "scripts" / "probe_retrieval.py").read_text(encoding="utf-8")
    probes = {m.lower() for m in re.findall(r'\("(?:answerable|not_in_docs|off_topic)", "[^"]*", "[^"]*", "([^"]+)"', src)}
    assert len(probes) >= 15, "could not read the probe list"
    clash = [q["id"] for q in _queries() if q["split"] == "test" and q["query"].lower() in probes]
    assert not clash, f"test-split queries that are calibration probes: {clash}"


def test_gate_cases_require_search_kb():
    """What: a case that grades the retrieval gate requires search_kb. Use when: CI. Inputs: none. Output: pass or fail.
    Side effects: none. Role: a verdict and a decline can only follow a search (doc 07 s6.5)."""
    for c in _cases():
        if "gate" in c["expected"]:
            assert "search_kb" in c["expected"]["tool_trajectory"]["required"], c["id"]


def test_request_verification_is_only_required_where_the_model_can_call_it():
    """What: a case whose first turn is the card, or whose first text carries a typed PIN placeholder, must not require
    or allow-with-calls request_verification. Use when: CI. Inputs: none. Output: pass or fail. Side effects: none.
    Role: 07 s5.2 line 537 (the tool exists only in V0), s6.3 step 6 line 612 (card first: verified before any model turn),
    s7.3 line 765 (a typed PIN is refused in code: fixed reply, no model call)."""
    for c in _cases():
        first = c["turns"][0]
        card_first = first["kind"] == "verification"
        typed_pin = first["kind"] == "text" and "{persona.pin}" in first["text"]
        if card_first or typed_pin:
            t = c["expected"]["tool_trajectory"]
            assert "request_verification" not in t["required"], f"{c['id']}: tool not callable in this flow"
            assert "request_verification" not in t["allowed"] or t.get("max_calls", {}).get("request_verification") == 0, c["id"]


def test_fixture_ids_do_not_label_the_attack():
    """What: the fixture article id and file name carry no attack words, because the model reads the id in the doc
    wrapper (07 s5.3 line 545). Use when: CI. Inputs: none. Output: pass or fail. Side effects: none.
    Role: this is the X14 injection case (doc 06 X14); the test-only marking lives in the directory, source_url and dataset."""
    for c in _cases():
        for f in c.get("fixtures", {}).get("articles", []):
            article = parse_article((GOLDEN / f).read_text(encoding="utf-8"), Path(f).name)
            for word in ("poison", "attack", "inject", "x14", "malicious"):
                assert word not in article.article_id.lower() and word not in Path(f).name.lower(), f"{f}: id says '{word}'"

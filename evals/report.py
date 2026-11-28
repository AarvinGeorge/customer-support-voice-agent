"""What: builds the run report (raw counts) and writes it as a dated Markdown file and a JSON file.
Use when: a run finished. Inputs: case grades, the Gate 1 result (or None), run metadata, the time.
Output: a dict (with the decision paths each run took), Markdown text, two files under the output directory. Side effects: writes files.
Role: the record of the golden pass rate; train and test are reported separately and counts are never rounded into a percentage."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from .model import CaseGrade

VERDICTS = ("pass", "fail", "error")


def _counts(items: list[str]) -> dict[str, int]:
    """What: count pass, fail, error in a list. Use when: summarising. Inputs: statuses. Output: dict with all three keys.
    Side effects: none. Role: helper."""
    return {v: items.count(v) for v in VERDICTS}


def build_report(grades: list[CaseGrade], gate1: dict[str, Any] | None, meta: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    """What: assemble the report dict. Use when: after run_cases and run_gate1. Inputs: grades, gate1 result or None, meta, now.
    Output: dict with cases (total, verdicts and by_split over the plain cases only, known_limitation with the tagged
    cases' own counts, detail), checks (per grader raw counts), gate1, meta, generated_at.
    Side effects: none. Role: the single source for both output files. A case tagged known_limitation (D-083) is
    never counted in the plain pass counts: its pass means it held the documented safe path, not that it is fixed."""
    plain = [g for g in grades if not g.known_limitation]
    tagged = [g for g in grades if g.known_limitation]
    verdicts = [g.verdict for g in plain]
    by_split = {s: _counts([g.verdict for g in plain if g.split == s]) for s in ("train", "test")}
    checks: dict[str, list[str]] = {}
    for g in grades:
        for c in g.checks:
            name = "turn.N" if c.name.startswith("turn.") else c.name
            checks.setdefault(name, []).append(c.status)
    return {
        "generated_at": now.isoformat(),
        "meta": meta,
        "cases": {
            "total": len(grades),
            "verdicts": _counts(verdicts),
            "by_split": by_split,
            "known_limitation": {"total": len(tagged), "verdicts": _counts([g.verdict for g in tagged]),
                                 "cases": [{"id": g.case_id, "split": g.split, "tag": g.known_limitation,
                                            "verdict": g.verdict} for g in tagged]},
            "detail": [{"id": g.case_id, "split": g.split, "verdict": g.verdict, "known_limitation": g.known_limitation,
                        "checks": [{"name": c.name, "status": c.status, "detail": c.detail,
                                    **({"exercised": c.exercised} if c.exercised is not None else {})} for c in g.checks]} for g in grades],
        },
        "checks": {k: _counts(v) for k, v in sorted(checks.items())},
        "paths": [{"case": g.case_id, "probe": c.name.removeprefix("path:"), "exercised": c.exercised, "detail": c.detail}
                  for g in grades for c in g.checks if c.exercised is not None],
        "gate1": gate1,
    }


def all_green(r: dict[str, Any]) -> bool:
    """What: did the run pass. Use when: the CLI exit code. Inputs: the report dict. Output: True when at least one case
    ran and no case failed or errored, plain or tagged. Side effects: none. Role: a known limitation that left its safe
    path is a failure like any other; a tagged pass is not a plain pass but does not fail the run."""
    c = r["cases"]
    k = c["known_limitation"]["verdicts"]
    v = c["verdicts"]
    return c["total"] > 0 and v["fail"] == 0 and v["error"] == 0 and k["fail"] == 0 and k["error"] == 0


def render_markdown(r: dict[str, Any]) -> str:
    """What: render the report as Markdown. Use when: writing the file. Inputs: the report dict. Output: text (no long dashes).
    Side effects: none. Role: the human-readable copy; every number is a raw count."""
    L: list[str] = [f"# Golden run, {r['generated_at']}", ""]
    for k, v in r["meta"].items():
        L.append(f"- {k}: {v}")
    c = r["cases"]
    plain_n = sum(c["verdicts"].values())
    L += ["", "## Cases", "", "| scope | pass | fail | error |", "|---|---|---|---|",
          f"| all ({plain_n}) | {c['verdicts']['pass']} | {c['verdicts']['fail']} | {c['verdicts']['error']} |"]
    for s, v in c["by_split"].items():
        L.append(f"| {s} ({sum(v.values())}) | {v['pass']} | {v['fail']} | {v['error']} |")
    L += ["", "A case passes only when every check passes; error means data needed to decide was missing.", "",
          "| case | split | verdict | not passing |", "|---|---|---|---|"]
    for d in c["detail"]:
        bad = "; ".join(f"{x['name']} {x['status']}: {x['detail']}" for x in d["checks"] if x["status"] != "pass")
        tag = f" (known limitation {d['known_limitation']})" if d.get("known_limitation") else ""
        L.append(f"| {d['id']} | {d['split']} | {d['verdict']}{tag} | {bad or '-'} |")
    k = c["known_limitation"]
    L += ["", "## Known limitations (not counted as fixed)", ""]
    if k["total"]:
        L += [f"{k['total']} tagged cases: {k['verdicts']['pass']} held the documented safe path, {k['verdicts']['fail']} fail, "
              f"{k['verdicts']['error']} error. They are not in the counts above.", "", "| case | split | decision | verdict |",
              "|---|---|---|---|"]
        L += [f"| {x['id']} | {x['split']} | {x['tag']} | {x['verdict']} |" for x in k["cases"]]
    else:
        L.append("None in this run.")
    L += ["", "## Checks (raw counts across cases)", "", "| check | pass | fail | error |", "|---|---|---|---|"]
    for name, v in r["checks"].items():
        L.append(f"| {name} | {v['pass']} | {v['fail']} | {v['error']} |")
    L += ["", "## Decision paths (did the run test the decision, or take the trivial path)", ""]
    if not r.get("paths"):
        L.append("No case in this run lists a path probe.")
    else:
        L += ["| case | probe | path | detail |", "|---|---|---|---|"]
        for p in r["paths"]:
            L.append(f"| {p['case']} | {p['probe']} | {'exercised' if p['exercised'] else 'NOT exercised'} | {p['detail']} |")
        L.append("")
        L.append("A pass with NOT exercised means the invariant held on the trivial path; it does not prove the decision.")
    L += ["", "## Gate 1 (retrieval verdict by label)", ""]
    g = r["gate1"]
    if g is None:
        L.append("Gate 1 was not run in this invocation.")
    else:
        L.append(f"{g['n']} labelled queries, searched at k = {g['k']} (the verdict does not depend on k).")
        for split, labels in g["confusion"].items():
            verdicts = sorted({v for cell in labels.values() for v in cell})
            L += ["", f"### {split}", "", "| label | " + " | ".join(verdicts) + " |", "|---|" + "---|" * len(verdicts)]
            for label, cell in sorted(labels.items()):
                L.append(f"| {label} | " + " | ".join(str(cell.get(v, 0)) for v in verdicts) + " |")
        L += ["", "### recall@k (answerable queries, gold chunk in the top k)", "", "| split | " + " | ".join(f"k={k}" for k in g["recall_at_k"]["train"]) + " |",
              "|---|" + "---|" * len(g["recall_at_k"]["train"])]
        for split, ks in g["recall_at_k"].items():
            L.append(f"| {split} | " + " | ".join(f"{v['hits']}/{v['n']}" for v in ks.values()) + " |")
        L += ["", f"Tier leaks (forbidden chunk returned): {g['tier_leaks'] or 'none'}", f"Failed searches: {g['failures'] or 'none'}"]
    return "\n".join(L) + "\n"


def write_report(r: dict[str, Any], out_dir: Path, now: dt.datetime) -> tuple[Path, Path]:
    """What: write golden-YYYY-MM-DD-HHMMSS.md and .json. Use when: the run finished. Inputs: report, directory, time.
    Output: the two paths. Side effects: creates the directory and writes two files. Role: the dated record."""
    import json
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"golden-{now.strftime('%Y-%m-%d-%H%M%S')}"
    md, js = out_dir / f"{stem}.md", out_dir / f"{stem}.json"
    md.write_text(render_markdown(r))
    js.write_text(json.dumps(r, indent=2, sort_keys=True) + "\n")
    return md, js

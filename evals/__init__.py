"""What: the golden runner package (CS-E-06.S1): `python -m evals run` drives the agent's HTTP API like the browser,
grades every golden case with code graders only, prints Gate 1 confusion counts and writes a dated report.
Use when: a measured pass rate is needed (doc 09 s5, doc 06 s19). Inputs: evals/golden/*.yaml, a running stack or a test double.
Output: report files under evals/reports. Side effects: none at import. Role: the evals lane's single entry point."""

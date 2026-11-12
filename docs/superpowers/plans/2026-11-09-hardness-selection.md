# Literature-based hardness selection implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adopt explicit literature-grounded case-selection rules and diagnose existing real cases with independently verified easy-instance certificates.

**Architecture:** Preserve source cases and graphs. A standalone local screening command loads only explicitly supplied summaries, verifies provenance, computes request coupling and exact reductions, and records certificates plus a structural search shortlist. Full multi-solver timing confirmation is a separate declared protocol, never falsely claimed by this screening command.

**Tech Stack:** Python, existing SciPy/NetworkX environment, JSON/CSV, pytest.

**Spec:** `configs/hardness_policy_v1.json`, `고난도_사례선정_확정기준.md`, and relevant sections of `레퍼런스.md`.

## Global Constraints

- User delegated method decisions and explicitly does not want more clarification or approval questions.
- Never execute QPU, authentication, notebook, or remote jobs. No credentials inspection.
- Do not manufacture requests or edges to make examples harder.
- Do not alter existing cases, original data, notebook, or historical results.
- Numerical thresholds are this study's declared choices, not literature-prescribed constants.
- Research input remains real; tiny artificial graph fixtures are software correctness tests only.
- No package installation for this structural screening task.

## Review Focus

- A missing within-request edge invalidates the request-count upper bound.
- A reduction certificate must lift to a valid independent set of the original graph.
- A stale summary/hash, malformed graph, or solver error must never be classified hard.
- A pairwise relaxation cannot certify full operational feasibility or difficulty.
- Empty residuals and all-easy collections must produce zero hard candidates, not a forced top percentile.

### Task 1: Structural diagnostics and certificate verification

**Files:** `bike_research/hardness.py`, `tests/test_hardness.py`.

**Interfaces:** graph/case JSON in; structure metrics, verified certificate and residual metrics out. Request clique bound requires all request groups to be cliques. Simplicial elimination selects a vertex whose open neighborhood is a clique, removes its closed neighborhood, and records an objective offset.

- [x] Write failing tests for disconnected cliques, connected request-bound graphs, simplicial reductions, unsolved residual, invalid selected IDs and missing clique edges.
- [x] Implement bounded exact reductions and independent certificate checking.
- [x] Run targeted tests and record results.

### Task 2: Read-only input screening command

**Files:** `select_hard_cases.py`, tests in `tests/test_hardness.py`.

**Interfaces:** `--summaries` explicit CSV paths; default policy `configs/hardness_policy_v1.json`; default output `outputs/hardness_v1`. Emit report.csv, selection.json, per-case certificates, policy/source hashes and manifest. Structural candidates are pending classical confirmation, never confirmed hard.

- [x] Test source SHA mismatch and report continuation, graph relaxation scope, all-easy collection.
- [x] Implement CLI and deterministic residual-size strata shortlist.
- [x] Run against the three existing summaries, preserving original data/results.

### Task 3: Adopt method and verify outputs

**Files:** `고난도_사례선정_확정기준.md`, additive note in `레퍼런스.md`, policy JSON.

- [x] Record source-backed decisions and researcher-defined thresholds.
- [x] Record screening results with no fabricated hardness claim.
- [x] Independently review implementation and run full pytest suite.
- [x] Report actual changes, measured findings, and which command stages are implemented.

## Execution ruling

Work directly in the user's requested workspace. User has delegated research decisions and requested code there; no additional design approval is needed. This task implements structural screening, not an unrequested paid run or the full future performance campaign. Git commits are not required for the requested local deliverables.

## Verification ledger

- Implementer observed missing-feature RED then targeted GREEN; final hardness tests 22 passed.
- Independent reviewer checked mathematics and methodological claims; fixes cover unhashed historical bounds, baseline eligibility, cross-request policy, malformed inputs, nonfinite sidecars and duplicate row shortlisting.
- Root full suite: **65 passed in 16.58s**. All review fixes have regression coverage.
- Final real-data audit: 37 certificate_easy, 36 request-clique bounds, 1 complete simplicial elimination; zero structural candidates. Root checked result count and policy SHA independently.
- Original cases, notebook and QPU code were preserved. No QPU/authentication/notebook execution; no new classical solver campaign or dependency installation.

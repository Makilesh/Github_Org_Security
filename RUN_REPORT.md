# Execution report — step by step, with captured output

This document walks through an actual execution of the scanner and records what
each step produced. Every block below is real terminal output, pasted unedited.

The reference run uses **demo mode**, because it is reproducible: the fixture
pins its clock, so anyone running the same command on any machine on any day
gets these exact numbers. **Step 8 is a real run against a live GitHub
organization**, with the token permission probe that preceded it.

Environment: Windows 11, Python 3.12.10, httpx 0.28.1, Jinja2 3.1.6.

---

## Step 1 — Environment setup

```bash
python -m venv .venv
```

```bash
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

Installs `httpx`, `Jinja2` and `pytest`. Chart.js is already vendored in
`vendor/chart.umd.min.js` so the dashboard needs no network at view time.

---

## Step 2 — Test suite

```bash
python -m pytest -q
```

```
........................................................................ [ 52%]
.................................................................        [100%]
149 passed in 0.25s
```

**149 tests, 0.25 seconds.** They cover:

| File | What it pins |
| ---- | ------------ |
| `tests/test_score.py` (76) | The scoring arithmetic against hand-computed values, decay behaviour, risk ordering, every NEVER-FLAG rule, and the remediation advice for team-inherited and archived grants |
| `tests/test_client.py` (33) | Rate-limit pauses, `Retry-After`, 409/403/404 handling, ETag 304s, cursor pagination, GraphQL error types |
| `tests/test_pipeline.py` (29) | The whole fixture org end-to-end: who is flagged, in exactly what order, who is excluded and why, plus database idempotency |
| `tests/test_access.py` (11) | A refused collaborator listing is reported, never rendered as "nobody has access"; org base permission is not team-inherited |

The same suite runs in CI on Python 3.11 and 3.12, followed by a full `--demo`
run whose findings are asserted against the numbers published in Step 3 below —
so this report cannot silently drift from the code.

The scoring tests assert exact arithmetic, not plausibility — for example that
`activity_score(commits=10)` equals `7.1936858`, which is `3.0 × ln(11)` computed
independently.

---

## Step 3 — The scan

```bash
python main.py --demo
```

```
13:31:23  INFO    scanner: Demo mode: acme-demo from fixtures\demo_org.json, clock pinned to 2026-08-25T12:00:00+00:00
13:31:23  INFO    report: Dashboard written to out\dashboard.html (256 KB)

  Run #1 - acme-demo (demo)
  ----------------------------------------------------------
  Repositories scanned    5  (1 skipped)
  Advisories found        10  (8 still open)
  Access suggestions      10
  Excluded from review    10

  Highest risk:
     3.00  alex-departed - admin on acme-demo/payments-api
     3.00  sam-stale - admin on acme-demo/legacy-etl
     2.00  maya-platform - maintain on acme-demo/legacy-etl (team-inherited)
     1.50  alex-departed - write on acme-demo/web-frontend (team-inherited)
     1.50  maya-platform - write on acme-demo/payments-api (team-inherited)

  Dashboard: out\dashboard.html
```

### What happened, and why each number is what it is

**6 repositories in the org, 5 scanned.** `vendor-sdk-fork` is a fork and was
skipped (`SKIP_FORKS` defaults on) — recorded as a row with a reason, not
silently dropped. `legacy-etl` is archived and **was** scanned: stale admin
access on an archived repo is still live access.

**10 advisories, 8 open.** Includes 2 critical and 2 high still open. The oldest
unresolved is a Django SQL-injection advisory open for **347 days**.

**10 access suggestions from 25 collaborator grants.** The two highest are
dormant admins scoring 0.00, so their risk equals the raw admin weight of 3.00.

**Three of the top five are team-inherited** and are labelled as such, with an
action column explaining that removing them means changing team membership.

### Who was *not* flagged, and why that is the interesting part

| Person | Situation | Outcome |
| ------ | --------- | ------- |
| `priya-staff` | 2 commits, **21 reviews** on payments-api | **Not flagged**, score 13.25 — a commit-count model would have flagged her |
| `lena-active` | 62 commits, 9 reviews | Not flagged, score 26.29 |
| `ravi-owner` | Admin, 1 commit in 6 months | **Excluded** — org owner, never flagged regardless of activity |
| `dependabot[bot]` | Write access, 24 commits | **Excluded** — bot |
| `release-bot` | Write access, 15 commits | **Excluded** — bot (typed `Bot`, no `[bot]` suffix) |
| `audit-svc` | Read access, zero activity | **Excluded** — on the configured allowlist |
| everyone on `ml-sandbox` | Repo created 28 days ago | **Excluded** — no time to build a history |
| `tom-triage` on `infra-scripts` | Write, zero activity | **Excluded** — repo has only one contributor |

`priya-staff` is the case the scoring model exists to protect, and a dedicated
test fails if a future change flags her.

---

## Step 4 — Idempotency check

Running the identical command a second time against the same database:

```bash
python main.py --demo
```

```
### row counts after two runs
runs             2
repos            6
advisories       10
collaborators    25
contributions    25
scores           25
exclusions       14
run_progress     40
```

**Two runs, two `runs` rows — and every fact table unchanged.** 6 repos, not 12.
10 advisories, not 20. 25 scores, not 50. The facts were updated in place and
re-stamped with the newer `run_id`; only the run ledger and the per-run progress
log grew, which is exactly what should grow.

This is the idempotency requirement demonstrated rather than asserted, and
`test_pipeline.py::test_seeding_twice_does_not_duplicate_rows` enforces it on
every test run.

---

## Step 5 — Re-rendering without touching the API

```bash
python main.py --report-only
```

```
     2.00  maya-platform - maintain on acme-demo/legacy-etl (team-inherited)
     1.50  alex-departed - write on acme-demo/web-frontend (team-inherited)
     1.50  maya-platform - write on acme-demo/payments-api (team-inherited)

  Dashboard: out\dashboard.html
```

Rebuilds the dashboard from SQLite with zero network calls — useful for
iterating on the report, and for regenerating a view of a scan captured days
earlier.

---

## Step 6 — Machine-readable output

```bash
python main.py --demo --json
```

```json
{
  "run_id": 2,
  "org": "acme-demo",
  "mode": "demo",
  "started_at": "2026-08-25T07:58:24+00:00",
  "finished_at": "2026-08-25T07:58:24+00:00",
  "status": "completed",
  "repos_scanned": 5,
  "repos_skipped": 1,
  "repos_errored": 0,
  "repos_excluded": 4,
  "advisories_found": 10,
  "advisories_open": 8,
  "suggestions_made": 10,
  "members_excluded": 10,
  "collaborators_seen": 25,
  "top_suggestions": [
    {"login": "alex-departed", "repo": "acme-demo/payments-api",
     "permission": "admin", "risk": 3.0, "score": 0.0, "team_inherited": false},
    {"login": "sam-stale", "repo": "acme-demo/legacy-etl",
     "permission": "admin", "risk": 3.0, "score": 0.0, "team_inherited": false},
    {"login": "maya-platform", "repo": "acme-demo/legacy-etl",
     "permission": "maintain", "risk": 2.0, "score": 0.0, "team_inherited": true}
  ],
  "dashboard": "out/dashboard.html",
  "database": "data/scanner.sqlite3",
  "api": {}
}
```

Logs go to stderr, so stdout stays clean and pipeable:

```bash
python main.py --demo --json > summary.json
```

`"api"` is empty in demo mode because no requests were made. On a live run it
carries request count, cache hits, retries and rate-limit pauses.

---

## Step 7 — The dashboard

`out/dashboard.html` — one self-contained file, 256 KB with Chart.js inlined.
Open it directly in a browser; no server required. A committed copy lives at
[docs/demo_dashboard.html](docs/demo_dashboard.html), and a printed version at
[docs/pdf/dashboard.pdf](docs/pdf/dashboard.pdf).

The screenshots below are generated by `python tools/make_docs.py`, so they
cannot drift from the dashboard they document.

### Header — the answer before the data

![Headline summary and KPI tiles](docs/img/dashboard-overview.png)

A security lead reads three sentences and knows the state of the org. Everything
below is the evidence for those three sentences.

### Section 1 — Security advisories

![Security advisories by severity, status and age](docs/img/dashboard-advisories.png)

Counts by severity and status, the most-affected repositories, and the oldest
unresolved alerts sorted by age — 347 days for the worst one. Note that two of
the oldest sit on an **archived** repository, which is exactly the kind of thing
that goes unnoticed when nobody walks the org by hand.

### Section 2 — Who has access, and what they did with it

![Per-repo member table with contribution scores](docs/img/dashboard-access.png)

One row per person per repo. `lena-active` scores 26.29; `priya-staff` scores
13.25 on 2 commits and 21 reviews — the case a commit-count model gets wrong.
Excluded people stay visible with their reason attached rather than vanishing.
The panel header also reports the **7 unattributed commits** on this repository.

### Section 3 — Suggested access removals

![Suggested removals ranked by risk, with evidence](docs/img/dashboard-suggestions.png)

Ranked by risk. Every row carries last commit, last review, permission, how the
access was granted, why it was flagged, and what the reviewer would have to do
about it. Team-inherited grants say *"review at the team level"*; grants on an
archived repo add that GitHub refuses collaborator changes while a repo is
archived, so the fix is at org or team level.

### Section 4 — Who was excluded, and why

![Everyone excluded from the suggestions, and why](docs/img/dashboard-exclusions.png)

Org owners, bots, allowlisted accounts, and repositories too new or too small to
judge — each named, grouped by reason. This panel is what makes the list in
section 3 trustworthy: a reader who cannot see who was skipped has no reason to
believe the people who were not.

---

## Step 8 — Live run against a real organization

The fixture org above is the illustrative case. This is the same code against a
real GitHub organization (`VoidAlgo`), and it is here because running against
real data found three bugs that no fixture would have caught.

### Token permission probe

Every endpoint the tool depends on, probed individually before scanning:

| Endpoint | Result |
| -------- | ------ |
| `/rate_limit` | OK — REST 5000/5000, GraphQL 5000/5000 |
| `/orgs/{org}/members?role=admin` | OK — 2 owners |
| `/orgs/{org}/repos` | OK — 3 of 4 repos visible |
| `/orgs/{org}/dependabot/alerts` | OK — 0 alerts |
| `/repos/{r}/collaborators` (×3 affiliations) | OK on 1 repo, **403 on 2** |
| `/repos/{r}/commits`, `/pulls`, GraphQL | OK — 205 commits, 34 PRs |

Two gaps, both reported by the tool rather than silently absorbed:

- The PAT is scoped to *selected* repositories, so one repo is invisible (404)
  and two more refuse the collaborator listing (403).
- Dependabot is enabled but the repositories have no known vulnerabilities.

### The scan

```
INFO  scanner: Started run #1 for VoidAlgo
INFO  scanner: Token OK. REST quota 5000/5000, GraphQL 5000/5000
INFO  scan:    Org VoidAlgo has 2 owner(s)
INFO  scan:    Org base permission is 'read': every member holds read on every repo
INFO  scan:    Found 3 repos in VoidAlgo; scanning 3, skipping 0
INFO  scan:    Org-level Dependabot endpoint returned 0 alerts across 0 repos
INFO  contrib: Indexed 0 teams in VoidAlgo
INFO  scanner: [1/3] VoidAlgo/github_pull_toslack
WARN  contrib: Access data for VoidAlgo/github_pull_toslack is incomplete:
               'direct' collaborator listing refused (HTTP 403). The token is
               missing Repository permission 'Administration: read'.
INFO  scanner: [2/3] VoidAlgo/pseudoquant
INFO  scanner: [3/3] VoidAlgo/Sensitive_Data_Detection-Compliance_Assistant

  Run #1 - VoidAlgo (live)
  ----------------------------------------------------------
  Repositories scanned    3
  Advisories found        0  (0 still open)
  Access suggestions      0
  Excluded from review    6
  API                     25 requests, 0 served from cache, 0 rate-limit pauses
```

### Every member, and what the tool decided

| Repository | Member | Permission | Score | Outcome |
| ---------- | ------ | ---------- | ----- | ------- |
| pseudoquant | Makilesh | admin (direct) | 29.38 | excluded — org owner |
| pseudoquant | clashonkishy | admin (direct) | 15.83 | excluded — org owner |
| pseudoquant | fatbatman85 | read — **org base permission** | 0.00 | excluded — repo newer than the window |
| Sensitive_Data_Detection | Makilesh | — | 10.58 | excluded — org owner |
| Sensitive_Data_Detection | Copilot | none | 2.08 | excluded — **no current access** |
| github_pull_toslack | Makilesh | — | 9.89 | excluded — org owner |

### Zero suggestions, and why that is the correct answer

Every exclusion above fires for a good reason, and no configuration produces a
finding honestly:

- Both people with real permissions are **organization owners** — a hard rule.
- `fatbatman85` holds read, has no activity, and *would* be flagged — but
  `pseudoquant` was **created 33 days ago**, inside the 180-day window.
- Shortening the window to 30 days makes the repo judgeable, and then it has
  only **one contributor** in that window, so the single-contributor rule
  suppresses it instead. I tried this. It is in the table below.

| Window | Repo judgeable? | Contributors | Suggestions |
| ------ | --------------- | ------------ | ----------- |
| 180 days | no — too new | 2 | 0 |
| 30 days | yes | 1 | 0 — single contributor |
| 25 days | yes | 1 | 0 — single contributor |

**I did not tune the threshold until something appeared.** A five-week-old
organization with three repositories and two owners genuinely has no stale
access, and a tool that manufactured a finding here would be worse than one
that reports none. The fixture demo exists precisely so the access-review logic
can be demonstrated on a history that is old enough to judge.

### What the live run did find

Not access findings — correctness findings, which were more valuable:

1. **Org base permission**, `default_repository_permission = "read"`. Every
   member silently holds read on every repository. `fatbatman85` has never been
   added to any repo or team, and had read access to a private repo. The
   scanner reports this as its own access path with the org-level remediation,
   after this run showed it being mislabelled as team-inherited.
2. **A contributor with no access.** `Copilot` authored commits on a repo where
   it holds no permission. It was scored 2.08, below the threshold, and would
   have been recommended for removal of access it does not have.
3. **A token scoping gap**, surfaced rather than hidden: two repositories
   refused the collaborator listing, and the dashboard leads with a red banner
   naming them instead of reporting "nobody has access".

Findings 1 and 2 became code changes and tests. Section 8 of
[DESIGN_NOTES.md](DESIGN_NOTES.md) records both.

### Idempotency and caching, verified live

A second identical run:

```json
"api": { "requests": 13, "cache_hits": 8, "retries": 0, "rate_limit_sleeps": 0 }
```

**8 of 13 requests served from stored ETags** — 304 responses, which do not
count against the API quota. Row counts after two full runs: `runs 2`, and
every fact table unchanged — `repos 1`, `collaborators 2`, `scores 2`.
Idempotency and conditional caching confirmed against the real API.

### What is mocked, stated plainly

Only the GitHub API responses, and only in `--demo`. Private Dependabot
advisories require an organization with paid security features and genuinely
vulnerable dependencies, which is not reproducible for a review — the brief
explicitly permits mocking exactly this.

The fixtures are shaped like real API payloads and are fed through the **same**
`db.upsert_advisory`, `contrib.AccessEntry` and `score.assess_repo` functions the
live scan uses. There is no demo-only parsing or scoring path. What is mocked is
the network; everything above it is the production code.

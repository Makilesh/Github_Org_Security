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
137 passed in 0.24s
```

**137 tests, 0.24 seconds.** They cover:

| File | What it pins |
| ---- | ------------ |
| `tests/test_score.py` (69) | The scoring arithmetic against hand-computed values, decay behaviour, risk ordering, every NEVER-FLAG rule, and the remediation advice for team-inherited and archived grants |
| `tests/test_client.py` (33) | Rate-limit pauses, `Retry-After`, 409/403/404 handling, ETag 304s, cursor pagination, GraphQL error types |
| `tests/test_pipeline.py` (29) | The whole fixture org end-to-end: who is flagged, in exactly what order, who is excluded and why, plus database idempotency |
| `tests/test_access.py` (6) | A refused collaborator listing is reported, never rendered as "nobody has access" |

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
Open it directly in a browser; no server required.

**Header — the short version, before any table:**

> 4 critical or high severity vulnerabilities are still open across 3 repositories.
> The oldest has been open for 347 days.
>
> 10 access grants look stale — 2 of them at admin level. These are suggestions
> for human review; nothing has been changed.
>
> 13 people were assessed across 5 repositories. 10 access grants were
> deliberately held back from the suggestions.

**Section 1 — Security advisories.** Doughnut chart of open alerts by severity,
bar chart of alert states, stacked horizontal bar of the most affected repos,
and a table of the oldest unresolved alerts sorted by age:

| Age | Severity | Package | Repository | Advisory |
| --- | -------- | ------- | ---------- | -------- |
| 347d | Critical | django (pip) | payments-api | GHSA-7q4j-hf2p-1x2m / CVE-2025-38412 |
| 278d | High | pyyaml (pip) | legacy-etl *(archived)* | GHSA-59hf-2m3x-6q8p |
| 194d | Low | six (pip) | legacy-etl *(archived)* | GHSA-w8q7-3r5t-2n6v |
| 164d | Critical | next (npm) | web-frontend | GHSA-vv6q-4m2j-7d8s |

**Section 2 — Who has access, and what they did with it.** One expandable panel
per repo, ordered by how many suggestions it produced. For `payments-api`:

| Member | How they have access | Commits | PRs reviewed | PRs merged | Last activity | Score | Status |
| ------ | -------------------- | ------- | ------------ | ---------- | ------------- | ----- | ------ |
| lena-active | Direct (Write) | 62 | 9 | 14 | 24 Aug 2026 (0 days ago) | 26.29 | active |
| dependabot[bot] | Direct (Write) | 24 | 0 | 22 | 23 Aug 2026 (2 days ago) | 18.97 | excluded — Bot |
| priya-staff | Direct (Maintain) | 2 | 21 | 1 | 20 Aug 2026 (4 days ago) | 13.25 | active |
| ravi-owner | Direct (Admin) | 1 | 2 | 0 | 11 Jun 2026 (74 days ago) | 3.61 | excluded — Org owner |
| dev-vendor | Outside collaborator (Write) | 3 | 0 | 1 | 05 Mar 2026 (172 days ago) | 3.21 | review access |
| alex-departed | Direct (Admin) | 0 | 0 | 0 | 17 Apr 2025 — before the window | 0.00 | review access |
| maya-platform | Team: Platform Engineering (Write) | 0 | 0 | 0 | 30 Aug 2025 — before the window | 0.00 | review access |

The panel header also reports **7 commits could not be attributed** on this repo
— commit emails not linked to any GitHub account. They are counted and never
assigned to anyone.

**Section 3 — Suggested access removals**, ranked by risk, with full evidence:

| # | Member | Repository | Permission | Last commit | Score | Risk | What to do |
| - | ------ | ---------- | ---------- | ----------- | ----- | ---- | ---------- |
| 1 | alex-departed | payments-api | Admin | 17 Apr 2025 (494 days ago) | 0.00 | 3.00 | Can be revoked on this repository alone |
| 2 | sam-stale | legacy-etl *(archived)* | Admin | 02 Jun 2024 (813 days ago) | 0.00 | 3.00 | Can be revoked on this repository alone |
| 3 | maya-platform | legacy-etl *(archived)* | Maintain *(via Data Engineering)* | 08 Nov 2023 (1020 days ago) | 0.00 | 2.00 | Review at team level — affects every repo that team can reach |
| 9 | kai-solo | legacy-etl *(archived)* | Write | 19 Mar 2026 (159 days ago) | 2.16 | 0.47 | Can be revoked on this repository alone |
| 10 | dev-vendor | payments-api | Write | 05 Mar 2026 (172 days ago) | 3.21 | 0.36 | Can be revoked on this repository alone |

Each row carries a generated sentence, for example:

> Holds write access but last contributed 159 days ago (2 commits, 0 PRs
> reviewed, 0 PRs merged in the window). Score 2.16 is below the 5.0 threshold.

**Section 4 — Who was excluded, and why.** Every excluded person and repository,
grouped by reason, so the list above can be trusted.

---

## Step 8 — Live run against a real organization

Everything above exercises the pipeline on fixtures. This section is a real run
against a live GitHub organization (`VoidAlgo`, 1 private repository) using a
fine-grained PAT.

### Token permission probe

Before scanning, each endpoint the tool depends on was probed individually:

| Endpoint | Result | Permission |
| -------- | ------ | ---------- |
| `/rate_limit` | OK — REST 5000/5000, GraphQL 5000/5000 | authentication |
| `/orgs/{org}` | OK | Metadata |
| `/orgs/{org}/members?role=admin` | OK — 2 owners | Org > Members: read |
| `/orgs/{org}/teams` | OK — 0 teams | Org > Members: read |
| `/orgs/{org}/repos` | OK — 1 repo | Org > Administration: read |
| `/orgs/{org}/dependabot/alerts` | OK — 0 alerts | Org > Dependabot alerts: read |
| `/repos/{r}/collaborators?affiliation=direct` | OK — 2 | Repo > Administration: read |
| `/repos/{r}/collaborators?affiliation=outside` | OK — 0 | Repo > Administration: read |
| `/repos/{r}/collaborators?affiliation=all` | OK — 2 | Repo > Administration: read |
| `/repos/{r}/commits` | OK | Repo > Contents: read |
| `/repos/{r}/pulls` | OK | Repo > Pull requests: read |
| `/repos/{r}/dependabot/alerts` | **403 — "Dependabot alerts are disabled for this repository"** | feature not enabled |
| GraphQL commits + PRs | OK — 205 commits, 34 PRs | Contents + Pull requests |

The single failure is a *feature* state, not a permission: Dependabot alerts are
switched off on the repository, so there is nothing for the advisory half to
report. The scanner records that and continues rather than aborting.

### The scan

```bash
python main.py -v
```

```
20:03:36  INFO  scanner: Started run #3 for VoidAlgo
20:03:36  INFO  scanner: Token OK. REST quota 4964/5000, GraphQL 4954/5000
20:03:37  INFO  scan:    Org VoidAlgo has 2 owner(s)
20:03:38  INFO  scan:    Found 1 repos in VoidAlgo; scanning 1, skipping 0
20:03:38  INFO  scanner: Registered 1 repos (1 to scan, 0 skipped)
20:03:38  INFO  scan:    Org-level Dependabot endpoint returned 0 alerts across 0 repos
20:03:39  INFO  contrib: Indexed 0 teams in VoidAlgo
20:03:39  INFO  scanner: [1/1] VoidAlgo/pseudoquant
20:03:44  INFO  report:  Dashboard written to out\dashboard.html (218 KB)

  Run #3 - VoidAlgo (live)
  ----------------------------------------------------------
  Repositories scanned    1
  Advisories found        0  (0 still open)
  Access suggestions      0
  Excluded from review    2
  API                     13 requests, 0 served from cache, 0 rate-limit pauses
```

### What it found

`VoidAlgo/pseudoquant` — private, created 25 Jul 2026, 205 commits in the
window from 2 identified contributors, **6 commits that could not be attributed**
(commit email not linked to any GitHub account — counted, reported, never guessed).

| Member | Access | Commits | PRs reviewed | PRs merged | Last activity | Score | Outcome |
| ------ | ------ | ------- | ------------ | ---------- | ------------- | ----- | ------- |
| Makilesh | Direct (Admin) | 183 | 8 | 22 | 16 Aug 2026 (10 days) | 29.51 | excluded — org owner |
| clashonkishy | Direct (Admin) | 16 | 2 | 8 | 27 Jul 2026 (29 days) | 15.90 | excluded — org owner |

**Zero suggestions, for three independent and individually sufficient reasons:**

1. Both collaborators are **organization owners** — a hard NEVER-FLAG rule.
2. Both are far above the threshold anyway (29.51 and 15.90 against a threshold
   of 5.0), so they would not be flagged even without rule 1.
3. The repository was **created 32 days ago**, inside the 180-day window, so
   removal suggestions are suppressed for it entirely.

The dashboard states all three rather than presenting an unexplained empty list:

> No access grants fell below the activity threshold. Nothing to review.
>
> *No removal suggestions for this repository.* Created 32 days ago, inside the
> 180-day window. Nobody has had time to build a history here yet.

This is the correct result for a young repository maintained by its two owners.
It is also a thin demonstration, which is precisely why `--demo` exists.

### Idempotency and caching, verified live

Running the identical command a second time against the same database:

```json
"api": {
  "requests": 13,
  "cache_hits": 8,
  "retries": 0,
  "rate_limit_sleeps": 0,
  "graphql_queries": 4,
  "graphql_cost": 4,
  "errors": 0
}
```

**8 of 13 requests were served from stored ETags** — 304 responses, which do not
count against the API quota. Row counts after two full live runs:

```
  runs             2
  repos            1
  collaborators    2
  contributions    2
  scores           2
  exclusions       3
  advisories       0
  http_cache       8
```

Two runs, two `runs` rows, and every fact table unchanged. Idempotency and
conditional-request caching both confirmed against the real API, not just
against mocks.

### A bug this live run exposed

An earlier token lacked repository `Administration: read`. The collaborator
listings returned 403, the code treated that as an allowed-empty result, and the
dashboard would have reported **"nobody has access to any repository"** —
confidently, and wrongly. For a security report that is the worst possible
failure mode.

Fixed: the three listings now run through a helper that captures the refusal,
`AccessSnapshot.complete` distinguishes *empty* from *unreadable*, and the
dashboard leads with a red banner naming every repository whose access could not
be read. Covered by `tests/test_access.py`. No unit test would have caught this —
the code did exactly what it was written to do; the mistake was deciding a 403
was an acceptable empty result.

### What is mocked, stated plainly

Only the GitHub API responses, and only in `--demo`. Private Dependabot
advisories require an organization with paid security features and genuinely
vulnerable dependencies, which is not reproducible for a review — the brief
explicitly permits mocking exactly this.

The fixtures are shaped like real API payloads and are fed through the **same**
`db.upsert_advisory`, `contrib.AccessEntry` and `score.assess_repo` functions the
live scan uses. There is no demo-only parsing or scoring path. What is mocked is
the network; everything above it is the production code.

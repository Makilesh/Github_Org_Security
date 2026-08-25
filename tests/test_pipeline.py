"""End-to-end tests over the fixture organization.

These pin the *whole* result, not just individual formulas: which people get
flagged on which repos, in exactly which order, and who is excluded and why.
If a refactor quietly changes a weight, a tie-break or an exclusion rule, this
file fails with a readable diff.

The fixture pins its own clock (`as_of`), so these assertions do not rot.
"""

from __future__ import annotations

import json

import pytest

import config
import demo
from config import ExclusionReason
from db import Database, RunStatus
from score import assess_repo, collect_exclusions, rank_suggestions


@pytest.fixture(scope="module")
def org():
    return demo.load_org()


@pytest.fixture(scope="module")
def assessments(org):
    results = []
    for repo_input, members, _extras in demo.iter_assessable(org):
        results.append(
            assess_repo(
                repo_input, members,
                org_owners=org.owners, allowlist=org.allowlist, now=org.as_of,
            )
        )
    return results


@pytest.fixture(scope="module")
def by_repo(assessments):
    return {a.full_name.split("/")[-1]: a for a in assessments}


# --------------------------------------------------------------------------
# Repo selection
# --------------------------------------------------------------------------

def test_forks_are_skipped_by_default(org, by_repo):
    assert "vendor-sdk-fork" not in by_repo
    assert any(r["name"] == "vendor-sdk-fork" for r in org.repos), "fixture still has the fork"


def test_archived_repo_is_still_scanned(by_repo):
    """The point: stale admin access on an archived repo is still stale access."""
    assert "legacy-etl" in by_repo
    assert by_repo["legacy-etl"].repo_exclusion is None


def test_five_repos_assessed(by_repo):
    assert set(by_repo) == {
        "payments-api", "web-frontend", "legacy-etl", "ml-sandbox", "infra-scripts"
    }


# --------------------------------------------------------------------------
# Per-repo outcomes
# --------------------------------------------------------------------------

def test_payments_api_flags(by_repo):
    flagged = {m.login for m in by_repo["payments-api"].flagged}
    assert flagged == {
        "alex-departed",   # admin, no activity at all
        "sam-stale",       # write, last commit predates the window
        "tom-triage",      # triage, never contributed
        "maya-platform",   # write, inherited from Platform Engineering
        "dev-vendor",      # outside collaborator, 3 commits 173 days ago
    }


def test_payments_api_exclusions(by_repo):
    excluded = {m.login: m.excluded_reason for m in by_repo["payments-api"].excluded}
    assert excluded == {
        "ravi-owner": ExclusionReason.ORG_OWNER,
        "dependabot[bot]": ExclusionReason.BOT,
        "audit-svc": ExclusionReason.ALLOWLISTED,
    }


def test_active_engineers_are_left_alone(by_repo):
    safe = {m.login for m in by_repo["payments-api"].scored
            if not m.flagged and not m.excluded}
    assert safe == {"lena-active", "priya-staff"}


def test_review_heavy_engineer_survives_on_both_repos(by_repo):
    """priya-staff commits almost nothing and must still never be flagged."""
    for repo in ("payments-api", "web-frontend"):
        priya = next(m for m in by_repo[repo].scored if m.login == "priya-staff")
        assert not priya.flagged, f"review-only contributor flagged on {repo}"
        assert priya.commits <= 2


def test_web_frontend_flags_only_the_departed_team_member(by_repo):
    assert {m.login for m in by_repo["web-frontend"].flagged} == {"alex-departed"}


def test_bot_with_heavy_commit_volume_is_excluded_not_credited(by_repo):
    release_bot = next(m for m in by_repo["web-frontend"].scored
                       if m.login == "release-bot")
    assert release_bot.excluded_reason == ExclusionReason.BOT
    assert not release_bot.flagged


def test_legacy_etl_flags(by_repo):
    assert {m.login for m in by_repo["legacy-etl"].flagged} == {
        "sam-stale", "tom-triage", "maya-platform", "kai-solo"
    }


def test_new_repo_suppresses_all_suggestions(by_repo):
    sandbox = by_repo["ml-sandbox"]
    assert sandbox.repo_exclusion == ExclusionReason.NEW_REPO
    assert sandbox.flagged == []
    alex = next(m for m in sandbox.scored if m.login == "alex-departed")
    assert alex.score == 0.0 and alex.excluded_reason == ExclusionReason.NEW_REPO


def test_single_contributor_repo_suppresses_all_suggestions(by_repo):
    solo = by_repo["infra-scripts"]
    assert solo.repo_exclusion == ExclusionReason.SINGLE_CONTRIBUTOR
    assert solo.flagged == []
    tom = next(m for m in solo.scored if m.login == "tom-triage")
    assert tom.excluded_reason == ExclusionReason.SINGLE_CONTRIBUTOR


# --------------------------------------------------------------------------
# The suggestion list as a whole
# --------------------------------------------------------------------------

EXPECTED_RANKING = [
    ("alex-departed", 101),   # admin,           risk 3.00
    ("sam-stale", 103),       # admin, archived, risk 3.00
    ("maya-platform", 103),   # maintain (team), risk 2.00
    ("alex-departed", 102),   # write (team),    risk 1.50
    ("maya-platform", 101),   # write (team),    risk 1.50
    ("sam-stale", 101),       # write,           risk 1.50
    ("tom-triage", 101),      # triage,          risk 0.50
    ("tom-triage", 103),      # read,            risk 0.50
    ("kai-solo", 103),        # write, faint recent activity
    ("dev-vendor", 101),      # outside write, 173 days stale
]


def test_ranking_matches_expected_order(assessments):
    ranked = rank_suggestions(assessments)
    assert [(m.login, m.repo_id) for m in ranked] == EXPECTED_RANKING


def test_ten_suggestions_total(assessments):
    assert len(rank_suggestions(assessments)) == 10


def test_risks_are_monotonically_non_increasing(assessments):
    risks = [m.risk for m in rank_suggestions(assessments)]
    assert risks == sorted(risks, reverse=True)


def test_highest_risk_is_a_dormant_admin(assessments):
    top = rank_suggestions(assessments)[0]
    assert top.permission == "admin"
    assert top.risk == 3.0
    assert top.score == 0.0


def test_every_suggestion_carries_an_explanation(assessments):
    for suggestion in rank_suggestions(assessments):
        assert suggestion.reason
        assert "threshold" in suggestion.reason


def test_team_only_suggestions_say_how_to_act_on_them(assessments):
    team_flags = [m for m in rank_suggestions(assessments) if m.is_team_only]
    assert {m.login for m in team_flags} == {"maya-platform", "alex-departed"}
    for flag in team_flags:
        assert "team level" in flag.removal_note
        assert flag.teams


def test_direct_and_team_access_are_not_conflated(assessments):
    """alex-departed holds direct access on one repo and team access on another."""
    ranked = rank_suggestions(assessments)
    direct = next(m for m in ranked if m.login == "alex-departed" and m.repo_id == 101)
    inherited = next(m for m in ranked if m.login == "alex-departed" and m.repo_id == 102)
    assert not direct.is_team_only
    assert inherited.is_team_only
    assert "Platform Engineering" in inherited.removal_note


def test_scoring_is_deterministic(org):
    """Two identical runs must produce identical numbers, to the last decimal."""
    def run():
        return [
            (m.login, m.repo_id, round(m.score, 12), round(m.risk, 12))
            for m in rank_suggestions([
                assess_repo(r, ms, org_owners=org.owners,
                            allowlist=org.allowlist, now=org.as_of)
                for r, ms, _ in demo.iter_assessable(org)
            ])
        ]

    assert run() == run()


def test_exclusion_panel_covers_every_skipped_person(assessments):
    excluded = collect_exclusions(assessments)
    reasons = {m.excluded_reason for m in excluded}
    assert reasons == {
        ExclusionReason.ORG_OWNER,
        ExclusionReason.BOT,
        ExclusionReason.ALLOWLISTED,
        ExclusionReason.NEW_REPO,
        ExclusionReason.SINGLE_CONTRIBUTOR,
    }
    assert all(m.excluded_detail for m in excluded), "every exclusion needs a reason to show"


def test_no_org_owner_appears_in_any_suggestion(assessments, org):
    logins = {m.login.lower() for m in rank_suggestions(assessments)}
    assert not (logins & org.owners)


def test_no_bot_appears_in_any_suggestion(assessments):
    logins = {m.login.lower() for m in rank_suggestions(assessments)}
    assert not any(login.endswith("[bot]") or login == "release-bot" for login in logins)


# --------------------------------------------------------------------------
# Storage: idempotency and resume
# --------------------------------------------------------------------------

def test_seeding_twice_does_not_duplicate_rows(tmp_path, org):
    """The idempotency requirement, exercised through the real upserts."""
    path = tmp_path / "scan.sqlite3"

    with Database(path) as db:
        run_one = db.start_run(org.org, config.summary())
        demo.seed_database(db, run_one, org)
        first = db.run_counts(run_one)
        db.finish_run(run_one, RunStatus.COMPLETED)

    with Database(path) as db:
        run_two = db.start_run(org.org, config.summary())
        demo.seed_database(db, run_two, org)
        second = db.run_counts(run_two)

        assert first["repos_scanned"] == second["repos_scanned"] == 5
        assert first["advisories_found"] == second["advisories_found"] == 10
        assert db.count("repos") == 6           # 5 scanned + 1 skipped fork
        assert db.count("advisories") == 10
        assert db.count("runs") == 2
        # Rows were updated in place and re-stamped with the newer run.
        assert db.query_one(
            "SELECT COUNT(*) AS n FROM advisories WHERE run_id = ?", (run_two,)
        )["n"] == 10


def test_advisory_counts_by_state(tmp_path, org):
    with Database(tmp_path / "scan.sqlite3") as db:
        run_id = db.start_run(org.org, config.summary())
        demo.seed_database(db, run_id, org)

        assert db.count("advisories", "state = 'open'") == 8
        assert db.count("advisories", "state = 'fixed'") == 1
        assert db.count("advisories", "state = 'dismissed'") == 1
        assert db.count("advisories", "severity = 'critical' AND state = 'open'") == 2

        oldest = db.query_one(
            "SELECT ghsa_id, created_at FROM advisories WHERE state = 'open' "
            "ORDER BY created_at ASC LIMIT 1"
        )
        assert oldest["ghsa_id"] == "GHSA-7q4j-hf2p-1x2m"


def test_three_access_paths_survive_a_round_trip(tmp_path, org):
    with Database(tmp_path / "scan.sqlite3") as db:
        run_id = db.start_run(org.org, config.summary())
        demo.seed_database(db, run_id, org)

        maya = db.query_one(
            "SELECT * FROM collaborators WHERE repo_id = 101 AND login = 'maya-platform'"
        )
        assert maya["is_team"] == 1 and maya["is_direct"] == 0 and maya["is_outside"] == 0
        assert maya["team_names"] == "Platform Engineering"

        vendor = db.query_one(
            "SELECT * FROM collaborators WHERE repo_id = 101 AND login = 'dev-vendor'"
        )
        assert vendor["is_outside"] == 1 and vendor["is_direct"] == 0

        lena = db.query_one(
            "SELECT * FROM collaborators WHERE repo_id = 101 AND login = 'lena-active'"
        )
        assert lena["is_direct"] == 1 and lena["permission_direct"] == "write"


def test_unattributed_commits_are_recorded_not_guessed(tmp_path, org):
    with Database(tmp_path / "scan.sqlite3") as db:
        run_id = db.start_run(org.org, config.summary())
        demo.seed_database(db, run_id, org)

        stats = db.query_one("SELECT * FROM repo_stats WHERE repo_id = 101")
        assert stats["unattributed_commits"] == 7
        total = db.query_one(
            "SELECT SUM(unattributed_commits) AS n FROM repo_stats"
        )["n"]
        assert total == 22

        # And nobody was credited with them.
        credited = db.query_one(
            "SELECT SUM(commits) AS n FROM contributions WHERE repo_id = 101"
        )["n"]
        assert credited == 92   # 62 + 2 + 1 + 24 + 3, with the 7 unattributed left out


def test_resume_skips_completed_repos(tmp_path, org):
    from db import Stage

    with Database(tmp_path / "scan.sqlite3") as db:
        run_id = db.start_run(org.org, config.summary())
        demo.seed_database(db, run_id, org)
        done = db.completed_repo_ids(run_id, Stage.ADVISORIES)
        assert done == {101, 102, 103, 104, 105}
        assert 106 not in done      # the fork was never scanned
        assert db.stage_done(run_id, 101, Stage.CONTRIBUTIONS)


def test_config_summary_is_json_serialisable_and_tokenless():
    payload = json.dumps(config.summary())
    assert "token" not in payload.lower()
    assert json.loads(payload)["scoring"]["threshold"] == 5.0

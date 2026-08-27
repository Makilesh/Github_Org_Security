"""Tests for the scoring model and the NEVER FLAG rules.

Everything here is a pure function of its inputs - no network, no database,
no wall clock - so the assertions are on exact arithmetic rather than on
"roughly plausible" behaviour. The hand-computed values below are the ones a
reviewer can check with a calculator.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import log1p

import pytest

from config import ExclusionReason, ScoringConfig
from score import (
    activity_score,
    assess_repo,
    collect_exclusions,
    contribution_score,
    days_between,
    decay_factor,
    explain_flag,
    is_bot_login,
    member_exclusion,
    MemberInput,
    rank_suggestions,
    repo_exclusion,
    RepoInput,
    risk_score,
    score_member,
)

NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)
CFG = ScoringConfig()


def days_ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


# --------------------------------------------------------------------------
# The activity formula
# --------------------------------------------------------------------------

class TestActivityScore:
    def test_zero_activity_scores_zero(self):
        assert activity_score(0, 0, 0, 0, CFG) == 0.0

    def test_matches_the_specified_formula_exactly(self):
        expected = (
            3.0 * log1p(10) + 2.5 * log1p(4) + 2.0 * log1p(6) + 1.0 * log1p(8)
        )
        assert activity_score(10, 4, 6, 8, CFG) == pytest.approx(expected)

    def test_single_commit_reference_point(self):
        # 3.0 * log1p(1) = 3 * 0.6931... = 2.0794...
        assert activity_score(commits=1, cfg=CFG) == pytest.approx(2.0794415, rel=1e-6)

    def test_ten_commits_reference_point(self):
        assert activity_score(commits=10, cfg=CFG) == pytest.approx(7.1936858, rel=1e-6)

    def test_logarithmic_damping_not_linear(self):
        """The 0->5 gap must dwarf the 200->205 gap. That is the whole point."""
        early_gain = activity_score(commits=5, cfg=CFG) - activity_score(commits=0, cfg=CFG)
        late_gain = activity_score(commits=205, cfg=CFG) - activity_score(commits=200, cfg=CFG)
        assert early_gain > 20 * late_gain

    def test_each_signal_uses_its_stated_weight(self):
        one = log1p(1)
        assert activity_score(commits=1, cfg=CFG) == pytest.approx(3.0 * one)
        assert activity_score(reviews=1, cfg=CFG) == pytest.approx(2.5 * one)
        assert activity_score(prs_merged=1, cfg=CFG) == pytest.approx(2.0 * one)
        assert activity_score(prs_opened=1, cfg=CFG) == pytest.approx(1.0 * one)

    def test_negative_counts_are_clamped_not_trusted(self):
        assert activity_score(-5, -5, -5, -5, CFG) == 0.0


# --------------------------------------------------------------------------
# Decay
# --------------------------------------------------------------------------

class TestDecay:
    def test_no_decay_today(self):
        assert decay_factor(0, CFG) == 1.0

    def test_exactly_half_at_one_half_life(self):
        assert decay_factor(180, CFG) == pytest.approx(0.5)

    def test_quarter_at_two_half_lives(self):
        assert decay_factor(360, CFG) == pytest.approx(0.25)

    def test_future_timestamps_do_not_boost_the_score(self):
        """A clock-skewed 'last activity in the future' must not exceed 1.0."""
        assert decay_factor(-90, CFG) == 1.0

    def test_score_is_activity_times_decay(self):
        activity = activity_score(20, 5, 3, 3, CFG)
        assert contribution_score(20, 5, 3, 3, 90, CFG) == pytest.approx(
            activity * 0.5 ** (90 / 180)
        )

    def test_dormant_prolific_contributor_falls_below_a_recent_light_one(self):
        """300 commits eighteen months ago should lose to 4 commits last week."""
        dormant = contribution_score(commits=300, days_since_last_activity=540, cfg=CFG)
        recent = contribution_score(commits=4, days_since_last_activity=7, cfg=CFG)
        assert dormant < recent

    def test_no_activity_decays_to_zero_regardless_of_elapsed_days(self):
        assert contribution_score(0, 0, 0, 0, 5, CFG) == 0.0
        assert contribution_score(0, 0, 0, 0, 5000, CFG) == 0.0


# --------------------------------------------------------------------------
# Risk
# --------------------------------------------------------------------------

class TestRisk:
    def test_risk_equals_weight_over_one_plus_score(self):
        assert risk_score(3.0, 5.0) == pytest.approx(3.0 / 6.0)

    def test_dormant_admin_outranks_dormant_reader(self):
        assert risk_score(3.0, 0.0) > risk_score(0.5, 0.0)

    def test_at_zero_score_risk_is_the_permission_weight(self):
        assert risk_score(3.0, 0.0) == 3.0

    def test_active_admin_outranked_by_dormant_admin(self):
        assert risk_score(3.0, 0.0) > risk_score(3.0, 25.0)

    @pytest.mark.parametrize(
        "permission,weight",
        [("admin", 3.0), ("maintain", 2.0), ("write", 1.5), ("triage", 0.5), ("read", 0.5)],
    )
    def test_permission_weights_are_as_specified(self, permission, weight):
        assert CFG.weight_for(permission) == weight

    def test_legacy_permission_names_map_correctly(self):
        assert CFG.weight_for("push") == CFG.weight_for("write")
        assert CFG.weight_for("pull") == CFG.weight_for("read")

    def test_unknown_permission_is_not_treated_as_harmless(self):
        """An unrecognised custom role must not score as read-only."""
        assert CFG.weight_for("custom-deploy-role") == 1.5
        assert CFG.weight_for(None) == 1.5


# --------------------------------------------------------------------------
# Scoring one member
# --------------------------------------------------------------------------

class TestScoreMember:
    def test_member_with_no_activity_scores_zero_and_carries_no_date(self):
        member = MemberInput(login="ghost", permission="admin")
        scored = score_member(member, repo_id=1, now=NOW, cfg=CFG)
        assert scored.score == 0.0
        assert scored.days_since_activity is None
        assert scored.risk == 3.0

    def test_recent_contributor_scores_close_to_raw_activity(self):
        member = MemberInput(
            login="lena", permission="write", commits=62, reviews=9,
            prs_merged=14, prs_opened=15, last_activity_at=days_ago(1),
        )
        scored = score_member(member, repo_id=1, now=NOW, cfg=CFG)
        assert scored.score == pytest.approx(scored.activity * 0.5 ** (1 / 180))
        assert scored.score > CFG.threshold

    def test_reviewer_who_barely_commits_is_not_flagged(self):
        """The staff-engineer case: a naive commit count would flag them."""
        reviewer = MemberInput(
            login="priya", permission="maintain", commits=2, reviews=21,
            prs_merged=1, prs_opened=2, last_activity_at=days_ago(5),
        )
        scored = score_member(reviewer, repo_id=1, now=NOW, cfg=CFG)
        assert scored.commits < 3
        assert scored.score > CFG.threshold

    def test_days_between_is_never_negative(self):
        assert days_between(NOW, NOW + timedelta(days=3)) == 0.0

    def test_days_between_returns_none_without_a_timestamp(self):
        assert days_between(NOW, None) is None


# --------------------------------------------------------------------------
# NEVER FLAG - member rules
# --------------------------------------------------------------------------

class TestMemberExclusions:
    def test_org_owner_is_never_flagged(self):
        owner = MemberInput(login="ravi-owner", permission="admin")
        reason, detail = member_exclusion(owner, org_owners={"ravi-owner"}, allowlist=[])
        assert reason == ExclusionReason.ORG_OWNER
        assert detail

    def test_owner_match_is_case_insensitive(self):
        owner = MemberInput(login="Ravi-Owner", permission="admin")
        reason, _ = member_exclusion(owner, org_owners={"ravi-owner"}, allowlist=[])
        assert reason == ExclusionReason.ORG_OWNER

    @pytest.mark.parametrize(
        "login,user_type",
        [("dependabot[bot]", "User"), ("renovate[bot]", None), ("release-bot", "Bot")],
    )
    def test_bots_are_never_flagged(self, login, user_type):
        member = MemberInput(login=login, permission="write", user_type=user_type)
        reason, _ = member_exclusion(member, org_owners=set(), allowlist=[])
        assert reason == ExclusionReason.BOT

    def test_a_human_named_like_a_robot_is_still_flagged(self):
        """'bot' in the middle of a name must not exclude a person."""
        member = MemberInput(login="robotnik", permission="write", user_type="User")
        assert member_exclusion(member, org_owners=set(), allowlist=[]) is None

    def test_allowlisted_login_is_never_flagged(self):
        member = MemberInput(login="audit-svc", permission="read")
        reason, _ = member_exclusion(member, org_owners=set(), allowlist=["audit-svc"])
        assert reason == ExclusionReason.ALLOWLISTED

    def test_exclusion_precedence_is_stable(self):
        """An owner who is also allowlisted always reports as an owner."""
        member = MemberInput(login="ravi-owner", permission="admin")
        reason, _ = member_exclusion(
            member, org_owners={"ravi-owner"}, allowlist=["ravi-owner"]
        )
        assert reason == ExclusionReason.ORG_OWNER

    def test_ordinary_member_has_no_exclusion(self):
        member = MemberInput(login="alex", permission="admin", user_type="User")
        assert member_exclusion(member, org_owners=set(), allowlist=[]) is None

    @pytest.mark.parametrize(
        "login,user_type,expected",
        [
            ("dependabot[bot]", None, True),
            ("some-app[bot]", None, True),
            ("normal-user", "Bot", True),
            ("normal-user", "User", False),
            ("bot-wrangler", "User", False),
            (None, None, False),
        ],
    )
    def test_is_bot_login(self, login, user_type, expected):
        assert is_bot_login(login, user_type) is expected


# --------------------------------------------------------------------------
# NEVER FLAG - repo rules
# --------------------------------------------------------------------------

class TestRepoExclusions:
    def test_repo_created_inside_the_window_is_excluded(self):
        repo = RepoInput(repo_id=1, full_name="o/new", created_at=days_ago(30),
                         contributor_count=4)
        reason, detail = repo_exclusion(repo, now=NOW, lookback_days=180)
        assert reason == ExclusionReason.NEW_REPO
        assert "30 days ago" in detail

    def test_repo_older_than_the_window_is_judged_normally(self):
        repo = RepoInput(repo_id=1, full_name="o/old", created_at=days_ago(400),
                         contributor_count=4)
        assert repo_exclusion(repo, now=NOW, lookback_days=180) is None

    def test_single_contributor_repo_is_excluded(self):
        repo = RepoInput(repo_id=1, full_name="o/solo", created_at=days_ago(400),
                         contributor_count=1)
        reason, _ = repo_exclusion(repo, now=NOW, min_contributors=2)
        assert reason == ExclusionReason.SINGLE_CONTRIBUTOR

    def test_zero_contributor_repo_is_excluded_too(self):
        repo = RepoInput(repo_id=1, full_name="o/dead", created_at=days_ago(400),
                         contributor_count=0)
        reason, _ = repo_exclusion(repo, now=NOW, min_contributors=2)
        assert reason == ExclusionReason.SINGLE_CONTRIBUTOR

    def test_empty_repo_is_excluded_before_anything_else(self):
        repo = RepoInput(repo_id=1, full_name="o/empty", created_at=days_ago(400),
                         contributor_count=0, is_empty=True)
        reason, _ = repo_exclusion(repo, now=NOW)
        assert reason == ExclusionReason.EMPTY_REPO

    def test_archived_repo_is_still_judged(self):
        """Archived is a tag, not an excuse: the access is still live."""
        repo = RepoInput(repo_id=1, full_name="o/legacy", created_at=days_ago(2000),
                         contributor_count=3, is_archived=True)
        assert repo_exclusion(repo, now=NOW) is None

    def test_new_repo_wins_over_single_contributor(self):
        """A brand-new solo repo reports as new, which is the more informative reason."""
        repo = RepoInput(repo_id=1, full_name="o/fresh", created_at=days_ago(10),
                         contributor_count=1)
        reason, _ = repo_exclusion(repo, now=NOW, lookback_days=180, min_contributors=2)
        assert reason == ExclusionReason.NEW_REPO


# --------------------------------------------------------------------------
# Whole-repo assessment
# --------------------------------------------------------------------------

MATURE_REPO = RepoInput(
    repo_id=101, full_name="acme/payments-api",
    created_at=datetime(2021, 3, 4, tzinfo=timezone.utc), contributor_count=5,
)

# Same repo, archived. Archiving must change the remediation advice and
# nothing else - it is not a reason to stop assessing the repo.
ARCHIVED_REPO = RepoInput(
    repo_id=102, full_name="acme/legacy-etl",
    created_at=datetime(2019, 1, 22, tzinfo=timezone.utc), contributor_count=5,
    is_archived=True,
)


def member(login, permission, **kw) -> MemberInput:
    return MemberInput(login=login, permission=permission, **kw)


class TestAssessRepo:
    def test_inactive_member_is_flagged_with_evidence(self):
        result = assess_repo(
            MATURE_REPO, [member("alex-departed", "admin")], now=NOW, cfg=CFG
        )
        assert len(result.flagged) == 1
        flag = result.flagged[0]
        assert flag.risk == 3.0
        assert flag.reason and "admin" in flag.reason
        assert "below the 5.0 threshold" in flag.reason

    def test_active_member_is_not_flagged(self):
        result = assess_repo(
            MATURE_REPO,
            [member("lena", "write", commits=62, reviews=9, prs_merged=14,
                    prs_opened=15, last_activity_at=days_ago(1))],
            now=NOW, cfg=CFG,
        )
        assert result.flagged == []

    def test_excluded_members_are_scored_but_never_flagged(self):
        """They stay in the output. Dropping them would hide the org from the reader."""
        result = assess_repo(
            MATURE_REPO,
            [member("ravi-owner", "admin"), member("dependabot[bot]", "write")],
            org_owners={"ravi-owner"}, allowlist=[], now=NOW, cfg=CFG,
        )
        assert result.flagged == []
        assert len(result.scored) == 2
        assert {m.excluded_reason for m in result.excluded} == {
            ExclusionReason.ORG_OWNER, ExclusionReason.BOT
        }

    def test_repo_level_exclusion_suppresses_every_suggestion(self):
        new_repo = RepoInput(repo_id=104, full_name="acme/ml-sandbox",
                             created_at=days_ago(28), contributor_count=2)
        result = assess_repo(
            new_repo, [member("alex-departed", "write"), member("tom", "admin")],
            now=NOW, cfg=CFG,
        )
        assert result.flagged == []
        assert result.repo_exclusion == ExclusionReason.NEW_REPO
        assert all(m.excluded_reason == ExclusionReason.NEW_REPO for m in result.scored)

    def test_member_exclusion_takes_precedence_over_repo_exclusion(self):
        new_repo = RepoInput(repo_id=104, full_name="acme/ml-sandbox",
                             created_at=days_ago(28), contributor_count=2)
        result = assess_repo(
            new_repo, [member("ravi-owner", "admin")],
            org_owners={"ravi-owner"}, now=NOW, cfg=CFG,
        )
        assert result.scored[0].excluded_reason == ExclusionReason.ORG_OWNER

    def test_team_only_access_is_labelled_and_still_suggested(self):
        result = assess_repo(
            MATURE_REPO,
            [MemberInput(login="maya-platform", permission="write", is_team=True,
                         teams=("Platform Engineering",),
                         access_label="via Platform Engineering (write)")],
            now=NOW, cfg=CFG,
        )
        flag = result.flagged[0]
        assert flag.is_team_only
        assert "Platform Engineering" in flag.removal_note
        assert "team level" in flag.removal_note

    def test_direct_access_note_differs_from_team_note(self):
        result = assess_repo(
            MATURE_REPO,
            [MemberInput(login="sam", permission="write", is_direct=True)],
            now=NOW, cfg=CFG,
        )
        flag = result.flagged[0]
        assert not flag.is_team_only
        assert "this repo alone" in flag.removal_note

    def test_archived_repo_note_does_not_tell_you_to_revoke_on_the_repo(self):
        """GitHub refuses collaborator changes on an archived repo.

        Advising "revoke this on the repo" there sends a reviewer to a settings
        page that will not let them do it, so the note has to say otherwise.
        """
        result = assess_repo(
            ARCHIVED_REPO,
            [MemberInput(login="sam-stale", permission="admin", is_direct=True)],
            now=NOW, cfg=CFG,
        )
        flag = result.flagged[0]
        assert flag.repo_archived
        assert "archived" in flag.removal_note
        assert "org or team level" in flag.removal_note

    def test_archived_note_also_applies_to_team_inherited_access(self):
        result = assess_repo(
            ARCHIVED_REPO,
            [MemberInput(login="maya-platform", permission="maintain", is_team=True,
                         teams=("Data Engineering",))],
            now=NOW, cfg=CFG,
        )
        flag = result.flagged[0]
        assert "Data Engineering" in flag.removal_note      # team advice survives
        assert "archived" in flag.removal_note              # and is added to

    def test_archiving_changes_advice_but_never_the_score(self):
        """Stale access on an archived repo is just as live - only harder to remove."""
        live = assess_repo(
            MATURE_REPO, [member("sam", "admin")], now=NOW, cfg=CFG,
        ).flagged[0]
        archived = assess_repo(
            ARCHIVED_REPO, [member("sam", "admin")], now=NOW, cfg=CFG,
        ).flagged[0]
        assert live.score == archived.score
        assert live.risk == archived.risk
        assert live.removal_note != archived.removal_note

    def test_threshold_boundary_is_strict_less_than(self):
        cfg = ScoringConfig(threshold=2.0794415416798357)  # exactly one commit
        result = assess_repo(
            MATURE_REPO,
            [member("edge", "write", commits=1, last_activity_at=NOW)],
            now=NOW, cfg=cfg,
        )
        assert result.flagged == []  # equal to the threshold is not below it


# --------------------------------------------------------------------------
# Ranking
# --------------------------------------------------------------------------

class TestRanking:
    def _assessments(self):
        repo_a = assess_repo(
            MATURE_REPO,
            [
                member("alex-departed", "admin"),
                member("tom-triage", "triage"),
                member("sam-stale", "write"),
            ],
            now=NOW, cfg=CFG,
        )
        repo_b = assess_repo(
            RepoInput(repo_id=103, full_name="acme/legacy-etl",
                      created_at=days_ago(2700), contributor_count=2),
            [member("maya-platform", "maintain")],
            now=NOW, cfg=CFG,
        )
        return [repo_a, repo_b]

    def test_sorted_by_risk_highest_first(self):
        ranked = rank_suggestions(self._assessments())
        assert [m.login for m in ranked] == [
            "alex-departed",   # admin,    risk 3.0
            "maya-platform",   # maintain, risk 2.0
            "sam-stale",       # write,    risk 1.5
            "tom-triage",      # triage,   risk 0.5
        ]
        assert [m.risk for m in ranked] == sorted((m.risk for m in ranked), reverse=True)

    def test_ranking_is_deterministic_for_ties(self):
        """Two runs over unchanged data must emit the same order."""
        first = [m.login for m in rank_suggestions(self._assessments())]
        second = [m.login for m in rank_suggestions(self._assessments())]
        assert first == second

    def test_collect_exclusions_groups_by_reason(self):
        result = assess_repo(
            MATURE_REPO,
            [member("ravi-owner", "admin"), member("bot[bot]", "write"),
             member("audit-svc", "read")],
            org_owners={"ravi-owner"}, allowlist=["audit-svc"], now=NOW, cfg=CFG,
        )
        excluded = collect_exclusions([result])
        assert [m.excluded_reason for m in excluded] == [
            ExclusionReason.ALLOWLISTED, ExclusionReason.BOT, ExclusionReason.ORG_OWNER
        ]


# --------------------------------------------------------------------------
# Explanations - the 'why flagged' column a non-engineer reads
# --------------------------------------------------------------------------

class TestExplanations:
    def test_explains_total_inactivity_without_inventing_a_date(self):
        scored = score_member(member("ghost", "admin"), 1, now=NOW, cfg=CFG)
        text = explain_flag(scored, CFG)
        assert "no recorded commits" in text
        assert "admin" in text

    def test_explains_partial_activity_with_real_numbers(self):
        scored = score_member(
            member("dev-vendor", "write", commits=3, prs_merged=1, prs_opened=1,
                   last_activity_at=days_ago(173)),
            1, now=NOW, cfg=CFG,
        )
        text = explain_flag(scored, CFG)
        assert "173 days ago" in text
        assert "3 commits" in text

    def test_explanation_names_the_threshold(self):
        scored = score_member(member("ghost", "read"), 1, now=NOW, cfg=CFG)
        assert "5.0 threshold" in explain_flag(scored, CFG)


class TestContributorsWithoutAccess:
    """A contributor is not the same thing as a collaborator.

    Found on a live organization: a coding agent's commits were attributed to
    an account that held no permission on the repo. It was scored, fell below
    the threshold, and would have been suggested for "access removal" - of
    access it never had.
    """

    def _repo(self):
        return RepoInput(repo_id=1, full_name="acme/api",
                         created_at=days_ago(900), contributor_count=4)

    def test_contributor_with_no_access_is_never_flagged(self):
        contributor = MemberInput(login="Copilot", permission=None, commits=1,
                                  last_activity_at=days_ago(3))
        result = assess_repo(self._repo(), [contributor], now=NOW, cfg=CFG)
        assert result.flagged == []
        assert result.scored[0].excluded_reason == ExclusionReason.NO_ACCESS

    def test_they_still_appear_in_the_table_with_their_score(self):
        """Context is the point - they are not dropped, just not suggested."""
        contributor = MemberInput(login="departed-dev", commits=40,
                                  last_activity_at=days_ago(10))
        result = assess_repo(self._repo(), [contributor], now=NOW, cfg=CFG)
        assert len(result.scored) == 1
        assert result.scored[0].score > 0

    @pytest.mark.parametrize(
        "kwargs",
        [{"is_direct": True}, {"is_outside": True}, {"is_team": True}, {"is_base": True}],
    )
    def test_any_real_access_path_makes_them_flaggable(self, kwargs):
        member = MemberInput(login="dormant", permission="write", **kwargs)
        result = assess_repo(self._repo(), [member], now=NOW, cfg=CFG)
        assert result.flagged, f"holder of {kwargs} should be assessable"

    def test_has_access_is_false_only_when_every_path_is_absent(self):
        assert not MemberInput(login="x").has_access
        assert MemberInput(login="x", is_base=True).has_access


class TestExplanationChoosesTheRightCause:
    """A flag has two possible causes and naming the wrong one looks broken.

    From the live run: a member with one commit made today was flagged, and the
    sentence read "last contributed 0 days ago" - arithmetically right, and it
    reads like a bug.
    """

    def test_recent_but_thin_leads_with_volume(self):
        scored = score_member(
            MemberInput(login="newish", permission="admin", commits=1,
                        last_activity_at=NOW),
            1, now=NOW, cfg=CFG,
        )
        text = explain_flag(scored, CFG)
        assert "contributed only 1 commit" in text
        assert "0 days ago" not in text

    def test_long_dormant_leads_with_recency(self):
        scored = score_member(
            MemberInput(login="stale", permission="write", commits=2,
                        last_activity_at=days_ago(150)),
            1, now=NOW, cfg=CFG,
        )
        text = explain_flag(scored, CFG)
        assert "last contributed 150 days ago" in text

    def test_no_activity_at_all_says_so(self):
        scored = score_member(MemberInput(login="ghost", permission="admin"),
                              1, now=NOW, cfg=CFG)
        assert "no recorded commits" in explain_flag(scored, CFG)

    def test_every_variant_names_the_threshold_and_permission(self):
        for member in (
            MemberInput(login="a", permission="admin"),
            MemberInput(login="b", permission="write", commits=1, last_activity_at=NOW),
            MemberInput(login="c", permission="read", commits=2,
                        last_activity_at=days_ago(150)),
        ):
            text = explain_flag(score_member(member, 1, now=NOW, cfg=CFG), CFG)
            assert "5.0 threshold" in text
            assert member.permission in text

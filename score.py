"""Stage three: turn access + activity into a score, a risk, and a suggestion.

The whole model is four lines:

    activity = 3.0*log1p(commits) + 2.5*log1p(reviews)
             + 2.0*log1p(prs_merged) + 1.0*log1p(prs_opened)

    score    = activity * 0.5 ** (days_since_last_activity / 180)

    risk     = permission_weight / (1 + score)

    flag if  score < THRESHOLD  and no exclusion applies

Why this shape:

* **log1p** - contribution is not linear. The gap between 0 and 5 commits is
  the interesting one; the gap between 200 and 205 is noise. log1p also
  handles zero without a special case, and stops one prolific month from
  making someone look permanently active.
* **Weights 3 / 2.5 / 2 / 1** - reviews are weighted nearly as heavily as
  commits on purpose. A staff engineer or security reviewer who writes little
  code but reviews constantly is exactly the person a naive commit-count model
  wrongly flags for removal.
* **Half-life decay** - access risk is about *now*. Someone with 300 commits
  who vanished 18 months ago has 1/8th the score of the same person today.
  A hard cutoff would make someone at 179 days safe and at 181 days flagged;
  a smooth decay keeps the ordering sensible at the boundary.
* **risk = weight / (1 + score)** - severity, not just inactivity. A dormant
  admin outranks a dormant reader, because the consequence of the stale grant
  is what the reviewer is triaging. The `1 +` keeps it finite at score 0.

Everything here is a pure function of its inputs, which is what makes the
test suite meaningful: no network, no clock, no database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import log1p
from typing import Iterable, Sequence

import config
from config import ExclusionReason, ScoringConfig


# --------------------------------------------------------------------------
# The formulas
# --------------------------------------------------------------------------

def activity_score(
    commits: int = 0,
    reviews: int = 0,
    prs_merged: int = 0,
    prs_opened: int = 0,
    cfg: ScoringConfig | None = None,
) -> float:
    """Weighted, log-damped sum of what a member did in the window."""
    cfg = cfg or config.SCORING
    return (
        cfg.commit_weight * log1p(max(0, commits))
        + cfg.review_weight * log1p(max(0, reviews))
        + cfg.pr_merged_weight * log1p(max(0, prs_merged))
        + cfg.pr_opened_weight * log1p(max(0, prs_opened))
    )


def decay_factor(days_since_last_activity: float, cfg: ScoringConfig | None = None) -> float:
    """0.5 ** (days / half_life). 1.0 today, 0.5 at one half-life, and so on."""
    cfg = cfg or config.SCORING
    days = max(0.0, float(days_since_last_activity))
    return 0.5 ** (days / cfg.half_life_days)


def contribution_score(
    commits: int = 0,
    reviews: int = 0,
    prs_merged: int = 0,
    prs_opened: int = 0,
    days_since_last_activity: float = 0.0,
    cfg: ScoringConfig | None = None,
) -> float:
    """The decayed contribution score for one member on one repo."""
    cfg = cfg or config.SCORING
    activity = activity_score(commits, reviews, prs_merged, prs_opened, cfg)
    if activity <= 0:
        # No activity decays to nothing regardless of elapsed time.
        return 0.0
    return activity * decay_factor(days_since_last_activity, cfg)


def risk_score(permission_weight: float, score: float) -> float:
    """How much a stale grant matters: heavier permission, lower activity."""
    return permission_weight / (1.0 + max(0.0, score))


def days_between(later: datetime, earlier: datetime | None) -> float | None:
    if earlier is None:
        return None
    return max(0.0, (later - earlier).total_seconds() / 86400.0)


# --------------------------------------------------------------------------
# Remediation advice
# --------------------------------------------------------------------------

def remediation_note(
    *,
    is_team_only: bool,
    teams: Sequence[str] = (),
    is_archived: bool = False,
    is_base_only: bool = False,
) -> str:
    """What a reviewer would actually have to do to act on a suggestion.

    Lives here, and is called by both the scorer and the dashboard, so the
    advice shown on the report can never drift from the advice the model
    believes it is giving.

    Three cases the obvious wording gets wrong:

    * **Organization base permission** is not a grant on the repository at all.
      It is one setting under Member privileges that hands every member the
      same access to every repository. Telling a reviewer to revoke it "on this
      repo" is impossible advice, and the blast radius of the real fix is the
      entire organization. This is easy to mistake for team-inherited access,
      because both show up as "in the full collaborator list, but not a direct
      grant" - and a real scan of a live org is exactly where that mistake
      surfaces.
    * **Team-inherited access** cannot be revoked on the repo at all. Sending
      someone to the repo's settings page for it wastes their time and hides
      the blast radius of the real fix.
    * **Archived repositories** reject collaborator changes outright - GitHub
      makes the repo read-only, settings included. "Revoke this on the repo"
      is advice that cannot be followed. Unarchiving to remove one grant also
      re-opens the repo for writes, so the safer remedy is normally to remove
      the access at the org or team level and leave it archived. This matters
      more than it sounds: in the demo org the single highest-risk finding
      sits on an archived repo.
    """
    if is_base_only:
        note = (
            "Access comes from the organization's base permission, not from any "
            "grant on this repository, so it cannot be revoked here. The fix is "
            "one setting - Organization Settings > Member privileges > Base "
            "permissions - and it changes what every member can reach."
        )
    elif is_team_only:
        via = ", ".join(teams) if teams else "a team"
        note = (
            f"Access is inherited from {via}. Removing it means changing "
            f"team membership, which affects every repo that team can reach - "
            f"review at the team level, not here."
        )
    else:
        note = "Direct repository grant; can be revoked on this repo alone."

    if is_archived and not is_base_only:
        note += (
            " The repository is archived, and GitHub refuses collaborator "
            "changes while it is - remove the grant at the org or team level, "
            "or unarchive, revoke, and re-archive."
        )

    return note


# --------------------------------------------------------------------------
# Inputs and outputs
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class MemberInput:
    """Everything the scorer needs about one member on one repo."""

    login: str
    permission: str | None = None
    commits: int = 0
    reviews: int = 0
    prs_merged: int = 0
    prs_opened: int = 0
    last_commit_at: datetime | None = None
    last_review_at: datetime | None = None
    last_activity_at: datetime | None = None
    user_type: str | None = None
    is_direct: bool = False
    is_outside: bool = False
    is_team: bool = False
    is_base: bool = False
    teams: tuple[str, ...] = ()
    access_label: str = ""

    @property
    def has_access(self) -> bool:
        """Does this person actually hold access right now?

        A repo's contributor list and its collaborator list are not the same
        set. Someone can have written half the code and hold no permission
        today - a departed contributor, or a coding agent whose commits are
        attributed to an account that was never a collaborator. They belong in
        the per-repo table as context, but suggesting the removal of access
        they do not have is nonsense, and it is the kind of nonsense that
        destroys trust in the whole report.

        A permission counts on its own: it only ever comes from the collaborator
        listing, so its presence means access exists even when the *kind* of
        access could not be determined. The flags say which path; the permission
        says whether. Requiring a flag as well would silently drop anyone whose
        access path we failed to classify - the same "confidently wrong" failure
        as reporting a refused listing as nobody-has-access.
        """
        return bool(
            self.permission
            or self.is_direct or self.is_outside or self.is_team or self.is_base
        )


@dataclass(frozen=True)
class RepoInput:
    """Repo-level facts that can suppress suggestions for everyone in it."""

    repo_id: int
    full_name: str
    created_at: datetime | None = None
    contributor_count: int = 0
    is_archived: bool = False
    is_fork: bool = False
    is_empty: bool = False


@dataclass
class MemberScore:
    """One scored member. `flagged` is a suggestion, never an action."""

    login: str
    repo_id: int
    permission: str | None
    permission_weight: float
    activity: float
    score: float
    risk: float
    days_since_activity: float | None
    flagged: bool
    reason: str | None = None
    excluded_reason: str | None = None
    excluded_detail: str | None = None
    access_label: str = ""
    is_team_only: bool = False
    is_base_only: bool = False
    repo_archived: bool = False
    teams: tuple[str, ...] = ()
    last_commit_at: datetime | None = None
    last_review_at: datetime | None = None
    last_activity_at: datetime | None = None
    commits: int = 0
    reviews: int = 0
    prs_merged: int = 0
    prs_opened: int = 0

    @property
    def excluded(self) -> bool:
        return self.excluded_reason is not None

    @property
    def removal_note(self) -> str:
        """What a reviewer would actually have to do to act on this."""
        return remediation_note(
            is_team_only=self.is_team_only,
            is_base_only=self.is_base_only,
            teams=self.teams,
            is_archived=self.repo_archived,
        )


@dataclass
class RepoAssessment:
    repo_id: int
    full_name: str
    scored: list[MemberScore] = field(default_factory=list)
    repo_exclusion: str | None = None
    repo_exclusion_detail: str | None = None

    @property
    def flagged(self) -> list[MemberScore]:
        return [m for m in self.scored if m.flagged]

    @property
    def excluded(self) -> list[MemberScore]:
        return [m for m in self.scored if m.excluded]


# --------------------------------------------------------------------------
# Exclusions - the NEVER FLAG rules
# --------------------------------------------------------------------------

def repo_exclusion(
    repo: RepoInput,
    *,
    now: datetime | None = None,
    lookback_days: int | None = None,
    min_contributors: int | None = None,
) -> tuple[str, str] | None:
    """A reason no one in this repo should be flagged, or None.

    These are all "we do not have the evidence to judge" rules, not "this repo
    is fine" rules. The repo still appears in the dashboard with its advisories
    and its member table; only the removal suggestions are withheld.
    """
    now = now or datetime.now(timezone.utc)
    lookback_days = config.LOOKBACK_DAYS if lookback_days is None else lookback_days
    min_contributors = config.MIN_CONTRIBUTORS if min_contributors is None else min_contributors

    if repo.is_empty:
        return (
            ExclusionReason.EMPTY_REPO,
            "The repository has no commits, so there is no contribution history to judge.",
        )

    if repo.created_at is not None:
        age_days = (now - repo.created_at).total_seconds() / 86400.0
        if age_days < lookback_days:
            return (
                ExclusionReason.NEW_REPO,
                f"Created {age_days:.0f} days ago, inside the {lookback_days}-day "
                f"window. Nobody has had time to build a history here yet.",
            )

    if repo.contributor_count < min_contributors:
        return (
            ExclusionReason.SINGLE_CONTRIBUTOR,
            f"Only {_count(repo.contributor_count, 'contributor')} in the window. "
            f"With no one to compare against, a low score says nothing useful.",
        )

    return None


def member_exclusion(
    member: MemberInput,
    *,
    org_owners: frozenset[str] | set[str] = frozenset(),
    allowlist: Sequence[str] | None = None,
) -> tuple[str, str] | None:
    """A reason this specific person should never be flagged, or None.

    Checked in a fixed order so the recorded reason is stable across runs -
    an owner who is also allowlisted always reports as an owner.
    """
    allowlist = config.LOGIN_ALLOWLIST if allowlist is None else allowlist
    login = member.login.lower()

    if login in {o.lower() for o in org_owners}:
        return (
            ExclusionReason.ORG_OWNER,
            "Organization owners are never suggested for removal, regardless of activity.",
        )

    if is_bot_login(member.login, member.user_type):
        return (
            ExclusionReason.BOT,
            "Bot or app account. Its access is driven by an integration, not by a person.",
        )

    if login in {a.lower() for a in allowlist}:
        return (
            ExclusionReason.ALLOWLISTED,
            "Listed in LOGIN_ALLOWLIST, so exempt from removal suggestions by policy.",
        )

    return None


def is_bot_login(login: str | None, user_type: str | None = None) -> bool:
    if user_type and str(user_type).lower() == "bot":
        return True
    return bool(login) and str(login).lower().endswith("[bot]")


# --------------------------------------------------------------------------
# Assessment
# --------------------------------------------------------------------------

def score_member(
    member: MemberInput,
    repo_id: int,
    *,
    now: datetime | None = None,
    cfg: ScoringConfig | None = None,
    repo_archived: bool = False,
) -> MemberScore:
    """Score one member. No exclusion logic here - that is applied separately.

    `repo_archived` does not affect the score. It only changes the remediation
    advice, because stale access on an archived repo is just as live as
    anywhere else - it is only harder to remove.
    """
    cfg = cfg or config.SCORING
    now = now or datetime.now(timezone.utc)

    days = days_between(now, member.last_activity_at)
    effective_days = cfg.no_activity_days if days is None else days

    activity = activity_score(
        member.commits, member.reviews, member.prs_merged, member.prs_opened, cfg
    )
    score = contribution_score(
        member.commits, member.reviews, member.prs_merged, member.prs_opened,
        effective_days, cfg,
    )
    weight = cfg.weight_for(member.permission)

    return MemberScore(
        login=member.login,
        repo_id=repo_id,
        permission=member.permission,
        permission_weight=weight,
        activity=activity,
        score=score,
        risk=risk_score(weight, score),
        days_since_activity=days,
        flagged=False,                       # decided by assess_repo
        access_label=member.access_label,
        is_team_only=member.is_team and not (member.is_direct or member.is_outside),
        is_base_only=member.is_base and not (
            member.is_direct or member.is_outside or member.is_team
        ),
        repo_archived=repo_archived,
        teams=member.teams,
        last_commit_at=member.last_commit_at,
        last_review_at=member.last_review_at,
        last_activity_at=member.last_activity_at,
        commits=member.commits,
        reviews=member.reviews,
        prs_merged=member.prs_merged,
        prs_opened=member.prs_opened,
    )


def assess_repo(
    repo: RepoInput,
    members: Iterable[MemberInput],
    *,
    org_owners: frozenset[str] | set[str] = frozenset(),
    allowlist: Sequence[str] | None = None,
    now: datetime | None = None,
    cfg: ScoringConfig | None = None,
) -> RepoAssessment:
    """Score everyone on one repo and decide who gets flagged.

    Every member is scored and returned, including excluded ones: the
    dashboard shows the full picture and a separate panel explains each
    exclusion. Silently dropping people would make the report look tidier and
    be less trustworthy.
    """
    cfg = cfg or config.SCORING
    now = now or datetime.now(timezone.utc)

    assessment = RepoAssessment(repo_id=repo.repo_id, full_name=repo.full_name)
    repo_block = repo_exclusion(repo, now=now)
    if repo_block:
        assessment.repo_exclusion, assessment.repo_exclusion_detail = repo_block

    for member in members:
        scored = score_member(
            member, repo.repo_id, now=now, cfg=cfg, repo_archived=repo.is_archived
        )

        blocked = member_exclusion(member, org_owners=org_owners, allowlist=allowlist)
        if blocked:
            scored.excluded_reason, scored.excluded_detail = blocked
        elif not member.has_access:
            scored.excluded_reason = ExclusionReason.NO_ACCESS
            scored.excluded_detail = (
                "Appears in this repository's history but holds no direct, "
                "outside, team or organization-base access today, so there is "
                "no grant to remove."
            )
        elif repo_block:
            scored.excluded_reason, scored.excluded_detail = repo_block

        if not scored.excluded and scored.score < cfg.threshold:
            scored.flagged = True
            scored.reason = explain_flag(scored, cfg)

        assessment.scored.append(scored)

    return assessment


#: Below this many days since last activity, a flag is about how little someone
#: contributed rather than how long ago they stopped. Presentation only - it
#: never affects the score.
RECENT_BUT_THIN_DAYS = 30


def _count(n: int, singular: str, plural: str | None = None) -> str:
    """'1 commit' / '3 commits'. Sloppy plurals make a report look automated."""
    return f"{n} {singular if n == 1 else (plural or singular + 's')}"


def explain_flag(scored: MemberScore, cfg: ScoringConfig | None = None) -> str:
    """One sentence a non-engineer can act on. This is the 'why' column.

    A score falls below the threshold for one of two different reasons, and
    saying the wrong one makes the report look broken. Someone can be flagged
    because they stopped contributing months ago, or because they are present
    but barely contribute. Reporting "last contributed 0 days ago" next to a
    flag reads as a contradiction, even though the arithmetic is right - so the
    sentence leads with whichever cause actually applies.
    """
    cfg = cfg or config.SCORING
    perm = scored.permission or "unknown"
    window = int(cfg.no_activity_days)

    volume = (
        f"{_count(scored.commits, 'commit')}, "
        f"{_count(scored.reviews, 'PR')} reviewed, "
        f"{_count(scored.prs_merged, 'PR')} merged"
    )

    if scored.activity == 0:
        why = (
            "has no recorded commits, reviews or pull requests"
            f" in the last {window} days"
        )
    elif scored.days_since_activity is None:
        why = f"has activity that could not be dated ({volume})"
    elif scored.days_since_activity < RECENT_BUT_THIN_DAYS:
        # Present, but barely. Recency is not the problem here; volume is.
        why = f"has contributed only {volume} in the last {window} days"
    else:
        why = (
            f"last contributed {int(scored.days_since_activity)} days ago"
            f" ({volume} in the window)"
        )

    return (
        f"Holds {perm} access but {why}. "
        f"Score {scored.score:.2f} is below the {cfg.threshold:.1f} threshold."
    )


def rank_suggestions(assessments: Iterable[RepoAssessment]) -> list[MemberScore]:
    """Every flagged member across every repo, highest risk first.

    Ties break on login then repo so that two runs over unchanged data emit
    the same order - the report is meant to be diffable.
    """
    flagged = [m for a in assessments for m in a.flagged]
    return sorted(flagged, key=lambda m: (-m.risk, m.login.lower(), m.repo_id))


def collect_exclusions(assessments: Iterable[RepoAssessment]) -> list[MemberScore]:
    """Everyone who was skipped, for the dashboard's exclusion panel."""
    excluded = [m for a in assessments for m in a.excluded]
    return sorted(excluded, key=lambda m: (m.excluded_reason or "", m.login.lower()))

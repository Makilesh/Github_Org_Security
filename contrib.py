"""Stage two: who has access to each repo, and what they actually did there.

Two halves that must not be confused with each other:

**Access** comes from REST, and is fetched once per affiliation so the three
kinds stay distinguishable:

* `affiliation=direct`   - a permission granted on this repo specifically
* `affiliation=outside`  - a collaborator who is not an org member
* team-inherited         - access that comes from team membership

The three are genuinely different findings. Revoking direct access is a
one-click repo change; revoking team-inherited access means changing a team
and affects every other repo that team touches; an outside collaborator on a
private repo is a different risk conversation altogether. Merging them into
one "permission" column would destroy exactly the distinction that makes the
report actionable, so they are stored separately and labelled separately.

**Contribution** comes from GraphQL, one query per repo rather than one per
member. For a repo with 40 collaborators that is the difference between 2
requests and 80. Two connections are walked - commit history and pull
requests with their nested reviews - and everything is bucketed by author in
a single pass.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

import config
from client import (
    EmptyRepositoryError,
    ForbiddenError,
    GitHubClient,
    GitHubError,
    NotFoundError,
)
from db import Database, Stage
from scan import RepoRecord, parse_ts, window_start

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Helpers shared by both halves
# --------------------------------------------------------------------------

#: Ordered strongest-first. Used to collapse several access paths into one
#: "effective" permission for display - never for storage of the source data.
PERMISSION_RANK = ("admin", "maintain", "write", "triage", "read")

_PERMISSION_ALIASES = {"push": "write", "pull": "read"}


def normalise_permission(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().lower()
    return _PERMISSION_ALIASES.get(value, value)


def permission_from_payload(collaborator: Mapping[str, Any]) -> str | None:
    """Best available permission name for a REST collaborator object.

    `role_name` is preferred because it carries custom repository roles, which
    the boolean `permissions` block flattens away. We fall back to the boolean
    block, read strongest-first.
    """
    role = normalise_permission(collaborator.get("role_name"))
    if role:
        return role

    perms = collaborator.get("permissions") or {}
    for name in ("admin", "maintain", "push", "triage", "pull"):
        if perms.get(name):
            return normalise_permission(name)
    return None


def strongest_permission(*values: str | None) -> str | None:
    """The highest-ranked permission among several access paths."""
    best: str | None = None
    best_rank = len(PERMISSION_RANK)
    for value in values:
        norm = normalise_permission(value)
        if not norm:
            continue
        try:
            rank = PERMISSION_RANK.index(norm)
        except ValueError:
            # An unrecognised custom role. Treat it as strong rather than
            # weak: guessing "harmless" about an unknown role is the wrong
            # way to be wrong.
            rank = PERMISSION_RANK.index("write")
        if rank < best_rank:
            best, best_rank = norm, rank
    return best


def is_bot(login: str | None, user_type: str | None = None) -> bool:
    """Bots are excluded from suggestions; the check is deliberately broad."""
    if user_type and str(user_type).lower() == "bot":
        return True
    if not login:
        return False
    return str(login).lower().endswith("[bot]")


# --------------------------------------------------------------------------
# Teams
# --------------------------------------------------------------------------

@dataclass
class TeamIndex:
    """Org teams and their members, built once per run.

    Cost is 1 + T requests for the whole organization, not per repo, so the
    marginal cost of precise team attribution across 100 repos is one extra
    request per repo (the repo's team list).
    """

    members_by_team: dict[str, set[str]] = field(default_factory=dict)
    team_names: dict[str, str] = field(default_factory=dict)
    available: bool = False
    error: str | None = None

    def logins_for(self, slug: str) -> set[str]:
        return self.members_by_team.get(slug, set())

    def name_for(self, slug: str) -> str:
        return self.team_names.get(slug, slug)


def fetch_org_teams(gh: GitHubClient, org: str) -> TeamIndex:
    """Index every team in the org and its members.

    Returns an index with `available=False` if teams cannot be read. The scan
    continues either way: without it, team-inherited access is still detected
    (by set difference against the full collaborator list), we just cannot name
    the team responsible.
    """
    index = TeamIndex()
    if not config.RESOLVE_TEAMS:
        index.error = "RESOLVE_TEAMS is off"
        return index

    try:
        teams = list(gh.paginate(f"/orgs/{org}/teams"))
    except (ForbiddenError, NotFoundError) as exc:
        index.error = str(exc)
        log.warning("Cannot read org teams (%s); team-inherited access will be "
                    "detected but not attributed to a named team", exc)
        return index

    for team in teams:
        slug = str(team.get("slug", ""))
        if not slug:
            continue
        index.team_names[slug] = str(team.get("name") or slug)
        try:
            members = {
                str(m.get("login", "")).lower()
                for m in gh.paginate(f"/orgs/{org}/teams/{slug}/members")
                if m.get("login")
            }
        except GitHubError as exc:
            log.warning("Cannot read members of team %s (%s)", slug, exc)
            members = set()
        index.members_by_team[slug] = members

    index.available = True
    log.info("Indexed %d teams in %s", len(index.members_by_team), org)
    return index


def fetch_repo_teams(gh: GitHubClient, full_name: str) -> list[dict[str, Any]]:
    """Teams that have been granted access to one repo."""
    try:
        return list(gh.paginate(f"/repos/{full_name}/teams", allow_404=True, allow_403=True))
    except GitHubError as exc:
        log.warning("Cannot read teams for %s (%s)", full_name, exc)
        return []


# --------------------------------------------------------------------------
# Collaborators
# --------------------------------------------------------------------------

@dataclass
class AccessEntry:
    """One person's access to one repo, with each path kept separate."""

    login: str
    user_id: int | None = None
    user_type: str | None = None
    site_admin: bool = False
    role_name: str | None = None

    direct: str | None = None       # permission granted on this repo directly
    outside: str | None = None      # permission held as an outside collaborator
    team: str | None = None         # permission inherited from a team
    teams: list[str] = field(default_factory=list)

    @property
    def is_direct(self) -> bool:
        return self.direct is not None

    @property
    def is_outside(self) -> bool:
        return self.outside is not None

    @property
    def is_team(self) -> bool:
        return self.team is not None

    @property
    def effective(self) -> str | None:
        return strongest_permission(self.direct, self.outside, self.team)

    @property
    def access_label(self) -> str:
        """Human-readable summary of how this person got in."""
        parts = []
        if self.is_direct:
            parts.append(f"direct ({self.direct})")
        if self.is_outside:
            parts.append(f"outside collaborator ({self.outside})")
        if self.is_team:
            via = ", ".join(self.teams) if self.teams else "a team"
            parts.append(f"via {via} ({self.team})")
        return "; ".join(parts) or "unknown"


def fetch_collaborators(
    gh: GitHubClient,
    record: RepoRecord,
    team_index: TeamIndex | None = None,
) -> dict[str, AccessEntry]:
    """Build the access picture for one repo, keyed by lowercased login.

    Three REST listings plus (optionally) the repo's team list:

    * `direct` and `outside` are asked for explicitly, because those are the
      two affiliations GitHub will answer precisely.
    * `all` is fetched to find team-inherited access: anyone visible in `all`
      but absent from `direct` holds their access through a team. GitHub has
      no `affiliation=team`, so this difference is the only way to isolate it.
    * the repo's team list, intersected with the org team index, turns that
      into a named team.

    Known limitation, stated rather than hidden: a member holding *both* a
    direct grant and team access is reported as direct. The REST listing
    reports one effective permission per person and does not decompose it, so
    claiming to know the team component for that person would be a guess.
    """
    full_name = record.full_name
    entries: dict[str, AccessEntry] = {}

    def entry_for(collab: Mapping[str, Any]) -> AccessEntry | None:
        login = str(collab.get("login", ""))
        if not login:
            return None
        key = login.lower()
        entry = entries.get(key)
        if entry is None:
            entry = AccessEntry(
                login=login,
                user_id=collab.get("id"),
                user_type=collab.get("type"),
                site_admin=bool(collab.get("site_admin")),
                role_name=collab.get("role_name"),
            )
            entries[key] = entry
        return entry

    direct_logins: set[str] = set()
    for collab in gh.paginate(
        f"/repos/{full_name}/collaborators", {"affiliation": "direct"},
        allow_403=True, allow_404=True,
    ):
        entry = entry_for(collab)
        if entry is None:
            continue
        entry.direct = permission_from_payload(collab)
        direct_logins.add(entry.login.lower())

    for collab in gh.paginate(
        f"/repos/{full_name}/collaborators", {"affiliation": "outside"},
        allow_403=True, allow_404=True,
    ):
        entry = entry_for(collab)
        if entry is None:
            continue
        entry.outside = permission_from_payload(collab)

    inherited: dict[str, str | None] = {}
    for collab in gh.paginate(
        f"/repos/{full_name}/collaborators", {"affiliation": "all"},
        allow_403=True, allow_404=True,
    ):
        login = str(collab.get("login", "")).lower()
        if not login or login in direct_logins:
            continue
        entry = entry_for(collab)
        if entry is None:
            continue
        entry.team = permission_from_payload(collab)
        inherited[login] = entry.team

    # Name the responsible team(s) where we can.
    if inherited and team_index and team_index.available:
        for team in fetch_repo_teams(gh, full_name):
            slug = str(team.get("slug", ""))
            if not slug:
                continue
            name = team_index.name_for(slug)
            for login in team_index.logins_for(slug):
                if login in inherited and login in entries:
                    if name not in entries[login].teams:
                        entries[login].teams.append(name)

    return entries


def store_collaborators(
    db: Database, run_id: int, repo_id: int, entries: Mapping[str, AccessEntry]
) -> None:
    """Replace this repo's access rows atomically.

    The delete matters: without it, someone whose access was revoked between
    runs would stay in the report forever. It runs inside the same transaction
    as the inserts, so an interruption cannot leave the repo with no rows.
    """
    with db.transaction():
        db.clear_collaborators(repo_id)
        for entry in entries.values():
            teams = ", ".join(entry.teams) if entry.teams else None
            if entry.is_direct:
                db.upsert_collaborator(
                    run_id, repo_id, entry.login, affiliation="direct",
                    permission=entry.direct, user_id=entry.user_id,
                    user_type=entry.user_type, role_name=entry.role_name,
                    site_admin=entry.site_admin,
                )
            if entry.is_outside:
                db.upsert_collaborator(
                    run_id, repo_id, entry.login, affiliation="outside",
                    permission=entry.outside, user_id=entry.user_id,
                    user_type=entry.user_type, role_name=entry.role_name,
                    site_admin=entry.site_admin,
                )
            if entry.is_team:
                db.upsert_collaborator(
                    run_id, repo_id, entry.login, affiliation="team",
                    permission=entry.team, user_id=entry.user_id,
                    user_type=entry.user_type, role_name=entry.role_name,
                    site_admin=entry.site_admin, team_names=teams,
                )
            db.set_effective_permission(repo_id, entry.login, entry.effective)


# --------------------------------------------------------------------------
# Contribution data (GraphQL)
# --------------------------------------------------------------------------

COMMITS_QUERY = """
query RepoCommits($owner: String!, $name: String!, $since: GitTimestamp!,
                  $first: Int!, $after: String) {
  rateLimit { limit cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    defaultBranchRef {
      target {
        ... on Commit {
          history(since: $since, first: $first, after: $after) {
            totalCount
            pageInfo { hasNextPage endCursor }
            nodes {
              committedDate
              author { user { login } name email }
            }
          }
        }
      }
    }
  }
}
"""

PULLS_QUERY = """
query RepoPulls($owner: String!, $name: String!, $first: Int!, $reviews: Int!,
                $after: String) {
  rateLimit { limit cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    pullRequests(first: $first, after: $after,
                 orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        createdAt
        updatedAt
        mergedAt
        author { login __typename }
        reviews(first: $reviews) {
          totalCount
          nodes {
            submittedAt
            state
            author { login __typename }
          }
        }
      }
    }
  }
}
"""


@dataclass
class MemberActivity:
    """Raw counts for one member on one repo, inside the lookback window."""

    login: str
    commits: int = 0
    prs_opened: int = 0
    prs_merged: int = 0
    reviews: int = 0
    last_commit_at: datetime | None = None
    last_pr_at: datetime | None = None
    last_review_at: datetime | None = None

    @property
    def last_activity_at(self) -> datetime | None:
        stamps = [s for s in (self.last_commit_at, self.last_pr_at, self.last_review_at) if s]
        return max(stamps) if stamps else None


@dataclass
class RepoActivity:
    """Everything the scoring stage needs about one repo's history."""

    repo_id: int
    full_name: str
    members: dict[str, MemberActivity] = field(default_factory=dict)
    total_commits: int = 0
    unattributed_commits: int = 0
    is_empty: bool = False
    truncated: list[str] = field(default_factory=list)
    window_start: datetime | None = None

    def member(self, login: str) -> MemberActivity:
        key = login.lower()
        if key not in self.members:
            self.members[key] = MemberActivity(login=login)
        return self.members[key]

    @property
    def contributor_count(self) -> int:
        return len(self.members)


def _latest(current: datetime | None, candidate: datetime | None) -> datetime | None:
    if candidate is None:
        return current
    if current is None or candidate > current:
        return candidate
    return current


def fetch_commit_activity(
    gh: GitHubClient, record: RepoRecord, activity: RepoActivity, since: datetime
) -> None:
    """Walk the default branch's history inside the window.

    Commits whose `author.user` is null are counted as **unattributed** and
    reported as a total. That happens when the commit email is not linked to
    any GitHub account. We never guess who it was: matching on the raw name or
    email would silently credit - or fail to credit - the wrong person, and an
    access review that quietly misattributes work is worse than one that
    admits the gap.
    """
    cursor: str | None = None
    pages = 0

    while True:
        data = gh.graphql(
            COMMITS_QUERY,
            {
                "owner": record.owner,
                "name": record.name,
                "since": since.isoformat().replace("+00:00", "Z"),
                "first": config.GQL_COMMIT_PAGE,
                "after": cursor,
            },
        )
        repository = data.get("repository") or {}
        branch = repository.get("defaultBranchRef")
        if not branch:
            # No default branch means no commits: an empty repository.
            activity.is_empty = True
            return

        target = branch.get("target") or {}
        history = target.get("history")
        if not history:
            activity.is_empty = True
            return

        for node in history.get("nodes") or []:
            committed = parse_ts(node.get("committedDate"))
            activity.total_commits += 1
            author = node.get("author") or {}
            user = author.get("user")
            if not user or not user.get("login"):
                activity.unattributed_commits += 1
                continue
            member = activity.member(str(user["login"]))
            member.commits += 1
            member.last_commit_at = _latest(member.last_commit_at, committed)

        page_info = history.get("pageInfo") or {}
        pages += 1
        if not page_info.get("hasNextPage"):
            return
        if pages >= config.GQL_MAX_PAGES:
            activity.truncated.append(
                f"commit history stopped after {pages} pages "
                f"({activity.total_commits} commits)"
            )
            return
        cursor = page_info.get("endCursor")


def fetch_pull_activity(
    gh: GitHubClient, record: RepoRecord, activity: RepoActivity, since: datetime
) -> None:
    """Walk pull requests, newest-updated first, and their reviews.

    Ordering by UPDATED_AT descending lets us stop as soon as a page falls
    entirely outside the window, which is what keeps this affordable on repos
    with thousands of historical PRs.

    Two counting decisions worth stating plainly:

    * A **merged** PR is credited to its author, not to whoever pressed the
      merge button. We are measuring who contributed the work that shipped,
      and the merger is often a release manager or an automation.
    * **reviews** counts *distinct pull requests a member reviewed*, not raw
      review events. Otherwise someone who leaves six separate comments on one
      PR outranks someone who carefully reviewed three, which is the opposite
      of what the number is supposed to mean.
    """
    cursor: str | None = None
    pages = 0
    reviewed_prs: dict[str, set[int]] = defaultdict(set)

    while True:
        data = gh.graphql(
            PULLS_QUERY,
            {
                "owner": record.owner,
                "name": record.name,
                "first": config.GQL_PR_PAGE,
                "reviews": config.GQL_REVIEW_PAGE,
                "after": cursor,
            },
        )
        repository = data.get("repository") or {}
        pulls = repository.get("pullRequests") or {}
        nodes = pulls.get("nodes") or []

        exhausted_window = False
        for node in nodes:
            updated = parse_ts(node.get("updatedAt"))
            if updated and updated < since:
                # Ordered by updatedAt desc: everything after this is older.
                exhausted_window = True
                break

            number = int(node.get("number") or 0)
            created = parse_ts(node.get("createdAt"))
            merged = parse_ts(node.get("mergedAt"))

            author = node.get("author") or {}
            login = author.get("login")
            if login and not is_bot(login, author.get("__typename")):
                member = activity.member(str(login))
                if created and created >= since:
                    member.prs_opened += 1
                    member.last_pr_at = _latest(member.last_pr_at, created)
                if merged and merged >= since:
                    member.prs_merged += 1
                    member.last_pr_at = _latest(member.last_pr_at, merged)

            reviews = node.get("reviews") or {}
            review_nodes = reviews.get("nodes") or []
            if (reviews.get("totalCount") or 0) > len(review_nodes):
                activity.truncated.append(
                    f"PR #{number} had {reviews['totalCount']} reviews, "
                    f"read the first {len(review_nodes)}"
                )
            for review in review_nodes:
                submitted = parse_ts(review.get("submittedAt"))
                if not submitted or submitted < since:
                    continue
                reviewer = review.get("author") or {}
                reviewer_login = reviewer.get("login")
                if not reviewer_login:
                    continue
                if is_bot(reviewer_login, reviewer.get("__typename")):
                    continue
                if str(reviewer_login).lower() == str(login or "").lower():
                    continue  # self-reviews are not review contribution
                member = activity.member(str(reviewer_login))
                reviewed_prs[str(reviewer_login).lower()].add(number)
                member.last_review_at = _latest(member.last_review_at, submitted)

        page_info = pulls.get("pageInfo") or {}
        pages += 1
        if exhausted_window or not page_info.get("hasNextPage"):
            break
        if pages >= config.GQL_MAX_PAGES:
            activity.truncated.append(f"pull request history stopped after {pages} pages")
            break
        cursor = page_info.get("endCursor")

    for key, numbers in reviewed_prs.items():
        activity.members[key].reviews = len(numbers)


def fetch_repo_activity(
    gh: GitHubClient, record: RepoRecord, *, since: datetime | None = None
) -> RepoActivity:
    """All contribution data for one repo: two GraphQL queries, not two per user."""
    since = since or window_start()
    activity = RepoActivity(
        repo_id=record.repo_id, full_name=record.full_name, window_start=since
    )

    try:
        fetch_commit_activity(gh, record, activity, since)
    except EmptyRepositoryError:
        activity.is_empty = True
    except NotFoundError:
        log.warning("%s disappeared during the scan; skipping its history", record.full_name)
        activity.truncated.append("repository not found during history fetch")
        return activity

    if not activity.is_empty:
        try:
            fetch_pull_activity(gh, record, activity, since)
        except NotFoundError:
            activity.truncated.append("pull requests unavailable")

    return activity


def store_activity(db: Database, run_id: int, activity: RepoActivity) -> None:
    for member in activity.members.values():
        db.upsert_contribution(
            run_id, activity.repo_id, member.login,
            commits=member.commits,
            prs_opened=member.prs_opened,
            prs_merged=member.prs_merged,
            reviews=member.reviews,
            last_commit_at=_iso(member.last_commit_at),
            last_pr_at=_iso(member.last_pr_at),
            last_review_at=_iso(member.last_review_at),
            last_activity_at=_iso(member.last_activity_at),
        )


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds") if value else None


def fetch_last_commit_ever(
    gh: GitHubClient, full_name: str, login: str
) -> datetime | None:
    """The member's most recent commit on this repo, of any age.

    The GraphQL history is deliberately window-scoped, which means it cannot
    answer "when did this person last commit?" for someone whose last commit
    predates the window - and that is precisely the person a reviewer needs a
    date for before revoking anything. One cheap REST call (`per_page=1`)
    fills that in.

    Called only for flagged members, so the cost is bounded by the size of the
    suggestion list, not by the size of the org. Returns None when the member
    has never committed here, or when the repo is empty (409).
    """
    try:
        result = gh.get(
            f"/repos/{full_name}/commits",
            {"author": login, "per_page": 1},
            allow_404=True,
            allow_403=True,
        )
    except EmptyRepositoryError:
        return None
    except GitHubError as exc:
        log.debug("Could not read last commit for %s on %s: %s", login, full_name, exc)
        return None

    commits = result.data or []
    if not isinstance(commits, list) or not commits:
        return None
    commit = (commits[0] or {}).get("commit") or {}
    author = commit.get("author") or {}
    return parse_ts(author.get("date"))


# --------------------------------------------------------------------------
# Orchestration for one repo
# --------------------------------------------------------------------------

def collect_repo(
    gh: GitHubClient,
    db: Database,
    run_id: int,
    record: RepoRecord,
    team_index: TeamIndex | None = None,
    *,
    since: datetime | None = None,
) -> tuple[dict[str, AccessEntry], RepoActivity]:
    """Fetch and store access + contribution data for one repo.

    Each stage is marked done as it completes, so a crash resumes at the next
    unfinished stage rather than refetching the whole repo.
    """
    since = since or window_start()

    entries = fetch_collaborators(gh, record, team_index)
    store_collaborators(db, run_id, record.repo_id, entries)
    db.mark_stage(run_id, record.repo_id, Stage.COLLABORATORS,
                  detail=f"{len(entries)} with access")

    activity = fetch_repo_activity(gh, record, since=since)

    with db.transaction():
        store_activity(db, run_id, activity)
        db.upsert_repo_stats(
            run_id, record.repo_id,
            total_commits=activity.total_commits,
            unattributed_commits=activity.unattributed_commits,
            contributor_count=activity.contributor_count,
            collaborator_count=len(entries),
            window_start=_iso(since),
        )
        if activity.is_empty:
            db.set_repo_scan_status(record.repo_id, "scanned", is_empty=True)
            db.upsert_exclusion(
                run_id, record.repo_id, "*", config.ExclusionReason.EMPTY_REPO,
                detail="repository has no commits",
            )
        db.mark_stage(
            run_id, record.repo_id, Stage.CONTRIBUTIONS,
            detail="; ".join(activity.truncated) if activity.truncated else
                  f"{activity.total_commits} commits, {activity.contributor_count} contributors",
        )

    return entries, activity

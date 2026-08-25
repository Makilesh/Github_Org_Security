"""Stage one: organization members, repositories, and security advisories.

Three jobs, in the order `main.py` runs them:

1. `fetch_org_members` - who belongs to the org, and which of them are owners.
   Owners are loaded first because "never flag an org owner" is a hard rule
   and the rest of the pipeline needs that set before it scores anything.
2. `fetch_repos` - every repo in the org, with the skip decision recorded
   rather than applied silently. A repo we chose not to scan is still a row
   in the database with a `skip_reason`.
3. `collect_advisories` - Dependabot alerts, org-wide in one paginated call
   where the token allows it, per-repo where it does not.

The org-level endpoint matters more than it looks. For a 100-repo org it is
roughly 1-3 requests instead of 100, which is the difference between a scan
that fits comfortably in the hourly quota and one that does not.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Iterator, Mapping

import config
from client import (
    EmptyRepositoryError,
    ForbiddenError,
    GitHubClient,
    GitHubError,
    NotFoundError,
)
from db import Database, Stage

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Repository selection
# --------------------------------------------------------------------------

@dataclass
class RepoRecord:
    """A repository plus the decision we made about it."""

    raw: Mapping[str, Any]
    skip_reason: str | None = None      # None means "scan it"
    tags: list[str] = field(default_factory=list)

    @property
    def repo_id(self) -> int:
        return int(self.raw["id"])

    @property
    def name(self) -> str:
        return str(self.raw.get("name", ""))

    @property
    def full_name(self) -> str:
        return str(self.raw.get("full_name", ""))

    @property
    def owner(self) -> str:
        return str((self.raw.get("owner") or {}).get("login", ""))

    @property
    def is_archived(self) -> bool:
        return bool(self.raw.get("archived"))

    @property
    def is_fork(self) -> bool:
        return bool(self.raw.get("fork"))

    @property
    def created_at(self) -> datetime | None:
        return parse_ts(self.raw.get("created_at"))

    @property
    def should_scan(self) -> bool:
        return self.skip_reason is None


def parse_ts(value: str | None) -> datetime | None:
    """Parse a GitHub ISO-8601 timestamp into an aware UTC datetime."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def window_start(now: datetime | None = None, lookback_days: int | None = None) -> datetime:
    """The oldest timestamp inside the lookback window."""
    now = now or datetime.now(timezone.utc)
    days = config.LOOKBACK_DAYS if lookback_days is None else lookback_days
    return now - timedelta(days=days)


def decide_repo(repo: Mapping[str, Any], *, now: datetime | None = None) -> RepoRecord:
    """Apply the repo-level selection rules, recording the reason.

    Note what is *not* here: being archived is a tag, not a skip. Stale admin
    access on an archived repository is still stale admin access, and an
    archived repo can always be unarchived by anyone holding that access.
    Being brand new is also not a skip at this stage - the repo is scanned and
    its advisories reported; it is only excluded from *access suggestions*
    later, in score.py, because there has not been time to build a history.
    """
    record = RepoRecord(raw=repo)
    name = str(repo.get("name", ""))

    if config.REPO_DENYLIST and name in config.REPO_DENYLIST:
        record.skip_reason = "on REPO_DENYLIST"
        return record

    if config.REPO_ALLOWLIST and name not in config.REPO_ALLOWLIST:
        record.skip_reason = "not on REPO_ALLOWLIST"
        return record

    if repo.get("fork"):
        record.tags.append(config.ExclusionReason.FORK_REPO)
        if config.SKIP_FORKS:
            record.skip_reason = "fork (SKIP_FORKS is on)"
            return record

    if repo.get("archived"):
        record.tags.append(config.ExclusionReason.ARCHIVED_REPO)
        if config.SKIP_ARCHIVED:
            record.skip_reason = "archived (SKIP_ARCHIVED is on)"
            return record

    if config.REPO_VISIBILITY in ("public", "private"):
        visibility = repo.get("visibility") or ("private" if repo.get("private") else "public")
        if visibility != config.REPO_VISIBILITY:
            record.skip_reason = f"visibility is {visibility}, wanted {config.REPO_VISIBILITY}"
            return record

    created = parse_ts(repo.get("created_at"))
    if created and created > window_start(now):
        # Scanned, but tagged: score.py will not suggest removals here.
        record.tags.append(config.ExclusionReason.NEW_REPO)

    if repo.get("size") == 0 and not repo.get("pushed_at"):
        record.tags.append(config.ExclusionReason.EMPTY_REPO)

    return record


def fetch_repos(gh: GitHubClient, org: str) -> list[RepoRecord]:
    """Every repository in the org, with skip decisions attached.

    `type=all` is deliberate: the default (`all` on the org endpoint) includes
    private repos the token can see, and those are exactly the ones where
    stale access matters most.
    """
    records: list[RepoRecord] = []
    for raw in gh.paginate(f"/orgs/{org}/repos", {"type": "all", "sort": "full_name"}):
        records.append(decide_repo(raw))
        if config.MAX_REPOS and len(records) >= config.MAX_REPOS:
            log.warning("Stopping repo enumeration at MAX_REPOS=%d", config.MAX_REPOS)
            break

    scanning = sum(1 for r in records if r.should_scan)
    log.info("Found %d repos in %s; scanning %d, skipping %d",
             len(records), org, scanning, len(records) - scanning)
    return records


# --------------------------------------------------------------------------
# Organization members and owners
# --------------------------------------------------------------------------

def fetch_org_members(gh: GitHubClient, db: Database, run_id: int, org: str) -> set[str]:
    """Record org members and return the set of owner logins (lowercased).

    Owners come from `role=admin` on the members endpoint. If the token cannot
    read the member list we return an empty set and log loudly - the scan can
    still run, but every owner would then be scored like anyone else, so the
    caller is told rather than left to discover it in the output.
    """
    owners: set[str] = set()

    try:
        for member in gh.paginate(f"/orgs/{org}/members", {"role": "admin"}):
            login = str(member.get("login", ""))
            if not login:
                continue
            owners.add(login.lower())
            db.upsert_org_member(
                run_id, login, user_id=member.get("id"),
                user_type=member.get("type"), org_role="admin", is_owner=True,
            )
    except (ForbiddenError, NotFoundError) as exc:
        log.error(
            "Could not read org owners (%s). Every member will be scored as a "
            "normal collaborator, so review the suggestions before acting on them.",
            exc,
        )
        return owners

    for member in gh.paginate(f"/orgs/{org}/members", {"role": "member"}, allow_403=True):
        login = str(member.get("login", ""))
        if not login or login.lower() in owners:
            continue
        db.upsert_org_member(
            run_id, login, user_id=member.get("id"),
            user_type=member.get("type"), org_role="member", is_owner=False,
        )

    log.info("Org %s has %d owner(s)", org, len(owners))
    return owners


# --------------------------------------------------------------------------
# Advisories
# --------------------------------------------------------------------------

@dataclass
class AdvisoryHarvest:
    """Where the alerts came from, and what we got."""

    by_repo: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    source: str = "repo"                    # 'org' | 'repo'
    org_endpoint_available: bool = False
    org_endpoint_error: str | None = None
    repos_without_dependabot: list[str] = field(default_factory=list)
    total: int = 0


#: Dependabot alert states worth reporting. `auto_dismissed` is a real state
#: GitHub sets when an alert is closed by a rule rather than a person, and it
#: is kept distinct from `dismissed` because "a human decided this is fine" and
#: "a rule closed it automatically" are different claims.
ALERT_STATES = ("open", "fixed", "dismissed", "auto_dismissed")


def fetch_org_advisories(gh: GitHubClient, org: str) -> AdvisoryHarvest:
    """One paginated pass over the whole org's Dependabot alerts.

    Returns a harvest with `org_endpoint_available=False` (and the reason) if
    the endpoint is not usable, rather than raising. The caller then falls
    back to per-repo. A 403 here is common and benign: it usually means the
    fine-grained token lacks org-level "Dependabot alerts: read", not that
    anything is wrong.
    """
    harvest = AdvisoryHarvest(source="org")
    try:
        for alert in gh.paginate(
            f"/orgs/{org}/dependabot/alerts",
            {"state": ",".join(ALERT_STATES), "per_page": config.PER_PAGE},
        ):
            repo_name = ((alert.get("repository") or {}).get("full_name") or "").lower()
            if not repo_name:
                continue
            harvest.by_repo[repo_name].append(alert)
            harvest.total += 1
        harvest.org_endpoint_available = True
        log.info("Org-level Dependabot endpoint returned %d alerts across %d repos",
                 harvest.total, len(harvest.by_repo))
    except (ForbiddenError, NotFoundError) as exc:
        harvest.org_endpoint_error = str(exc)
        log.warning("Org-level Dependabot endpoint unavailable (%s); falling back to per-repo",
                    exc)
    except GitHubError as exc:
        harvest.org_endpoint_error = str(exc)
        log.warning("Org-level Dependabot request failed (%s); falling back to per-repo", exc)

    return harvest


def fetch_repo_advisories(gh: GitHubClient, full_name: str) -> tuple[list[dict[str, Any]], str | None]:
    """Alerts for one repo. Returns (alerts, unavailable_reason).

    A 403 or 404 here means Dependabot is disabled for the repo (or the token
    cannot see it). That is not an error in the scan - most orgs have repos
    with Dependabot off - so it is returned as a reason string and surfaced in
    the dashboard, not raised.
    """
    alerts: list[dict[str, Any]] = []
    try:
        for alert in gh.paginate(
            f"/repos/{full_name}/dependabot/alerts",
            {"state": ",".join(ALERT_STATES)},
            allow_404=True,
            allow_403=True,
        ):
            alerts.append(alert)
    except GitHubError as exc:
        return [], str(exc)
    return alerts, None


def collect_advisories(
    gh: GitHubClient,
    db: Database,
    run_id: int,
    org: str,
    repos: Iterable[RepoRecord],
    *,
    harvest: AdvisoryHarvest | None = None,
) -> AdvisoryHarvest:
    """Store advisories for every scannable repo, org endpoint first.

    Progress is committed per repo, so an interrupted run resumes at the next
    unfinished repo rather than refetching everything.
    """
    repos = list(repos)

    if harvest is None:
        harvest = (
            fetch_org_advisories(gh, org)
            if config.ORG_DEPENDABOT_FIRST
            else AdvisoryHarvest(source="repo")
        )

    already_done = db.completed_repo_ids(run_id, Stage.ADVISORIES)

    for record in repos:
        if not record.should_scan:
            continue
        if record.repo_id in already_done:
            log.debug("Advisories for %s already collected in this run; skipping",
                      record.full_name)
            continue

        if harvest.org_endpoint_available:
            alerts = harvest.by_repo.get(record.full_name.lower(), [])
            source = "org"
            unavailable = None
        else:
            alerts, unavailable = fetch_repo_advisories(gh, record.full_name)
            source = "repo"
            harvest.by_repo[record.full_name.lower()] = alerts
            harvest.total += len(alerts)
            if unavailable:
                harvest.repos_without_dependabot.append(record.full_name)

        open_count = 0
        with db.transaction():
            for alert in alerts:
                db.upsert_advisory(run_id, record.repo_id, alert, source=source)
                if alert.get("state") == "open":
                    open_count += 1
            db.upsert_repo_stats(run_id, record.repo_id, open_advisories=open_count)
            db.mark_stage(
                run_id, record.repo_id, Stage.ADVISORIES,
                detail=f"{len(alerts)} alerts via {source}"
                + (f"; {unavailable}" if unavailable else ""),
            )

    return harvest


# --------------------------------------------------------------------------
# Repo registration
# --------------------------------------------------------------------------

def register_repos(db: Database, run_id: int, repos: Iterable[RepoRecord]) -> tuple[int, int]:
    """Write every repo row, including the ones we decided not to scan.

    Returns (scanning, skipped). Skipped repos are stored too: a dashboard
    that quietly omits them would hide part of the org from the reader.
    """
    scanning = skipped = 0
    for record in repos:
        db.upsert_repo(run_id, record.raw)
        if record.should_scan:
            db.set_repo_scan_status(record.repo_id, "scanned")
            scanning += 1
        else:
            db.set_repo_scan_status(record.repo_id, "skipped", skip_reason=record.skip_reason)
            db.upsert_exclusion(
                run_id, record.repo_id, "*",
                _skip_reason_code(record.skip_reason),
                detail=record.skip_reason,
            )
            skipped += 1

        for tag in record.tags:
            db.upsert_exclusion(run_id, record.repo_id, "*", tag, detail=tag_detail(tag))

    return scanning, skipped


#: Prose for a repository tag. A dashboard row reading "repository tag" tells
#: the reader nothing; these say what the tag actually meant for the scan.
TAG_DETAILS: dict[str, str] = {
    config.ExclusionReason.ARCHIVED_REPO:
        "Archived, but still scanned - access on an archived repo is still live "
        "access, and anyone holding it can unarchive the repo.",
    config.ExclusionReason.FORK_REPO:
        "A fork. Contribution history here mostly belongs to the upstream project.",
    config.ExclusionReason.NEW_REPO:
        "Created inside the lookback window, so nobody has had time to build a "
        "history yet. Advisories are still reported.",
    config.ExclusionReason.EMPTY_REPO:
        "No commits, so there is no contribution history to judge.",
}


def tag_detail(tag: str) -> str:
    return TAG_DETAILS.get(tag, "repository tag")


def _skip_reason_code(reason: str | None) -> str:
    text = (reason or "").lower()
    if "fork" in text:
        return config.ExclusionReason.FORK_REPO
    if "archived" in text:
        return config.ExclusionReason.ARCHIVED_REPO
    return "repo_filtered"

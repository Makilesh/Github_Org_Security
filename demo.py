"""Fixture-backed demo mode: the whole pipeline with no token and no org.

Why this exists, in the words of the brief: *use dummy/mock repos for any part
of the API that is impractical to access, and say clearly where you mocked
something and why.*

What is mocked: the GitHub API responses themselves, in `fixtures/demo_org.json`.
Private Dependabot advisories need an organization with paid security features
and genuinely vulnerable dependencies, which is not something a candidate can
conjure for a review. Everything downstream of the API - the parsing, the
three-way collaborator split, the scoring, the exclusion rules, the database,
the dashboard - is the *same code* that runs against api.github.com. The
fixtures are shaped like real API payloads and are fed through the same
`db.upsert_advisory` / `score.assess_repo` functions, not through a parallel
demo-only path.

What is real: everything except the network.

The fixture pins `as_of`, so demo output is reproducible. Running it today and
running it in three months produce identical scores, which is what makes it
usable as a reference result during a walkthrough.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

import config
from contrib import AccessEntry
from db import Database, Stage
from scan import RepoRecord, decide_repo, parse_ts, tag_detail
from score import MemberInput, RepoInput

log = logging.getLogger(__name__)

DEFAULT_FIXTURE = config.PROJECT_ROOT / "fixtures" / "demo_org.json"


@dataclass
class DemoOrg:
    """A parsed fixture file."""

    org: str
    as_of: datetime
    lookback_days: int
    owners: set[str]
    allowlist: tuple[str, ...]
    repos: list[dict[str, Any]] = field(default_factory=list)
    teams: dict[str, Any] = field(default_factory=dict)
    people: dict[str, Any] = field(default_factory=dict)
    source_path: Path | None = None

    def person(self, login: str) -> dict[str, Any]:
        return self.people.get(login, {})


def load_org(path: str | Path | None = None) -> DemoOrg:
    path = Path(path or DEFAULT_FIXTURE)
    raw = json.loads(path.read_text(encoding="utf-8"))
    as_of = parse_ts(raw.get("as_of")) or datetime.now(timezone.utc)
    return DemoOrg(
        org=raw["org"],
        as_of=as_of,
        lookback_days=int(raw.get("lookback_days", config.LOOKBACK_DAYS)),
        owners={o.lower() for o in raw.get("owners", [])},
        allowlist=tuple(raw.get("allowlist", [])),
        repos=raw.get("repos", []),
        teams=raw.get("teams", {}),
        people=raw.get("people", {}),
        source_path=path,
    )


# --------------------------------------------------------------------------
# Fixture -> the shapes the real pipeline uses
# --------------------------------------------------------------------------

def to_repo_record(repo: Mapping[str, Any], org: str) -> RepoRecord:
    """Build the same RepoRecord the live scanner builds, skip rules included."""
    raw = dict(repo)
    raw.setdefault("owner", {"login": org})
    raw.pop("collaborators", None)
    raw.pop("activity", None)
    raw.pop("advisories", None)
    raw.pop("last_commit_ever", None)
    raw.pop("unattributed_commits", None)
    return decide_repo(raw)


def to_alert(advisory: Mapping[str, Any], index: int) -> dict[str, Any]:
    """Reshape a fixture advisory into a real Dependabot alert payload.

    Deliberately verbose: this goes through `db.upsert_advisory` unchanged, so
    the fixture exercises the same parsing (GHSA extraction, CVE identifier
    lookup, manifest path, severity normalisation) as a live alert.
    """
    identifiers = [{"type": "GHSA", "value": advisory["ghsa_id"]}]
    if advisory.get("cve_id"):
        identifiers.append({"type": "CVE", "value": advisory["cve_id"]})

    return {
        "number": index,
        "state": advisory.get("state", "open"),
        "created_at": advisory.get("created_at"),
        "updated_at": advisory.get("updated_at") or advisory.get("created_at"),
        "dismissed_at": advisory.get("dismissed_at"),
        "dismissed_reason": advisory.get("dismissed_reason"),
        "fixed_at": advisory.get("fixed_at"),
        "html_url": advisory.get(
            "html_url", f"https://github.com/advisories/{advisory['ghsa_id']}"
        ),
        "dependency": {
            "package": {
                "name": advisory.get("package"),
                "ecosystem": advisory.get("ecosystem"),
            },
            "manifest_path": advisory.get("manifest_path", ""),
        },
        "security_advisory": {
            "ghsa_id": advisory["ghsa_id"],
            "severity": advisory.get("severity", "unknown"),
            "summary": advisory.get("summary"),
            "identifiers": identifiers,
        },
    }


def to_access_entries(
    repo: Mapping[str, Any], org: DemoOrg
) -> dict[str, AccessEntry]:
    """Rebuild the three-way access split from the fixture."""
    entries: dict[str, AccessEntry] = {}
    for collab in repo.get("collaborators", []):
        login = collab["login"]
        person = org.person(login)
        entry = entries.setdefault(
            login.lower(),
            AccessEntry(
                login=login,
                user_id=person.get("id"),
                user_type=person.get("type", "User"),
                role_name=collab.get("permission"),
            ),
        )
        affiliation = collab.get("affiliation", "direct")
        permission = collab.get("permission")
        if affiliation == "direct":
            entry.direct = permission
        elif affiliation == "outside":
            entry.outside = permission
        elif affiliation == "team":
            entry.team = permission
            for slug in collab.get("teams", []):
                name = (org.teams.get(slug) or {}).get("name", slug)
                if name not in entry.teams:
                    entry.teams.append(name)
    return entries


def to_score_inputs(
    repo: Mapping[str, Any], org: DemoOrg
) -> tuple[RepoInput, list[MemberInput], dict[str, Any]]:
    """Produce exactly what `score.assess_repo` expects."""
    entries = to_access_entries(repo, org)
    activity = repo.get("activity", {})
    last_ever = repo.get("last_commit_ever", {})

    members: list[MemberInput] = []
    for key, entry in sorted(entries.items()):
        stats = activity.get(entry.login, {})
        last_commit = parse_ts(stats.get("last_commit_at"))
        last_review = parse_ts(stats.get("last_review_at"))
        stamps = [s for s in (last_commit, last_review) if s]
        members.append(
            MemberInput(
                login=entry.login,
                permission=entry.effective,
                commits=int(stats.get("commits", 0)),
                reviews=int(stats.get("reviews", 0)),
                prs_merged=int(stats.get("prs_merged", 0)),
                prs_opened=int(stats.get("prs_opened", 0)),
                last_commit_at=last_commit,
                last_review_at=last_review,
                last_activity_at=max(stamps) if stamps else None,
                user_type=org.person(entry.login).get("type", "User"),
                is_direct=entry.is_direct,
                is_outside=entry.is_outside,
                is_team=entry.is_team,
                teams=tuple(entry.teams),
                access_label=entry.access_label,
            )
        )

    repo_input = RepoInput(
        repo_id=int(repo["id"]),
        full_name=repo["full_name"],
        created_at=parse_ts(repo.get("created_at")),
        contributor_count=len(activity),
        is_archived=bool(repo.get("archived")),
        is_fork=bool(repo.get("fork")),
        is_empty=not activity and not repo.get("pushed_at"),
    )

    extras = {"entries": entries, "last_commit_ever": last_ever}
    return repo_input, members, extras


# --------------------------------------------------------------------------
# Seeding the database
# --------------------------------------------------------------------------

def seed_database(db: Database, run_id: int, org: DemoOrg) -> dict[str, int]:
    """Write the fixture through the same upserts the live scan uses."""
    counts = {"repos_scanned": 0, "repos_skipped": 0, "advisories": 0, "collaborators": 0}

    for login, person in org.people.items():
        db.upsert_org_member(
            run_id, login, user_id=person.get("id"), user_type=person.get("type"),
            org_role="admin" if login.lower() in org.owners else "member",
            is_owner=login.lower() in org.owners,
        )

    for repo in org.repos:
        record = to_repo_record(repo, org.org)
        db.upsert_repo(run_id, record.raw)

        if not record.should_scan:
            db.set_repo_scan_status(record.repo_id, "skipped", skip_reason=record.skip_reason)
            db.upsert_exclusion(
                run_id, record.repo_id, "*", config.ExclusionReason.FORK_REPO,
                detail=record.skip_reason,
            )
            counts["repos_skipped"] += 1
            continue

        db.set_repo_scan_status(record.repo_id, "scanned")
        counts["repos_scanned"] += 1

        for tag in record.tags:
            db.upsert_exclusion(run_id, record.repo_id, "*", tag, detail=tag_detail(tag))

        open_advisories = 0
        with db.transaction():
            for index, advisory in enumerate(repo.get("advisories", []), start=1):
                db.upsert_advisory(run_id, record.repo_id, to_alert(advisory, index),
                                   source="org")
                counts["advisories"] += 1
                if advisory.get("state") == "open":
                    open_advisories += 1

            entries = to_access_entries(repo, org)
            db.clear_collaborators(record.repo_id)
            for entry in entries.values():
                teams = ", ".join(entry.teams) if entry.teams else None
                if entry.is_direct:
                    db.upsert_collaborator(run_id, record.repo_id, entry.login,
                                           affiliation="direct", permission=entry.direct,
                                           user_id=entry.user_id, user_type=entry.user_type)
                if entry.is_outside:
                    db.upsert_collaborator(run_id, record.repo_id, entry.login,
                                           affiliation="outside", permission=entry.outside,
                                           user_id=entry.user_id, user_type=entry.user_type)
                if entry.is_team:
                    db.upsert_collaborator(run_id, record.repo_id, entry.login,
                                           affiliation="team", permission=entry.team,
                                           user_id=entry.user_id, user_type=entry.user_type,
                                           team_names=teams)
                db.set_effective_permission(record.repo_id, entry.login, entry.effective)
                counts["collaborators"] += 1

            activity = repo.get("activity", {})
            last_ever = repo.get("last_commit_ever", {})
            # Everyone with access *or* activity gets a contribution row, so
            # people with access and zero contribution are visible rather than
            # missing - they are the whole point of the review.
            for login in sorted(set(activity) | {e.login for e in entries.values()}):
                stats = activity.get(login, {})
                last_commit = stats.get("last_commit_at")
                last_review = stats.get("last_review_at")
                stamps = [s for s in (last_commit, last_review) if s]
                db.upsert_contribution(
                    run_id, record.repo_id, login,
                    commits=int(stats.get("commits", 0)),
                    prs_opened=int(stats.get("prs_opened", 0)),
                    prs_merged=int(stats.get("prs_merged", 0)),
                    reviews=int(stats.get("reviews", 0)),
                    last_commit_at=last_commit,
                    last_review_at=last_review,
                    last_activity_at=max(stamps) if stamps else None,
                )
                if login in last_ever:
                    db.set_last_commit_ever(record.repo_id, login, last_ever[login])

            db.upsert_repo_stats(
                run_id, record.repo_id,
                total_commits=sum(int(s.get("commits", 0)) for s in activity.values())
                + int(repo.get("unattributed_commits", 0)),
                unattributed_commits=int(repo.get("unattributed_commits", 0)),
                contributor_count=len(activity),
                collaborator_count=len(entries),
                open_advisories=open_advisories,
                window_start=(org.as_of.isoformat()),
            )
            db.mark_stage(run_id, record.repo_id, Stage.ADVISORIES, detail="demo fixture")
            db.mark_stage(run_id, record.repo_id, Stage.COLLABORATORS, detail="demo fixture")
            db.mark_stage(run_id, record.repo_id, Stage.CONTRIBUTIONS, detail="demo fixture")

    return counts


def iter_assessable(org: DemoOrg) -> Iterator[tuple[RepoInput, list[MemberInput], dict[str, Any]]]:
    """Yield scoring inputs for every repo the scanner would actually scan."""
    for repo in org.repos:
        record = to_repo_record(repo, org.org)
        if not record.should_scan:
            continue
        yield to_score_inputs(repo, org)

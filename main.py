"""CLI entry point.

    python main.py                 # scan the org in .env, write the dashboard
    python main.py --demo          # same pipeline on fixtures: no token needed
    python main.py --resume        # continue the last interrupted run
    python main.py --report-only   # re-render from the database, no API calls
    python main.py --json          # machine-readable run summary on stdout

The scoring pass reads from SQLite rather than from memory, deliberately: the
live scan and the demo run therefore share one code path, and `--report-only`
can rebuild the dashboard from a database produced days earlier.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import config
import demo as demo_mod
import report
from client import GitHubClient, GitHubError, RateLimitError
from contrib import AccessEntry, collect_repo, fetch_last_commit_ever, fetch_org_teams
from db import Database, RunStatus, Stage
from scan import (
    all_org_members,
    collect_advisories,
    fetch_org_members,
    fetch_org_profile,
    fetch_repos,
    parse_ts,
    register_repos,
    window_start,
)
from score import MemberInput, RepoInput, assess_repo

log = logging.getLogger("scanner")


def setup_logging(verbose: bool, quiet: bool) -> None:
    level = logging.DEBUG if verbose else (logging.WARNING if quiet else logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,      # stdout stays clean for --json
    )
    # httpcore logs every socket operation at DEBUG, which buries our own -v
    # output completely. Keep the transport quiet unless it actually fails.
    for noisy in ("httpx", "httpcore", "httpcore.http11", "httpcore.connection"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# --------------------------------------------------------------------------
# Scoring pass - shared by live and demo runs
# --------------------------------------------------------------------------

def member_input_from_row(row: Any) -> MemberInput:
    """Rebuild a scoring input from a database row, access paths intact."""
    entry = AccessEntry(login=row["login"], user_type=row["user_type"])
    entry.direct = row["permission_direct"]
    entry.outside = row["permission_outside"]
    entry.team = row["permission_team"]
    entry.base = row["permission_base"]
    if row["team_names"]:
        entry.teams = [t.strip() for t in str(row["team_names"]).split(",") if t.strip()]

    last_activity = parse_ts(row["last_activity_at"])
    return MemberInput(
        login=row["login"],
        permission=row["effective_permission"] or entry.effective,
        commits=row["commits"] or 0,
        reviews=row["reviews"] or 0,
        prs_merged=row["prs_merged"] or 0,
        prs_opened=row["prs_opened"] or 0,
        last_commit_at=parse_ts(row["last_commit_at"]),
        last_review_at=parse_ts(row["last_review_at"]),
        last_activity_at=last_activity,
        user_type=row["user_type"],
        is_direct=bool(row["is_direct"]),
        is_outside=bool(row["is_outside"]),
        is_team=bool(row["is_team"]),
        is_base=bool(row["is_base"]),
        teams=tuple(entry.teams),
        access_label=entry.access_label,
    )


def score_run(
    db: Database,
    run_id: int,
    *,
    owners: set[str],
    allowlist: Sequence[str],
    now: datetime,
) -> list:
    """Score every scanned repo and persist the results."""
    assessments = []

    for repo in db.repos_for_run(run_id):
        repo_input = RepoInput(
            repo_id=repo["repo_id"],
            full_name=repo["full_name"],
            created_at=parse_ts(repo["created_at"]),
            contributor_count=repo["contributor_count"] or 0,
            is_archived=bool(repo["is_archived"]),
            is_fork=bool(repo["is_fork"]),
            is_empty=bool(repo["is_empty"]),
        )
        members = [member_input_from_row(row) for row in db.members_for_repo(repo["repo_id"])]
        assessment = assess_repo(
            repo_input, members, org_owners=owners, allowlist=allowlist, now=now
        )

        with db.transaction():
            for scored in assessment.scored:
                db.upsert_score(
                    run_id, repo_input.repo_id, scored.login,
                    activity=scored.activity, score=scored.score, risk=scored.risk,
                    permission=scored.permission,
                    permission_weight=scored.permission_weight,
                    days_since_activity=scored.days_since_activity,
                    flagged=scored.flagged, reason=scored.reason,
                )
                if scored.excluded:
                    db.upsert_exclusion(
                        run_id, repo_input.repo_id, scored.login,
                        scored.excluded_reason or "unknown",
                        detail=scored.excluded_detail,
                        permission=scored.permission,
                    )
            if assessment.repo_exclusion:
                db.upsert_exclusion(
                    run_id, repo_input.repo_id, "*", assessment.repo_exclusion,
                    detail=assessment.repo_exclusion_detail,
                )
            db.mark_stage(run_id, repo_input.repo_id, Stage.SCORED,
                          detail=f"{len(assessment.flagged)} flagged")

        assessments.append(assessment)

    return assessments


def enrich_flagged(gh: GitHubClient, db: Database, run_id: int) -> int:
    """Fill in the all-time last commit date for flagged members only.

    Bounded by the size of the suggestion list, not the size of the org, and it
    turns 'no activity in the window' into a real date a reviewer can weigh.
    """
    rows = db.query(
        """
        SELECT s.repo_id, s.login, r.full_name
          FROM scores s
          JOIN repos r ON r.repo_id = s.repo_id
          LEFT JOIN contributions k ON k.repo_id = s.repo_id AND k.login = s.login
         WHERE s.run_id = ? AND s.flagged = 1
           AND (k.last_commit_at IS NULL AND k.last_commit_ever IS NULL)
        """,
        (run_id,),
    )

    filled = 0
    for row in rows:
        when = fetch_last_commit_ever(gh, row["full_name"], row["login"])
        db.upsert_contribution(run_id, row["repo_id"], row["login"])  # ensure the row exists
        db.set_last_commit_ever(
            row["repo_id"], row["login"],
            when.isoformat(timespec="seconds") if when else None,
        )
        if when:
            filled += 1
    if rows:
        log.info("Looked up historical commit dates for %d flagged members (%d found)",
                 len(rows), filled)
    return filled


# --------------------------------------------------------------------------
# Run modes
# --------------------------------------------------------------------------

def run_live(args: argparse.Namespace, db: Database) -> tuple[int, dict[str, Any]]:
    config.validate()
    org = args.org or config.GITHUB_ORG
    now = datetime.now(timezone.utc)

    resumed = db.resumable_run(org) if args.resume else None
    if resumed:
        run_id = int(resumed["run_id"])
        log.info("Resuming run #%d started at %s", run_id, resumed["started_at"])
    else:
        run_id = db.start_run(org, config.summary())
        log.info("Started run #%d for %s", run_id, org)

    if config.USE_ETAG_CACHE:
        purged = db.purge_cache(config.CACHE_TTL_HOURS)
        if purged:
            log.debug("Dropped %d stale cache entries", purged)

    with GitHubClient(cache=db, use_cache=not args.no_cache) as gh:
        quota = gh.check_auth()
        log.info("Token OK. REST quota %s/%s, GraphQL %s/%s",
                 quota["core_remaining"], quota["core_limit"],
                 quota["graphql_remaining"], quota["graphql_limit"])

        owners = fetch_org_members(gh, db, run_id, org)
        profile = fetch_org_profile(gh, org)
        members = all_org_members(db)
        repos = fetch_repos(gh, org)
        if not repos:
            log.error(
                "No repositories are visible in '%s'. Either the organization has "
                "none, or this token is not scoped to it (a fine-grained PAT must "
                "be created with '%s' as the resource owner, not your user account).",
                org, org,
            )
        scanning, skipped = register_repos(db, run_id, repos)
        log.info("Registered %d repos (%d to scan, %d skipped)",
                 len(repos), scanning, skipped)

        collect_advisories(gh, db, run_id, org, repos)

        team_index = fetch_org_teams(gh, org)
        since = window_start(now)
        already = db.completed_repo_ids(run_id, Stage.CONTRIBUTIONS)

        to_scan = [r for r in repos if r.should_scan]
        for index, record in enumerate(to_scan, start=1):
            if record.repo_id in already:
                log.debug("[%d/%d] %s already done in this run", index, len(to_scan),
                          record.full_name)
                continue
            log.info("[%d/%d] %s", index, len(to_scan), record.full_name)
            try:
                collect_repo(
                    gh, db, run_id, record, team_index, since=since,
                    org_base_permission=profile.get("default_repository_permission"),
                    org_members=members,
                )
            except RateLimitError:
                raise
            except GitHubError as exc:
                log.error("Failed on %s: %s", record.full_name, exc)
                db.set_repo_scan_status(record.repo_id, "error", error=str(exc))
                db.mark_stage(run_id, record.repo_id, Stage.CONTRIBUTIONS,
                              status="error", detail=str(exc))

        score_run(db, run_id, owners=owners, allowlist=config.LOGIN_ALLOWLIST, now=now)
        enrich_flagged(gh, db, run_id)
        score_run(db, run_id, owners=owners, allowlist=config.LOGIN_ALLOWLIST, now=now)

        api_stats = gh.stats.as_dict()

    return run_id, {"mode": "live", "api": api_stats, "now": now}


def run_demo(args: argparse.Namespace, db: Database) -> tuple[int, dict[str, Any]]:
    org = demo_mod.load_org(args.fixture)
    log.info("Demo mode: %s from %s, clock pinned to %s",
             org.org, org.source_path, org.as_of.isoformat())

    run_id = db.start_run(org.org, {**config.summary(), "mode": "demo",
                                    "org": org.org, "as_of": org.as_of.isoformat()})
    demo_mod.seed_database(db, run_id, org)
    score_run(db, run_id, owners=org.owners, allowlist=org.allowlist, now=org.as_of)

    return run_id, {"mode": "demo", "api": {}, "now": org.as_of}


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def build_summary(
    db: Database, run_id: int, meta: dict[str, Any], dashboard: Path | None
) -> dict[str, Any]:
    run = db.query_one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    counts = db.run_counts(run_id)

    top = [
        {
            "login": row["login"],
            "repo": row["full_name"],
            "permission": row["permission"],
            "risk": round(row["risk"], 4),
            "score": round(row["score"], 4),
            "team_inherited": bool(row["is_team"]) and not bool(row["is_direct"]),
        }
        for row in db.query(
            """
            SELECT s.login, s.permission, s.risk, s.score, r.full_name,
                   c.is_team, c.is_direct
              FROM scores s
              JOIN repos r ON r.repo_id = s.repo_id
              LEFT JOIN collaborators c ON c.repo_id = s.repo_id AND c.login = s.login
             WHERE s.run_id = ? AND s.flagged = 1
             ORDER BY s.risk DESC, s.login, s.repo_id
             LIMIT 5
            """,
            (run_id,),
        )
    ]

    return {
        "run_id": run_id,
        "org": run["org"],
        "mode": meta.get("mode"),
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "status": run["status"],
        "repos_scanned": counts["repos_scanned"],
        "repos_skipped": counts["repos_skipped"],
        "repos_errored": counts["repos_errored"],
        "repos_excluded": counts["repos_excluded"],
        "advisories_found": counts["advisories_found"],
        "advisories_open": counts["advisories_open"],
        "suggestions_made": counts["suggestions_made"],
        "members_excluded": counts["members_excluded"],
        "collaborators_seen": counts["collaborators"],
        "top_suggestions": top,
        "dashboard": str(dashboard) if dashboard else None,
        "database": str(db.path),
        "api": meta.get("api", {}),
    }


def print_human_summary(summary: dict[str, Any]) -> None:
    lines = [
        "",
        f"  Run #{summary['run_id']} - {summary['org']} ({summary['mode']})",
        f"  {'-' * 58}",
        f"  Repositories scanned    {summary['repos_scanned']}"
        + (f"  ({summary['repos_skipped']} skipped)" if summary["repos_skipped"] else ""),
        f"  Advisories found        {summary['advisories_found']}"
        f"  ({summary['advisories_open']} still open)",
        f"  Access suggestions      {summary['suggestions_made']}",
        f"  Excluded from review    {summary['members_excluded']}",
    ]
    if summary["repos_errored"]:
        lines.append(f"  Repositories errored    {summary['repos_errored']}")
    if summary.get("api"):
        api = summary["api"]
        lines.append(
            f"  API                     {api.get('requests', 0)} requests, "
            f"{api.get('cache_hits', 0)} served from cache, "
            f"{api.get('rate_limit_sleeps', 0)} rate-limit pauses"
        )
    if summary["top_suggestions"]:
        lines.append("")
        lines.append("  Highest risk:")
        for item in summary["top_suggestions"]:
            via = " (team-inherited)" if item["team_inherited"] else ""
            lines.append(
                f"    {item['risk']:>5.2f}  {item['login']} - {item['permission']}"
                f" on {item['repo']}{via}"
            )
    if summary.get("dashboard"):
        lines += ["", f"  Dashboard: {summary['dashboard']}"]
    lines.append("")
    print("\n".join(lines))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Scan a GitHub organization for security advisories and stale access.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Access removals are only ever suggested. This tool never revokes anything.\n"
        ),
    )
    parser.add_argument("--org", help="organization login (defaults to GITHUB_ORG in .env)")
    parser.add_argument("--demo", action="store_true",
                        help="run the full pipeline on bundled fixtures; no token required")
    parser.add_argument("--fixture", help="fixture file for --demo",
                        default=None)
    parser.add_argument("--resume", action="store_true",
                        help="continue the most recent unfinished run instead of starting over")
    parser.add_argument("--report-only", action="store_true",
                        help="re-render the dashboard from the database without calling the API")
    parser.add_argument("--run-id", type=int,
                        help="run to render with --report-only (default: the latest)")
    parser.add_argument("--json", action="store_true",
                        help="print a machine-readable run summary to stdout")
    parser.add_argument("--db", help="SQLite path (default: data/scanner.sqlite3)")
    parser.add_argument("--out", help="dashboard output path (default: out/dashboard.html)")
    parser.add_argument("--limit", type=int, help="scan at most N repositories")
    parser.add_argument("--no-cache", action="store_true",
                        help="ignore stored ETags and refetch everything")
    parser.add_argument("--no-report", action="store_true", help="skip rendering the dashboard")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-q", "--quiet", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose, args.quiet or args.json)

    if args.limit:
        config.MAX_REPOS = args.limit

    db_path = Path(args.db) if args.db else config.DB_PATH
    out_path = Path(args.out) if args.out else config.REPORT_PATH

    status = RunStatus.COMPLETED
    error: str | None = None
    dashboard: Path | None = None

    with Database(db_path) as db:
        try:
            if args.report_only:
                latest = db.latest_run(args.org or None)
                run_id = args.run_id or (int(latest["run_id"]) if latest else None)
                if not run_id:
                    print("No run found in the database to render.", file=sys.stderr)
                    return 2
                meta = {"mode": "report-only", "api": {}, "now": datetime.now(timezone.utc)}
            elif args.demo:
                run_id, meta = run_demo(args, db)
            else:
                run_id, meta = run_live(args, db)

            if not args.no_report:
                dashboard = report.render(db, run_id, out_path, now=meta.get("now"))

        except KeyboardInterrupt:
            status, error = RunStatus.INTERRUPTED, "interrupted by user"
            log.warning("Interrupted. Progress is saved; re-run with --resume to continue.")
            return 130
        except (config.ConfigError, GitHubError) as exc:
            status, error = RunStatus.FAILED, str(exc)
            log.error("%s", exc)
            if isinstance(exc, RateLimitError):
                log.error("Progress is saved. Re-run with --resume once the quota resets.")
            return 1

        if not args.report_only:
            counts = db.run_counts(run_id)
            db.finish_run(
                run_id, status,
                repos_scanned=counts["repos_scanned"],
                repos_skipped=counts["repos_skipped"],
                advisories_found=counts["advisories_found"],
                suggestions_made=counts["suggestions_made"],
                members_excluded=counts["members_excluded"],
                error=error,
            )

        summary = build_summary(db, run_id, meta, dashboard)

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print_human_summary(summary)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

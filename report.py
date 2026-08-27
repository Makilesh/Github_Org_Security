"""Stage four: render the dashboard.

The brief's real requirement is the hard part: *a security lead could read it
in under two minutes*. That drove three decisions here.

1. **Answers before data.** Every section opens with a plain sentence stating
   what the numbers mean ("3 people hold admin access they have not used in
   six months"). The table underneath is the evidence for that sentence, not a
   puzzle the reader has to solve.
2. **Evidence next to every suggestion.** A removal suggestion with no dates
   next to it is an accusation. Each row carries last commit, last review,
   permission, how the access was granted, and the sentence explaining the
   flag - enough for a reviewer to decide without opening GitHub.
3. **The exclusion panel is a feature, not an appendix.** A reviewer who
   cannot see who was skipped has no reason to trust the list of who was not.

Output is one self-contained HTML file with Chart.js inlined, so it can be
emailed, committed, or opened from a USB stick with no server and no network.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from jinja2 import Environment, FileSystemLoader, select_autoescape

import config
from db import Database
from scan import parse_ts
from score import remediation_note

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Formatting helpers exposed to the template
# --------------------------------------------------------------------------

def fmt_date(value: str | None) -> str:
    parsed = parse_ts(value)
    return parsed.strftime("%d %b %Y") if parsed else "-"


def age_days(value: str | None, now: datetime | None = None) -> int | None:
    parsed = parse_ts(value)
    if not parsed:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0, int((now - parsed).total_seconds() // 86400))


def fmt_ago(value: str | None, now: datetime | None = None) -> str:
    """'12 Mar 2026 (173 days ago)' - the date and the distance both matter."""
    days = age_days(value, now)
    if days is None:
        return "never"
    return f"{fmt_date(value)} ({days} days ago)"


def humanise_permission(value: str | None) -> str:
    return (value or "unknown").replace("_", " ").title()


# --------------------------------------------------------------------------
# Context building
# --------------------------------------------------------------------------

def advisory_context(db: Database, run_id: int, now: datetime) -> dict[str, Any]:
    """Section 1: what is broken, how badly, and for how long."""
    rows = db.query(
        """
        SELECT a.*, r.full_name, r.name AS repo_name, r.is_archived, r.html_url AS repo_url
          FROM advisories a
          JOIN repos r ON r.repo_id = a.repo_id
         WHERE a.run_id = ?
        """,
        (run_id,),
    )

    by_severity: dict[str, dict[str, int]] = {
        sev: {"open": 0, "fixed": 0, "dismissed": 0, "total": 0}
        for sev in config.SEVERITY_ORDER
    }
    by_state: dict[str, int] = defaultdict(int)
    per_repo: dict[str, dict[str, Any]] = {}
    open_rows: list[dict[str, Any]] = []

    for row in rows:
        severity = (row["severity"] or "unknown").lower()
        if severity not in by_severity:
            by_severity[severity] = {"open": 0, "fixed": 0, "dismissed": 0, "total": 0}

        state = (row["state"] or "unknown").lower()
        # auto_dismissed is grouped with dismissed for the headline counts, but
        # the raw state is preserved in the database and in the state chart.
        bucket = "dismissed" if state.endswith("dismissed") else state
        if bucket not in by_severity[severity]:
            by_severity[severity][bucket] = 0

        by_severity[severity][bucket] += 1
        by_severity[severity]["total"] += 1
        by_state[state] += 1

        repo = per_repo.setdefault(
            row["full_name"],
            {"full_name": row["full_name"], "name": row["repo_name"],
             "url": row["repo_url"], "is_archived": bool(row["is_archived"]),
             "open_total": 0, **{sev: 0 for sev in config.SEVERITY_ORDER}},
        )

        if state == "open":
            repo["open_total"] += 1
            if severity in repo:
                repo[severity] += 1
            open_rows.append({
                "ghsa_id": row["ghsa_id"],
                "cve_id": row["cve_id"],
                "severity": severity,
                "package": row["package_name"],
                "ecosystem": row["ecosystem"],
                "manifest": row["manifest_path"],
                "summary": row["summary"],
                "repo": row["full_name"],
                "repo_url": row["repo_url"],
                "url": row["html_url"],
                "created_at": row["created_at"],
                "age_days": age_days(row["created_at"], now) or 0,
                "is_archived": bool(row["is_archived"]),
            })

    open_rows.sort(key=lambda a: (-a["age_days"], a["ghsa_id"]))
    most_affected = sorted(
        (r for r in per_repo.values() if r["open_total"]),
        key=lambda r: (-r["open_total"], r["full_name"]),
    )[: config.TOP_AFFECTED_REPOS]

    total_open = sum(v["open"] for v in by_severity.values())
    critical_high = sum(
        by_severity.get(sev, {}).get("open", 0) for sev in ("critical", "high")
    )

    return {
        "rows_total": len(rows),
        "by_severity": by_severity,
        "by_state": dict(by_state),
        "total_open": total_open,
        "critical_high_open": critical_high,
        "oldest_unresolved": open_rows[:8],
        "oldest": open_rows[0] if open_rows else None,
        "most_affected": most_affected,
        "chart_severity": {
            "labels": [s.title() for s in config.SEVERITY_ORDER],
            "data": [by_severity.get(s, {}).get("open", 0) for s in config.SEVERITY_ORDER],
            "colors": [config.SEVERITY_COLORS[s] for s in config.SEVERITY_ORDER],
        },
        "chart_state": {
            "labels": [s.replace("_", " ").title() for s in sorted(by_state)],
            "data": [by_state[s] for s in sorted(by_state)],
        },
        "chart_repos": {
            "labels": [r["name"] for r in most_affected],
            "series": [
                {
                    "label": sev.title(),
                    "color": config.SEVERITY_COLORS[sev],
                    "data": [r[sev] for r in most_affected],
                }
                for sev in config.SEVERITY_ORDER
            ],
        },
    }


def access_path_label(row: Mapping[str, Any]) -> str:
    """The three access kinds, never merged into one word."""
    parts = []
    if row["is_direct"]:
        parts.append(f"Direct ({humanise_permission(row['permission_direct'])})")
    if row["is_outside"]:
        parts.append(f"Outside collaborator ({humanise_permission(row['permission_outside'])})")
    if row["is_team"]:
        teams = row["team_names"] or "a team"
        parts.append(f"Team: {teams} ({humanise_permission(row['permission_team'])})")
    if row["is_base"]:
        parts.append(f"Org base permission ({humanise_permission(row['permission_base'])})")
    return " + ".join(parts) or "No current access"


def repo_context(db: Database, run_id: int, now: datetime) -> list[dict[str, Any]]:
    """Section 2: per-repo member tables."""
    repos = []
    repo_exclusions = defaultdict(list)
    for row in db.query(
        "SELECT repo_id, reason, detail FROM exclusions WHERE run_id = ? AND login = '*'",
        (run_id,),
    ):
        repo_exclusions[row["repo_id"]].append((row["reason"], row["detail"]))

    for repo in db.repos_for_run(run_id):
        members = []
        for row in db.query(
            """
            SELECT s.*, c.is_direct, c.is_outside, c.is_team, c.team_names,
                   c.permission_direct, c.permission_outside, c.permission_team,
                   c.user_type, c.is_base, c.permission_base,
                   k.commits, k.prs_opened, k.prs_merged, k.reviews,
                   k.last_commit_at, k.last_review_at, k.last_activity_at,
                   k.last_commit_ever,
                   e.reason AS exclusion_reason, e.detail AS exclusion_detail
              FROM scores s
              LEFT JOIN collaborators c ON c.repo_id = s.repo_id AND c.login = s.login
              LEFT JOIN contributions k ON k.repo_id = s.repo_id AND k.login = s.login
              LEFT JOIN exclusions   e ON e.repo_id = s.repo_id AND e.login = s.login
             WHERE s.repo_id = ?
             ORDER BY s.score DESC, s.login
            """,
            (repo["repo_id"],),
        ):
            members.append({
                "login": row["login"],
                "permission": humanise_permission(row["permission"]),
                "access_path": access_path_label(row),
                "is_team_only": bool(row["is_team"]) and not (row["is_direct"] or row["is_outside"]),
                "is_base_only": bool(row["is_base"]) and not (
                    row["is_direct"] or row["is_outside"] or row["is_team"]),
                "score": row["score"],
                "activity": row["activity"],
                "risk": row["risk"],
                "commits": row["commits"] or 0,
                "reviews": row["reviews"] or 0,
                "prs_merged": row["prs_merged"] or 0,
                "prs_opened": row["prs_opened"] or 0,
                "last_commit": row["last_commit_at"] or row["last_commit_ever"],
                "last_review": row["last_review_at"],
                "last_activity": row["last_activity_at"],
                # Falling back to the all-time commit date matters: "never" is
                # wrong when what we mean is "not in the last 180 days, and the
                # last time was in November".
                "last_seen": (row["last_activity_at"] or row["last_commit_at"]
                              or row["last_commit_ever"]),
                "last_seen_in_window": bool(row["last_activity_at"]),
                "flagged": bool(row["flagged"]),
                "excluded_reason": row["exclusion_reason"],
                "excluded_label": config.EXCLUSION_LABELS.get(row["exclusion_reason"] or ""),
            })

        tags = [reason for reason, _ in repo_exclusions.get(repo["repo_id"], [])]
        blocking = next(
            (
                (reason, detail)
                for reason, detail in repo_exclusions.get(repo["repo_id"], [])
                if reason in (
                    config.ExclusionReason.NEW_REPO,
                    config.ExclusionReason.SINGLE_CONTRIBUTOR,
                    config.ExclusionReason.EMPTY_REPO,
                )
            ),
            None,
        )

        repos.append({
            "repo_id": repo["repo_id"],
            "name": repo["name"],
            "full_name": repo["full_name"],
            "url": repo["html_url"],
            "is_archived": bool(repo["is_archived"]),
            "is_private": bool(repo["is_private"]),
            "is_empty": bool(repo["is_empty"]),
            "created_at": repo["created_at"],
            "pushed_at": repo["pushed_at"],
            "contributor_count": repo["contributor_count"] or 0,
            "collaborator_count": repo["collaborator_count"] or 0,
            "total_commits": repo["total_commits"] or 0,
            "unattributed_commits": repo["unattributed_commits"] or 0,
            "open_advisories": repo["open_advisories"] or 0,
            "members": members,
            "flagged_count": sum(1 for m in members if m["flagged"]),
            "tags": tags,
            "blocked_reason": blocking[0] if blocking else None,
            "blocked_detail": blocking[1] if blocking else None,
        })

    repos.sort(key=lambda r: (-r["flagged_count"], -r["open_advisories"], r["full_name"]))
    return repos


def suggestion_context(db: Database, run_id: int, now: datetime) -> list[dict[str, Any]]:
    """Section 3: the removal suggestions, highest risk first."""
    rows = db.query(
        """
        SELECT s.*, r.full_name, r.name AS repo_name, r.html_url AS repo_url,
               r.is_archived, r.is_private,
               c.is_direct, c.is_outside, c.is_team, c.is_base, c.team_names,
               c.permission_direct, c.permission_outside, c.permission_team,
               c.permission_base,
               k.commits, k.reviews, k.prs_merged, k.prs_opened,
               k.last_commit_at, k.last_review_at, k.last_activity_at, k.last_commit_ever
          FROM scores s
          JOIN repos r ON r.repo_id = s.repo_id
          LEFT JOIN collaborators c ON c.repo_id = s.repo_id AND c.login = s.login
          LEFT JOIN contributions k ON k.repo_id = s.repo_id AND k.login = s.login
         WHERE s.run_id = ? AND s.flagged = 1
         ORDER BY s.risk DESC, s.login, s.repo_id
        """,
        (run_id,),
    )

    suggestions = []
    for row in rows:
        team_only = bool(row["is_team"]) and not (row["is_direct"] or row["is_outside"])
        base_only = bool(row["is_base"]) and not (
            row["is_direct"] or row["is_outside"] or row["is_team"])
        suggestions.append({
            "login": row["login"],
            "repo": row["full_name"],
            "repo_name": row["repo_name"],
            "repo_url": row["repo_url"],
            "is_archived": bool(row["is_archived"]),
            "is_private": bool(row["is_private"]),
            "permission": humanise_permission(row["permission"]),
            "access_path": access_path_label(row),
            "team_only": team_only,
            "base_only": base_only,
            "team_names": row["team_names"],
            "score": row["score"],
            "risk": row["risk"],
            "commits": row["commits"] or 0,
            "reviews": row["reviews"] or 0,
            "prs_merged": row["prs_merged"] or 0,
            "last_commit": row["last_commit_at"] or row["last_commit_ever"],
            "last_commit_in_window": bool(row["last_commit_at"]),
            "last_review": row["last_review_at"],
            "days_since": row["days_since_activity"],
            "reason": row["reason"],
            # Shared with score.MemberScore.removal_note so the advice on the
            # page cannot drift from the advice the model reasons about.
            "action": remediation_note(
                is_team_only=team_only,
                is_base_only=base_only,
                teams=[t.strip() for t in str(row["team_names"] or "").split(",") if t.strip()],
                is_archived=bool(row["is_archived"]),
            ),
        })
    return suggestions


def exclusion_context(db: Database, run_id: int) -> dict[str, Any]:
    """The panel that makes the rest of the report trustworthy."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for row in db.query(
        """
        SELECT e.*, r.full_name, r.name AS repo_name
          FROM exclusions e
          JOIN repos r ON r.repo_id = e.repo_id
         WHERE e.run_id = ? AND e.login != '*'
         ORDER BY e.reason, e.login, r.full_name
        """,
        (run_id,),
    ):
        grouped[row["reason"]].append({
            "login": row["login"],
            "repo": row["full_name"],
            "repo_name": row["repo_name"],
            "permission": humanise_permission(row["permission"]),
            "detail": row["detail"],
        })

    repo_level: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in db.query(
        """
        SELECT e.*, r.full_name, r.name AS repo_name
          FROM exclusions e
          JOIN repos r ON r.repo_id = e.repo_id
         WHERE e.run_id = ? AND e.login = '*'
         ORDER BY e.reason, r.full_name
        """,
        (run_id,),
    ):
        repo_level[row["reason"]].append({
            "repo": row["full_name"],
            "repo_name": row["repo_name"],
            "detail": row["detail"],
        })

    total = sum(len(v) for v in grouped.values())
    return {
        "members": {k: v for k, v in sorted(grouped.items())},
        "repos": {k: v for k, v in sorted(repo_level.items())},
        "labels": config.EXCLUSION_LABELS,
        "member_total": total,
        "repo_total": sum(len(v) for v in repo_level.values()),
    }


def build_context(db: Database, run_id: int, *, now: datetime | None = None) -> dict[str, Any]:
    run = db.query_one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    if run is None:
        raise ValueError(f"No run {run_id} in the database")

    now = now or datetime.now(timezone.utc)
    advisories = advisory_context(db, run_id, now)
    repos = repo_context(db, run_id, now)
    suggestions = suggestion_context(db, run_id, now)
    exclusions = exclusion_context(db, run_id)

    scored_people = db.query_one(
        "SELECT COUNT(DISTINCT login) AS n FROM scores WHERE run_id = ?", (run_id,)
    )["n"]

    # Data gaps are promoted to the top of the page, not buried. A report that
    # silently omits what it could not read is worse than no report.
    gaps = [
        {"repo": row["full_name"], "detail": row["detail"]}
        for row in db.query(
            """
            SELECT r.full_name, e.detail
              FROM exclusions e JOIN repos r ON r.repo_id = e.repo_id
             WHERE e.run_id = ? AND e.reason = ?
             ORDER BY r.full_name
            """,
            (run_id, config.ExclusionReason.ACCESS_UNREADABLE),
        )
    ]
    admin_flags = sum(1 for s in suggestions if s["permission"].lower() == "admin")
    counts = db.run_counts(run_id)

    return {
        "run": dict(run),
        "config": json.loads(run["config_json"]),
        "org": run["org"],
        "generated_at": now,
        "generated_at_label": now.strftime("%d %b %Y, %H:%M UTC"),
        "lookback_days": config.LOOKBACK_DAYS,
        "threshold": config.SCORING.threshold,
        "kpis": {
            "repos_scanned": counts["repos_scanned"],
            "repos_skipped": counts["repos_skipped"],
            "open_advisories": advisories["total_open"],
            "critical_high_open": advisories["critical_high_open"],
            "suggestions": len(suggestions),
            "admin_suggestions": admin_flags,
            "people_reviewed": scored_people,
            "excluded": exclusions["member_total"],
        },
        "data_gaps": gaps,
        "advisories": advisories,
        "repos": repos,
        "suggestions": suggestions,
        "exclusions": exclusions,
        "severity_order": config.SEVERITY_ORDER,
        "severity_colors": config.SEVERITY_COLORS,
        "unattributed_total": sum(r["unattributed_commits"] for r in repos),
    }


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def _chart_js() -> str:
    """Inline Chart.js so the page needs no network. Degrades to tables if absent."""
    path = config.VENDOR_DIR / "chart.umd.min.js"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    log.warning(
        "%s is missing, so the dashboard will render without charts. "
        "Run: python main.py --fetch-vendor", path,
    )
    return ""


def render(
    db: Database,
    run_id: int,
    output_path: str | Path | None = None,
    *,
    now: datetime | None = None,
) -> Path:
    context = build_context(db, run_id, now=now)

    env = Environment(
        loader=FileSystemLoader(str(config.TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    # Bind the run's clock into the filters. Using the wall clock here would
    # make every "N days ago" in the page drift between renders of the same
    # run - and would make the pinned-clock demo stop being reproducible.
    as_of = context["generated_at"]
    env.filters["fmt_date"] = fmt_date
    env.filters["fmt_ago"] = lambda value: fmt_ago(value, as_of)
    env.filters["age_days"] = lambda value: age_days(value, as_of)
    env.filters["permission"] = humanise_permission

    template = env.get_template("dashboard.html.jinja")
    html = template.render(chart_js=_chart_js(), **context)

    output_path = Path(output_path or config.REPORT_PATH)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    log.info("Dashboard written to %s (%.0f KB)", output_path, len(html) / 1024)
    return output_path

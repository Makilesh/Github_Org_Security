"""SQLite storage: schema, upserts and the queries the dashboard needs.

Design rules this module enforces:

* Every fact table is keyed on a natural GitHub identifier - `repo_id`,
  `(repo_id, ghsa_id, manifest_path)`, `(repo_id, login)` - and written with
  `INSERT ... ON CONFLICT DO UPDATE`. Running the scanner twice updates rows
  in place; it never duplicates them.
* Every fact row carries the `run_id` that last touched it, so a stale row
  left over from an earlier run is visible rather than silently mixed in.
* Progress is recorded per repo per stage and committed immediately, so a
  crashed run can resume where it stopped instead of starting over.

The one deviation from "key on ghsa_id" in the brief: a single repository can
carry several open Dependabot alerts for the same GHSA in different manifests
(e.g. two lockfiles pinning the same vulnerable package). Keying on
`(repo_id, ghsa_id)` alone would drop all but one of them, so `manifest_path`
is part of the key. `ghsa_id` is still the identifier used for cross-repo
grouping, and it is indexed for that.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

SCHEMA_VERSION = 1


# --------------------------------------------------------------------------
# Stages, used for resume bookkeeping
# --------------------------------------------------------------------------

class Stage:
    REPO = "repo"
    ADVISORIES = "advisories"
    COLLABORATORS = "collaborators"
    CONTRIBUTIONS = "contributions"
    SCORED = "scored"


class RunStatus:
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


def utcnow() -> str:
    """Timestamps are stored as ISO-8601 UTC strings, so they sort as text."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);

-- One row per invocation of the scanner. -----------------------------------
CREATE TABLE IF NOT EXISTS runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org             TEXT    NOT NULL,
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
    status          TEXT    NOT NULL,
    config_json     TEXT    NOT NULL,
    repos_scanned   INTEGER NOT NULL DEFAULT 0,
    repos_skipped   INTEGER NOT NULL DEFAULT 0,
    advisories_found INTEGER NOT NULL DEFAULT 0,
    suggestions_made INTEGER NOT NULL DEFAULT 0,
    members_excluded INTEGER NOT NULL DEFAULT 0,
    error           TEXT
);

-- Org members, with the owner flag that drives a NEVER FLAG rule. ----------
CREATE TABLE IF NOT EXISTS org_members (
    login       TEXT PRIMARY KEY,
    run_id      INTEGER NOT NULL,
    user_id     INTEGER,
    user_type   TEXT,
    org_role    TEXT,               -- 'admin' (owner) | 'member'
    is_owner    INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT    NOT NULL
);

-- Repositories. ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS repos (
    repo_id         INTEGER PRIMARY KEY,
    run_id          INTEGER NOT NULL,
    org             TEXT    NOT NULL,
    name            TEXT    NOT NULL,
    full_name       TEXT    NOT NULL,
    html_url        TEXT,
    visibility      TEXT,
    is_private      INTEGER NOT NULL DEFAULT 0,
    is_archived     INTEGER NOT NULL DEFAULT 0,
    is_fork         INTEGER NOT NULL DEFAULT 0,
    is_empty        INTEGER NOT NULL DEFAULT 0,
    default_branch  TEXT,
    created_at      TEXT,
    pushed_at       TEXT,
    scan_status     TEXT,           -- 'scanned' | 'skipped' | 'error'
    skip_reason     TEXT,
    scan_error      TEXT,
    scanned_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_repos_run ON repos(run_id);

-- Per-repo counters that are not per-member. -------------------------------
CREATE TABLE IF NOT EXISTS repo_stats (
    repo_id                 INTEGER PRIMARY KEY REFERENCES repos(repo_id) ON DELETE CASCADE,
    run_id                  INTEGER NOT NULL,
    total_commits           INTEGER NOT NULL DEFAULT 0,
    unattributed_commits    INTEGER NOT NULL DEFAULT 0,
    contributor_count       INTEGER NOT NULL DEFAULT 0,
    collaborator_count      INTEGER NOT NULL DEFAULT 0,
    open_advisories         INTEGER NOT NULL DEFAULT 0,
    window_start            TEXT,
    updated_at              TEXT NOT NULL
);

-- Resume bookkeeping: one row per (run, repo, stage). ----------------------
CREATE TABLE IF NOT EXISTS run_progress (
    run_id      INTEGER NOT NULL,
    repo_id     INTEGER NOT NULL,
    stage       TEXT    NOT NULL,
    status      TEXT    NOT NULL,   -- 'done' | 'error'
    detail      TEXT,
    updated_at  TEXT    NOT NULL,
    PRIMARY KEY (run_id, repo_id, stage)
);

-- Dependabot advisories. ---------------------------------------------------
CREATE TABLE IF NOT EXISTS advisories (
    repo_id         INTEGER NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
    ghsa_id         TEXT    NOT NULL,
    manifest_path   TEXT    NOT NULL DEFAULT '',
    run_id          INTEGER NOT NULL,
    alert_number    INTEGER,
    severity        TEXT,
    state           TEXT,           -- open | fixed | dismissed | auto_dismissed
    package_name    TEXT,
    ecosystem       TEXT,
    summary         TEXT,
    cve_id          TEXT,
    html_url        TEXT,
    created_at      TEXT,
    updated_at      TEXT,
    dismissed_at    TEXT,
    dismissed_reason TEXT,
    fixed_at        TEXT,
    source          TEXT,           -- 'org' | 'repo' (which endpoint it came from)
    seen_at         TEXT NOT NULL,
    PRIMARY KEY (repo_id, ghsa_id, manifest_path)
);
CREATE INDEX IF NOT EXISTS idx_adv_ghsa ON advisories(ghsa_id);
CREATE INDEX IF NOT EXISTS idx_adv_state ON advisories(state, severity);
CREATE INDEX IF NOT EXISTS idx_adv_run ON advisories(run_id);

-- Collaborators. -----------------------------------------------------------
-- Direct, outside and team-inherited access are three different things. They
-- are recorded in separate columns on one row per (repo, login) and are never
-- merged into a single "permission" field by the writer; `effective_permission`
-- is a derived convenience only, and the three source columns stay authoritative.
CREATE TABLE IF NOT EXISTS collaborators (
    repo_id                 INTEGER NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
    login                   TEXT    NOT NULL,
    run_id                  INTEGER NOT NULL,
    user_id                 INTEGER,
    user_type               TEXT,           -- 'User' | 'Bot'
    is_direct               INTEGER NOT NULL DEFAULT 0,
    is_outside              INTEGER NOT NULL DEFAULT 0,
    is_team                 INTEGER NOT NULL DEFAULT 0,
    permission_direct       TEXT,
    permission_outside      TEXT,
    permission_team         TEXT,
    team_names              TEXT,           -- comma separated; which teams grant it
    effective_permission    TEXT,
    role_name               TEXT,
    site_admin              INTEGER NOT NULL DEFAULT 0,
    updated_at              TEXT    NOT NULL,
    PRIMARY KEY (repo_id, login)
);
CREATE INDEX IF NOT EXISTS idx_collab_login ON collaborators(login);

-- Contribution counts inside the lookback window. --------------------------
CREATE TABLE IF NOT EXISTS contributions (
    repo_id         INTEGER NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
    login           TEXT    NOT NULL,
    run_id          INTEGER NOT NULL,
    commits         INTEGER NOT NULL DEFAULT 0,
    prs_opened      INTEGER NOT NULL DEFAULT 0,
    prs_merged      INTEGER NOT NULL DEFAULT 0,
    reviews         INTEGER NOT NULL DEFAULT 0,
    last_commit_at  TEXT,
    last_pr_at      TEXT,
    last_review_at  TEXT,
    last_activity_at TEXT,
    -- Most recent commit of any age, looked up only for flagged members. The
    -- window-scoped columns above cannot answer "when did they last commit?"
    -- for someone whose last commit predates the window - which is exactly the
    -- person a reviewer needs a date for.
    last_commit_ever TEXT,
    updated_at      TEXT    NOT NULL,
    PRIMARY KEY (repo_id, login)
);
CREATE INDEX IF NOT EXISTS idx_contrib_login ON contributions(login);

-- Scores and recommendations. ----------------------------------------------
CREATE TABLE IF NOT EXISTS scores (
    repo_id             INTEGER NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
    login               TEXT    NOT NULL,
    run_id              INTEGER NOT NULL,
    activity            REAL    NOT NULL DEFAULT 0,
    score               REAL    NOT NULL DEFAULT 0,
    risk                REAL    NOT NULL DEFAULT 0,
    permission          TEXT,
    permission_weight   REAL    NOT NULL DEFAULT 0,
    days_since_activity REAL,
    flagged             INTEGER NOT NULL DEFAULT 0,
    reason              TEXT,
    updated_at          TEXT    NOT NULL,
    PRIMARY KEY (repo_id, login)
);
CREATE INDEX IF NOT EXISTS idx_scores_risk ON scores(flagged, risk DESC);

-- Everyone who was skipped, and why. ---------------------------------------
-- Repo-level exclusions use login = '*'.
CREATE TABLE IF NOT EXISTS exclusions (
    repo_id     INTEGER NOT NULL,
    login       TEXT    NOT NULL DEFAULT '*',
    run_id      INTEGER NOT NULL,
    reason      TEXT    NOT NULL,
    detail      TEXT,
    permission  TEXT,
    updated_at  TEXT    NOT NULL,
    PRIMARY KEY (repo_id, login, reason)
);
CREATE INDEX IF NOT EXISTS idx_excl_reason ON exclusions(reason);

-- Conditional-request cache. A 304 does not count against the quota. -------
CREATE TABLE IF NOT EXISTS http_cache (
    cache_key       TEXT PRIMARY KEY,
    etag            TEXT,
    last_modified   TEXT,
    body            TEXT NOT NULL,
    status          INTEGER,
    fetched_at      TEXT NOT NULL
);
"""


class Database:
    """Thin wrapper over sqlite3 with the upserts this project needs.

    Also satisfies the cache protocol `client.GitHubClient` expects
    (`get_cached` / `set_cached`), which is why client.py never imports db.py.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.conn: sqlite3.Connection | None = None

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        if self.conn is not None:
            return self.conn
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        self.conn = conn
        return conn

    def init_schema(self) -> None:
        conn = self.connect()
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> "Database":
        self.connect()
        self.init_schema()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def _c(self) -> sqlite3.Connection:
        return self.connect()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self._c.execute(sql, params)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return self._c.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self._c.execute(sql, params).fetchone()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Group writes for one repo so progress is saved atomically."""
        conn = self._c
        conn.execute("BEGIN")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    # -- runs --------------------------------------------------------------

    def start_run(self, org: str, config: Mapping[str, Any]) -> int:
        cur = self.execute(
            "INSERT INTO runs(org, started_at, status, config_json) VALUES (?, ?, ?, ?)",
            (org, utcnow(), RunStatus.RUNNING, json.dumps(config, sort_keys=True)),
        )
        run_id = int(cur.lastrowid)
        return run_id

    def finish_run(
        self,
        run_id: int,
        status: str,
        *,
        repos_scanned: int = 0,
        repos_skipped: int = 0,
        advisories_found: int = 0,
        suggestions_made: int = 0,
        members_excluded: int = 0,
        error: str | None = None,
    ) -> None:
        self.execute(
            """
            UPDATE runs SET finished_at = ?, status = ?, repos_scanned = ?,
                   repos_skipped = ?, advisories_found = ?, suggestions_made = ?,
                   members_excluded = ?, error = ?
             WHERE run_id = ?
            """,
            (
                utcnow(), status, repos_scanned, repos_skipped, advisories_found,
                suggestions_made, members_excluded, error, run_id,
            ),
        )

    def latest_run(self, org: str | None = None, status: str | None = None) -> sqlite3.Row | None:
        sql = "SELECT * FROM runs WHERE 1=1"
        params: list[Any] = []
        if org:
            sql += " AND org = ?"
            params.append(org)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY run_id DESC LIMIT 1"
        return self.query_one(sql, params)

    def resumable_run(self, org: str) -> sqlite3.Row | None:
        """The most recent run that never finished, i.e. the one to resume."""
        return self.query_one(
            "SELECT * FROM runs WHERE org = ? AND status IN (?, ?) "
            "ORDER BY run_id DESC LIMIT 1",
            (org, RunStatus.RUNNING, RunStatus.INTERRUPTED),
        )

    # -- progress ----------------------------------------------------------

    def mark_stage(
        self, run_id: int, repo_id: int, stage: str,
        status: str = "done", detail: str | None = None,
    ) -> None:
        self.execute(
            """
            INSERT INTO run_progress(run_id, repo_id, stage, status, detail, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, repo_id, stage) DO UPDATE SET
                status = excluded.status,
                detail = excluded.detail,
                updated_at = excluded.updated_at
            """,
            (run_id, repo_id, stage, status, detail, utcnow()),
        )

    def stage_done(self, run_id: int, repo_id: int, stage: str) -> bool:
        row = self.query_one(
            "SELECT status FROM run_progress WHERE run_id = ? AND repo_id = ? AND stage = ?",
            (run_id, repo_id, stage),
        )
        return bool(row and row["status"] == "done")

    def completed_repo_ids(self, run_id: int, stage: str) -> set[int]:
        rows = self.query(
            "SELECT repo_id FROM run_progress WHERE run_id = ? AND stage = ? AND status = 'done'",
            (run_id, stage),
        )
        return {int(r["repo_id"]) for r in rows}

    # -- upserts -----------------------------------------------------------

    def upsert_org_member(
        self, run_id: int, login: str, *, user_id: int | None = None,
        user_type: str | None = None, org_role: str | None = None,
        is_owner: bool = False,
    ) -> None:
        self.execute(
            """
            INSERT INTO org_members(login, run_id, user_id, user_type, org_role, is_owner, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(login) DO UPDATE SET
                run_id = excluded.run_id,
                user_id = COALESCE(excluded.user_id, org_members.user_id),
                user_type = COALESCE(excluded.user_type, org_members.user_type),
                org_role = COALESCE(excluded.org_role, org_members.org_role),
                is_owner = excluded.is_owner,
                updated_at = excluded.updated_at
            """,
            (login, run_id, user_id, user_type, org_role, int(is_owner), utcnow()),
        )

    def org_owners(self) -> set[str]:
        rows = self.query("SELECT login FROM org_members WHERE is_owner = 1")
        return {str(r["login"]).lower() for r in rows}

    def upsert_repo(self, run_id: int, repo: Mapping[str, Any]) -> int:
        """`repo` is a raw REST repository object."""
        repo_id = int(repo["id"])
        self.execute(
            """
            INSERT INTO repos(repo_id, run_id, org, name, full_name, html_url, visibility,
                              is_private, is_archived, is_fork, default_branch,
                              created_at, pushed_at, scan_status, scanned_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo_id) DO UPDATE SET
                run_id = excluded.run_id,
                org = excluded.org,
                name = excluded.name,
                full_name = excluded.full_name,
                html_url = excluded.html_url,
                visibility = excluded.visibility,
                is_private = excluded.is_private,
                is_archived = excluded.is_archived,
                is_fork = excluded.is_fork,
                default_branch = excluded.default_branch,
                created_at = excluded.created_at,
                pushed_at = excluded.pushed_at,
                scan_status = excluded.scan_status,
                scanned_at = excluded.scanned_at
            """,
            (
                repo_id,
                run_id,
                (repo.get("owner") or {}).get("login", ""),
                repo.get("name", ""),
                repo.get("full_name", ""),
                repo.get("html_url"),
                repo.get("visibility"),
                int(bool(repo.get("private"))),
                int(bool(repo.get("archived"))),
                int(bool(repo.get("fork"))),
                repo.get("default_branch"),
                repo.get("created_at"),
                repo.get("pushed_at"),
                "pending",
                utcnow(),
            ),
        )
        return repo_id

    def set_repo_scan_status(
        self, repo_id: int, status: str, *,
        skip_reason: str | None = None, error: str | None = None,
        is_empty: bool | None = None,
    ) -> None:
        sets = ["scan_status = ?", "scanned_at = ?"]
        params: list[Any] = [status, utcnow()]
        if skip_reason is not None:
            sets.append("skip_reason = ?")
            params.append(skip_reason)
        if error is not None:
            sets.append("scan_error = ?")
            params.append(error)
        if is_empty is not None:
            sets.append("is_empty = ?")
            params.append(int(is_empty))
        params.append(repo_id)
        self.execute(f"UPDATE repos SET {', '.join(sets)} WHERE repo_id = ?", params)

    def upsert_repo_stats(self, run_id: int, repo_id: int, **fields: Any) -> None:
        allowed = (
            "total_commits", "unattributed_commits", "contributor_count",
            "collaborator_count", "open_advisories", "window_start",
        )
        values = {k: fields.get(k) for k in allowed}
        self.execute(
            """
            INSERT INTO repo_stats(repo_id, run_id, total_commits, unattributed_commits,
                                   contributor_count, collaborator_count, open_advisories,
                                   window_start, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo_id) DO UPDATE SET
                run_id = excluded.run_id,
                total_commits = excluded.total_commits,
                unattributed_commits = excluded.unattributed_commits,
                contributor_count = excluded.contributor_count,
                collaborator_count = excluded.collaborator_count,
                open_advisories = excluded.open_advisories,
                window_start = excluded.window_start,
                updated_at = excluded.updated_at
            """,
            (
                repo_id, run_id,
                int(values["total_commits"] or 0),
                int(values["unattributed_commits"] or 0),
                int(values["contributor_count"] or 0),
                int(values["collaborator_count"] or 0),
                int(values["open_advisories"] or 0),
                values["window_start"],
                utcnow(),
            ),
        )

    def upsert_advisory(self, run_id: int, repo_id: int, alert: Mapping[str, Any], source: str = "repo") -> str:
        """`alert` is a raw Dependabot alert object."""
        dep = alert.get("dependency") or {}
        pkg = dep.get("package") or {}
        sec_adv = alert.get("security_advisory") or {}
        identifiers = sec_adv.get("identifiers") or []
        cve = next((i.get("value") for i in identifiers if i.get("type") == "CVE"), None)
        ghsa = sec_adv.get("ghsa_id") or next(
            (i.get("value") for i in identifiers if i.get("type") == "GHSA"), None
        ) or f"ALERT-{alert.get('number')}"
        manifest = dep.get("manifest_path") or ""

        self.execute(
            """
            INSERT INTO advisories(repo_id, ghsa_id, manifest_path, run_id, alert_number,
                                   severity, state, package_name, ecosystem, summary, cve_id,
                                   html_url, created_at, updated_at, dismissed_at,
                                   dismissed_reason, fixed_at, source, seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo_id, ghsa_id, manifest_path) DO UPDATE SET
                run_id = excluded.run_id,
                alert_number = excluded.alert_number,
                severity = excluded.severity,
                state = excluded.state,
                package_name = excluded.package_name,
                ecosystem = excluded.ecosystem,
                summary = excluded.summary,
                cve_id = excluded.cve_id,
                html_url = excluded.html_url,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at,
                dismissed_at = excluded.dismissed_at,
                dismissed_reason = excluded.dismissed_reason,
                fixed_at = excluded.fixed_at,
                source = excluded.source,
                seen_at = excluded.seen_at
            """,
            (
                repo_id, ghsa, manifest, run_id, alert.get("number"),
                (sec_adv.get("severity") or alert.get("security_vulnerability", {}).get("severity") or "unknown").lower(),
                alert.get("state"),
                pkg.get("name"),
                pkg.get("ecosystem"),
                sec_adv.get("summary"),
                cve,
                alert.get("html_url"),
                alert.get("created_at"),
                alert.get("updated_at"),
                alert.get("dismissed_at"),
                alert.get("dismissed_reason"),
                alert.get("fixed_at"),
                source,
                utcnow(),
            ),
        )
        return ghsa

    def upsert_collaborator(
        self, run_id: int, repo_id: int, login: str, *,
        affiliation: str,                      # 'direct' | 'outside' | 'team'
        permission: str | None,
        user_id: int | None = None,
        user_type: str | None = None,
        role_name: str | None = None,
        site_admin: bool = False,
        team_names: str | None = None,
    ) -> None:
        """Record one access path for one member.

        Called once per affiliation. Direct, outside and team-inherited access
        each set their own flag and permission column; a member holding two of
        them keeps both, visibly, on the same row.
        """
        if affiliation not in ("direct", "outside", "team"):
            raise ValueError(f"unknown affiliation: {affiliation!r}")

        flag_col = {"direct": "is_direct", "outside": "is_outside", "team": "is_team"}[affiliation]
        perm_col = {
            "direct": "permission_direct",
            "outside": "permission_outside",
            "team": "permission_team",
        }[affiliation]

        self.execute(
            f"""
            INSERT INTO collaborators(repo_id, login, run_id, user_id, user_type,
                                      {flag_col}, {perm_col}, team_names, role_name,
                                      site_admin, updated_at)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
            ON CONFLICT(repo_id, login) DO UPDATE SET
                run_id = excluded.run_id,
                user_id = COALESCE(excluded.user_id, collaborators.user_id),
                user_type = COALESCE(excluded.user_type, collaborators.user_type),
                {flag_col} = 1,
                {perm_col} = excluded.{perm_col},
                team_names = COALESCE(excluded.team_names, collaborators.team_names),
                role_name = COALESCE(excluded.role_name, collaborators.role_name),
                site_admin = excluded.site_admin,
                updated_at = excluded.updated_at
            """,
            (repo_id, login, run_id, user_id, user_type, permission, team_names,
             role_name, int(site_admin), utcnow()),
        )

    def set_effective_permission(self, repo_id: int, login: str, permission: str | None) -> None:
        """Derived field only. The three source columns remain authoritative."""
        self.execute(
            "UPDATE collaborators SET effective_permission = ? WHERE repo_id = ? AND login = ?",
            (permission, repo_id, login),
        )

    def clear_collaborators(self, repo_id: int) -> None:
        """Drop a repo's collaborator rows before a fresh fetch.

        Without this, someone whose access was revoked between runs would
        linger forever. Called inside the same transaction as the re-insert,
        so a crash cannot leave the repo with no collaborators at all.
        """
        self.execute("DELETE FROM collaborators WHERE repo_id = ?", (repo_id,))

    def upsert_contribution(
        self, run_id: int, repo_id: int, login: str, *,
        commits: int = 0, prs_opened: int = 0, prs_merged: int = 0, reviews: int = 0,
        last_commit_at: str | None = None, last_pr_at: str | None = None,
        last_review_at: str | None = None, last_activity_at: str | None = None,
    ) -> None:
        self.execute(
            """
            INSERT INTO contributions(repo_id, login, run_id, commits, prs_opened, prs_merged,
                                      reviews, last_commit_at, last_pr_at, last_review_at,
                                      last_activity_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo_id, login) DO UPDATE SET
                run_id = excluded.run_id,
                commits = excluded.commits,
                prs_opened = excluded.prs_opened,
                prs_merged = excluded.prs_merged,
                reviews = excluded.reviews,
                last_commit_at = excluded.last_commit_at,
                last_pr_at = excluded.last_pr_at,
                last_review_at = excluded.last_review_at,
                last_activity_at = excluded.last_activity_at,
                updated_at = excluded.updated_at
            """,
            (repo_id, login, run_id, commits, prs_opened, prs_merged, reviews,
             last_commit_at, last_pr_at, last_review_at, last_activity_at, utcnow()),
        )

    def set_last_commit_ever(self, repo_id: int, login: str, when: str | None) -> None:
        """Backfill the all-time last commit date for one member on one repo."""
        self.execute(
            "UPDATE contributions SET last_commit_ever = ? WHERE repo_id = ? AND login = ?",
            (when, repo_id, login),
        )

    def upsert_score(
        self, run_id: int, repo_id: int, login: str, *,
        activity: float, score: float, risk: float,
        permission: str | None, permission_weight: float,
        days_since_activity: float | None, flagged: bool, reason: str | None = None,
    ) -> None:
        self.execute(
            """
            INSERT INTO scores(repo_id, login, run_id, activity, score, risk, permission,
                               permission_weight, days_since_activity, flagged, reason, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo_id, login) DO UPDATE SET
                run_id = excluded.run_id,
                activity = excluded.activity,
                score = excluded.score,
                risk = excluded.risk,
                permission = excluded.permission,
                permission_weight = excluded.permission_weight,
                days_since_activity = excluded.days_since_activity,
                flagged = excluded.flagged,
                reason = excluded.reason,
                updated_at = excluded.updated_at
            """,
            (repo_id, login, run_id, activity, score, risk, permission,
             permission_weight, days_since_activity, int(flagged), reason, utcnow()),
        )

    def upsert_exclusion(
        self, run_id: int, repo_id: int, login: str, reason: str,
        detail: str | None = None, permission: str | None = None,
    ) -> None:
        self.execute(
            """
            INSERT INTO exclusions(repo_id, login, run_id, reason, detail, permission, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo_id, login, reason) DO UPDATE SET
                run_id = excluded.run_id,
                detail = excluded.detail,
                permission = excluded.permission,
                updated_at = excluded.updated_at
            """,
            (repo_id, login, run_id, reason, detail, permission, utcnow()),
        )

    # -- HTTP cache (the protocol client.GitHubClient expects) -------------

    def get_cached(self, key: str) -> tuple[str | None, str | None, str] | None:
        """Return (etag, last_modified, body) for a cache key, or None."""
        row = self.query_one(
            "SELECT etag, last_modified, body FROM http_cache WHERE cache_key = ?", (key,)
        )
        if row is None:
            return None
        return row["etag"], row["last_modified"], row["body"]

    def set_cached(
        self, key: str, etag: str | None, body: str,
        last_modified: str | None = None, status: int | None = 200,
    ) -> None:
        self.execute(
            """
            INSERT INTO http_cache(cache_key, etag, last_modified, body, status, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                etag = excluded.etag,
                last_modified = excluded.last_modified,
                body = excluded.body,
                status = excluded.status,
                fetched_at = excluded.fetched_at
            """,
            (key, etag, last_modified, body, status, utcnow()),
        )

    def purge_cache(self, ttl_hours: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=ttl_hours)).isoformat(timespec="seconds")
        cur = self.execute("DELETE FROM http_cache WHERE fetched_at < ?", (cutoff,))
        return cur.rowcount or 0

    # -- read helpers used by scoring and reporting ------------------------

    def repos_for_run(self, run_id: int, *, scanned_only: bool = True) -> list[sqlite3.Row]:
        sql = """
            SELECT r.*, s.contributor_count, s.collaborator_count, s.total_commits,
                   s.unattributed_commits, s.open_advisories, s.window_start
              FROM repos r
              LEFT JOIN repo_stats s ON s.repo_id = r.repo_id
             WHERE r.run_id = ?
        """
        if scanned_only:
            sql += " AND r.scan_status = 'scanned'"
        sql += " ORDER BY r.full_name"
        return self.query(sql, (run_id,))

    def members_for_repo(self, repo_id: int) -> list[sqlite3.Row]:
        """Everyone with access to a repo, or activity on it, or both.

        A FULL OUTER JOIN in SQLite terms: collaborators with no contributions
        are the people the review exists to find, and contributors who no
        longer hold access still belong in the per-repo table as context.
        """
        return self.query(
            """
            SELECT
                COALESCE(c.login, k.login)      AS login,
                c.user_type, c.is_direct, c.is_outside, c.is_team,
                c.permission_direct, c.permission_outside, c.permission_team,
                c.team_names, c.effective_permission,
                COALESCE(k.commits, 0)          AS commits,
                COALESCE(k.prs_opened, 0)       AS prs_opened,
                COALESCE(k.prs_merged, 0)       AS prs_merged,
                COALESCE(k.reviews, 0)          AS reviews,
                k.last_commit_at, k.last_pr_at, k.last_review_at,
                k.last_activity_at, k.last_commit_ever
              FROM collaborators c
              LEFT JOIN contributions k
                     ON k.repo_id = c.repo_id AND k.login = c.login
             WHERE c.repo_id = ?
            UNION
            SELECT
                k.login, NULL, 0, 0, 0, NULL, NULL, NULL, NULL, NULL,
                k.commits, k.prs_opened, k.prs_merged, k.reviews,
                k.last_commit_at, k.last_pr_at, k.last_review_at,
                k.last_activity_at, k.last_commit_ever
              FROM contributions k
             WHERE k.repo_id = ?
               AND k.login NOT IN (SELECT login FROM collaborators WHERE repo_id = ?)
             ORDER BY login
            """,
            (repo_id, repo_id, repo_id),
        )

    # -- counts used by the run summary / --json ---------------------------

    def count(self, table: str, where: str = "", params: Sequence[Any] = ()) -> int:
        sql = f"SELECT COUNT(*) AS n FROM {table}"
        if where:
            sql += f" WHERE {where}"
        row = self.query_one(sql, params)
        return int(row["n"]) if row else 0

    def run_counts(self, run_id: int) -> dict[str, int]:
        return {
            "repos_scanned": self.count("repos", "run_id = ? AND scan_status = 'scanned'", (run_id,)),
            "repos_skipped": self.count("repos", "run_id = ? AND scan_status = 'skipped'", (run_id,)),
            "repos_errored": self.count("repos", "run_id = ? AND scan_status = 'error'", (run_id,)),
            "advisories_found": self.count("advisories", "run_id = ?", (run_id,)),
            "advisories_open": self.count("advisories", "run_id = ? AND state = 'open'", (run_id,)),
            "suggestions_made": self.count("scores", "run_id = ? AND flagged = 1", (run_id,)),
            # Member and repository exclusions are counted separately: "14 people
            # were excluded" and "14 exclusion rows exist" are different claims,
            # and the repo-level rows use login = '*'.
            "members_excluded": self.count(
                "exclusions", "run_id = ? AND login != '*'", (run_id,)
            ),
            "repos_excluded": self.count(
                "exclusions", "run_id = ? AND login = '*'", (run_id,)
            ),
            "collaborators": self.count("collaborators", "run_id = ?", (run_id,)),
        }

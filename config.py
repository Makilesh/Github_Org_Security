"""Central configuration for the org security / access-hygiene scanner.

Every tunable in the project lives in this file. Nothing else should
hard-code a weight, a threshold, an endpoint or a timing constant.

Values come from three places, in increasing order of precedence:
    1. the defaults written below
    2. a `.env` file next to this module
    3. real environment variables

The scoring numbers are grouped into `ScoringConfig` so tests can build a
variant without mutating global state.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

PROJECT_ROOT = Path(__file__).resolve().parent


# --------------------------------------------------------------------------
# .env loading (no third-party dependency; we only need the simple cases)
# --------------------------------------------------------------------------

def load_dotenv(path: Path | None = None, *, override: bool = False) -> dict[str, str]:
    """Parse a `.env` file into os.environ. Returns what it parsed.

    Supports `KEY=value`, `export KEY=value`, `#` comments, and single or
    double quoted values. Malformed lines are skipped rather than raising: a
    typo in .env should not stop a scan that has everything else it needs.
    """
    path = path or (PROJECT_ROOT / ".env")
    parsed: dict[str, str] = {}
    if not path.is_file():
        return parsed

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if not key:
            continue
        parsed[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return parsed


load_dotenv()


# --------------------------------------------------------------------------
# env helpers
# --------------------------------------------------------------------------

def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def _list(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return tuple(default)
    return tuple(item.strip() for item in raw.split(",") if item.strip())


# --------------------------------------------------------------------------
# Auth + target
# --------------------------------------------------------------------------

#: Fine-grained PAT. Needs, at the organization level:
#:     Members: read             (org owners / member list)
#:     Administration: read      (repo list)
#:     Dependabot alerts: read   (advisories)
#: and at the repository level:
#:     Metadata: read, Contents: read, Pull requests: read, Administration: read
GITHUB_TOKEN: str = _str("GITHUB_TOKEN")

#: The organization login to scan, e.g. "acme-corp".
GITHUB_ORG: str = _str("GITHUB_ORG")

GITHUB_API_URL: str = _str("GITHUB_API_URL", "https://api.github.com")
GITHUB_GRAPHQL_URL: str = _str("GITHUB_GRAPHQL_URL", "https://api.github.com/graphql")
GITHUB_API_VERSION: str = _str("GITHUB_API_VERSION", "2022-11-28")
USER_AGENT: str = _str("USER_AGENT", "org-hygiene-scanner/1.0")


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

DB_PATH: Path = Path(_str("DB_PATH", str(PROJECT_ROOT / "data" / "scanner.sqlite3")))
REPORT_PATH: Path = Path(_str("REPORT_PATH", str(PROJECT_ROOT / "out" / "dashboard.html")))
TEMPLATE_DIR: Path = Path(_str("TEMPLATE_DIR", str(PROJECT_ROOT / "templates")))

#: Chart.js is vendored here so the dashboard opens with no network access.
VENDOR_DIR: Path = Path(_str("VENDOR_DIR", str(PROJECT_ROOT / "vendor")))


# --------------------------------------------------------------------------
# Scan window and repo selection
# --------------------------------------------------------------------------

#: How far back contribution data is gathered. Also the window used to decide
#: whether a repo is "too new to judge" (a NEVER FLAG rule).
LOOKBACK_DAYS: int = _int("LOOKBACK_DAYS", 180)

#: Forks are skipped by default: contribution history on a fork mostly belongs
#: to the upstream project, so access there is weak evidence either way.
SKIP_FORKS: bool = _bool("SKIP_FORKS", True)

#: Archived repos are always scanned (stale admin access on an archived repo is
#: still access) but are tagged so the dashboard can separate them out.
SKIP_ARCHIVED: bool = _bool("SKIP_ARCHIVED", False)

#: "public", "private" or "all"
REPO_VISIBILITY: str = _str("REPO_VISIBILITY", "all")

#: Optional hard limit, useful for a first smoke run against a large org.
MAX_REPOS: int = _int("MAX_REPOS", 0)  # 0 = no limit

#: Only scan these repo names (comma separated). Empty means the whole org.
REPO_ALLOWLIST: tuple[str, ...] = _list("REPO_ALLOWLIST")

#: Never scan these repo names.
REPO_DENYLIST: tuple[str, ...] = _list("REPO_DENYLIST")


# --------------------------------------------------------------------------
# Scoring - the numbers below are the contract with score.py
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ScoringConfig:
    """Weights and thresholds for the contribution score.

        activity = 3.0*log1p(commits)
                 + 2.5*log1p(reviews)
                 + 2.0*log1p(prs_merged)
                 + 1.0*log1p(prs_opened)

        score    = activity * 0.5 ** (days_since_last_activity / 180)

        risk     = permission_weight / (1 + score)
    """

    commit_weight: float = 3.0
    review_weight: float = 2.5
    pr_merged_weight: float = 2.0
    pr_opened_weight: float = 1.0

    #: Days for a score to halve. The 0.5 base is fixed by the formula.
    half_life_days: float = 180.0

    #: A member is flagged for review when score < threshold.
    #: Calibration: one fresh commit and nothing else scores 2.08; five score
    #: 5.37; ten score 7.19. The default therefore flags roughly "fewer than
    #: four recent commits and no review activity".
    threshold: float = 5.0

    permission_weights: Mapping[str, float] = field(
        default_factory=lambda: {
            "admin": 3.0,
            "maintain": 2.0,
            "write": 1.5,
            "push": 1.5,    # REST sometimes reports the legacy name
            "triage": 0.5,
            "read": 0.5,
            "pull": 0.5,    # legacy name for read
        }
    )

    #: Used when GitHub reports a permission we do not recognise. Set to match
    #: "write" so an unknown role is never quietly treated as harmless.
    default_permission_weight: float = 1.5

    #: A member with no activity at all scores 0 whatever the decay does, so
    #: this does not affect the score. It is the "days since last activity"
    #: shown as evidence on the dashboard: the width of the scan window, which
    #: is the honest statement ("nothing in the last N days") rather than a
    #: fabricated date we never observed.
    no_activity_days: float = float(LOOKBACK_DAYS)

    def weight_for(self, permission: str | None) -> float:
        if not permission:
            return self.default_permission_weight
        return self.permission_weights.get(
            permission.strip().lower(), self.default_permission_weight
        )


SCORING = ScoringConfig(
    commit_weight=_float("WEIGHT_COMMITS", 3.0),
    review_weight=_float("WEIGHT_REVIEWS", 2.5),
    pr_merged_weight=_float("WEIGHT_PRS_MERGED", 2.0),
    pr_opened_weight=_float("WEIGHT_PRS_OPENED", 1.0),
    half_life_days=_float("HALF_LIFE_DAYS", 180.0),
    threshold=_float("SCORE_THRESHOLD", 5.0),
)


# --------------------------------------------------------------------------
# Exclusion rules ("NEVER FLAG"). Reasons are recorded, never silently dropped.
# --------------------------------------------------------------------------

#: Logins that must never appear in the removal list - service accounts,
#: break-glass users, auditors.
LOGIN_ALLOWLIST: tuple[str, ...] = tuple(
    login.lower() for login in _list("LOGIN_ALLOWLIST")
)

#: A repo with fewer than this many contributors is not judged: with a single
#: contributor there is no meaningful comparison to make.
MIN_CONTRIBUTORS: int = _int("MIN_CONTRIBUTORS", 2)


class ExclusionReason:
    """Machine-readable exclusion codes; report.py maps these to prose."""

    ORG_OWNER = "org_owner"
    BOT = "bot"
    ALLOWLISTED = "allowlisted"
    NEW_REPO = "repo_newer_than_lookback"
    SINGLE_CONTRIBUTOR = "repo_single_contributor"
    ARCHIVED_REPO = "repo_archived"   # tagged only; not an exclusion by default
    FORK_REPO = "repo_fork"
    EMPTY_REPO = "repo_empty"


EXCLUSION_LABELS: dict[str, str] = {
    ExclusionReason.ORG_OWNER: "Organization owner",
    ExclusionReason.BOT: "Bot or app account",
    ExclusionReason.ALLOWLISTED: "On the configured allowlist",
    ExclusionReason.NEW_REPO: f"Repository created within the last {LOOKBACK_DAYS} days",
    ExclusionReason.SINGLE_CONTRIBUTOR: "Repository has only one contributor",
    ExclusionReason.ARCHIVED_REPO: "Repository is archived",
    ExclusionReason.FORK_REPO: "Repository is a fork",
    ExclusionReason.EMPTY_REPO: "Repository is empty (no commits)",
}


# --------------------------------------------------------------------------
# HTTP: rate limits, retries, caching
# --------------------------------------------------------------------------

REQUEST_TIMEOUT: float = _float("REQUEST_TIMEOUT", 30.0)
PER_PAGE: int = _int("PER_PAGE", 100)

#: When X-RateLimit-Remaining drops to this, pause until the window resets
#: instead of burning the last requests and taking a hard 403.
RATE_LIMIT_MIN_REMAINING: int = _int("RATE_LIMIT_MIN_REMAINING", 50)

#: Never sleep longer than this in one go, even if a reset header asks for it.
#: A pathological header should surface as an error, not an hour-long nap.
RATE_LIMIT_MAX_SLEEP: float = _float("RATE_LIMIT_MAX_SLEEP", 900.0)

#: Padding added to a computed sleep, to cover clock skew with GitHub.
RATE_LIMIT_SLEEP_PADDING: float = _float("RATE_LIMIT_SLEEP_PADDING", 2.0)

MAX_RETRIES: int = _int("MAX_RETRIES", 5)
BACKOFF_BASE: float = _float("BACKOFF_BASE", 1.5)
BACKOFF_CAP: float = _float("BACKOFF_CAP", 60.0)

#: Send stored ETags. A 304 response does not count against the quota.
USE_ETAG_CACHE: bool = _bool("USE_ETAG_CACHE", True)

#: Drop cached bodies older than this, so a resumed run cannot keep serving
#: indefinitely stale data out of a 304.
CACHE_TTL_HOURS: int = _int("CACHE_TTL_HOURS", 24 * 7)

#: Try the org-wide Dependabot endpoint first (one paginated call for the whole
#: org); fall back to per-repo when the token or plan does not allow it.
ORG_DEPENDABOT_FIRST: bool = _bool("ORG_DEPENDABOT_FIRST", True)

#: GraphQL page sizes. Kept modest: the commit history connection is the most
#: expensive part of the per-repo query and large pages time out on big repos.
GQL_COMMIT_PAGE: int = _int("GQL_COMMIT_PAGE", 100)
GQL_PR_PAGE: int = _int("GQL_PR_PAGE", 50)
GQL_REVIEW_PAGE: int = _int("GQL_REVIEW_PAGE", 50)

#: Hard stop on how many pages of history we walk for one repo, so a single
#: monorepo cannot consume an entire run.
GQL_MAX_PAGES: int = _int("GQL_MAX_PAGES", 40)


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

SEVERITY_ORDER: tuple[str, ...] = ("critical", "high", "medium", "low")
SEVERITY_COLORS: dict[str, str] = {
    "critical": "#b3122b",
    "high": "#d9480f",
    "medium": "#c98a00",
    "low": "#2f6f8f",
    "unknown": "#767676",
}

#: How many rows the "most affected repos" chart shows.
TOP_AFFECTED_REPOS: int = _int("TOP_AFFECTED_REPOS", 10)

LOG_LEVEL: str = _str("LOG_LEVEL", "INFO").upper()


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

class ConfigError(RuntimeError):
    """Raised when the configuration cannot support a scan."""


def validate() -> None:
    """Fail fast, with a message that says what to fix."""
    missing = []
    if not GITHUB_TOKEN:
        missing.append("GITHUB_TOKEN")
    if not GITHUB_ORG:
        missing.append("GITHUB_ORG")
    if missing:
        raise ConfigError(
            "Missing required setting(s): "
            + ", ".join(missing)
            + f". Add them to {PROJECT_ROOT / '.env'} (see .env.example)."
        )
    if SCORING.half_life_days <= 0:
        raise ConfigError("HALF_LIFE_DAYS must be greater than zero.")
    if LOOKBACK_DAYS <= 0:
        raise ConfigError("LOOKBACK_DAYS must be greater than zero.")


def summary() -> dict[str, object]:
    """The configuration recorded on each run row, for auditability.

    The token is never included.
    """
    return {
        "org": GITHUB_ORG,
        "lookback_days": LOOKBACK_DAYS,
        "skip_forks": SKIP_FORKS,
        "skip_archived": SKIP_ARCHIVED,
        "repo_visibility": REPO_VISIBILITY,
        "min_contributors": MIN_CONTRIBUTORS,
        "login_allowlist": list(LOGIN_ALLOWLIST),
        "scoring": {
            "commit_weight": SCORING.commit_weight,
            "review_weight": SCORING.review_weight,
            "pr_merged_weight": SCORING.pr_merged_weight,
            "pr_opened_weight": SCORING.pr_opened_weight,
            "half_life_days": SCORING.half_life_days,
            "threshold": SCORING.threshold,
            "permission_weights": dict(SCORING.permission_weights),
        },
    }

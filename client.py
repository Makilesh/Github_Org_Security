"""HTTP client for the GitHub REST and GraphQL APIs.

Everything the rest of the project needs from the network goes through here:

* **Rate limits.** `X-RateLimit-Remaining` is read off every response and the
  client pauses before the quota runs out, rather than after. `Retry-After`
  is honoured on 403 and 429, including GitHub's undocumented-but-real
  secondary rate limit.
* **Conditional requests.** Stored ETags are sent on every GET. A 304 costs
  nothing against the quota, so a re-run over an unchanged org is close to
  free. The cached envelope keeps the `Link` header alongside the body, so a
  304 on page 3 still knows where page 4 is.
* **Retries.** Transport errors and 5xx get exponential backoff with jitter.
  Anything else is raised as a typed exception - this module never returns a
  half-result or swallows a failure.

Error types are deliberately fine-grained, because the callers need to tell
them apart: a 409 means an empty repo (normal, keep going), a 403 might mean
"quota exhausted" (wait) or "Dependabot not enabled here" (fall back to the
per-repo endpoint), and those must not be handled the same way.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Mapping, Protocol
from urllib.parse import urlencode, urljoin

import httpx

import config

log = logging.getLogger(__name__)

_LINK_RE = re.compile(r'<(?P<url>[^>]+)>;\s*rel="(?P<rel>[^"]+)"')


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------

class GitHubError(RuntimeError):
    """Any non-success response that the client decided not to retry."""

    def __init__(self, message: str, *, status: int | None = None,
                 url: str | None = None, body: Any = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.url = url
        self.body = body

    def __str__(self) -> str:
        bits = [self.message]
        if self.status:
            bits.append(f"(HTTP {self.status})")
        if self.url:
            bits.append(f"at {self.url}")
        return " ".join(bits)


class NotFoundError(GitHubError):
    """404. Often expected - a repo with no Dependabot config, for instance."""


class ForbiddenError(GitHubError):
    """403 that is *not* a rate limit: a permission or feature-disabled error.

    Callers use this to fall back (org-level Dependabot -> per-repo) rather
    than to abort.
    """


class EmptyRepositoryError(GitHubError):
    """409 from a Git-data endpoint: the repository has no commits yet."""


class RateLimitError(GitHubError):
    """Quota exhausted and the wait would exceed RATE_LIMIT_MAX_SLEEP."""

    def __init__(self, message: str, *, reset_at: float | None = None, **kw: Any):
        super().__init__(message, **kw)
        self.reset_at = reset_at


class GraphQLError(GitHubError):
    """The GraphQL endpoint returned an `errors` array we cannot recover from."""

    def __init__(self, message: str, *, errors: list[dict[str, Any]] | None = None, **kw: Any):
        super().__init__(message, **kw)
        self.errors = errors or []


# --------------------------------------------------------------------------
# Cache protocol - satisfied by db.Database, so client.py never imports db.py
# --------------------------------------------------------------------------

class CacheStore(Protocol):
    def get_cached(self, key: str) -> tuple[str | None, str | None, str] | None: ...
    def set_cached(self, key: str, etag: str | None, body: str,
                   last_modified: str | None = ..., status: int | None = ...) -> None: ...


# --------------------------------------------------------------------------
# Results and stats
# --------------------------------------------------------------------------

@dataclass
class ApiResult:
    """One REST response, plus the bits of metadata callers care about."""

    data: Any
    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    from_cache: bool = False
    next_url: str | None = None


@dataclass
class ClientStats:
    """Cheap observability; printed at the end of a run and in --json."""

    requests: int = 0
    cache_hits: int = 0          # 304s served from the local cache
    retries: int = 0
    rate_limit_sleeps: int = 0
    rate_limit_slept_seconds: float = 0.0
    graphql_queries: int = 0
    graphql_cost: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "cache_hits": self.cache_hits,
            "retries": self.retries,
            "rate_limit_sleeps": self.rate_limit_sleeps,
            "rate_limit_slept_seconds": round(self.rate_limit_slept_seconds, 1),
            "graphql_queries": self.graphql_queries,
            "graphql_cost": self.graphql_cost,
            "errors": self.errors,
        }


@dataclass
class RateLimitState:
    """Last-seen values of the rate limit headers, per resource."""

    limit: int | None = None
    remaining: int | None = None
    reset_at: float | None = None
    resource: str | None = None
    used: int | None = None

    @property
    def seconds_until_reset(self) -> float:
        if self.reset_at is None:
            return 0.0
        return max(0.0, self.reset_at - time.time())


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

class GitHubClient:
    """Synchronous GitHub client with caching, backoff and rate-limit care.

    Usage::

        with Database(config.DB_PATH) as db, GitHubClient(cache=db) as gh:
            for repo in gh.paginate("/orgs/acme/repos", {"type": "all"}):
                ...
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        cache: CacheStore | None = None,
        api_url: str | None = None,
        graphql_url: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        min_remaining: int | None = None,
        use_cache: bool | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        transport: httpx.BaseTransport | None = None,
    ):
        self.token = token if token is not None else config.GITHUB_TOKEN
        self.api_url = (api_url or config.GITHUB_API_URL).rstrip("/")
        self.graphql_url = graphql_url or config.GITHUB_GRAPHQL_URL
        self.cache = cache
        self.use_cache = config.USE_ETAG_CACHE if use_cache is None else use_cache
        self.max_retries = config.MAX_RETRIES if max_retries is None else max_retries
        self.min_remaining = (
            config.RATE_LIMIT_MIN_REMAINING if min_remaining is None else min_remaining
        )
        self._sleep = sleep_fn
        self.stats = ClientStats()
        self.rate_limit = RateLimitState()

        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": config.GITHUB_API_VERSION,
            "User-Agent": config.USER_AGENT,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        self._http = httpx.Client(
            headers=headers,
            timeout=timeout if timeout is not None else config.REQUEST_TIMEOUT,
            follow_redirects=True,
            transport=transport,
        )

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # -- URL and cache helpers --------------------------------------------

    def _abs_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return urljoin(self.api_url + "/", path.lstrip("/"))

    @staticmethod
    def _cache_key(url: str, params: Mapping[str, Any] | None) -> str:
        if params:
            query = urlencode(sorted((k, v) for k, v in params.items() if v is not None))
            return f"{url}?{query}" if query else url
        return url

    def _cache_load(self, key: str) -> tuple[str | None, str | None, dict[str, Any]] | None:
        """Return (etag, last_modified, envelope) or None.

        The envelope is `{"data": ..., "link": ...}`; keeping the Link header
        with the body is what lets a 304 on one page still find the next one.
        """
        if not self.use_cache or self.cache is None:
            return None
        row = self.cache.get_cached(key)
        if not row:
            return None
        etag, last_modified, body = row
        try:
            envelope = json.loads(body)
        except (TypeError, ValueError):
            log.debug("Discarding unparseable cache entry for %s", key)
            return None
        if not isinstance(envelope, dict) or "data" not in envelope:
            return None
        return etag, last_modified, envelope

    def _cache_store(self, key: str, response: httpx.Response, data: Any) -> None:
        # `is None`, not truthiness: a cache backend that is empty (or defines
        # __len__) is still a usable cache, and testing it for truth would
        # silently disable caching for the whole run.
        if not self.use_cache or self.cache is None:
            return
        etag = response.headers.get("etag")
        last_modified = response.headers.get("last-modified")
        if not etag and not last_modified:
            return  # nothing to revalidate with; storing the body would be dead weight
        envelope = {"data": data, "link": response.headers.get("link", "")}
        try:
            body = json.dumps(envelope)
        except (TypeError, ValueError):
            return
        self.cache.set_cached(key, etag, body, last_modified, response.status_code)

    @staticmethod
    def _next_link(link_header: str | None) -> str | None:
        if not link_header:
            return None
        for match in _LINK_RE.finditer(link_header):
            if match.group("rel") == "next":
                return match.group("url")
        return None

    # -- rate limiting -----------------------------------------------------

    def _note_rate_limit(self, response: httpx.Response) -> None:
        h = response.headers
        try:
            if "x-ratelimit-remaining" in h:
                self.rate_limit.remaining = int(h["x-ratelimit-remaining"])
            if "x-ratelimit-limit" in h:
                self.rate_limit.limit = int(h["x-ratelimit-limit"])
            if "x-ratelimit-used" in h:
                self.rate_limit.used = int(h["x-ratelimit-used"])
            if "x-ratelimit-reset" in h:
                self.rate_limit.reset_at = float(h["x-ratelimit-reset"])
        except (TypeError, ValueError):
            log.debug("Unparseable rate limit headers: %s", dict(h))
        self.rate_limit.resource = h.get("x-ratelimit-resource", self.rate_limit.resource)

    def _pause(self, seconds: float, why: str) -> None:
        seconds = max(0.0, min(seconds, config.RATE_LIMIT_MAX_SLEEP))
        if seconds <= 0:
            return
        self.stats.rate_limit_sleeps += 1
        self.stats.rate_limit_slept_seconds += seconds
        log.warning("Pausing %.0fs: %s", seconds, why)
        self._sleep(seconds)

    def _wait_if_quota_low(self) -> None:
        """Pre-emptive pause, before the quota is actually gone."""
        rl = self.rate_limit
        if rl.remaining is None or rl.remaining > self.min_remaining:
            return
        wait = rl.seconds_until_reset + config.RATE_LIMIT_SLEEP_PADDING
        if wait <= 0:
            return
        if wait > config.RATE_LIMIT_MAX_SLEEP:
            raise RateLimitError(
                f"Rate limit for {rl.resource or 'core'} nearly exhausted "
                f"({rl.remaining} left) and reset is {wait:.0f}s away, over the "
                f"{config.RATE_LIMIT_MAX_SLEEP:.0f}s cap.",
                reset_at=rl.reset_at,
            )
        self._pause(
            wait,
            f"{rl.remaining} requests left on {rl.resource or 'core'} quota, "
            "waiting for the window to reset",
        )
        rl.remaining = None  # force a re-read from the next response

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float | None:
        raw = response.headers.get("retry-after")
        if not raw:
            return None
        raw = raw.strip()
        try:
            return float(raw)
        except ValueError:
            pass
        # RFC 7231 also allows an HTTP-date.
        try:
            when = datetime.strptime(raw, "%a, %d %b %Y %H:%M:%S GMT").replace(
                tzinfo=timezone.utc
            )
            return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
        except ValueError:
            return None

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with full jitter, capped."""
        window = min(config.BACKOFF_CAP, config.BACKOFF_BASE ** (attempt + 1))
        return random.uniform(0.0, window)

    # -- the one place a request is actually made --------------------------

    def _send(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Send one request, retrying transport errors, 5xx and rate limits.

        Returns the response for any status the caller might reasonably act on
        (2xx, 304, 404, 409, permission-403). Raises for everything else.
        """
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            self._wait_if_quota_low()
            try:
                self.stats.requests += 1
                response = self._http.request(
                    method, url, params=params, json=json_body,
                    headers=dict(extra_headers or {}),
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                self.stats.errors += 1
                if attempt >= self.max_retries:
                    raise GitHubError(
                        f"Transport error after {attempt + 1} attempts: {exc}", url=url
                    ) from exc
                delay = self._backoff(attempt)
                self.stats.retries += 1
                log.warning("Transport error on %s (%s); retrying in %.1fs", url, exc, delay)
                self._sleep(delay)
                continue

            self._note_rate_limit(response)
            status = response.status_code

            if status < 400 or status in (304, 404, 409):
                return response

            if status in (403, 429):
                decision = self._classify_403(response)
                if decision == "permission":
                    return response  # caller decides; this is not retryable
                if attempt >= self.max_retries:
                    raise RateLimitError(
                        f"Still rate limited after {attempt + 1} attempts: "
                        f"{self._error_message(response)}",
                        status=status, url=url, reset_at=self.rate_limit.reset_at,
                    )
                retry_after = self._retry_after_seconds(response)
                if retry_after is not None:
                    wait, why = retry_after, "server sent Retry-After"
                elif decision == "primary":
                    wait = self.rate_limit.seconds_until_reset + config.RATE_LIMIT_SLEEP_PADDING
                    why = "primary rate limit exhausted"
                else:
                    wait = self._backoff(attempt) + config.RATE_LIMIT_SLEEP_PADDING
                    why = "secondary rate limit"
                if wait > config.RATE_LIMIT_MAX_SLEEP:
                    raise RateLimitError(
                        f"Rate limited; the required wait of {wait:.0f}s exceeds the "
                        f"{config.RATE_LIMIT_MAX_SLEEP:.0f}s cap.",
                        status=status, url=url, reset_at=self.rate_limit.reset_at,
                    )
                self.stats.retries += 1
                self._pause(wait, f"{why} on {url}")
                continue

            if status >= 500:
                self.stats.errors += 1
                if attempt >= self.max_retries:
                    raise GitHubError(
                        f"Server error after {attempt + 1} attempts: "
                        f"{self._error_message(response)}",
                        status=status, url=url,
                    )
                delay = self._backoff(attempt)
                self.stats.retries += 1
                log.warning("HTTP %s on %s; retrying in %.1fs", status, url, delay)
                self._sleep(delay)
                continue

            # 400, 401, 422, ... - a real problem with the request itself.
            self.stats.errors += 1
            raise GitHubError(
                self._error_message(response), status=status, url=url,
                body=self._safe_json(response),
            )

        raise GitHubError(  # pragma: no cover - loop always returns or raises
            f"Request to {url} failed: {last_error}", url=url
        )

    def _classify_403(self, response: httpx.Response) -> str:
        """'primary' | 'secondary' | 'permission'.

        A 403 from GitHub is overloaded. Getting this wrong either burns the
        run on a permission error we should have skipped, or hammers the API
        on a rate limit we should have waited out.
        """
        if response.status_code == 429:
            return "secondary"
        remaining = response.headers.get("x-ratelimit-remaining")
        if remaining is not None and remaining.strip() == "0":
            return "primary"
        message = (self._error_message(response) or "").lower()
        if "secondary rate limit" in message or "abuse detection" in message:
            return "secondary"
        if "rate limit" in message:
            return "primary"
        if response.headers.get("retry-after"):
            return "secondary"
        return "permission"

    @staticmethod
    def _safe_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return None

    @classmethod
    def _error_message(cls, response: httpx.Response) -> str:
        body = cls._safe_json(response)
        if isinstance(body, dict):
            message = body.get("message")
            errors = body.get("errors")
            if message and errors:
                return f"{message}: {json.dumps(errors)[:300]}"
            if message:
                return str(message)
        text = (response.text or "").strip()
        return text[:300] or f"HTTP {response.status_code}"

    # -- REST --------------------------------------------------------------

    def get(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        allow_404: bool = False,
        allow_403: bool = False,
        use_cache: bool | None = None,
    ) -> ApiResult:
        """GET one URL, using and updating the ETag cache.

        `allow_404` / `allow_403` turn those statuses into `ApiResult(data=None)`
        instead of an exception, for endpoints where absence is expected.
        A 409 always raises `EmptyRepositoryError`: callers must handle empty
        repos explicitly rather than treating them as "no data".
        """
        url = self._abs_url(path)
        key = self._cache_key(url, params)
        caching = self.use_cache if use_cache is None else use_cache

        headers: dict[str, str] = {}
        cached = self._cache_load(key) if caching else None
        if cached:
            etag, last_modified, _ = cached
            if etag:
                headers["If-None-Match"] = etag
            elif last_modified:
                headers["If-Modified-Since"] = last_modified

        response = self._send("GET", url, params=params, extra_headers=headers)
        status = response.status_code

        if status == 304 and cached:
            self.stats.cache_hits += 1
            _, _, envelope = cached
            return ApiResult(
                data=envelope["data"], status=304, headers=response.headers,
                from_cache=True, next_url=self._next_link(envelope.get("link")),
            )

        if status == 304:
            # Revalidated against an entry we no longer hold. Refetch cold.
            log.debug("304 with no cached body for %s; refetching without ETag", key)
            response = self._send("GET", url, params=params)
            status = response.status_code

        if status == 409:
            raise EmptyRepositoryError(
                self._error_message(response), status=409, url=url,
            )

        if status == 404:
            if allow_404:
                return ApiResult(data=None, status=404, headers=response.headers)
            raise NotFoundError(self._error_message(response), status=404, url=url)

        if status == 403:
            if allow_403:
                return ApiResult(data=None, status=403, headers=response.headers)
            raise ForbiddenError(self._error_message(response), status=403, url=url)

        data = self._safe_json(response)
        if caching:
            self._cache_store(key, response, data)
        return ApiResult(
            data=data, status=status, headers=response.headers,
            next_url=self._next_link(response.headers.get("link")),
        )

    def paginate(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        items_key: str | None = None,
        allow_404: bool = False,
        allow_403: bool = False,
        max_pages: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield items across every page, following the `Link: rel="next"` header.

        Link-following (rather than incrementing `page`) is required because
        some endpoints - org-level Dependabot alerts among them - use cursor
        pagination and ignore `page` entirely.

        `items_key` pulls the list out of a wrapper object, e.g. `"items"` for
        the search endpoints.
        """
        query: dict[str, Any] = {"per_page": config.PER_PAGE}
        query.update({k: v for k, v in (params or {}).items() if v is not None})

        url: str | None = self._abs_url(path)
        first = True
        pages = 0

        while url:
            result = self.get(
                url,
                params=query if first else None,   # the next URL already carries them
                allow_404=allow_404,
                allow_403=allow_403,
            )
            first = False
            pages += 1

            data = result.data
            if data is None:
                return
            if items_key and isinstance(data, dict):
                data = data.get(items_key) or []
            if isinstance(data, dict):
                data = [data]
            if not isinstance(data, list):
                raise GitHubError(
                    f"Expected a list from {url}, got {type(data).__name__}", url=url
                )
            for item in data:
                yield item

            if max_pages and pages >= max_pages:
                log.warning("Stopped paginating %s after %d pages (cap reached)", path, pages)
                return
            url = result.next_url

    # -- GraphQL -----------------------------------------------------------

    def graphql(
        self,
        query: str,
        variables: Mapping[str, Any] | None = None,
        *,
        partial_ok: bool = False,
    ) -> dict[str, Any]:
        """Run one GraphQL query and return its `data` object.

        GraphQL reports failure with HTTP 200 and an `errors` array, so the
        status code alone means nothing here. Rate limiting arrives as
        `type: RATE_LIMITED`, which is retried; `NOT_FOUND` becomes a
        `NotFoundError` so callers can skip a repo that vanished mid-run.

        With `partial_ok`, a response carrying both `data` and timeout-ish
        errors returns the data and logs the errors. That happens on very
        large repositories, where one field times out and the rest is sound.
        """
        payload = {"query": query, "variables": dict(variables or {})}

        for attempt in range(self.max_retries + 1):
            response = self._send("POST", self.graphql_url, json_body=payload)
            self.stats.graphql_queries += 1

            if response.status_code == 404:
                raise NotFoundError("GraphQL endpoint returned 404", status=404,
                                    url=self.graphql_url)
            if response.status_code == 403:
                raise ForbiddenError(self._error_message(response), status=403,
                                     url=self.graphql_url)

            body = self._safe_json(response)
            if not isinstance(body, dict):
                raise GraphQLError("GraphQL response was not a JSON object",
                                   status=response.status_code, url=self.graphql_url)

            data = body.get("data")
            errors = body.get("errors") or []

            if isinstance(data, dict):
                limit = (data.get("rateLimit") or {}) if data else {}
                if limit:
                    self._note_graphql_rate_limit(limit)
                    self.stats.graphql_cost += int(limit.get("cost") or 0)

            if not errors:
                if data is None:
                    raise GraphQLError("GraphQL response had neither data nor errors",
                                       status=response.status_code, url=self.graphql_url,
                                       body=body)
                return data

            types = {str(e.get("type") or "").upper() for e in errors}
            messages = "; ".join(str(e.get("message", ""))[:200] for e in errors[:3])

            if "RATE_LIMITED" in types:
                if attempt >= self.max_retries:
                    raise RateLimitError(f"GraphQL rate limited: {messages}",
                                         url=self.graphql_url)
                wait = self.rate_limit.seconds_until_reset + config.RATE_LIMIT_SLEEP_PADDING
                self.stats.retries += 1
                self._pause(wait if wait > 0 else self._backoff(attempt),
                            "GraphQL rate limit")
                continue

            if "NOT_FOUND" in types:
                raise NotFoundError(f"GraphQL: {messages}", url=self.graphql_url, body=body)

            if data is not None and (partial_ok or types <= {"SERVICE_UNAVAILABLE", ""}):
                log.warning("GraphQL returned partial data with errors: %s", messages)
                return data

            self.stats.errors += 1
            raise GraphQLError(f"GraphQL query failed: {messages}",
                               errors=list(errors), url=self.graphql_url, body=body)

        raise GraphQLError("GraphQL query exhausted retries",  # pragma: no cover
                           url=self.graphql_url)

    def _note_graphql_rate_limit(self, limit: Mapping[str, Any]) -> None:
        """GraphQL has its own quota, reported in the query result, not headers."""
        try:
            if "remaining" in limit:
                self.rate_limit.remaining = int(limit["remaining"])
            if "limit" in limit:
                self.rate_limit.limit = int(limit["limit"])
            reset_at = limit.get("resetAt")
            if reset_at:
                self.rate_limit.reset_at = datetime.fromisoformat(
                    str(reset_at).replace("Z", "+00:00")
                ).timestamp()
            self.rate_limit.resource = "graphql"
        except (TypeError, ValueError):
            log.debug("Unparseable GraphQL rateLimit block: %s", dict(limit))

    # -- diagnostics -------------------------------------------------------

    def check_auth(self) -> dict[str, Any]:
        """Verify the token works before a long run. Raises on a bad token."""
        result = self.get("/rate_limit", use_cache=False)
        resources = (result.data or {}).get("resources", {})
        core = resources.get("core", {})
        graphql = resources.get("graphql", {})
        return {
            "core_remaining": core.get("remaining"),
            "core_limit": core.get("limit"),
            "graphql_remaining": graphql.get("remaining"),
            "graphql_limit": graphql.get("limit"),
            "resets_in_seconds": max(0, int((core.get("reset") or 0) - time.time())),
        }

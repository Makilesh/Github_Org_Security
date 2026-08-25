"""Tests for the HTTP layer: rate limits, conditional requests, backoff, errors.

Everything runs against `httpx.MockTransport`, and `sleep_fn` is injected so a
test that exercises a 900-second rate-limit wait finishes instantly while still
asserting the exact number of seconds the client *would* have slept.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest

import config
from client import (
    EmptyRepositoryError,
    ForbiddenError,
    GitHubClient,
    GitHubError,
    GraphQLError,
    NotFoundError,
    RateLimitError,
)


class MemoryCache:
    """Minimal CacheStore. Starts empty on purpose - an empty cache is still a cache."""

    def __init__(self):
        self.entries: dict[str, tuple[str | None, str | None, str]] = {}

    def get_cached(self, key):
        return self.entries.get(key)

    def set_cached(self, key, etag, body, last_modified=None, status=200):
        self.entries[key] = (etag, last_modified, body)


@pytest.fixture
def slept():
    return []


def make_client(handler, slept, **kw):
    return GitHubClient(
        "test-token",
        cache=kw.pop("cache", MemoryCache()),
        api_url="https://api.github.com",
        graphql_url="https://api.github.com/graphql",
        transport=httpx.MockTransport(handler),
        sleep_fn=slept.append,
        **kw,
    )


def ok(payload, **headers):
    base = {"x-ratelimit-remaining": "4000", "x-ratelimit-limit": "5000"}
    base.update(headers)
    return httpx.Response(200, json=payload, headers=base)


# --------------------------------------------------------------------------
# Status handling
# --------------------------------------------------------------------------

class TestStatusHandling:
    def test_409_on_an_empty_repo_is_its_own_error(self, slept):
        """Empty repos are normal. They must be distinguishable from failures."""
        gh = make_client(
            lambda r: httpx.Response(409, json={"message": "Git Repository is empty."}),
            slept,
        )
        with pytest.raises(EmptyRepositoryError) as exc:
            gh.get("/repos/o/r/commits")
        assert exc.value.status == 409

    def test_404_raises_by_default(self, slept):
        gh = make_client(lambda r: httpx.Response(404, json={"message": "Not Found"}), slept)
        with pytest.raises(NotFoundError):
            gh.get("/repos/o/missing")

    def test_404_returns_none_when_allowed(self, slept):
        gh = make_client(lambda r: httpx.Response(404, json={"message": "Not Found"}), slept)
        result = gh.get("/repos/o/missing", allow_404=True)
        assert result.data is None and result.status == 404

    def test_permission_403_is_not_retried_as_a_rate_limit(self, slept):
        """Dependabot-disabled must fail fast so the caller can fall back."""
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(
                403, json={"message": "Dependabot alerts are disabled for this repository"},
                headers={"x-ratelimit-remaining": "4000"},
            )

        gh = make_client(handler, slept)
        with pytest.raises(ForbiddenError):
            gh.get("/repos/o/r/dependabot/alerts")
        assert len(calls) == 1, "a permission error must not be retried"
        assert slept == []

    def test_403_returns_none_when_allowed(self, slept):
        gh = make_client(
            lambda r: httpx.Response(403, json={"message": "disabled"},
                                     headers={"x-ratelimit-remaining": "4000"}),
            slept,
        )
        assert gh.get("/x", allow_403=True).data is None

    def test_422_raises_immediately_without_retrying(self, slept):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(422, json={"message": "Validation failed",
                                             "errors": [{"field": "state"}]})

        gh = make_client(handler, slept)
        with pytest.raises(GitHubError) as exc:
            gh.get("/x")
        assert exc.value.status == 422
        assert "Validation failed" in str(exc.value)
        assert len(calls) == 1


# --------------------------------------------------------------------------
# Rate limits
# --------------------------------------------------------------------------

class TestRateLimits:
    def test_retry_after_is_honoured_exactly(self, slept):
        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(
                    403, json={"message": "You have exceeded a secondary rate limit"},
                    headers={"retry-after": "7", "x-ratelimit-remaining": "500"},
                )
            return ok({"done": True})

        gh = make_client(handler, slept)
        assert gh.get("/x").data == {"done": True}
        assert slept == [7.0]

    def test_429_is_treated_as_a_rate_limit(self, slept):
        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(429, json={"message": "Too many requests"},
                                      headers={"retry-after": "4"})
            return ok({"done": True})

        gh = make_client(handler, slept)
        assert gh.get("/x").data == {"done": True}
        assert slept == [4.0]

    def test_primary_limit_waits_for_the_reset_window(self, slept):
        calls = []
        reset = int(time.time()) + 45

        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(
                    403, json={"message": "API rate limit exceeded"},
                    headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)},
                )
            return ok({"done": True})

        gh = make_client(handler, slept)
        assert gh.get("/x").data == {"done": True}
        assert 40 < slept[0] < 55

    def test_client_pauses_before_the_quota_runs_out(self, slept):
        """Pre-emptive: pause while requests remain, not after they are gone."""
        reset = int(time.time()) + 30

        def handler(request):
            return ok({"n": 1}, **{"x-ratelimit-remaining": "3",
                                   "x-ratelimit-reset": str(reset)})

        gh = make_client(handler, slept, min_remaining=50)
        gh.get("/first")     # reads the low header
        gh.get("/second")    # must pause before sending
        assert slept, "client burned through the last requests without pausing"
        assert 25 < slept[0] < 40

    def test_wait_longer_than_the_cap_raises_instead_of_sleeping(self, slept, monkeypatch):
        monkeypatch.setattr(config, "RATE_LIMIT_MAX_SLEEP", 10.0)
        reset = int(time.time()) + 3600

        gh = make_client(
            lambda r: httpx.Response(
                403, json={"message": "API rate limit exceeded"},
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)},
            ),
            slept,
        )
        with pytest.raises(RateLimitError):
            gh.get("/x")

    def test_http_date_retry_after_is_parsed(self, slept):
        from email.utils import format_datetime
        from datetime import datetime, timedelta, timezone

        when = datetime.now(timezone.utc) + timedelta(seconds=20)
        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(
                    429, json={"message": "slow down"},
                    headers={"retry-after": format_datetime(when, usegmt=True)},
                )
            return ok({"done": True})

        gh = make_client(handler, slept)
        gh.get("/x")
        assert 10 < slept[0] <= 21


# --------------------------------------------------------------------------
# Retries
# --------------------------------------------------------------------------

class TestRetries:
    def test_5xx_is_retried_with_backoff(self, slept):
        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) < 3:
                return httpx.Response(502, text="bad gateway")
            return ok({"done": True})

        gh = make_client(handler, slept)
        assert gh.get("/x").data == {"done": True}
        assert len(slept) == 2
        assert all(s >= 0 for s in slept)

    def test_persistent_5xx_eventually_raises(self, slept):
        gh = make_client(lambda r: httpx.Response(500, text="boom"), slept, max_retries=2)
        with pytest.raises(GitHubError) as exc:
            gh.get("/x")
        assert exc.value.status == 500

    def test_transport_errors_are_retried(self, slept):
        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) < 2:
                raise httpx.ConnectError("connection reset", request=request)
            return ok({"done": True})

        gh = make_client(handler, slept)
        assert gh.get("/x").data == {"done": True}
        assert len(slept) == 1


# --------------------------------------------------------------------------
# Conditional requests
# --------------------------------------------------------------------------

class TestEtagCache:
    def _handler(self, calls):
        def handler(request):
            calls.append(request)
            if request.headers.get("if-none-match") == 'W/"v1"':
                return httpx.Response(304, headers={"x-ratelimit-remaining": "4999"})
            return ok({"v": 1}, etag='W/"v1"',
                      link='<https://api.github.com/x?page=2>; rel="next"')
        return handler

    def test_second_request_is_served_from_a_304(self, slept):
        calls = []
        gh = make_client(self._handler(calls), slept)

        first = gh.get("/x")
        second = gh.get("/x")

        assert first.data == second.data == {"v": 1}
        assert not first.from_cache and second.from_cache
        assert gh.stats.cache_hits == 1

    def test_a_304_still_knows_the_next_page(self, slept):
        """Link headers are cached with the body, or pagination breaks on re-runs."""
        calls = []
        gh = make_client(self._handler(calls), slept)
        gh.get("/x")
        cached = gh.get("/x")
        assert cached.next_url.endswith("page=2")

    def test_an_empty_cache_object_does_not_disable_caching(self, slept):
        """Regression: guarding on truthiness made a fresh cache never populate."""
        cache = MemoryCache()
        assert not cache.entries
        gh = make_client(self._handler([]), slept, cache=cache)
        gh.get("/x")
        assert cache.entries, "nothing was stored, so re-runs would never hit a 304"

    def test_caching_can_be_switched_off(self, slept):
        cache = MemoryCache()
        gh = make_client(self._handler([]), slept, cache=cache, use_cache=False)
        gh.get("/x")
        gh.get("/x")
        assert cache.entries == {}
        assert gh.stats.cache_hits == 0

    def test_responses_without_validators_are_not_stored(self, slept):
        cache = MemoryCache()
        gh = make_client(lambda r: ok({"v": 1}), slept, cache=cache)
        gh.get("/x")
        assert cache.entries == {}, "storing an unvalidatable body is dead weight"


# --------------------------------------------------------------------------
# Pagination
# --------------------------------------------------------------------------

class TestPagination:
    def test_follows_link_rel_next(self, slept):
        def handler(request):
            page = request.url.params.get("page", "1")
            if page == "1":
                return ok([{"n": 1}, {"n": 2}],
                          link='<https://api.github.com/items?page=2>; rel="next"')
            if page == "2":
                return ok([{"n": 3}],
                          link='<https://api.github.com/items?page=3>; rel="next"')
            return ok([{"n": 4}])

        gh = make_client(handler, slept)
        assert [i["n"] for i in gh.paginate("/items")] == [1, 2, 3, 4]

    def test_cursor_pagination_works_without_page_numbers(self, slept):
        """Org-level Dependabot alerts use cursors and ignore `page` entirely."""
        def handler(request):
            after = request.url.params.get("after")
            if not after:
                return ok([{"n": 1}],
                          link='<https://api.github.com/alerts?after=abc>; rel="next"')
            return ok([{"n": 2}])

        gh = make_client(handler, slept)
        assert [i["n"] for i in gh.paginate("/alerts")] == [1, 2]

    def test_per_page_is_applied_to_the_first_request(self, slept):
        seen = []

        def handler(request):
            seen.append(request.url.params.get("per_page"))
            return ok([])

        gh = make_client(handler, slept)
        list(gh.paginate("/items"))
        assert seen == [str(config.PER_PAGE)]

    def test_max_pages_stops_a_runaway(self, slept):
        def handler(request):
            return ok([{"n": 1}], link='<https://api.github.com/items?page=9>; rel="next"')

        gh = make_client(handler, slept)
        assert len(list(gh.paginate("/items", max_pages=3))) == 3

    def test_missing_resource_yields_nothing_when_allowed(self, slept):
        gh = make_client(lambda r: httpx.Response(404, json={"message": "Not Found"}), slept)
        assert list(gh.paginate("/items", allow_404=True)) == []


# --------------------------------------------------------------------------
# GraphQL
# --------------------------------------------------------------------------

class TestGraphQL:
    def test_returns_data_and_tracks_cost(self, slept):
        gh = make_client(
            lambda r: httpx.Response(200, json={
                "data": {"repository": {"name": "api"},
                         "rateLimit": {"cost": 4, "remaining": 4996, "limit": 5000,
                                       "resetAt": "2026-08-25T13:00:00Z"}}}),
            slept,
        )
        data = gh.graphql("query { repository { name } }")
        assert data["repository"]["name"] == "api"
        assert gh.stats.graphql_cost == 4

    def test_rate_limited_error_is_waited_out(self, slept):
        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(200, json={
                    "errors": [{"type": "RATE_LIMITED", "message": "slow down"}]})
            return httpx.Response(200, json={"data": {"ok": True}})

        gh = make_client(handler, slept)
        assert gh.graphql("query { x }")["ok"] is True
        assert len(calls) == 2

    def test_not_found_lets_the_caller_skip_a_repo(self, slept):
        gh = make_client(
            lambda r: httpx.Response(200, json={
                "errors": [{"type": "NOT_FOUND", "message": "Could not resolve to a Repository"}]}),
            slept,
        )
        with pytest.raises(NotFoundError):
            gh.graphql("query { x }")

    def test_other_errors_raise_graphql_error(self, slept):
        gh = make_client(
            lambda r: httpx.Response(200, json={
                "errors": [{"type": "FORBIDDEN", "message": "Resource not accessible"}]}),
            slept,
        )
        with pytest.raises(GraphQLError) as exc:
            gh.graphql("query { x }")
        assert "Resource not accessible" in str(exc.value)

    def test_http_200_with_errors_is_not_mistaken_for_success(self, slept):
        """GraphQL reports failure with a 200, so status alone means nothing."""
        gh = make_client(
            lambda r: httpx.Response(200, json={"errors": [{"message": "bad query"}]}),
            slept,
        )
        with pytest.raises(GraphQLError):
            gh.graphql("query { x }")

    def test_partial_data_is_returned_when_explicitly_allowed(self, slept):
        gh = make_client(
            lambda r: httpx.Response(200, json={
                "data": {"repository": {"name": "big"}},
                "errors": [{"type": "SERVICE_UNAVAILABLE", "message": "timed out"}]}),
            slept,
        )
        assert gh.graphql("query { x }", partial_ok=True)["repository"]["name"] == "big"


# --------------------------------------------------------------------------
# Bookkeeping
# --------------------------------------------------------------------------

def test_stats_are_reported_for_the_run_summary(slept):
    gh = make_client(lambda r: ok({"ok": True}, etag='W/"a"'), slept)
    gh.get("/x")
    stats = gh.stats.as_dict()
    assert stats["requests"] == 1
    assert set(stats) >= {"requests", "cache_hits", "retries", "rate_limit_sleeps",
                          "graphql_queries", "graphql_cost", "errors"}


def test_token_is_sent_as_a_bearer_header(slept):
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["version"] = request.headers.get("x-github-api-version")
        return ok({})

    gh = make_client(handler, slept)
    gh.get("/x")
    assert seen["auth"] == "Bearer test-token"
    assert seen["version"] == config.GITHUB_API_VERSION

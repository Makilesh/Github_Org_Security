"""A refused access listing must never look like 'nobody has access'.

This is the failure mode that matters most in a security report: an empty
result rendered confidently. These tests were written after a live token with
`Administration: read` missing produced exactly that, silently.
"""

from __future__ import annotations

import httpx
import pytest

import config
from client import GitHubClient
from contrib import AccessSnapshot, fetch_collaborators
from scan import decide_repo

REPO = {
    "id": 1, "name": "api", "full_name": "acme/api", "owner": {"login": "acme"},
    "private": True, "archived": False, "fork": False,
    "created_at": "2020-01-01T00:00:00Z", "pushed_at": "2026-08-01T00:00:00Z",
}


def make_client(handler):
    return GitHubClient(
        "t", cache=None, api_url="https://api.github.com",
        transport=httpx.MockTransport(handler), sleep_fn=lambda s: None,
    )


def collaborator(login, permission="write", **kw):
    return {"login": login, "id": 1, "type": "User", "role_name": permission, **kw}


class TestRefusedListings:
    def test_403_is_reported_not_swallowed(self):
        gh = make_client(lambda r: httpx.Response(
            403, json={"message": "Resource not accessible by personal access token"},
            headers={"x-ratelimit-remaining": "4000"},
        ))
        access = fetch_collaborators(gh, decide_repo(REPO))

        assert isinstance(access, AccessSnapshot)
        assert len(access) == 0
        assert not access.complete, "an empty result must not be reported as complete"
        assert len(access.errors) == 3, "each affiliation reports its own refusal"
        assert "Administration: read" in access.errors[0]

    def test_genuinely_empty_repo_is_complete(self):
        """Zero collaborators and zero errors is a real, trustworthy answer."""
        gh = make_client(lambda r: httpx.Response(
            200, json=[], headers={"x-ratelimit-remaining": "4000"}))
        access = fetch_collaborators(gh, decide_repo(REPO))

        assert len(access) == 0
        assert access.complete
        assert access.errors == []

    def test_partial_refusal_keeps_what_it_read(self):
        """If only `outside` is refused, the direct grants still count."""
        def handler(request):
            if request.url.params.get("affiliation") == "outside":
                return httpx.Response(403, json={"message": "Resource not accessible"},
                                      headers={"x-ratelimit-remaining": "4000"})
            return httpx.Response(200, json=[collaborator("ana", "admin")],
                                  headers={"x-ratelimit-remaining": "4000"})

        access = fetch_collaborators(make_client(handler), decide_repo(REPO))
        assert "ana" in access.entries
        assert access.entries["ana"].direct == "admin"
        assert not access.complete
        assert len(access.errors) == 1

    def test_successful_listing_separates_the_three_access_kinds(self):
        def handler(request):
            affiliation = request.url.params.get("affiliation")
            if affiliation == "direct":
                return httpx.Response(200, json=[collaborator("ana", "admin")],
                                      headers={"x-ratelimit-remaining": "4000"})
            if affiliation == "outside":
                return httpx.Response(200, json=[collaborator("vendor", "write")],
                                      headers={"x-ratelimit-remaining": "4000"})
            return httpx.Response(
                200,
                json=[collaborator("ana", "admin"), collaborator("maya", "write")],
                headers={"x-ratelimit-remaining": "4000"},
            )

        access = fetch_collaborators(make_client(handler), decide_repo(REPO))
        assert access.complete
        assert access.entries["ana"].is_direct and not access.entries["ana"].is_team
        assert access.entries["vendor"].is_outside
        # In `all` but not in `direct` -> team-inherited.
        assert access.entries["maya"].is_team and not access.entries["maya"].is_direct

    def test_pagination_is_followed_across_pages(self):
        def handler(request):
            if request.url.params.get("affiliation") != "direct":
                return httpx.Response(200, json=[], headers={"x-ratelimit-remaining": "4000"})
            if request.url.params.get("page") == "2":
                return httpx.Response(200, json=[collaborator("second")],
                                      headers={"x-ratelimit-remaining": "4000"})
            return httpx.Response(
                200, json=[collaborator("first")],
                headers={"x-ratelimit-remaining": "4000",
                         "link": '<https://api.github.com/repos/acme/api/collaborators'
                                 '?affiliation=direct&page=2>; rel="next"'},
            )

        access = fetch_collaborators(make_client(handler), decide_repo(REPO))
        assert set(access.entries) == {"first", "second"}
        assert access.complete


class TestDashboardSurfacesTheGap:
    def test_gap_reaches_the_report_context(self, tmp_path):
        """The warning banner is driven by an exclusion row, so it must be written."""
        import report
        from db import Database

        with Database(tmp_path / "t.sqlite3") as db:
            run_id = db.start_run("acme", config.summary())
            db.upsert_repo(run_id, REPO)
            db.set_repo_scan_status(1, "scanned")
            db.upsert_exclusion(
                run_id, 1, "*", config.ExclusionReason.ACCESS_UNREADABLE,
                detail="'direct' collaborator listing refused (HTTP 403).",
            )
            context = report.build_context(db, run_id)

        assert len(context["data_gaps"]) == 1
        assert context["data_gaps"][0]["repo"] == "acme/api"
        assert "403" in context["data_gaps"][0]["detail"]

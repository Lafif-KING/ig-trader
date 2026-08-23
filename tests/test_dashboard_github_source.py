from __future__ import annotations

import httpx
import respx

from dashboard.sources.github import GITHUB_API_BASE, fetch_github_status


def _route(method: str, path: str) -> str:
    return f"{GITHUB_API_BASE}{path}"


@respx.mock
def test_github_source_returns_safe_latest_workflow_metadata() -> None:
    respx.get(_route("GET", "/commits/main")).respond(
        json={"sha": "a" * 40, "commit": {"committer": {"date": "2026-08-23T10:00:00Z"}}}
    )
    respx.get(_route("GET", "/pulls"), params={"state": "open", "per_page": "20"}).respond(
        json=[
            {
                "number": 7,
                "title": "Public status",
                "state": "open",
                "html_url": "https://example.test/pr/7",
            }
        ]
    )
    respx.get(
        _route("GET", "/pulls"),
        params={"state": "closed", "sort": "updated", "direction": "desc", "per_page": "20"},
    ).respond(
        json=[
            {
                "number": 6,
                "title": "Merged status",
                "state": "closed",
                "merged_at": "2026-08-22T10:00:00Z",
                "html_url": "https://example.test/pr/6",
            }
        ]
    )
    respx.get(_route("GET", "/actions/runs"), params={"branch": "main", "per_page": "1"}).respond(
        json={
            "workflow_runs": [
                {
                    "id": 99,
                    "name": "CI",
                    "run_number": 12,
                    "status": "completed",
                    "conclusion": "failure",
                    "head_sha": "b" * 40,
                    "head_branch": "main",
                    "pull_requests": [{"number": 7}],
                    "html_url": "https://example.test/actions/99",
                    "run_started_at": "2026-08-23T10:00:00Z",
                    "updated_at": "2026-08-23T10:10:00Z",
                }
            ]
        }
    )
    respx.get(_route("GET", "/actions/runs/99/jobs"), params={"per_page": "100"}).respond(
        json={
            "jobs": [
                {
                    "steps": [
                        {"name": "Install", "conclusion": "success"},
                        {"name": "Dashboard tests", "conclusion": "failure"},
                        {"name": "Later", "conclusion": "skipped"},
                    ]
                }
            ]
        }
    )

    snapshot = fetch_github_status()

    assert snapshot.available is True
    assert snapshot.main_sha == "a" * 40
    assert snapshot.open_pull_requests[0].number == 7
    assert snapshot.merged_pull_requests[0].number == 6
    assert snapshot.latest_workflow is not None
    assert snapshot.latest_workflow.display_result == "FAIL"
    assert snapshot.latest_workflow.first_failed_step == "Dashboard tests"
    assert snapshot.latest_workflow.passed_steps == 1
    assert snapshot.latest_workflow.failed_steps == 1
    assert snapshot.latest_workflow.skipped_steps == 1
    assert snapshot.latest_workflow.pull_request == 7


@respx.mock
def test_github_source_degrades_without_exposing_response_details() -> None:
    respx.get(_route("GET", "/commits/main")).respond(status_code=503, text="private diagnostic")

    snapshot = fetch_github_status()

    assert snapshot.available is False
    assert snapshot.main_sha is None


def test_github_source_only_sends_get_requests(monkeypatch) -> None:
    methods: list[str] = []
    original_get = httpx.Client.get

    def recording_get(self, *args, **kwargs):
        methods.append("GET")
        return original_get(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "get", recording_get)
    with respx.mock:
        respx.get(_route("GET", "/commits/main")).respond(status_code=503)
        fetch_github_status()

    assert methods == ["GET"]

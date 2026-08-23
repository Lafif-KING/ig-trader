from __future__ import annotations

import httpx
import respx

from dashboard.sources.github import (
    ANONYMOUS_CACHE_TTL_SECONDS,
    GITHUB_API_BASE,
    TOKEN_CACHE_TTL_SECONDS,
    fetch_github_status,
    github_cache_ttl_seconds,
)


def _url(path: str) -> str:
    return f"{GITHUB_API_BASE}{path}"


def _commit() -> dict[str, object]:
    return {"sha": "a" * 40, "commit": {"committer": {"date": "2026-08-23T10:00:00Z"}}}


def _run(
    *,
    run_id: int,
    status: str,
    conclusion: str | None,
    branch: str,
    head_sha: str,
    pull_request: int | None = None,
    started_at: str = "2026-08-23T10:00:00Z",
) -> dict[str, object]:
    return {
        "id": run_id,
        "name": "CI",
        "run_number": run_id,
        "status": status,
        "conclusion": conclusion,
        "head_sha": head_sha,
        "head_branch": branch,
        "pull_requests": [{"number": pull_request}] if pull_request is not None else [],
        "html_url": f"https://example.test/actions/{run_id}",
        "run_started_at": started_at,
        "updated_at": started_at,
    }


def _respond_common(*, pulls: list[dict[str, object]], runs: list[dict[str, object]]) -> None:
    respx.get(_url("/commits/main")).respond(json=_commit())
    respx.get(_url("/pulls"), params={"state": "all", "per_page": 100}).respond(json=pulls)
    respx.get(_url("/actions/runs"), params={"per_page": 50}).respond(json={"workflow_runs": runs})


def _respond_jobs(run_id: int) -> None:
    respx.get(_url(f"/actions/runs/{run_id}/jobs"), params={"per_page": 100}).respond(
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


@respx.mock
def test_active_failed_pr_workflow_overrides_green_main() -> None:
    open_pr_sha = "b" * 40
    _respond_common(
        pulls=[
            {
                "number": 7,
                "title": "Public status",
                "state": "open",
                "head": {"sha": open_pr_sha},
                "html_url": "https://example.test/pr/7",
            },
            {
                "number": 6,
                "title": "Merged status",
                "state": "closed",
                "merged_at": "2026-08-22T10:00:00Z",
                "head": {"sha": "c" * 40},
                "html_url": "https://example.test/pr/6",
            },
        ],
        runs=[
            _run(
                run_id=99,
                status="completed",
                conclusion="success",
                branch="main",
                head_sha="a" * 40,
                started_at="2026-08-23T11:00:00Z",
            ),
            _run(
                run_id=100,
                status="completed",
                conclusion="failure",
                branch="codex/dashboard",
                head_sha=open_pr_sha,
                pull_request=7,
                started_at="2026-08-23T12:00:00Z",
            ),
        ],
    )
    _respond_jobs(100)

    snapshot = fetch_github_status()

    assert snapshot.available is True
    assert snapshot.main_sha == "a" * 40
    assert snapshot.open_pull_requests[0].head_sha == open_pr_sha
    assert snapshot.merged_pull_requests[0].number == 6
    assert snapshot.workflow_context == "ACTIVE PR #7"
    assert snapshot.latest_workflow is not None
    assert snapshot.latest_workflow.display_result == "FAIL"
    assert snapshot.latest_workflow.first_failed_step == "Dashboard tests"
    assert snapshot.latest_workflow.passed_steps == 1
    assert snapshot.latest_workflow.failed_steps == 1
    assert snapshot.latest_workflow.skipped_steps == 1
    assert snapshot.latest_workflow.pull_request == 7


@respx.mock
def test_active_running_pr_workflow_overrides_green_main() -> None:
    open_pr_sha = "b" * 40
    _respond_common(
        pulls=[
            {
                "number": 7,
                "title": "Public status",
                "state": "open",
                "head": {"sha": open_pr_sha},
                "html_url": "https://example.test/pr/7",
            }
        ],
        runs=[
            _run(
                run_id=99,
                status="completed",
                conclusion="success",
                branch="main",
                head_sha="a" * 40,
            ),
            _run(
                run_id=100,
                status="in_progress",
                conclusion=None,
                branch="codex/dashboard",
                head_sha=open_pr_sha,
                pull_request=7,
                started_at="2026-08-23T13:00:00Z",
            ),
        ],
    )
    _respond_jobs(100)

    snapshot = fetch_github_status()

    assert snapshot.workflow_context == "ACTIVE PR #7"
    assert snapshot.latest_workflow is not None
    assert snapshot.latest_workflow.display_result == "IN PROGRESS"


@respx.mock
def test_no_open_pr_falls_back_to_latest_main_workflow() -> None:
    _respond_common(
        pulls=[
            {
                "number": 6,
                "title": "Merged status",
                "state": "closed",
                "merged_at": "2026-08-22T10:00:00Z",
                "head": {"sha": "c" * 40},
                "html_url": "https://example.test/pr/6",
            }
        ],
        runs=[
            _run(
                run_id=99,
                status="completed",
                conclusion="success",
                branch="main",
                head_sha="a" * 40,
            )
        ],
    )
    _respond_jobs(99)

    snapshot = fetch_github_status()

    assert snapshot.workflow_context == "MAIN"
    assert snapshot.latest_workflow is not None
    assert snapshot.latest_workflow.display_result == "PASS"
    assert snapshot.latest_workflow.pull_request is None


@respx.mock
def test_github_source_degrades_without_exposing_response_details() -> None:
    respx.get(_url("/commits/main")).respond(status_code=503, text="private diagnostic")

    snapshot = fetch_github_status()

    assert snapshot.available is False
    assert snapshot.main_sha is None


def test_cache_policy_uses_longer_ttl_without_token() -> None:
    assert github_cache_ttl_seconds(has_token=True) == TOKEN_CACHE_TTL_SECONDS == 60
    assert github_cache_ttl_seconds(has_token=False) == ANONYMOUS_CACHE_TTL_SECONDS == 300


def test_github_source_only_sends_get_requests(monkeypatch) -> None:
    methods: list[str] = []
    original_get = httpx.Client.get

    def recording_get(self, *args, **kwargs):
        methods.append("GET")
        return original_get(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "get", recording_get)
    with respx.mock:
        respx.get(_url("/commits/main")).respond(status_code=503)
        fetch_github_status()

    assert methods == ["GET"]

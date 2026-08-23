"""Public, read-only GitHub source with no credentials required."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import httpx

from dashboard.models import GitHubStatus, PullRequest, WorkflowRun

GITHUB_API_BASE = "https://api.github.com/repos/Lafif-KING/ig-trader"
REQUEST_TIMEOUT_SECONDS = 8.0


def _headers() -> dict[str, str]:
    """Construct headers without exposing or storing an optional credential."""

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ig-trader-control-center",
    }
    github_token = os.environ.get("GITHUB_TOKEN")
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    return headers


def _safe_text(value: Any, *, limit: int = 180) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized[:limit] or None


def _pull_requests(payload: Any) -> tuple[PullRequest, ...]:
    if not isinstance(payload, list):
        return ()
    result: list[PullRequest] = []
    for item in payload:
        if not isinstance(item, Mapping) or not isinstance(item.get("number"), int):
            continue
        result.append(
            PullRequest(
                number=item["number"],
                title=_safe_text(item.get("title")) or "Untitled pull request",
                state=(_safe_text(item.get("state")) or "unknown").upper(),
                url=_safe_text(item.get("html_url")) or GITHUB_API_BASE,
                merged_at=_safe_text(item.get("merged_at")),
            )
        )
    return tuple(result)


def _workflow_run(payload: Any, client: httpx.Client) -> WorkflowRun | None:
    if not isinstance(payload, Mapping):
        return None
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list) or not runs or not isinstance(runs[0], Mapping):
        return None
    run = runs[0]
    jobs_response = client.get(f"/actions/runs/{run.get('id')}/jobs", params={"per_page": 100})
    jobs_response.raise_for_status()
    jobs_payload = jobs_response.json()
    jobs = jobs_payload.get("jobs", []) if isinstance(jobs_payload, Mapping) else []
    passed_steps = failed_steps = skipped_steps = 0
    first_failed_step: str | None = None
    if isinstance(jobs, list):
        for job in jobs:
            steps = job.get("steps", []) if isinstance(job, Mapping) else []
            if not isinstance(steps, list):
                continue
            for step in steps:
                if not isinstance(step, Mapping):
                    continue
                conclusion = step.get("conclusion")
                if conclusion == "success":
                    passed_steps += 1
                elif conclusion in {"skipped", "neutral"}:
                    skipped_steps += 1
                elif conclusion in {"failure", "cancelled", "timed_out", "action_required"}:
                    failed_steps += 1
                    if first_failed_step is None:
                        first_failed_step = _safe_text(step.get("name")) or "Unnamed failed step"
    conclusion = _safe_text(run.get("conclusion"))
    associated_pull_requests = run.get("pull_requests")
    pull_request = (
        associated_pull_requests[0].get("number")
        if isinstance(associated_pull_requests, list)
        and associated_pull_requests
        and isinstance(associated_pull_requests[0], Mapping)
        and isinstance(associated_pull_requests[0].get("number"), int)
        else None
    )
    return WorkflowRun(
        name=_safe_text(run.get("name")) or "GitHub Actions",
        number=run.get("run_number") if isinstance(run.get("run_number"), int) else 0,
        status=_safe_text(run.get("status")) or "unknown",
        conclusion=conclusion,
        head_sha=_safe_text(run.get("head_sha")) or "unknown",
        branch=_safe_text(run.get("head_branch")) or "main",
        url=_safe_text(run.get("html_url")) or GITHUB_API_BASE,
        started_at=_safe_text(run.get("run_started_at")),
        completed_at=_safe_text(run.get("updated_at")),
        passed_steps=passed_steps,
        failed_steps=failed_steps,
        skipped_steps=skipped_steps,
        pull_request=pull_request,
        first_failed_step=first_failed_step,
        failure_summary=(
            f"GitHub reported a failed step: {first_failed_step}." if first_failed_step else None
        ),
    )


def fetch_github_status() -> GitHubStatus:
    """Fetch only public metadata with HTTP GET requests.

    Failure deliberately degrades to small unavailable status. Neither response
    body nor request headers are logged, and the static roadmap stays available.
    """

    try:
        with httpx.Client(
            base_url=GITHUB_API_BASE,
            headers=_headers(),
            timeout=REQUEST_TIMEOUT_SECONDS,
            follow_redirects=False,
        ) as client:
            commit_response = client.get("/commits/main")
            commit_response.raise_for_status()
            commit = commit_response.json()
            open_prs_response = client.get("/pulls", params={"state": "open", "per_page": 20})
            open_prs_response.raise_for_status()
            closed_prs_response = client.get(
                "/pulls",
                params={"state": "closed", "sort": "updated", "direction": "desc", "per_page": 20},
            )
            closed_prs_response.raise_for_status()
            workflows_response = client.get(
                "/actions/runs", params={"branch": "main", "per_page": 1}
            )
            workflows_response.raise_for_status()
            if not isinstance(commit, Mapping):
                raise ValueError("Unexpected commit payload")
            merged = tuple(pr for pr in _pull_requests(closed_prs_response.json()) if pr.merged_at)
            commit_data = commit.get("commit") if isinstance(commit.get("commit"), Mapping) else {}
            committer = commit_data.get("committer") if isinstance(commit_data, Mapping) else {}
            return GitHubStatus(
                available=True,
                main_sha=_safe_text(commit.get("sha")),
                main_updated_at=_safe_text(
                    committer.get("date") if isinstance(committer, Mapping) else None
                ),
                open_pull_requests=_pull_requests(open_prs_response.json()),
                merged_pull_requests=merged,
                latest_workflow=_workflow_run(workflows_response.json(), client),
            )
    except (httpx.HTTPError, OSError, TypeError, ValueError):
        return GitHubStatus(available=False)

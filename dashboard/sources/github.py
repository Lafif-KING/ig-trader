"""Public, read-only GitHub source with PR-aware workflow selection."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import httpx

from dashboard.models import GitHubStatus, PullRequest, WorkflowRun

GITHUB_API_BASE = "https://api.github.com/repos/Lafif-KING/ig-trader"
REQUEST_TIMEOUT_SECONDS = 8.0
TOKEN_CACHE_TTL_SECONDS = 60
ANONYMOUS_CACHE_TTL_SECONDS = 300


def github_cache_ttl_seconds(*, has_token: bool) -> int:
    """Use fewer anonymous API refreshes without changing the display refresh interval."""

    return TOKEN_CACHE_TTL_SECONDS if has_token else ANONYMOUS_CACHE_TTL_SECONDS


def has_github_token() -> bool:
    """Return token presence only; the value is never exposed to the dashboard."""

    return bool(os.environ.get("GITHUB_TOKEN"))


def _headers() -> dict[str, str]:
    """Construct request headers without exposing or storing an optional credential."""

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
        head = item.get("head")
        head_sha = _safe_text(head.get("sha")) if isinstance(head, Mapping) else None
        result.append(
            PullRequest(
                number=item["number"],
                title=_safe_text(item.get("title")) or "Untitled pull request",
                state=(_safe_text(item.get("state")) or "unknown").upper(),
                url=_safe_text(item.get("html_url")) or GITHUB_API_BASE,
                head_sha=head_sha,
                merged_at=_safe_text(item.get("merged_at")),
            )
        )
    return tuple(result)


def _workflow_timestamp(run: Mapping[str, Any]) -> str:
    """ISO timestamps sort safely as strings and need no date parsing for selection."""

    return (
        _safe_text(run.get("run_started_at"), limit=40)
        or _safe_text(run.get("created_at"), limit=40)
        or _safe_text(run.get("updated_at"), limit=40)
        or ""
    )


def _run_pull_request_numbers(run: Mapping[str, Any]) -> set[int]:
    pull_requests = run.get("pull_requests")
    if not isinstance(pull_requests, list):
        return set()
    return {
        item["number"]
        for item in pull_requests
        if isinstance(item, Mapping) and isinstance(item.get("number"), int)
    }


def _select_workflow(
    payload: Any, open_pull_requests: tuple[PullRequest, ...]
) -> tuple[Mapping[str, Any] | None, str, PullRequest | None]:
    """Prefer the newest active PR workflow; otherwise use the newest main run."""

    runs = payload.get("workflow_runs") if isinstance(payload, Mapping) else None
    if not isinstance(runs, list):
        return None, "MAIN", None
    mapping_runs = tuple(run for run in runs if isinstance(run, Mapping))
    active_matches: list[tuple[Mapping[str, Any], PullRequest]] = []
    for run in mapping_runs:
        run_pr_numbers = _run_pull_request_numbers(run)
        run_head_sha = _safe_text(run.get("head_sha"), limit=80)
        for pull_request in open_pull_requests:
            if pull_request.number in run_pr_numbers or (
                pull_request.head_sha is not None and pull_request.head_sha == run_head_sha
            ):
                active_matches.append((run, pull_request))
                break
    if active_matches:
        run, pull_request = max(active_matches, key=lambda match: _workflow_timestamp(match[0]))
        return run, f"ACTIVE PR #{pull_request.number}", pull_request
    main_runs = tuple(
        run for run in mapping_runs if _safe_text(run.get("head_branch"), limit=80) == "main"
    )
    if not main_runs:
        return None, "MAIN", None
    return max(main_runs, key=_workflow_timestamp), "MAIN", None


def _workflow_run(
    run: Mapping[str, Any], client: httpx.Client, pull_request: PullRequest | None
) -> WorkflowRun:
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
    workflow_pull_requests = _run_pull_request_numbers(run)
    workflow_pr = pull_request.number if pull_request else next(iter(workflow_pull_requests), None)
    conclusion = _safe_text(run.get("conclusion"))
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
        pull_request=workflow_pr,
        first_failed_step=first_failed_step,
        failure_summary=(
            f"GitHub reported a failed step: {first_failed_step}." if first_failed_step else None
        ),
    )


def fetch_github_status() -> GitHubStatus:
    """Fetch public status through three GETs plus jobs for the selected workflow only."""

    try:
        with httpx.Client(
            base_url=GITHUB_API_BASE,
            headers=_headers(),
            timeout=REQUEST_TIMEOUT_SECONDS,
            follow_redirects=False,
        ) as client:
            commit_response = client.get("/commits/main")
            commit_response.raise_for_status()
            pull_requests_response = client.get("/pulls", params={"state": "all", "per_page": 100})
            pull_requests_response.raise_for_status()
            workflows_response = client.get("/actions/runs", params={"per_page": 50})
            workflows_response.raise_for_status()
            commit = commit_response.json()
            if not isinstance(commit, Mapping):
                raise ValueError("Unexpected commit payload")
            pull_requests = _pull_requests(pull_requests_response.json())
            open_pull_requests = tuple(pr for pr in pull_requests if pr.state == "OPEN")
            selected_run, workflow_context, selected_pr = _select_workflow(
                workflows_response.json(), open_pull_requests
            )
            commit_data = commit.get("commit") if isinstance(commit.get("commit"), Mapping) else {}
            committer = commit_data.get("committer") if isinstance(commit_data, Mapping) else {}
            return GitHubStatus(
                available=True,
                main_sha=_safe_text(commit.get("sha")),
                main_updated_at=_safe_text(
                    committer.get("date") if isinstance(committer, Mapping) else None
                ),
                open_pull_requests=open_pull_requests,
                merged_pull_requests=tuple(pr for pr in pull_requests if pr.merged_at),
                latest_workflow=(
                    _workflow_run(selected_run, client, selected_pr)
                    if selected_run is not None
                    else None
                ),
                workflow_context=workflow_context,
            )
    except (httpx.HTTPError, OSError, TypeError, ValueError):
        return GitHubStatus(available=False)

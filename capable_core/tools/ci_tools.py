"""CI/CD Monitoring Tools for the CapeAble Core Foundry multi-agent system.

Provides specialized tools for monitoring GitHub Actions and other CI pipelines.
"""

import time
from dataclasses import dataclass
from typing import Any

import structlog

from capable_core.tools.github_tools import CIStatus, _get_client, get_ci_failure_logs, get_ci_status


log = structlog.get_logger()


@dataclass
class CIRunResult:
    """Structured CI run result."""

    status: CIStatus
    duration_seconds: int
    failed_jobs: list[str]
    error_logs: str
    coverage_percent: float | None = None
    test_results: dict[str, Any] | None = None


class CIMonitor:
    """CI/CD Pipeline Monitor for tracking build and test status.

    Designed to be used by the Developer Agent for the Step 7-8 loop.
    """

    def __init__(self, repo_name: str, timeout: int = 600, poll_interval: int = 30):
        """Initialize the CI monitor.

        Args:
            repo_name: Repository in "owner/repo" format.
            timeout: Max seconds to wait for CI completion.
            poll_interval: Seconds between status checks.
        """
        self.repo_name = repo_name
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.client = _get_client()

    def monitor_pr(self, pr_number: int) -> CIRunResult:
        """
        Monitors CI for a PR until completion or timeout.

        Args:
            pr_number: The PR number to monitor.

        Returns:
            CIRunResult with status and details.
        """
        repo = self.client.get_repo(self.repo_name)
        pr = repo.get_pull(pr_number)
        commit_sha = pr.head.sha

        start_time = time.time()

        while time.time() - start_time < self.timeout:
            status = get_ci_status(self.repo_name, commit_sha)

            if status in [CIStatus.SUCCESS.value, CIStatus.FAILURE.value, CIStatus.CANCELLED.value]:
                duration = int(time.time() - start_time)

                if status == CIStatus.FAILURE.value:
                    logs = get_ci_failure_logs(self.repo_name, commit_sha)
                    failed_jobs = self._extract_failed_jobs(commit_sha)

                    return CIRunResult(status=CIStatus.FAILURE, duration_seconds=duration, failed_jobs=failed_jobs, error_logs=logs)

                return CIRunResult(status=CIStatus.SUCCESS, duration_seconds=duration, failed_jobs=[], error_logs="")

            time.sleep(self.poll_interval)

        return CIRunResult(status=CIStatus.UNKNOWN, duration_seconds=self.timeout, failed_jobs=[], error_logs="CI timed out")

    def _extract_failed_jobs(self, commit_sha: str) -> list[str]:
        """Extract list of failed job names."""
        repo = self.client.get_repo(self.repo_name)
        failed = []

        try:
            runs = repo.get_workflow_runs(head_sha=commit_sha)
            for run in runs:
                if run.conclusion == "failure":
                    for job in run.jobs():
                        if job.conclusion == "failure":
                            failed.append(f"{run.name}/{job.name}")
        except Exception:
            pass

        return failed


def monitor_ci_for_pr(repo_name: str, pr_number: int, timeout_seconds: int = 600, poll_interval: int = 15) -> str:
    """
    Tool function: Monitors CI pipeline for a PR until completion.

    This tool waits for all CI checks to complete and returns a clear PASS/FAIL status.
    Use this after creating or updating a PR to verify the build passes.

    Args:
        repo_name: Repository in "owner/repo" format.
        pr_number: The PR number to monitor.
        timeout_seconds: Maximum wait time (default: 10 minutes).
        poll_interval: Seconds between status checks (default: 15).

    Returns:
        CI result with clear CI_STATUS indicator (PASSED/FAILED/TIMEOUT).
    """
    from capable_core.tools.github_tools import CIStatus, _get_client, get_ci_failure_logs, get_ci_status

    client = _get_client()
    repo = client.get_repo(repo_name)
    pr = repo.get_pull(pr_number)
    commit_sha = pr.head.sha

    start_time = time.time()
    check_count = 0

    log.info("ci_monitor_started", repo=repo_name, pr=pr_number, sha=commit_sha[:8])

    # Terminal states that indicate CI is done
    terminal_states = {CIStatus.SUCCESS.value, CIStatus.FAILURE.value, CIStatus.CANCELLED.value}

    while True:
        elapsed = time.time() - start_time
        check_count += 1

        # Get current status with retry on connection errors
        try:
            status = get_ci_status(repo_name, commit_sha)
            log.info("ci_status_check", check=check_count, status=status, elapsed=f"{elapsed:.0f}s")
        except Exception as e:
            # Handle connection errors gracefully - just log and retry
            log.warning("ci_status_check_failed", check=check_count, error=str(e), elapsed=f"{elapsed:.0f}s")
            status = None  # Will retry on next iteration

        # Check if CI completed (only if we got a valid status)
        if status and status in terminal_states:
            duration = int(elapsed)

            if status == CIStatus.SUCCESS.value:
                return f"""
## CI Pipeline Result ✅

**CI_STATUS: PASSED**
**PR:** #{pr_number}
**Repository:** {repo_name}
**Duration:** {duration}s

### All Checks Passed ✅

---
**CI_STATUS: PASSED**
**REQUIRED_ACTION:** CI is green! Report back to Tech Lead with the successful PR.

DEVELOPMENT_COMPLETE:
- PR: #{pr_number}
- Repository: {repo_name}
- CI: PASSED
"""

            elif status == CIStatus.FAILURE.value:
                logs = get_ci_failure_logs(repo_name, commit_sha)
                failed_jobs = _extract_failed_jobs_for_sha(repo_name, commit_sha)

                return f"""
## CI Pipeline Result ❌

**CI_STATUS: FAILED**
**PR:** #{pr_number}
**Repository:** {repo_name}
**Duration:** {duration}s

### Failed Jobs
{chr(10).join(f"- {job}" for job in failed_jobs) or "Unknown"}

### Error Logs
```
{logs[:2000]}
```

---
**CI_STATUS: FAILED**
**REQUIRED_ACTION:** Fix the errors above and push a new commit to the PR branch.
**DO NOT** proceed to QA verification until CI passes.
"""

            else:  # CANCELLED
                return f"""
## CI Pipeline Result ⚠️

**CI_STATUS: CANCELLED**
**PR:** #{pr_number}
**Duration:** {duration}s

CI was cancelled. Check https://github.com/{repo_name}/pull/{pr_number}/checks
"""

        # Check timeout
        if elapsed >= timeout_seconds:
            return f"""
## CI Pipeline Result ⏱️

**CI_STATUS: TIMEOUT**
**PR:** #{pr_number}
**Repository:** {repo_name}
**Last Status:** {status}
**Waited:** {int(elapsed)}s

### CI Timeout ⏱️

---
**CI_STATUS: TIMEOUT**
**REQUIRED_ACTION:** CI did not complete within {timeout_seconds} seconds.
Check GitHub Actions manually at: https://github.com/{repo_name}/pull/{pr_number}/checks

You can either:
1. Wait longer and call `monitor_ci_for_pr` again with a higher timeout
2. Check the GitHub Actions page directly for status
"""

        # Wait before next check (shorter interval for faster response)
        time.sleep(poll_interval)


def _extract_failed_jobs_for_sha(repo_name: str, commit_sha: str) -> list[str]:
    """Extract list of failed job names for a commit."""
    client = _get_client()
    repo = client.get_repo(repo_name)
    failed = []

    try:
        runs = repo.get_workflow_runs(head_sha=commit_sha)
        for run in runs:
            if run.conclusion == "failure":
                for job in run.jobs():
                    if job.conclusion == "failure":
                        failed.append(f"{run.name}/{job.name}")
    except Exception as e:
        log.warning("failed_to_extract_failed_jobs", repo=repo_name, sha=commit_sha, error=str(e))

    return failed


def get_workflow_summary(repo_name: str) -> str:
    """
    Gets a summary of recent workflow runs for a repository.

    Args:
        repo_name: Repository in "owner/repo" format.

    Returns:
        Summary of recent CI runs.
    """
    client = _get_client()
    repo = client.get_repo(repo_name)

    try:
        runs = repo.get_workflow_runs()

        summary = "## Recent CI Runs\n\n"

        for run in list(runs)[:10]:
            status_icon = "✅" if run.conclusion == "success" else "❌" if run.conclusion == "failure" else "🔄"
            summary += f"{status_icon} **{run.name}** (#{run.run_number}): {run.conclusion or run.status}\n"
            summary += f"   Branch: {run.head_branch} | Commit: {run.head_sha[:7]}\n\n"

        return summary
    except Exception as e:
        return f"Error fetching workflows: {e!s}"


def trigger_workflow(repo_name: str, workflow_id: str, ref: str = "main") -> str:
    """
    Manually triggers a workflow dispatch event.

    Args:
        repo_name: Repository in "owner/repo" format.
        workflow_id: Workflow file name or ID.
        ref: Branch to run on (default: main).

    Returns:
        Success or error message.
    """
    client = _get_client()
    repo = client.get_repo(repo_name)

    try:
        workflow = repo.get_workflow(workflow_id)
        workflow.create_dispatch(ref)
        return f"✅ Workflow '{workflow_id}' triggered on branch '{ref}'."
    except Exception as e:
        return f"❌ Failed to trigger workflow: {e!s}"

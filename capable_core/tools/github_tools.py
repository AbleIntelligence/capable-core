"""
GitHub Tools for the CapAble-Core multi-agent system.

Provides comprehensive GitHub integration for issues, PRs, files, and CI monitoring.
"""

import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

import structlog
from github import Auth, Github, GithubException
from github.Repository import Repository


log = structlog.get_logger()


class PRStatus(Enum):
    """Pull Request status states."""

    DRAFT = "draft"
    OPEN = "open"
    MERGED = "merged"
    CLOSED = "closed"


class CIStatus(Enum):
    """CI Pipeline status states."""

    PENDING = "pending"
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass
class IssueData:
    """Structured issue data for agent consumption."""

    number: int
    title: str
    body: str
    labels: list[str]
    assignees: list[str]
    created_at: str
    priority: str = "normal"  # Derived from labels

    def to_prompt(self) -> str:
        """Format issue for LLM consumption."""
        return f"""
## Issue #{self.number}: {self.title}
**Priority:** {self.priority}
**Labels:** {", ".join(self.labels) or "None"}
**Created:** {self.created_at}

### Description
{self.body or "No description provided."}
"""


@dataclass
class PRData:
    """Structured PR data for agent consumption."""

    number: int
    url: str
    title: str
    branch: str
    base_branch: str
    status: PRStatus
    files_changed: list[str] = field(default_factory=list)
    ci_status: CIStatus | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert PR info to a serializable dictionary."""
        return {
            **asdict(self),
            "status": self.status.value,
            "ci_status": self.ci_status.value if self.ci_status else None,
        }


class GitHubClient:
    """Singleton GitHub client with connection pooling and error handling."""

    _instance: Optional["GitHubClient"] = None

    def __new__(cls) -> "GitHubClient":
        """Ensures only one instance of GitHubClient exists (singleton pattern).

        Returns:
            GitHubClient: _description_
        """
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        """Initialize the GitHub client with token authentication."""
        if self._initialized:
            return

        token = os.getenv("GITHUB_TOKEN")
        if not token:
            raise ValueError("GITHUB_TOKEN environment variable required")

        self.auth = Auth.Token(token)
        self.client = Github(auth=self.auth, per_page=100)
        self.current_user = self.client.get_user().login
        self._repo_cache: dict[str, Repository] = {}
        self._initialized = True
        log.info("github_client_initialized", user=self.current_user)

    def get_repo(self, repo_name: str) -> Repository:
        """Get repository with caching."""
        if repo_name not in self._repo_cache:
            self._repo_cache[repo_name] = self.client.get_repo(repo_name)
        return self._repo_cache[repo_name]


# Module-level client getter
def _get_client() -> GitHubClient:
    return GitHubClient()


# =============================================================================
# ISSUE TOOLS
# =============================================================================


def get_my_assigned_issues(repo_name: str, labels: list[str] | None = None) -> str:
    """
    Fetches open issues assigned to the authenticated user.

    Args:
        repo_name: Repository in "owner/repo" format.
        labels: Optional list of labels to filter by.

    Returns:
        Formatted string with issue summaries, sorted by priority.
    """
    try:
        client = _get_client()
        repo = client.get_repo(repo_name)

        issues = repo.get_issues(state="open", assignee=client.current_user, labels=labels or [])

        issue_list: list[IssueData] = []

        for issue in issues:
            if issue.pull_request:  # Skip PRs (GitHub API quirk)
                continue

            labels_list = [label.name for label in issue.labels]
            priority = "high" if any(p in labels_list for p in ["critical", "urgent", "P0", "P1"]) else "normal"

            issue_list.append(
                IssueData(
                    number=issue.number,
                    title=issue.title,
                    body=issue.body or "",
                    labels=labels_list,
                    assignees=[a.login for a in issue.assignees],
                    created_at=issue.created_at.isoformat(),
                    priority=priority,
                )
            )

        if not issue_list:
            return f"No issues assigned to {client.current_user} in {repo_name}."

        # Sort by priority (high first), then by creation date (oldest first)
        issue_list.sort(key=lambda x: (0 if x.priority == "high" else 1, x.created_at))

        report = f"## Issues Assigned to {client.current_user}\n\n"
        for issue in issue_list:
            report += issue.to_prompt() + "\n---\n"

        return report
    except GithubException as e:
        return f"Error fetching issues: {e.data.get('message', str(e)) if hasattr(e, 'data') else str(e)}"
    except Exception as e:
        log.error("get_my_assigned_issues_error", repo=repo_name, error=str(e))
        return f"Error fetching assigned issues: {e!s}"


def get_issue_content(repo_name: str, issue_number: int) -> str:
    """
    Fetches detailed content of a specific GitHub issue.

    Args:
        repo_name: Repository in "owner/repo" format.
        issue_number: The issue number.

    Returns:
        Structured issue content for agent consumption.
    """
    try:
        # Ensure issue_number is an integer (LLM might pass string)
        issue_number = int(issue_number)

        client = _get_client()
        repo = client.get_repo(repo_name)
        issue = repo.get_issue(number=issue_number)

        labels = [label.name for label in issue.labels] if issue.labels else []
        priority = "high" if any(p in labels for p in ["critical", "urgent", "P0", "P1"]) else "normal"

        data = IssueData(
            number=issue.number,
            title=issue.title,
            body=issue.body or "",
            labels=labels,
            assignees=[a.login for a in issue.assignees] if issue.assignees else [],
            created_at=issue.created_at.isoformat(),
            priority=priority,
        )

        # Also fetch comments for context
        result = data.to_prompt()

        try:
            comments = list(issue.get_comments())
            if comments:
                comments_text = ""
                for comment in comments[:5]:
                    comments_text += f"\n**{comment.user.login}** ({comment.created_at.date()}):\n{comment.body}\n"
                if comments_text:
                    result += f"\n### Discussion\n{comments_text}"
        except Exception as ce:
            log.warning("failed_to_fetch_comments", error=str(ce))

        return result

    except ValueError:
        return f"Error: Invalid issue number '{issue_number}' - must be an integer."
    except GithubException as e:
        return f"Error fetching issue #{issue_number}: {e.data.get('message', str(e)) if hasattr(e, 'data') else str(e)}"
    except Exception as e:
        log.error("get_issue_content_failed", error=str(e), issue=issue_number)
        return f"Error: {e!s}"


# =============================================================================
# FILE TOOLS
# =============================================================================


def get_file_content(repo_name: str, file_path: str, ref: str = "main") -> str:
    """
    Reads a file from the repository.

    Args:
        repo_name: Repository in "owner/repo" format.
        file_path: Path to the file within the repo.
        ref: Branch or commit SHA (default: main).

    Returns:
        File content as string.
    """
    try:
        client = _get_client()
        repo = client.get_repo(repo_name)

        contents = repo.get_contents(file_path, ref=ref)
        if isinstance(contents, list):
            return f"Error: {file_path} is a directory, not a file."
        return contents.decoded_content.decode("utf-8")
    except GithubException as e:
        return f"Error reading {file_path}: {e.data.get('message', str(e))}"
    except Exception as e:
        log.error("get_file_content_error", file_path=file_path, ref=ref, error=str(e))
        return f"Error reading {file_path}: {e!s}"


def get_directory_tree(repo_name: str, path: str = "", ref: str = "main") -> str:
    """
    Gets the directory structure of a repository path.

    Args:
        repo_name: Repository in "owner/repo" format.
        path: Directory path (empty for root).
        ref: Branch name (default: main).

    Returns:
        Tree structure as formatted string.
    """
    try:
        client = _get_client()
        repo = client.get_repo(repo_name)

        contents = repo.get_contents(path, ref=ref)
        if not isinstance(contents, list):
            return f"{path} is a file, not a directory."

        tree = f"Directory: {path or '/'}\n"

        # Sort: directories first, then files
        dirs = sorted([c for c in contents if c.type == "dir"], key=lambda x: x.path)
        files = sorted([c for c in contents if c.type == "file"], key=lambda x: x.path)

        for d in dirs:
            tree += f"  📁 {d.name}/\n"
        for f in files:
            tree += f"  📄 {f.name}\n"

        return tree
    except GithubException as e:
        return f"Error: {e.data.get('message', str(e))}"
    except Exception as e:
        log.error("get_directory_tree_error", path=path, ref=ref, error=str(e))
        return f"Error getting directory tree: {e!s}"


# =============================================================================
# BRANCH TOOLS
# =============================================================================


def create_branch_with_files(repo_name: str, branch_name: str, file_changes: dict[str, str], commit_message: str, base_branch: str = "main") -> str:
    """
    Creates a new branch AND pushes files to it in one atomic operation.

    ⚠️ ALL PARAMETERS ARE REQUIRED (except base_branch):
    1. repo_name (str): "owner/repo" format, e.g. "my-org/backend"
    2. branch_name (str): branch name, e.g. "fix-issue-123"
    3. file_changes (dict): {"filepath": "complete file content"} - MUST be actual code!
    4. commit_message (str): describe what changed, e.g. "Fix issue #123"

    EXAMPLE - Copy this format exactly:
    ```
    create_branch_with_files(
        repo_name="my-org/frontend",
        branch_name="fix-issue-42",
        file_changes={"src/utils.py": "import os\n\ndef hello():\n    return 'world'\n"},
        commit_message="Fix issue #42: Add hello function",
    )
    ```

    Args:
        repo_name: Repository in "owner/repo" format.
        branch_name: Name for the new branch.
        file_changes: Dict of {filepath: complete_file_content}. Content must be actual code, not descriptions.
        commit_message: Describes what was changed.
        base_branch: Branch to create from (default: "main").

    Returns:
        Success message with branch details, or error message.
    """
    client = _get_client()
    repo = client.get_repo(repo_name)

    # Validate file_changes is not empty
    if not file_changes:
        return "ERROR: file_changes cannot be empty. You must provide at least one file with its complete content."

    # Validate file_changes contains actual content
    for path, content in file_changes.items():
        if not content or len(content.strip()) < 10:
            return f"ERROR: File '{path}' has no content or content is too short. Provide the COMPLETE file content."

    try:
        # Step 1: Get the SHA of the base branch
        source = repo.get_branch(base_branch)
        base_sha = source.commit.sha

        # Step 2: Create the new branch
        try:
            repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=base_sha)
            log.info("branch_created", repo=repo_name, branch=branch_name, base=base_branch)
            branch_status = "created"
        except GithubException as e:
            error_msg = e.data.get("message", str(e)) if hasattr(e, "data") else str(e)
            if "Reference already exists" in error_msg:
                log.info("branch_exists", repo=repo_name, branch=branch_name)
                branch_status = "already existed"
            else:
                return f"Error creating branch: {error_msg}"

        # Step 3: Push all files to the branch
        files_updated = []
        files_created = []

        for file_path, content in file_changes.items():
            try:
                # Check if file exists
                existing = repo.get_contents(file_path, ref=branch_name)
                if isinstance(existing, list):
                    return (
                        f"ERROR: '{file_path}' is a directory in the repo (ref='{branch_name}'), "
                        "but file_changes expects file paths. Choose a file path like 'dir/file.py'."
                    )
                repo.update_file(path=file_path, message=f"{commit_message}", content=content, sha=existing.sha, branch=branch_name)
                files_updated.append(file_path)
                log.info("file_updated", path=file_path, branch=branch_name)
            except GithubException:
                # File doesn't exist, create it
                repo.create_file(path=file_path, message=f"{commit_message}", content=content, branch=branch_name)
                files_created.append(file_path)
                log.info("file_created", path=file_path, branch=branch_name)

        total_files = len(files_created) + len(files_updated)

        return f"""
SUCCESS: Branch {branch_status} and {total_files} file(s) pushed!

**Repository:** {repo_name}
**Branch:** {branch_name}
**Base:** {base_branch}

**Files Created:** {len(files_created)}
{chr(10).join(f"  - {f}" for f in files_created) if files_created else "  (none)"}

**Files Updated:** {len(files_updated)}
{chr(10).join(f"  - {f}" for f in files_updated) if files_updated else "  (none)"}

**Next Step:** Run tests on this branch using `run_tests_on_branch("{repo_name}", "{branch_name}", "pytest")`
Then create a PR using `create_pr_with_changes`.
"""
    except GithubException as e:
        error_msg = e.data.get("message", str(e)) if hasattr(e, "data") else str(e)
        return f"Error: {error_msg}"


def create_branch(repo_name: str, branch_name: str, base_branch: str = "main") -> str:
    """
    Creates a new branch from a base branch.

    Args:
        repo_name: Repository in "owner/repo" format.
        branch_name: Name for the new branch.
        base_branch: Branch to create from (default: main).

    Returns:
        Success message with branch details or error.
    """
    client = _get_client()
    repo = client.get_repo(repo_name)

    try:
        # Get the SHA of the base branch
        source = repo.get_branch(base_branch)
        base_sha = source.commit.sha

        # Create the new branch
        repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=base_sha)

        log.info("branch_created", repo=repo_name, branch=branch_name, base=base_branch)

        return f"""
SUCCESS: Branch created!

**Branch:** {branch_name}
**Base:** {base_branch}
**SHA:** {base_sha[:8]}

You can now push files to this branch using `push_files_to_branch`.
"""
    except GithubException as e:
        error_msg = e.data.get("message", str(e)) if hasattr(e, "data") else str(e)
        if "Reference already exists" in error_msg:
            return f"Branch '{branch_name}' already exists in {repo_name}. Use a different name or push directly to it."
        return f"Error creating branch: {error_msg}"


def push_files_to_branch(repo_name: str, branch_name: str, file_changes: dict[str, str], commit_message: str) -> str:
    """
    Pushes file changes to an existing branch.

    ⚠️ ALL 4 PARAMETERS ARE REQUIRED:
    1. repo_name (str): "owner/repo" format, e.g. "my-org/backend"
    2. branch_name (str): existing branch name, e.g. "fix-issue-123"
    3. file_changes (dict): {"filepath": "complete file content"} - MUST be actual code!
    4. commit_message (str): describe what changed, e.g. "Fix lint errors"

    EXAMPLE - Copy this format exactly:
    ```
    push_files_to_branch(
        repo_name="my-org/frontend",
        branch_name="fix-issue-42",
        file_changes={"src/utils.py": "import os\n\ndef hello():\n    return 'world'\n"},
        commit_message="Fix lint errors in utils.py",
    )
    ```

    Returns:
        Success message or error.
    """
    client = _get_client()
    repo = client.get_repo(repo_name)

    try:
        files_updated = []
        files_created = []

        for file_path, content in file_changes.items():
            try:
                # Check if file exists
                existing = repo.get_contents(file_path, ref=branch_name)
                if isinstance(existing, list):
                    return (
                        f"ERROR: '{file_path}' is a directory in the repo (ref='{branch_name}'), "
                        "but file_changes expects file paths. Choose a file path like 'dir/file.py'."
                    )
                repo.update_file(
                    path=file_path,
                    message=f"{commit_message} - update {file_path}",
                    content=content,
                    sha=existing.sha,
                    branch=branch_name,
                )
                files_updated.append(file_path)
                log.info("file_updated", path=file_path, branch=branch_name)
            except GithubException:
                # File doesn't exist, create it
                repo.create_file(path=file_path, message=f"{commit_message} - add {file_path}", content=content, branch=branch_name)
                files_created.append(file_path)
                log.info("file_created", path=file_path, branch=branch_name)

        return f"""
SUCCESS: Files pushed to branch '{branch_name}'!

**Created:** {len(files_created)} file(s)
{chr(10).join(f"  - {f}" for f in files_created) if files_created else "  (none)"}

**Updated:** {len(files_updated)} file(s)
{chr(10).join(f"  - {f}" for f in files_updated) if files_updated else "  (none)"}

You can now run tests on this branch using `run_tests_on_branch`.
"""
    except GithubException as e:
        error_msg = e.data.get("message", str(e)) if hasattr(e, "data") else str(e)
        return f"Error pushing files: {error_msg}"


def delete_files_from_branch(repo_name: str, branch_name: str, file_paths: list, commit_message: str) -> str:
    """
    Deletes files from an existing branch.

    Use this when you need to remove files that are no longer needed,
    such as deprecated modules, old config files, or files being replaced.

    ⚠️ ALL 4 PARAMETERS ARE REQUIRED:
    1. repo_name (str): "owner/repo" format, e.g. "my-org/backend"
    2. branch_name (str): existing branch name, e.g. "fix-issue-123"
    3. file_paths (list): list of file paths to delete, e.g. ["src/old_module.py", "config/deprecated.yaml"]
    4. commit_message (str): describe why files are being deleted

    EXAMPLE:
    ```
    delete_files_from_branch(
        repo_name="my-org/backend",
        branch_name="fix-issue-42",
        file_paths=["src/deprecated_utils.py", "tests/test_deprecated.py"],
        commit_message="Remove deprecated utils module",
    )
    ```

    Returns:
        Success message listing deleted files, or error.
    """
    client = _get_client()
    repo = client.get_repo(repo_name)

    try:
        files_deleted = []
        files_not_found = []

        for file_path in file_paths:
            try:
                # Get the file to obtain its SHA (required for deletion)
                existing = repo.get_contents(file_path, ref=branch_name)
                repo.delete_file(
                    path=file_path,
                    message=f"{commit_message} - delete {file_path}",
                    sha=existing.sha,
                    branch=branch_name,
                )
                files_deleted.append(file_path)
                log.info("file_deleted", path=file_path, branch=branch_name)
            except GithubException as e:
                if e.status == 404:
                    files_not_found.append(file_path)
                    log.warning("file_not_found_for_deletion", path=file_path, branch=branch_name)
                else:
                    raise

        result_parts = [f"SUCCESS: File deletion completed on branch '{branch_name}'!"]

        if files_deleted:
            result_parts.append(f"\n**Deleted:** {len(files_deleted)} file(s)")
            result_parts.extend(f"  - {f}" for f in files_deleted)

        if files_not_found:
            result_parts.append(f"\n**Not Found (skipped):** {len(files_not_found)} file(s)")
            result_parts.extend(f"  - {f}" for f in files_not_found)

        return "\n".join(result_parts)

    except GithubException as e:
        error_msg = e.data.get("message", str(e)) if hasattr(e, "data") else str(e)
        return f"Error deleting files: {error_msg}"


def get_branch_info(repo_name: str, branch_name: str) -> str:
    """
    Gets information about a branch including latest commit.

    Args:
        repo_name: Repository in "owner/repo" format.
        branch_name: The branch to check.

    Returns:
        Branch information or error.
    """
    client = _get_client()
    repo = client.get_repo(repo_name)

    try:
        branch = repo.get_branch(branch_name)
        commit = branch.commit

        return f"""
## Branch: {branch_name}

**Latest Commit:** {commit.sha[:8]}
**Author:** {commit.commit.author.name}
**Date:** {commit.commit.author.date.isoformat()}
**Message:** {commit.commit.message.split(chr(10))[0]}

**Protected:** {"Yes" if branch.protected else "No"}
"""
    except GithubException as e:
        return f"Error: {e.data.get('message', str(e)) if hasattr(e, 'data') else str(e)}"


# =============================================================================
# PR TOOLS
# =============================================================================


def create_pr(repo_name: str, branch_name: str, title: str, description: str, base_branch: str = "main", draft: bool = False) -> str:
    """
    Creates a Pull Request from an existing branch.

    Use this AFTER you've already created a branch with `create_branch_with_files`.

    Args:
        repo_name: Repository in "owner/repo" format.
        branch_name: The source branch (must already exist with commits).
        title: PR title.
        description: PR body text explaining the changes.
        base_branch: Target branch to merge into (default: "main").
        draft: Whether to create as draft PR (default: False).

    Returns:
        Success message with PR URL or error details.

    Example:
        create_pr(
            repo_name="my-org/backend",
            branch_name="fix-issue-42",
            title="Fix issue #42: Add validation to routes",
            description="This PR adds input validation to the API routes..."
        )
    """
    client = _get_client()
    repo = client.get_repo(repo_name)

    try:
        # Verify branch exists
        try:
            repo.get_branch(branch_name)
        except GithubException:
            return f"ERROR: Branch '{branch_name}' does not exist. Use `create_branch_with_files` first."

        # Create PR
        pr = repo.create_pull(title=title, body=description, head=branch_name, base=base_branch, draft=draft)

        log.info("pr_created", number=pr.number, url=pr.html_url, branch=branch_name)

        return f"""
SUCCESS: Pull Request Created!

**PR #{pr.number}:** {pr.title}
**URL:** {pr.html_url}
**Branch:** {branch_name} → {base_branch}

**NEXT STEP:** Use `monitor_ci_for_pr("{repo_name}", {pr.number})` to wait for CI.
"""

    except GithubException as e:
        error_msg = e.data.get("message", str(e)) if hasattr(e, "data") else str(e)
        if "A pull request already exists" in error_msg:
            # Find existing PR
            try:
                prs = repo.get_pulls(state="open", head=f"{repo.owner.login}:{branch_name}")
                for pr in prs:
                    return f"""
NOTE: A PR already exists for this branch!

**PR #{pr.number}:** {pr.title}
**URL:** {pr.html_url}

Use `monitor_ci_for_pr("{repo_name}", {pr.number})` to check CI status.
"""
            except Exception as e:
                log.warning("failed_to_find_existing_pr", repo=repo_name, branch=branch_name, error=str(e))
        log.error("pr_creation_failed", error=error_msg, branch=branch_name)
        return f"ERROR creating PR: {error_msg}"


def create_pr_with_changes(
    repo_name: str,
    issue_number: int,
    file_changes: dict[str, str],
    description: str,
    draft: bool = False,
    include_tests: bool = True,
) -> str:
    """
    Atomic Action: Creates a branch, commits changes, and opens a PR.

    Args:
        repo_name: Repository in "owner/repo" format.
        issue_number: The issue this PR fixes.
        file_changes: Dict of {filepath: content} for files to create/update.
        description: PR body text explaining the changes.
        draft: Whether to create as draft PR (default: False).
        include_tests: Validate test files are included (default: True).

    Returns:
        Success message with PR URL or error details.
    """
    client = _get_client()
    repo = client.get_repo(repo_name)

    # Validate test inclusion
    if include_tests:
        test_files = [f for f in file_changes if "test" in f.lower()]
        if not test_files:
            log.warning("pr_missing_tests", issue=issue_number)

    # Generate unique branch name
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    branch_name = f"ai-fix-{issue_number}-{timestamp}"

    try:
        # Get base branch
        base_branch = repo.default_branch
        source = repo.get_branch(base_branch)

        # Create feature branch
        repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=source.commit.sha)
        log.info("branch_created", branch=branch_name, base=base_branch)

        # Commit files
        for file_path, content in file_changes.items():
            try:
                # Check if file exists
                existing = repo.get_contents(file_path, ref=branch_name)
                repo.update_file(
                    path=file_path,
                    message=f"fix(#{issue_number}): Update {file_path}",
                    content=content,
                    sha=existing.sha,
                    branch=branch_name,
                )
                log.info("file_updated", path=file_path)
            except GithubException:
                # File doesn't exist, create it
                repo.create_file(
                    path=file_path,
                    message=f"feat(#{issue_number}): Add {file_path}",
                    content=content,
                    branch=branch_name,
                )
                log.info("file_created", path=file_path)

        # Create PR
        pr_title = f"fix: Resolve issue #{issue_number}"
        pr_body = f"""
## Summary
{description}

## Related Issue
Closes #{issue_number}

## Changes Made
{chr(10).join(f"- `{f}`" for f in file_changes)}

---
*This PR was automatically generated by CapeAble Core Foundry AI.*
"""

        pr = repo.create_pull(title=pr_title, body=pr_body, head=branch_name, base=base_branch, draft=draft)

        log.info("pr_created", number=pr.number, url=pr.html_url)

        return f"""
SUCCESS: Pull Request Created!

**PR #{pr.number}:** {pr.title}
**URL:** {pr.html_url}
**Branch:** {branch_name} → {base_branch}
**Files Changed:** {len(file_changes)}

Waiting for CI to run...
"""

    except GithubException as e:
        error_msg = e.data.get("message", str(e)) if hasattr(e, "data") else str(e)
        log.error("pr_creation_failed", error=error_msg, issue=issue_number)
        return f"ERROR creating PR: {error_msg}"


def update_pr_with_changes(repo_name: str, pr_number: int, file_changes: dict[str, str], commit_message: str) -> str:
    """
    Updates an existing PR with additional changes.

    Args:
        repo_name: Repository in "owner/repo" format.
        pr_number: The PR number to update.
        file_changes: Dict of {filepath: content} for files to update.
        commit_message: Commit message for the changes.

    Returns:
        Success or error message.
    """
    client = _get_client()
    repo = client.get_repo(repo_name)

    try:
        pr = repo.get_pull(pr_number)
        branch_name = pr.head.ref

        for file_path, content in file_changes.items():
            try:
                existing = repo.get_contents(file_path, ref=branch_name)
                repo.update_file(path=file_path, message=commit_message, content=content, sha=existing.sha, branch=branch_name)
            except GithubException:
                repo.create_file(path=file_path, message=commit_message, content=content, branch=branch_name)

        return f"SUCCESS: PR #{pr_number} updated with {len(file_changes)} file(s)."

    except GithubException as e:
        return f"ERROR updating PR: {e.data.get('message', str(e))}"


def get_pr_details(repo_name: str, pr_number: int) -> str:
    """
    Gets detailed information about a PR including files changed and CI status.

    Args:
        repo_name: Repository in "owner/repo" format.
        pr_number: The PR number.

    Returns:
        Formatted PR details.
    """
    client = _get_client()
    repo = client.get_repo(repo_name)

    try:
        pr = repo.get_pull(pr_number)
        files = pr.get_files()

        status = PRStatus.DRAFT if pr.draft else PRStatus.OPEN
        if pr.merged:
            status = PRStatus.MERGED
        elif pr.state == "closed":
            status = PRStatus.CLOSED

        # Get CI status
        ci_status = get_ci_status(repo_name, pr.head.sha)

        files_list = [f.filename for f in files]

        result = f"""
## PR #{pr.number}: {pr.title}

**Status:** {status.value.upper()}
**Branch:** {pr.head.ref} → {pr.base.ref}
**Author:** {pr.user.login}
**CI Status:** {ci_status}

### Files Changed ({len(files_list)})
{chr(10).join(f"- `{f}`" for f in files_list)}

### Description
{pr.body or "No description."}
"""
        return result

    except GithubException as e:
        return f"ERROR: {e.data.get('message', str(e))}"
    except Exception as e:
        log.error("get_pr_details_error", pr_number=pr_number, error=str(e))
        return f"Error getting PR details: {e!s}"


# =============================================================================
# CI MONITORING TOOLS
# =============================================================================


def get_ci_status(repo_name: str, commit_sha: str) -> str:
    """
    Gets the CI/CD status for a specific commit.

    Checks GitHub Actions workflow runs first (most common), then falls back
    to combined status for other CI systems.

    Args:
        repo_name: Repository in "owner/repo" format.
        commit_sha: The commit SHA to check.

    Returns:
        CI status string.
    """
    client = _get_client()
    repo = client.get_repo(repo_name)

    # First, check GitHub Actions workflow runs (most common CI for GitHub)
    try:
        runs = repo.get_workflow_runs(head_sha=commit_sha)

        if runs.totalCount > 0:
            # Check ALL workflow runs, not just the first one
            all_completed = True
            any_failed = False
            any_cancelled = False

            for run in runs:
                if run.status != "completed":
                    all_completed = False
                elif run.conclusion == "failure" or run.conclusion == "timed_out":
                    any_failed = True
                elif run.conclusion == "cancelled":
                    any_cancelled = True

            if not all_completed:
                return CIStatus.IN_PROGRESS.value
            elif any_failed:
                return CIStatus.FAILURE.value
            elif any_cancelled:
                return CIStatus.CANCELLED.value
            else:
                return CIStatus.SUCCESS.value
    except Exception:
        # Log but continue to check combined status
        pass

    # Fallback: Check combined status (for other CI systems like Jenkins, CircleCI, etc.)
    try:
        commit = repo.get_commit(commit_sha)
        status = commit.get_combined_status()

        # Only trust combined status if there are actual statuses
        if status.total_count > 0:
            if status.state == "success":
                return CIStatus.SUCCESS.value
            elif status.state == "pending":
                return CIStatus.PENDING.value
            elif status.state == "failure":
                return CIStatus.FAILURE.value
            else:
                return status.state
    except Exception:
        pass

    # No CI found
    return CIStatus.PENDING.value


def wait_for_ci_completion(repo_name: str, pr_number: int, timeout_seconds: int = 600, poll_interval: int = 30) -> str:
    """
    Waits for CI to complete on a PR.

    Args:
        repo_name: Repository in "owner/repo" format.
        pr_number: The PR number to monitor.
        timeout_seconds: Maximum wait time (default: 10 minutes).
        poll_interval: Seconds between status checks (default: 30).

    Returns:
        Final CI status with details.
    """
    client = _get_client()
    repo = client.get_repo(repo_name)

    start_time = time.time()

    try:
        pr = repo.get_pull(pr_number)
        commit_sha = pr.head.sha

        while time.time() - start_time < timeout_seconds:
            status = get_ci_status(repo_name, commit_sha)

            if status in [CIStatus.SUCCESS.value, CIStatus.FAILURE.value, CIStatus.CANCELLED.value]:
                # Get detailed logs if failed
                if status == CIStatus.FAILURE.value:
                    logs = get_ci_failure_logs(repo_name, commit_sha)
                    return f"CI_STATUS: {status.upper()}\n\n### Failure Logs\n{logs}"
                return f"CI_STATUS: {status.upper()}"

            log.info("ci_polling", status=status, elapsed=int(time.time() - start_time))
            time.sleep(poll_interval)

        return f"CI_STATUS: TIMEOUT (exceeded {timeout_seconds}s)"

    except GithubException as e:
        return f"CI_STATUS: ERROR - {e.data.get('message', str(e))}"


def get_ci_failure_logs(repo_name: str, commit_sha: str) -> str:
    """
    Retrieves failure logs from CI runs.

    Args:
        repo_name: Repository in "owner/repo" format.
        commit_sha: The commit SHA to get logs for.

    Returns:
        Extracted failure logs or error message.
    """
    client = _get_client()
    repo = client.get_repo(repo_name)

    try:
        runs = repo.get_workflow_runs(head_sha=commit_sha)

        for run in runs:
            if run.conclusion == "failure":
                # Get jobs for this run
                jobs = run.jobs()

                logs = f"**Workflow:** {run.name}\n"

                for job in jobs:
                    if job.conclusion == "failure":
                        logs += f"\n**Failed Job:** {job.name}\n"

                        # Get step failures
                        for step in job.steps:
                            if step.conclusion == "failure":
                                logs += f"  ❌ Step: {step.name}\n"

                return logs

        return "No failure details found."

    except Exception as e:
        return f"Error retrieving logs: {e!s}"


# =============================================================================
# COMMENT TOOLS
# =============================================================================


def add_pr_comment(repo_name: str, pr_number: int, comment: str) -> str:
    """
    Adds a comment to a PR.

    Args:
        repo_name: Repository in "owner/repo" format.
        pr_number: The PR number.
        comment: Comment text (supports Markdown).

    Returns:
        Success or error message.
    """
    client = _get_client()
    repo = client.get_repo(repo_name)

    try:
        pr = repo.get_pull(pr_number)
        pr.create_issue_comment(comment)
        return f"Comment added to PR #{pr_number}."
    except GithubException as e:
        return f"Error: {e.data.get('message', str(e))}"


def add_issue_comment(repo_name: str, issue_number: int, comment: str) -> str:
    """
    Adds a comment to an issue.

    Args:
        repo_name: Repository in "owner/repo" format.
        issue_number: The issue number.
        comment: Comment text (supports Markdown).

    Returns:
        Success or error message.
    """
    client = _get_client()
    repo = client.get_repo(repo_name)

    try:
        issue = repo.get_issue(number=issue_number)
        issue.create_comment(comment)
        return f"Comment added to issue #{issue_number}."
    except GithubException as e:
        return f"Error: {e.data.get('message', str(e))}"


# =============================================================================
# SECRETS & VARIABLES TOOLS
# =============================================================================


def get_repo_secrets_list(repo_name: str) -> str:
    """
    Lists all repository secrets (names only - values are never exposed).

    Args:
        repo_name: Repository in "owner/repo" format.

    Returns:
        List of secret names available in the repository.
    """
    client = _get_client()
    repo = client.get_repo(repo_name)

    try:
        secrets = repo.get_secrets()
        secret_names = [s.name for s in secrets]

        if not secret_names:
            return "No secrets found in repository."

        result = "**Repository Secrets (names only):**\n"
        for name in secret_names:
            result += f"- `{name}`\n"
        result += "\n*Note: Secret VALUES are never exposed. Use these names in your workflows/tests.*"
        return result
    except GithubException as e:
        return f"Error listing secrets: {e.data.get('message', str(e))}"
    except Exception as e:
        return f"Error: {e!s}"


def get_repo_variables(repo_name: str) -> str:
    """
    Lists all repository variables with their values.

    Variables are non-sensitive configuration values (unlike secrets).

    Args:
        repo_name: Repository in "owner/repo" format.

    Returns:
        Dictionary of variable names and values.
    """
    client = _get_client()
    repo = client.get_repo(repo_name)

    try:
        variables = repo.get_variables()
        var_dict = {v.name: v.value for v in variables}

        if not var_dict:
            return "No variables found in repository."

        result = "**Repository Variables:**\n```\n"
        for name, value in var_dict.items():
            result += f"{name}={value}\n"
        result += "```"
        return result
    except GithubException as e:
        return f"Error listing variables: {e.data.get('message', str(e))}"
    except Exception as e:
        return f"Error: {e!s}"


def get_env_template(repo_name: str, ref: str = "main") -> str:
    """
    Reads .env.example or similar template files.

    Understand what environment variables are needed for the project.

    Args:
        repo_name: Repository in "owner/repo" format.
        ref: Branch or commit to read from (default: main).

    Returns:
        Content of environment template file with notes on which secrets/variables
        are available to fill them.
    """
    client = _get_client()
    repo = client.get_repo(repo_name)

    # Common env template file names
    template_files = [
        ".env.example",
        ".env.template",
        ".env.sample",
        "env.example",
        ".env.test.example",
    ]

    env_content = None
    found_file = None

    try:
        for template_file in template_files:
            try:
                content = repo.get_contents(template_file, ref=ref)
                if content.encoding == "base64":
                    import base64

                    env_content = base64.b64decode(content.content).decode("utf-8")
                else:
                    env_content = content.decoded_content.decode("utf-8")
                found_file = template_file
                break
            except GithubException:
                continue

        if not env_content:
            return "No environment template file found (.env.example, .env.template, etc.)"

        # Parse the template to extract variable names
        env_vars = []
        for line in env_content.split("\n"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                var_name = line.split("=")[0].strip()
                env_vars.append(var_name)

        # Get available secrets and variables for cross-reference
        try:
            secrets = [s.name for s in repo.get_secrets()]
        except Exception:
            secrets = []

        try:
            variables = {v.name: v.value for v in repo.get_variables()}
        except Exception:
            variables = {}

        # Build result with mapping
        result = f"**Environment Template:** `{found_file}`\n\n"
        result += "```env\n"
        result += env_content
        result += "\n```\n\n"

        result += "**Variable Mapping:**\n"
        for var in env_vars:
            if var in secrets:
                result += f"- `{var}` → ✅ Available as GitHub Secret\n"
            elif var in variables:
                result += f"- `{var}` → ✅ Available as GitHub Variable: `{variables[var]}`\n"
            else:
                result += f"- `{var}` → ⚠️ Not found in secrets/variables\n"

        return result
    except Exception as e:
        return f"Error reading environment template: {e!s}"


def build_env_from_github(repo_name: str, ref: str = "main") -> str:
    """Builds an environment configuration by reading .env.example and mapping

    available GitHub secrets/variables to their corresponding env vars.

    Returns a structured format that can be passed to test runners.

    **IMPORTANT:** This does NOT expose secret VALUES - it returns a mapping
    that tells the sandbox/test runner which GitHub secrets to inject.

    Args:
        repo_name: Repository in "owner/repo" format.
        ref: Branch or commit to read from (default: main).

    Returns:
        JSON-like structure with env var mappings for test execution.
    """
    client = _get_client()
    repo = client.get_repo(repo_name)

    try:
        # Get available secrets and variables
        try:
            secrets = [s.name for s in repo.get_secrets()]
        except Exception:
            secrets = []

        try:
            variables = {v.name: v.value for v in repo.get_variables()}
        except Exception:
            variables = {}

        # Try to read .env.example
        template_files = [".env.example", ".env.template", ".env.sample"]
        env_vars = []

        for template_file in template_files:
            try:
                content = repo.get_contents(template_file, ref=ref)
                if content.encoding == "base64":
                    import base64

                    decoded = base64.b64decode(content.content).decode("utf-8")
                else:
                    decoded = content.decoded_content.decode("utf-8")

                for line in decoded.split("\n"):
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        var_name = line.split("=")[0].strip()
                        env_vars.append(var_name)
                break
            except GithubException:
                continue

        # Build the env config
        env_config: dict[str, Any] = {
            "from_secrets": {},  # Map env_var -> secret_name (for injection)
            "from_variables": {},  # Map env_var -> actual_value
            "missing": [],  # Vars that couldn't be mapped
            "secret_names": secrets,  # All available secrets
        }

        for var in env_vars:
            # Check if there's a matching secret
            if var in secrets:
                env_config["from_secrets"][var] = var
            # Check common naming patterns (e.g., DB_PASSWORD might be stored as DATABASE_PASSWORD)
            elif var.upper() in secrets:
                env_config["from_secrets"][var] = var.upper()
            # Check if it's a variable
            elif var in variables:
                env_config["from_variables"][var] = variables[var]
            elif var.upper() in variables:
                env_config["from_variables"][var] = variables[var.upper()]
            else:
                env_config["missing"].append(var)

        # Format output
        result = "**Environment Configuration for Tests:**\n\n"

        if env_config["from_secrets"]:
            result += "**Secrets to Inject (pass to sandbox):**\n```json\n"
            result += "{\n"
            for env_var, secret_name in env_config["from_secrets"].items():
                result += f'  "{env_var}": "GITHUB_SECRET:{secret_name}",\n'
            result += "}\n```\n\n"

        if env_config["from_variables"]:
            result += "**Variables (direct values):**\n```json\n"
            result += "{\n"
            for env_var, value in env_config["from_variables"].items():
                result += f'  "{env_var}": "{value}",\n'
            result += "}\n```\n\n"

        if env_config["missing"]:
            result += f"**⚠️ Missing (not in secrets/variables):** {', '.join(env_config['missing'])}\n"

        return result
    except Exception as e:
        return f"Error building env config: {e!s}"

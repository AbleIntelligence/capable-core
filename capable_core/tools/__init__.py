"""CapeAble Core Foundry Tools Package.

Provides tools for GitHub integration, CI monitoring, and sandbox execution.
"""

from capable_core.tools.ci_tools import (
    get_workflow_summary,
    monitor_ci_for_pr,
    trigger_workflow,
)
from capable_core.tools.github_tools import (
    add_issue_comment,
    add_pr_comment,
    create_pr_with_changes,
    get_ci_status,
    get_directory_tree,
    get_file_content,
    get_issue_content,
    get_my_assigned_issues,
    get_pr_details,
    update_pr_with_changes,
    wait_for_ci_completion,
)
from capable_core.tools.sandbox_tools import (
    lint_code,
    run_command_on_branch,
    run_mutation_tests,
    run_tests_in_sandbox,
    run_tests_with_coverage,
    validate_syntax,
)


__all__ = [
    "add_issue_comment",
    "add_pr_comment",
    "create_pr_with_changes",
    "get_ci_status",
    "get_directory_tree",
    "get_file_content",
    "get_issue_content",
    # GitHub
    "get_my_assigned_issues",
    "get_pr_details",
    "get_workflow_summary",
    "lint_code",
    # CI
    "monitor_ci_for_pr",
    "run_command_on_branch",
    "run_mutation_tests",
    # Sandbox
    "run_tests_in_sandbox",
    "run_tests_with_coverage",
    "trigger_workflow",
    "update_pr_with_changes",
    "validate_syntax",
    "wait_for_ci_completion",
]

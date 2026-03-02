"""
Integration tests for agent reasoning and tool wiring.

Validates that Developer and QA_Architect agents are correctly constructed
with the expected tools and system prompts, and that the prompt logic
would guide the LLM to call the right tools first.

All Google / Vertex AI dependencies are mocked so the suite works without
GOOGLE_API_KEY or any cloud credentials.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Fixtures - environment & heavy-import isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set minimal env vars so Pydantic Settings won't raise on import."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake_token_for_testing")
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-api-key-for-testing")
    monkeypatch.setenv("AGENT_PROVIDER_TYPE", "gemini")


@pytest.fixture()
def _fresh_modules() -> Any:
    """Remove cached application modules so each test re-imports cleanly."""
    stale = [k for k in sys.modules if k.startswith("capable_core.")]
    saved = {k: sys.modules.pop(k) for k in stale}
    yield
    # Restore to avoid side-effects between files
    for k, v in saved.items():
        sys.modules[k] = v


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tool_names(agent: Any) -> list[str]:
    """Extract the function names from an agent's tool list."""
    names: list[str] = []
    for t in getattr(agent, "tools", []):
        # ADK wraps plain functions; the original is accessible via __name__
        if callable(t):
            names.append(getattr(t, "__name__", str(t)))
        else:
            names.append(str(t))
    return names


# ===================================================================
# Developer Agent Tests
# ===================================================================


class TestDeveloperAgentReasoning:
    """Verify the Developer agent is wired correctly for its first-step behaviour."""

    # -- Factory construction ------------------------------------------------

    def test_developer_agent_has_get_directory_tree_tool(self) -> None:
        """Developer agent must include get_directory_tree so it can explore the repo."""
        from capable_core.agents.developer import create_developer_agent

        agent = create_developer_agent()
        names = _tool_names(agent)
        assert "get_directory_tree" in names, f"get_directory_tree missing from Developer tools: {names}"

    def test_developer_agent_has_get_file_content_tool(self) -> None:
        """Developer agent must include get_file_content for reading source code."""
        from capable_core.agents.developer import create_developer_agent

        agent = create_developer_agent()
        names = _tool_names(agent)
        assert "get_file_content" in names

    def test_developer_tools_include_full_set(self) -> None:
        """Developer agent should carry the core tool chain."""
        from capable_core.agents.developer import create_developer_agent

        agent = create_developer_agent()
        names = _tool_names(agent)

        expected = {
            "get_file_content",
            "get_directory_tree",
            "create_branch_with_files",
            "push_files_to_branch",
            "validate_syntax",
            "run_tests_on_branch",
            "lint_code_on_branch",
            "create_pr",
            "update_pr_with_changes",
            "monitor_ci_for_pr",
        }
        missing = expected - set(names)
        assert not missing, f"Developer agent missing tools: {missing}"

    # -- System prompt directs first step ------------------------------------

    def test_developer_prompt_instructs_directory_tree_first(self) -> None:
        """The system prompt must tell the Developer to explore with get_directory_tree before coding."""
        from capable_core.agents.developer import DEVELOPER_SYSTEM_PROMPT

        prompt_lower = DEVELOPER_SYSTEM_PROMPT.lower()
        # The prompt should mention exploring the project structure as Step 1
        assert "get_directory_tree" in prompt_lower, "Developer prompt does not mention get_directory_tree"
        # Locate the Step 1 / workflow section
        step1_idx = prompt_lower.find("step 1")
        assert step1_idx != -1, "Prompt missing Step 1 section"
        # get_directory_tree must appear *within* the Step 1 section (after its heading)
        tree_in_step1 = prompt_lower.find("get_directory_tree", step1_idx)
        assert tree_in_step1 != -1, "get_directory_tree is not referenced in the Step 1 section of the Developer prompt"

    # -- Mission parsing flow -----------------------------------------------

    def test_developer_prompt_contains_mission_workflow(self) -> None:
        """Developer prompt should describe the mission-based workflow."""
        from capable_core.agents.developer import DEVELOPER_SYSTEM_PROMPT

        prompt_lower = DEVELOPER_SYSTEM_PROMPT.lower()
        # Must mention reading/understanding the issue and the codebase
        assert "understand" in prompt_lower or "read" in prompt_lower, "Prompt does not instruct developer to read/understand the codebase"
        assert "workflow" in prompt_lower, "Prompt does not contain a workflow section"

    def test_developer_on_start_callback_parses_mission(self) -> None:
        """on_developer_start must extract mission context into a prompt."""
        from capable_core.agents.developer import on_developer_start

        context: dict[str, Any] = {
            "mission": {
                "description": "Fix the login bug in auth.py",
                "issue_number": 42,
                "repo_name": "acme/backend",
            },
            "feedback": "",
        }
        result = on_developer_start(context)
        prompt = result["developer_prompt"]
        assert "Fix the login bug" in prompt
        assert "#42" in prompt
        assert "acme/backend" in prompt

    def test_developer_on_start_includes_feedback_on_retry(self) -> None:
        """on_developer_start should include previous QA feedback when retrying."""
        from capable_core.agents.developer import on_developer_start

        context: dict[str, Any] = {
            "mission": {"description": "Add pagination"},
            "feedback": "Tests still failing on page 2 boundary",
        }
        result = on_developer_start(context)
        assert "Tests still failing on page 2 boundary" in result["developer_prompt"]


# ===================================================================
# QA Architect Agent Tests
# ===================================================================


class TestQAArchitectAgentReasoning:
    """Verify the QA_Architect agent triggers run_coverage_on_branch for PR review."""

    # -- Factory construction ------------------------------------------------

    def test_qa_agent_has_run_coverage_tool(self) -> None:
        """QA Architect must include run_coverage_on_branch in its tool belt."""
        from capable_core.agents.qa_architect import create_qa_architect_agent

        agent = create_qa_architect_agent()
        names = _tool_names(agent)
        assert "run_coverage_on_branch" in names, f"run_coverage_on_branch missing from QA tools: {names}"

    def test_qa_agent_has_get_pr_details_tool(self) -> None:
        """QA Architect must include get_pr_details to read the PR before testing."""
        from capable_core.agents.qa_architect import create_qa_architect_agent

        agent = create_qa_architect_agent()
        names = _tool_names(agent)
        assert "get_pr_details" in names

    def test_qa_agent_tools_include_full_set(self) -> None:
        """QA Architect should carry the essential tool chain."""
        from capable_core.agents.qa_architect import create_qa_architect_agent

        agent = create_qa_architect_agent()
        names = _tool_names(agent)

        expected = {
            "get_pr_details",
            "get_file_content",
            "get_directory_tree",
            "get_branch_info",
            "run_tests_on_branch",
            "run_coverage_on_branch",
            "lint_code_on_branch",
            "push_files_to_branch",
            "add_pr_comment",
        }
        missing = expected - set(names)
        assert not missing, f"QA Architect agent missing tools: {missing}"

    # -- System prompt directs coverage on PR --------------------------------

    def test_qa_prompt_instructs_pr_details_first(self) -> None:
        """QA prompt should instruct calling get_pr_details FIRST."""
        from capable_core.agents.qa_architect import QA_ARCHITECT_SYSTEM_PROMPT_NO_MUTATION

        prompt_lower = QA_ARCHITECT_SYSTEM_PROMPT_NO_MUTATION.lower()
        assert "get_pr_details" in prompt_lower
        # The "DO THIS FIRST" or "FIRST" instruction must precede coverage
        first_idx = prompt_lower.find("first")
        pr_idx = prompt_lower.find("get_pr_details")
        assert first_idx != -1 and pr_idx != -1

    def test_qa_prompt_instructs_coverage_on_branch(self) -> None:
        """QA prompt must tell the agent to call run_coverage_on_branch."""
        from capable_core.agents.qa_architect import QA_ARCHITECT_SYSTEM_PROMPT_NO_MUTATION

        prompt_lower = QA_ARCHITECT_SYSTEM_PROMPT_NO_MUTATION.lower()
        assert "run_coverage_on_branch" in prompt_lower, "QA prompt does not reference run_coverage_on_branch"

    def test_qa_prompt_coverage_follows_pr_details(self) -> None:
        """In the workflow, coverage should come after reading PR details."""
        from capable_core.agents.qa_architect import QA_ARCHITECT_SYSTEM_PROMPT_NO_MUTATION

        prompt_lower = QA_ARCHITECT_SYSTEM_PROMPT_NO_MUTATION.lower()
        pr_idx = prompt_lower.find("get_pr_details")
        cov_idx = prompt_lower.find("run_coverage_on_branch")
        assert pr_idx < cov_idx, "run_coverage_on_branch appears before get_pr_details — wrong ordering"


# ===================================================================
# Config-level validation tests (no cloud creds needed)
# ===================================================================


class TestConfigValidation:
    """Verify validate_environment behaviour with mocked env."""

    def test_validation_passes_with_gemini_and_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No errors when provider is gemini and GOOGLE_API_KEY is set."""
        monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
        monkeypatch.setenv("AGENT_PROVIDER_TYPE", "gemini")
        # Force re-import to pick up fresh env
        sys.modules.pop("capable_core.config", None)
        from capable_core.config import validate_environment

        errors = validate_environment()
        google_errors = [e for e in errors if "GOOGLE" in e]
        assert google_errors == []

    def test_validation_skips_google_for_litellm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No Google-related errors when provider is litellm."""
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        monkeypatch.setenv("AGENT_PROVIDER_TYPE", "litellm")
        sys.modules.pop("capable_core.config", None)
        from capable_core.config import validate_environment

        errors = validate_environment()
        google_errors = [e for e in errors if "GOOGLE" in e]
        assert google_errors == [], f"Unexpected Google errors for litellm provider: {google_errors}"

    def test_validation_flags_missing_google_for_gemini(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Should error when provider is gemini but no Google credentials are set."""
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        monkeypatch.setenv("AGENT_PROVIDER_TYPE", "gemini")
        sys.modules.pop("capable_core.config", None)
        from capable_core.config import validate_environment

        errors = validate_environment()
        google_errors = [e for e in errors if "GOOGLE" in e]
        assert len(google_errors) == 1
        assert "GOOGLE_CLOUD_PROJECT" in google_errors[0]

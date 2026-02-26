"""
Integration tests for CLI smoke, module imports, and config validation.

Covers gaps left by test_agent_reasoning.py:
- CLI entry point (run.py main / run_nightwatch) actually executes without crash.
- Every module in the package can be imported (catches circular imports).
- Config validation correctly flags missing credentials.

All external calls (Docker, Google AI, GitHub) are mocked.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set minimal env vars so Pydantic Settings won't raise on import."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake_token_for_testing")
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-api-key-for-testing")
    monkeypatch.setenv("AGENT_PROVIDER_TYPE", "gemini")


@pytest.fixture()
def _fresh_config(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Drop cached config module so env changes take effect."""
    yield
    sys.modules.pop("capable_core.config", None)


# ===================================================================
# CLI Smoke Tests
# ===================================================================


class TestCLISmoke:
    """Verify the CLI entry point loads, validates, and runs --dry-run."""

    @staticmethod
    def _run_cli(*args: str, env_overrides: dict[str, str] | None = None) -> tuple[int, str, str]:
        """Run the CLI as a subprocess and return (returncode, stdout, stderr).

        Uses bytes mode + manual decode to avoid Windows cp1255 / text=True issues.
        """
        import os

        env = {
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
            "GITHUB_TOKEN": "ghp_fake",
            "GOOGLE_API_KEY": "fake-key",
            "AGENT_PROVIDER_TYPE": "gemini",
        }
        if env_overrides:
            env.update(env_overrides)

        result = subprocess.run(
            [sys.executable, "-m", "capable_core.run", *args],
            capture_output=True,
            timeout=30,
            env=env,
        )
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        return result.returncode, stdout, stderr

    def test_foundry_run_dry_run_succeeds(self) -> None:
        """foundry-run --dry-run should exit 0 and print mission prompt."""
        rc, stdout, stderr = self._run_cli("--repo", "test-org/test-repo", "--dry-run")
        assert rc == 0, f"CLI exited {rc}:\nSTDOUT: {stdout}\nSTDERR: {stderr}"
        assert "DRY RUN" in stdout or "dry_run" in stdout.lower()

    def test_foundry_run_dry_run_contains_repo(self) -> None:
        """--dry-run output should include the target repo name."""
        rc, stdout, _ = self._run_cli("--repo", "acme/backend", "--dry-run")
        assert rc == 0
        assert "acme/backend" in stdout

    def test_foundry_run_dry_run_with_issue_number(self) -> None:
        """--dry-run with --issue should reference that issue number."""
        rc, stdout, _ = self._run_cli("--repo", "acme/backend", "--issue", "99", "--dry-run")
        assert rc == 0
        assert "#99" in stdout

    def test_foundry_run_missing_repo_flag_exits_nonzero(self) -> None:
        """Omitting the required --repo flag should exit with error."""
        rc, _, _ = self._run_cli()
        assert rc != 0

    def test_foundry_run_fails_without_github_token(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """CLI should exit 1 when validate_environment reports missing creds."""
        monkeypatch.setattr("sys.argv", ["foundry-run", "--repo", "acme/backend", "--dry-run"])

        # Mock validate_environment to simulate missing GITHUB_TOKEN
        monkeypatch.setattr(
            "capable_core.run.validate_environment",
            lambda: ["GITHUB_TOKEN is required"],
        )

        from capable_core.run import main

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "GITHUB_TOKEN" in captured.out

    def test_cli_main_dry_run_in_process(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """Run main() in-process with --dry-run to verify it prints without crashing."""
        monkeypatch.setattr("sys.argv", ["foundry-run", "--repo", "test-org/repo", "--dry-run"])
        from capable_core.run import main

        # main() does not call sys.exit on dry-run success
        try:
            main()
        except SystemExit as e:
            assert e.code is None or e.code == 0, f"main() exited with code {e.code}"

        captured = capsys.readouterr()
        assert "DRY RUN" in captured.out

    def test_cli_main_dry_run_no_exit(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """main() with --dry-run should complete successfully and print mission info."""
        monkeypatch.setattr("sys.argv", ["foundry-run", "--repo", "acme/backend", "--issue", "7", "--dry-run"])

        from capable_core.run import main

        # main() may or may not call sys.exit — handle both
        try:
            main()
        except SystemExit as e:
            assert e.code is None or e.code == 0, f"main() exited with code {e.code}"

        captured = capsys.readouterr()
        assert "DRY RUN" in captured.out
        assert "acme/backend" in captured.out
        assert "#7" in captured.out

    def test_run_nightwatch_dry_run_returns_mission(self) -> None:
        """run_nightwatch(dry_run=True) should return status=dry_run with a mission string."""
        from capable_core.run import run_nightwatch

        result = run_nightwatch(repo_name="test-org/repo", dry_run=True)
        assert result["status"] == "dry_run"
        assert result["would_execute"] is True
        assert "test-org/repo" in result["mission"]

    def test_run_nightwatch_dry_run_with_issue(self) -> None:
        """run_nightwatch(dry_run=True, issue_number=42) should include the issue in the mission."""
        from capable_core.run import run_nightwatch

        result = run_nightwatch(repo_name="test-org/repo", issue_number=42, dry_run=True)
        assert result["status"] == "dry_run"
        assert "#42" in result["mission"]


# ===================================================================
# Module Import Tests
# ===================================================================

# Every .py module in the package. If a new module is added and has a
# circular import, it will fail here before it breaks production.
ALL_MODULES = [
    "capable_core",
    "capable_core.config",
    "capable_core.run",
    "capable_core.agents",
    "capable_core.agents.agent",
    "capable_core.agents.developer",
    "capable_core.agents.qa_architect",
    "capable_core.agents.parallel_squads",
    "capable_core.agents.squads",
    "capable_core.tools",
    "capable_core.tools.github_tools",
    "capable_core.tools.ci_tools",
    "capable_core.tools.sandbox_tools",
    "capable_core.flows",
    "capable_core.flows.nightwatch",
    "capable_core.flows.nightwatch.agents",
]


class TestModuleImports:
    """Ensure every package module imports without error."""

    @pytest.mark.parametrize("module_name", ALL_MODULES)
    def test_module_imports_cleanly(self, module_name: str) -> None:
        """Import the module; any ImportError or circular import will surface here."""
        # Remove cached version so we get a fresh import path
        sys.modules.pop(module_name, None)
        try:
            mod = importlib.import_module(module_name)
            assert mod is not None
        except Exception as exc:
            pytest.fail(f"Failed to import {module_name}: {exc}")

    def test_package_version_is_defined(self) -> None:
        """capable_core.__version__ should be a non-empty string."""
        import capable_core

        assert hasattr(capable_core, "__version__")
        assert isinstance(capable_core.__version__, str)
        assert len(capable_core.__version__) > 0

    def test_package_exports_workflow_classes(self) -> None:
        """Top-level package should re-export NightwatchWorkflow, WorkflowConfig, WorkflowResult."""
        from capable_core import NightwatchWorkflow, WorkflowConfig, WorkflowResult

        assert NightwatchWorkflow is not None
        assert WorkflowConfig is not None
        assert WorkflowResult is not None


# ===================================================================
# Configuration Validation Tests
# ===================================================================


class TestConfigValidationExtended:
    """Extended config validation covering edge cases beyond test_agent_reasoning.py."""

    def test_missing_github_token_is_flagged(self, monkeypatch: pytest.MonkeyPatch, _fresh_config: Any) -> None:
        """validate_environment must report missing GITHUB_TOKEN."""
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
        monkeypatch.setenv("AGENT_PROVIDER_TYPE", "gemini")
        sys.modules.pop("capable_core.config", None)
        from capable_core.config import validate_environment

        errors = validate_environment()
        github_errors = [e for e in errors if "GITHUB_TOKEN" in e]
        assert len(github_errors) == 1

    def test_missing_all_credentials_returns_multiple_errors(self, monkeypatch: pytest.MonkeyPatch, _fresh_config: Any) -> None:
        """Missing both GITHUB_TOKEN and Google creds should yield multiple errors."""
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        monkeypatch.setenv("AGENT_PROVIDER_TYPE", "gemini")
        sys.modules.pop("capable_core.config", None)
        from capable_core.config import validate_environment

        errors = validate_environment()
        assert len(errors) >= 2
        topics = " ".join(errors)
        assert "GITHUB_TOKEN" in topics
        assert "GOOGLE" in topics

    def test_vertex_ai_project_satisfies_google_requirement(self, monkeypatch: pytest.MonkeyPatch, _fresh_config: Any) -> None:
        """GOOGLE_CLOUD_PROJECT alone (no API key) should be valid for Gemini provider."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.setenv("AGENT_PROVIDER_TYPE", "gemini")
        sys.modules.pop("capable_core.config", None)
        from capable_core.config import validate_environment

        errors = validate_environment()
        assert errors == []

    def test_litellm_provider_skips_google_check(self, monkeypatch: pytest.MonkeyPatch, _fresh_config: Any) -> None:
        """Provider litellm should not require any Google credentials."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        monkeypatch.setenv("AGENT_PROVIDER_TYPE", "litellm")
        sys.modules.pop("capable_core.config", None)
        from capable_core.config import validate_environment

        errors = validate_environment()
        assert errors == []

    def test_hf_local_provider_skips_google_check(self, monkeypatch: pytest.MonkeyPatch, _fresh_config: Any) -> None:
        """Provider hf-local should not require any Google credentials."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        monkeypatch.setenv("AGENT_PROVIDER_TYPE", "hf-local")
        sys.modules.pop("capable_core.config", None)
        from capable_core.config import validate_environment

        errors = validate_environment()
        assert errors == []

    def test_settings_loads_agent_defaults(self) -> None:
        """Settings.agent should have sensible defaults for iteration limits and quality gates."""
        from capable_core.config import settings

        assert settings.agent.dev_max_iterations >= 1
        assert settings.agent.qa_max_iterations >= 1
        assert settings.agent.min_coverage_percent > 0
        assert settings.agent.max_surviving_mutants >= 0
        assert settings.agent.ci_timeout > 0
        assert settings.agent.ci_poll_interval > 0

    def test_settings_loads_sandbox_defaults(self) -> None:
        """SandboxConfig should expose Docker resource limits."""
        from capable_core.config import settings

        assert settings.sandbox.default_image == "python:3.11-slim"
        assert settings.sandbox.timeout > 0
        assert settings.sandbox.memory_limit  # non-empty
        assert settings.sandbox.cpu_limit > 0

    def test_settings_per_role_overrides_empty_by_default(self, monkeypatch: pytest.MonkeyPatch, _fresh_config: Any) -> None:
        """Per-role model overrides should be empty strings when env vars are unset."""
        monkeypatch.delenv("AGENT_DEVELOPER_MODEL", raising=False)
        monkeypatch.delenv("AGENT_DEVELOPER_PROVIDER", raising=False)
        monkeypatch.delenv("AGENT_QA_MODEL", raising=False)
        monkeypatch.delenv("AGENT_QA_PROVIDER", raising=False)
        sys.modules.pop("capable_core.config", None)
        from capable_core.config import AgentConfig

        # Instantiate directly to bypass .env file
        config = AgentConfig(
            _env_file=None,  # type: ignore[call-arg]
        )
        assert config.developer_model == ""
        assert config.developer_provider == ""
        assert config.qa_model == ""
        assert config.qa_provider == ""

    def test_settings_per_role_overrides_from_env(self, monkeypatch: pytest.MonkeyPatch, _fresh_config: Any) -> None:
        """Per-role model overrides should be read from env vars."""
        monkeypatch.setenv("AGENT_DEVELOPER_MODEL", "gpt-4o")
        monkeypatch.setenv("AGENT_DEVELOPER_PROVIDER", "litellm")
        monkeypatch.setenv("AGENT_QA_MODEL", "claude-sonnet")
        monkeypatch.setenv("AGENT_QA_PROVIDER", "claude")
        sys.modules.pop("capable_core.config", None)
        from capable_core.config import Settings

        s = Settings()
        assert s.agent.developer_model == "gpt-4o"
        assert s.agent.developer_provider == "litellm"
        assert s.agent.qa_model == "claude-sonnet"
        assert s.agent.qa_provider == "claude"

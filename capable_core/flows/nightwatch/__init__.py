"""
Nightwatch Workflow - Autonomous Issue Resolution System.

Orchestrates the Tech Lead → Dev Squad → QA Architect pipeline.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog


log = structlog.get_logger()


@dataclass
class WorkflowConfig:
    """Configuration for the Nightwatch workflow."""

    repo_name: str
    max_dev_iterations: int = 3
    max_qa_iterations: int = 2
    ci_timeout_seconds: int = 600
    ci_poll_interval: int = 30
    min_coverage: float = 80.0
    min_mutation_score: float = 60.0


@dataclass
class WorkflowResult:
    """Result of a Nightwatch workflow execution."""

    success: bool
    status: str
    pr_number: int | None = None
    pr_url: str | None = None
    issue_number: int | None = None
    iterations: int = 0
    duration_seconds: float = 0
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


class NightwatchWorkflow:
    """
    The Nightwatch Workflow orchestrates the full autonomous development pipeline.

    Steps:
    1. Tech Lead checks inbox for assigned issues
    2. Tech Lead delegates to Dev Squad
    3. Developer implements fix with self-correction loop
    4. Developer creates PR and monitors CI
    5. QA Architect verifies quality
    6. Tech Lead performs final staleness check
    7. PR flagged for human review

    Usage:
        workflow = NightwatchWorkflow(config)
        result = workflow.execute()
    """

    def __init__(self, config: WorkflowConfig):
        """Initialize the Nightwatch workflow with the given configuration."""
        self.config = config
        self.state: dict[str, Any] = {
            "repo_name": config.repo_name,
            "started_at": None,
            "current_step": 1,
            "iterations": 0,
        }

    def execute(self, issue_number: int | None = None) -> WorkflowResult:
        """
        Execute the Nightwatch workflow.

        Args:
            issue_number: Optional specific issue to fix.
                         If None, scans inbox for assigned issues.

        Returns:
            WorkflowResult with execution details.
        """
        from capable_core.agents.agent import root_agent

        self.state["started_at"] = datetime.now()
        self.state["issue_number"] = issue_number

        log.info("workflow_started", repo=self.config.repo_name, issue=issue_number)

        try:
            # Execute with root agent (Tech Lead)
            mission = self._create_mission_prompt(issue_number)
            result = root_agent.run(mission)
            return self._parse_result(result)

        except Exception as e:
            log.error("workflow_failed", error=str(e))
            return WorkflowResult(success=False, status="error", error=str(e), duration_seconds=self._get_duration())

    def _create_mission_prompt(self, issue_number: int | None) -> str:
        """Creates the mission prompt for Tech Lead."""
        if issue_number:
            return f"""
Mission Time: {datetime.now().isoformat()}
Target Repository: {self.config.repo_name}
Specific Issue: #{issue_number}

ORDERS:
1. Read issue #{issue_number}
2. Delegate fix to dev_squad
3. Ensure quality gates are met
4. Report when complete
"""
        else:
            return f"""
Mission Time: {datetime.now().isoformat()}
Target Repository: {self.config.repo_name}

ORDERS:
1. Check inbox for assigned issues
2. If found, fix the highest priority one
3. Ensure quality gates are met
4. Report when complete
"""

    def _parse_result(self, result: str) -> WorkflowResult:
        """Parses the Tech Lead's output into WorkflowResult."""
        duration = self._get_duration()

        if "MISSION_STATUS: COMPLETE" in result:
            return WorkflowResult(success=True, status="complete", duration_seconds=duration, details={"raw_output": result})
        elif "Inbox Zero" in result:
            return WorkflowResult(success=True, status="idle", duration_seconds=duration, details={"raw_output": result})
        elif "MISSION_STATUS: FAILED" in result:
            return WorkflowResult(success=False, status="failed", duration_seconds=duration, details={"raw_output": result})
        else:
            return WorkflowResult(success=False, status="unknown", duration_seconds=duration, details={"raw_output": result})

    def _get_duration(self) -> float:
        """Calculate workflow duration in seconds."""
        if self.state.get("started_at"):
            return (datetime.now() - self.state["started_at"]).total_seconds()
        return 0


__all__ = ["NightwatchWorkflow", "WorkflowConfig", "WorkflowResult"]

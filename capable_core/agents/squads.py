"""
Squad Utilities - Termination conditions and state helpers.

The main squad creation is now in agents/agent.py for ADK CLI compatibility.
"""

from typing import Any, ClassVar

import structlog


log = structlog.get_logger()


# =============================================================================
# TERMINATION CONDITIONS
# =============================================================================


def dev_loop_termination(state: dict[str, Any]) -> bool:
    """Termination condition for the Developer self-correction loop (Step 6a/6b)."""
    if state.get("local_tests_passed") and state.get("ready_for_pr"):
        log.info("dev_loop_terminated", reason="tests_passed")
        return True

    iterations = state.get("dev_iterations", 0)
    if iterations >= 3:
        log.warning("dev_loop_terminated", reason="max_iterations", iterations=iterations)
        return True

    return False


def ci_loop_termination(state: dict[str, Any]) -> bool:
    """Termination condition for the CI monitoring loop (Step 8a/8b)."""
    ci_status = state.get("ci_status", "").lower()

    if ci_status == "success":
        log.info("ci_loop_terminated", reason="ci_passed")
        return True

    if ci_status == "failure":
        retries = state.get("ci_retries", 0)
        if retries >= 2:
            log.warning("ci_loop_terminated", reason="ci_max_retries")
            return True

    return False


def qa_loop_termination(state: dict[str, Any]) -> bool:
    """Termination condition for the QA verification loop (Step 11a/11b)."""
    if state.get("qa_approved"):
        log.info("qa_loop_terminated", reason="qa_approved")
        return True

    iterations = state.get("qa_iterations", 0)
    if iterations >= 2:
        log.warning("qa_loop_terminated", reason="max_iterations")
        return True

    return False


# =============================================================================
# BACKWARD COMPATIBILITY EXPORTS
# =============================================================================


def get_configured_tech_lead() -> Any:
    """DEPRECATED: Use `from .agent import root_agent` instead."""
    from .agent import root_agent

    return root_agent


def create_dev_qa_squad(max_iterations: int = 3) -> Any:
    """DEPRECATED: Use `from .agent import create_root_agent` instead."""
    from .agent import create_root_agent

    return create_root_agent()


# Re-export dev_squad for backward compatibility
def _get_dev_squad():
    from .agent import root_agent

    return root_agent


dev_squad = property(lambda self: _get_dev_squad())


# =============================================================================
# STATE MACHINE HELPERS
# =============================================================================


class WorkflowState:
    """Manages state transitions through the workflow."""

    STEPS: ClassVar[dict[int, str]] = {
        1: "inbox_check",
        2: "mission_prep",
        3: "delegation",
        4: "dev_read_code",
        5: "dev_implement",
        6: "dev_local_test",
        7: "dev_create_pr",
        8: "dev_monitor_ci",
        9: "qa_read_pr",
        10: "qa_run_tests",
        11: "qa_report",
        12: "staleness_check",
        13: "final_decision",
    }

    def __init__(self):
        """Initialize workflow state tracker at step 1."""
        self.current_step: int = 1
        self.step_history: list = []
        self.state_data: dict[str, Any] = {}

    def advance(self) -> None:
        """Move to next step."""
        self.step_history.append(self.current_step)
        self.current_step += 1

    def go_back_to(self, step: int) -> None:
        """Return to a previous step (for loops)."""
        self.step_history.append(f"back_to_{step}")
        self.current_step = step

    def get_step_name(self) -> str:
        """Get current step name."""
        return self.STEPS.get(self.current_step, "unknown")

    def to_dict(self) -> dict[str, Any]:
        """Export state for context.state."""
        return {
            "current_step": self.current_step,
            "step_name": self.get_step_name(),
            "step_history": self.step_history,
            **self.state_data,
        }

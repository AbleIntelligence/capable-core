"""
CapeAble Core Foundry Agents Package.

Hierarchical Agent Tree following Google ADK patterns.

ADK CLI Discovery:
    This package exposes `root_agent` for ADK CLI commands:
    - `adk web agents/`  - Launch interactive web UI
    - `adk run agents/`  - Run agent from command line

    Modes (via environment variable):
    - Sequential (default): FOUNDRY_PARALLEL_MODE=false - one issue at a time
    - Parallel: FOUNDRY_PARALLEL_MODE=true FOUNDRY_MAX_WORKERS=3 - multiple issues simultaneously
"""

# ADK CLI Discovery - REQUIRED for `adk web` and `adk run`
from . import agent

# Export root_agent at package level for convenience
from .agent import get_parallel_agent, root_agent

# Export sub-agents for direct use
from .developer import create_developer_agent, developer_agent

# Parallel execution support
from .parallel_squads import (
    ParallelOrchestrator,
    create_issue_worker,
    create_parallel_sdlc_team,
    create_parallel_tech_lead,
)
from .qa_architect import create_qa_architect_agent, qa_architect_agent


# Backward compatibility exports
tech_lead = root_agent

__all__ = [
    "ParallelOrchestrator",
    # ADK Entry Points
    "agent",
    "create_developer_agent",
    "create_issue_worker",
    "create_parallel_sdlc_team",
    # Parallel execution
    "create_parallel_tech_lead",
    "create_qa_architect_agent",
    # Sub-agents
    "developer_agent",
    "get_parallel_agent",
    "qa_architect_agent",
    "root_agent",
    # Backward compatibility
    "tech_lead",
]

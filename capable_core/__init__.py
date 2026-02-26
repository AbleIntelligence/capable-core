"""
CapAble-Core - Autonomous Multi-Agent Development System.

========================================================

A production-ready multi-agent system using Google's Agent Development Kit (ADK)
that automates the developer-QA workflow.

Architecture:
    Tech Lead Agent (Root)
        ├── Dev Squad (LoopAgent)
        │   ├── Developer Agent
        │   └── QA Engineer
        └── QA Architect Agent

Usage:
    from capable_core import NightwatchWorkflow, WorkflowConfig

    config = WorkflowConfig(repo_name="owner/repo")
    workflow = NightwatchWorkflow(config)
    result = workflow.execute()

CLI:
    python run.py --repo "owner/repo"
"""

__version__ = "0.2.0"
__author__ = "CapAble Core Foundry Team"

from capable_core.flows.nightwatch import NightwatchWorkflow, WorkflowConfig, WorkflowResult


__all__ = [
    "NightwatchWorkflow",
    "WorkflowConfig",
    "WorkflowResult",
    "__version__",
]

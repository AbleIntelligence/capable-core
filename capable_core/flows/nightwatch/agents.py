"""
Nightwatch Agents - Re-export from agents package.

DEPRECATED: Import directly from capable_core.agents module instead.

    from capable_core.agents.agent import root_agent
"""

from typing import Any

from capable_core.agents.agent import root_agent
from capable_core.agents.developer import create_developer_agent, developer_agent
from capable_core.agents.qa_architect import create_qa_architect_agent, qa_architect_agent


# Backward compatibility aliases
tech_lead = root_agent


def get_configured_tech_lead() -> Any:
    """Return the configured root Tech Lead agent."""
    return root_agent


__all__ = [
    "create_developer_agent",
    "create_qa_architect_agent",
    "developer_agent",
    "get_configured_tech_lead",
    "qa_architect_agent",
    "root_agent",
    "tech_lead",
]

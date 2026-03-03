#!/usr/bin/env python3
"""
CapAble-Core CLI wrappers for ADK web and API server.

Thin entry points that delegate to the ADK CLI with the correct
subcommand and a sensible default AGENTS_DIR, so users can simply run:

    capable-web              # launches Web UI for capable_core agents
    capable-api              # launches REST API for capable_core agents

All extra flags (--port, --host, --reload, etc.) are forwarded to ADK.
An explicit AGENTS_DIR can still be passed to override the default.
"""

from __future__ import annotations

import sys
from pathlib import Path

from google.adk.cli.cli_tools_click import main as adk_cli


# Absolute path to the capable_core package directory.
# ADK will discover agent sub-packages (e.g. agents/) inside it.
_PACKAGE_DIR = str(Path(__file__).resolve().parent)


def _set_default_agents_dir(subcommand: str) -> None:
    """Point the AGENTS_DIR Click argument at the installed package."""
    import click  # imported here to avoid top-level dep on click internals

    ctx = click.Context(adk_cli)
    cmd = adk_cli.get_command(ctx, subcommand)
    if cmd is None:
        return
    for param in cmd.params:
        if param.name == "agents_dir":
            param.default = _PACKAGE_DIR
            break


def web() -> None:
    """Launch the ADK Web UI for CapAble-Core agents.

    All arguments are forwarded to ``adk web``.
    Run ``capable-web --help`` for full usage.
    """
    _set_default_agents_dir("web")
    adk_cli(["web", *sys.argv[1:]])


def api_server() -> None:
    """Launch the ADK API server for CapAble-Core agents.

    All arguments are forwarded to ``adk api_server``.
    Run ``capable-api --help`` for full usage.
    """
    _set_default_agents_dir("api_server")
    adk_cli(["api_server", *sys.argv[1:]])

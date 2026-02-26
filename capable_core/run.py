#!/usr/bin/env python3
"""
CapAble core - Nightwatch Runner.

=======================================

Main entry point for the autonomous multi-agent development system.
Triggers the Tech Lead to scan for issues and orchestrate the Dev Squad.

Usage:
    foundry-run --repo "owner/repo"
    foundry-run --repo "owner/repo" --issue 123
    foundry-run --repo "owner/repo" --dry-run

Environment Variables:
    GITHUB_TOKEN: GitHub Personal Access Token (required)
    GOOGLE_API_KEY: Google AI API Key (required)
    AGENT_DEV_MAX_ITERATIONS: Max dev retry loops (default: 3)
"""

import argparse
import sys
from datetime import datetime

import structlog

# Load environment variables from .env file
from dotenv import load_dotenv

from capable_core.config import settings, validate_environment
from capable_core.flows.nightwatch import NightwatchWorkflow, WorkflowConfig


load_dotenv()

# Configure structured logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.dev.ConsoleRenderer(colors=True),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

log = structlog.get_logger()


# =============================================================================
# RUNNER
# =============================================================================


def run_nightwatch(
    repo_name: str,
    issue_number: int | None = None,
    dry_run: bool = False,
    verbose: bool = False,
) -> dict:
    """Execute the Nightwatch workflow via the NightwatchWorkflow orchestrator.

    Args:
        repo_name: Target repository (owner/repo format).
        issue_number: Optional specific issue to fix.
        dry_run: If True, only validate without executing.
        verbose: Enable verbose logging.

    Returns:
        Dict with execution results.
    """
    log.info("nightwatch_starting", repo=repo_name, issue=issue_number, dry_run=dry_run)

    config = WorkflowConfig(
        repo_name=repo_name,
        max_dev_iterations=settings.agent.dev_max_iterations,
        max_qa_iterations=settings.agent.qa_max_iterations,
        ci_timeout_seconds=settings.agent.ci_timeout,
        ci_poll_interval=settings.agent.ci_poll_interval,
        min_coverage=settings.agent.min_coverage_percent,
    )
    workflow = NightwatchWorkflow(config)

    if dry_run:
        mission = workflow._create_mission_prompt(issue_number)
        log.info("dry_run_mode", mission=mission)
        return {"status": "dry_run", "mission": mission, "would_execute": True}

    result = workflow.execute(issue_number=issue_number)

    if result.success:
        log.info("nightwatch_complete", status=result.status, duration=result.duration_seconds)
    else:
        log.error("nightwatch_failed", status=result.status, error=result.error)

    return {
        "status": result.status,
        "success": result.success,
        "result": result.details.get("raw_output", ""),
        "repo": repo_name,
        "issue": result.issue_number or issue_number,
        "pr_url": result.pr_url,
        "duration": result.duration_seconds,
        "error": result.error,
    }


# =============================================================================
# CLI
# =============================================================================


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="CapAble core - Nightwatch Autonomous Development System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  capable-run --repo "my-org/backend"
      Scan for assigned issues and fix the highest priority one

  capable-run --repo "my-org/backend" --issue 123
      Fix a specific issue

  capable-run --repo "my-org/backend" --dry-run
      Validate configuration without executing

Environment Variables:
  GITHUB_TOKEN         GitHub Personal Access Token (required)
  GOOGLE_API_KEY       Google AI API Key (required unless using Vertex AI)
  GOOGLE_PROJECT_ID    GCP Project ID (for Vertex AI)
""",
    )

    parser.add_argument("--repo", "-r", type=str, required=True, help="Target GitHub repository (owner/repo format)")

    parser.add_argument("--issue", "-i", type=int, default=None, help="Specific issue number to fix (optional)")

    parser.add_argument("--dry-run", "-d", action="store_true", help="Validate configuration without executing")

    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")

    args = parser.parse_args()

    # Validate environment
    errors = validate_environment()
    if errors:
        print("❌ Configuration Error:")
        for error in errors:
            print(f"   - {error}")
        print("\nPlease set required environment variables in .env file.")
        sys.exit(1)

    print(f"""
╔══════════════════════════════════════════════════════════════╗
║           CAPABLE-CORE SYSTEM - NIGHTWATCH SYSTEM           ║
║                  Autonomous Development                     ║
╚══════════════════════════════════════════════════════════════╝

🎯 Target: {args.repo}
📋 Issue: {f"#{args.issue}" if args.issue else "Auto-detect from inbox"}
🔧 Mode: {"Dry Run" if args.dry_run else "LIVE EXECUTION"}
⏰ Started: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
""")

    # Execute
    result = run_nightwatch(repo_name=args.repo, issue_number=args.issue, dry_run=args.dry_run, verbose=args.verbose)

    # Report results
    print("\n" + "=" * 60)

    if result.get("success") or result["status"] in ("complete", "idle"):
        print("✅ MISSION COMPLETE")
        print("\n📄 Agent Response:")
        print("-" * 40)
        print(result.get("result", "No response"))
        if result.get("pr_url"):
            print(f"\n🔗 PR: {result['pr_url']}")
        if result.get("duration"):
            print(f"⏱  Duration: {result['duration']:.1f}s")
    elif result["status"] == "dry_run":
        print("🔍 DRY RUN COMPLETE")
        print("\n📋 Would execute mission:")
        print("-" * 40)
        print(result.get("mission", ""))
    else:
        print("💀 MISSION FAILED")
        print(f"\n❌ Error: {result.get('error', 'Unknown error')}")
        sys.exit(1)


if __name__ == "__main__":
    main()

"""
Parallel Squad Architecture for CapeAble Core Foundry.

===================================================

This module implements parallel issue processing where multiple issues
can be worked on simultaneously by separate Developer+QA worker pairs.

Architecture:
    Tech Lead (Dispatcher)
        │
        ├─► Reads all assigned issues
        │
        └─► Spawns parallel IssueWorkers
                │
                ├── Worker 1: Developer → QA (Issue #101)
                ├── Worker 2: Developer → QA (Issue #102)
                └── Worker 3: Developer → QA (Issue #103)

Usage:
    from capable_core.agents.parallel_squads import create_parallel_tech_lead

    # Create with max 3 parallel workers
    tech_lead = create_parallel_tech_lead(max_parallel_workers=3)
"""

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Any

import structlog
from google.adk import Agent
from google.adk.agents import ParallelAgent
from google.adk.planners import BuiltInPlanner
from google.genai import types

from capable_core.config import settings
from capable_core.tools.github_tools import (
    add_issue_comment,
    add_pr_comment,
    build_env_from_github,
    get_directory_tree,
    get_env_template,
    get_file_content,
    get_issue_content,
    get_my_assigned_issues,
    get_pr_details,
    get_repo_secrets_list,
    get_repo_variables,
    push_files_to_branch,
)
from capable_core.tools.sandbox_tools import (
    lint_code_on_branch,
    run_tests_on_branch,
)

from .developer import create_developer_agent
from .qa_architect import create_qa_architect_agent


log = structlog.get_logger()


# =============================================================================
# DATA STRUCTURES
# =============================================================================


class IssueStatus(Enum):
    """Status of an issue in the parallel pipeline."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    PR_CREATED = "pr_created"
    QA_IN_PROGRESS = "qa_in_progress"
    QA_PASSED = "qa_passed"
    QA_FAILED = "qa_failed"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class IssueTask:
    """Represents an issue to be processed by a worker."""

    issue_number: int
    repo_name: str
    title: str
    priority: str = "normal"
    status: IssueStatus = IssueStatus.PENDING
    pr_number: int | None = None
    pr_url: str | None = None
    worker_id: str | None = None
    error: str | None = None
    retries: int = 0
    max_retries: int = 3


@dataclass
class WorkerResult:
    """Result from a worker processing an issue."""

    issue_number: int
    success: bool
    pr_number: int | None = None
    pr_url: str | None = None
    error: str | None = None
    qa_passed: bool = False
    coverage: float | None = None


# =============================================================================
# ISSUE WORKER - Handles one issue end-to-end (Developer → QA)
# =============================================================================

ISSUE_WORKER_PROMPT = """
You are an Issue Worker - a SUPERVISOR that manages Developer and QA sub-agents for ONE issue.

## YOUR IDENTITY
You are: {worker_name}
Your Developer: {developer_name}
Your QA: {qa_name}

## YOUR ROLE: SUPERVISOR & COACH
- You delegate work to your Developer and QA
- You can HELP if Developer struggles (investigate, provide guidance, allow retries)
- You do NOT write code yourself - you guide and coach
- Maximum 3 attempts before escalating to Tech Lead

## FIRST: CHECK YOUR ASSIGNMENT
Look in state for your assignment key: `state['{assignment_key}']`
- If MISSING or empty → Output the IDLE result below, then CALL `transfer_to_agent(agent_name='Parallel_Tech_Lead')` and STOP:
```
══════════════════════════════════════════════════════════════
RESULT FROM {worker_name}:
STATUS: WORKER_IDLE
REASON: No issue assigned to this worker
══════════════════════════════════════════════════════════════
```
- If has issue data → Proceed with workflow

**Your assignment may include `env_config`** - this contains:
- `from_secrets`: Environment variables that map to GitHub secrets (for DB, API keys, etc.)
- `from_variables`: Environment variables with direct values
- Pass this to Developer/QA so they can run tests that need database/external services

## STATE ISOLATION - CRITICAL!
**You can ONLY access YOUR assignment key: `state['{assignment_key}']`**
- NEVER read other workers' state keys (issue_for_worker_2, issue_for_worker_3, etc.)
- NEVER process issues assigned to other workers
- Your issue is ONLY in `state['{assignment_key}']`

## WORKFLOW

### Phase 0: Verify Issue Has No PR Yet (CRITICAL!)
**BEFORE delegating to Developer, check if this issue already has a PR!**

1. Use `get_issue_content` to read the issue
2. Look in the issue body/comments for:
   - PR links ("PR #123", "fixes #N in PR", etc.)
   - "DEVELOPMENT_COMPLETE" comments from other developers
   - Branch references like "fix-issue-XXX"

**If issue already has a PR:**
```
══════════════════════════════════════════════════════════════
RESULT FROM {worker_name}:
STATUS: WORKER_SKIP
ISSUE: #<number>
REASON: Already has PR (found: <PR link or reference>)
══════════════════════════════════════════════════════════════
```
Then CALL the function (don't just write it!): `transfer_to_agent(agent_name='Parallel_Tech_Lead')`

**If no PR exists:** Proceed to Phase 1

### Phase 1: Delegate to Developer
**Pass the env_config from your assignment to Developer!**
Include in your delegation message:
```
MISSION: <issue details>
ENV_CONFIG: <from state['{assignment_key}'].env_config>
- Secrets available: <list secret names>
- Variables: <list variable values>
Use these when running tests that need database/external services.
```

Call: `transfer_to_agent(agent_name='{developer_name}')`

**AFTER DEVELOPER RETURNS - CHECK STATUS:**

**Case A: DEVELOPMENT_COMPLETE** → Go to Phase 2 (QA)

**Case B: DEVELOPMENT_BLOCKED** → Go to Phase 1.5 (Coach & Retry)

### Phase 1.5: Coach & Retry (if Developer blocked)
**You have tools to investigate! Use them to help Developer.**

1. **Investigate the problem:**
   - Use `get_file_content` to read relevant files
   - Use `get_directory_tree` to understand structure
   - Understand WHY Developer failed

2. **Provide guidance:**
   Output specific coaching for Developer:
   ```
   COACHING_DEVELOPER:
   - Problem identified: <what you found>
   - Suggestion: <specific guidance>
   - Files to focus on: <list>
   - Retry attempt: <N>/3
   ```

3. **Retry with Developer:**
   Call: `transfer_to_agent(agent_name='{developer_name}')` again

4. **After 3 failed attempts:** Stop retrying, go to Phase 3 with ESCALATE status

### Phase 2: Delegate to QA (only if Developer succeeded)
**Pass the env_config to QA as well!** They need it for test execution.
```
PR_TO_TEST: #<pr_number>
ENV_CONFIG: <from state['{assignment_key}'].env_config>
```

Call: `transfer_to_agent(agent_name='{qa_name}')`

### Phase 3: Report & Return to Tech Lead
**CRITICAL: `transfer_to_agent` is a FUNCTION you must CALL, not text to write!**

**Your output will be seen by Tech Lead. Use this EXACT format with your worker name!**

**On SUCCESS:**
```
══════════════════════════════════════════════════════════════
RESULT FROM {worker_name}:
STATUS: WORKER_COMPLETE
ISSUE: #<number>
PR_NUMBER: #<pr_number>
PR_URL: <pr_url>
QA_STATUS: PASSED/FAILED
ATTEMPTS: <N>
══════════════════════════════════════════════════════════════
```
Then CALL the function: `transfer_to_agent(agent_name='Parallel_Tech_Lead')`

**On FAILURE (after retries exhausted):**
```
══════════════════════════════════════════════════════════════
RESULT FROM {worker_name}:
STATUS: WORKER_ESCALATE
ISSUE: #<number>
ERROR: BLOCKED_AFTER_RETRIES
- Attempts: 3
- Last Error: <from Developer>
- Investigation: <what you found>
- Suggestion for Tech Lead: <your recommendation>
```
Then CALL the function: `transfer_to_agent(agent_name='Parallel_Tech_Lead')`

**IMPORTANT:**
- `transfer_to_agent` is a TOOL/FUNCTION - you must INVOKE it like any other tool!
- Do NOT just write "transfer_to_agent(...)" as text - that does nothing!
- Without CALLING this function, control will NOT return to Tech Lead!

## AVAILABLE TOOLS (for investigation only)
- `get_file_content(repo_name, file_path, ref)` - Read files to understand the problem
- `get_directory_tree(repo_name, path, ref)` - Explore repo structure
- `get_issue_content(repo_name, issue_number)` - Re-read issue details

## STRICT RULES
1. **Work on ONLY your assigned issue** - `state['{assignment_key}']`
2. **Never touch other workers' issues** - even if you see them in state
3. **Max 3 Developer attempts** - then escalate, don't loop forever
4. **Coach, don't code** - provide guidance, let Developer do the work
5. **ALWAYS CALL transfer_to_agent at the end** - it's a FUNCTION, not text!
   - Don't write "transfer_to_agent(...)" - INVOKE IT as a tool!
   - Without calling it, you will be stuck and Tech Lead won't get results!
"""


def create_issue_worker(
    worker_id: str,
    model: str | None = None,
    developer_model: str | None = None,
    qa_model: str | None = None,
    provider_type: str | None = None,
) -> Agent:
    """
    Creates an Issue Worker agent that handles one issue end-to-end.

    Each worker has its own Developer and QA sub-agents to avoid
    state conflicts when running in parallel.

    Args:
        worker_id: Unique identifier for this worker (e.g., "worker-1")
        model: Model for the worker coordinator (defaults to config fast_model)
        developer_model: Model for Developer (defaults to config model_name)
        qa_model: Model for QA (defaults to config fast_model)
        provider_type: Model provider (defaults to config provider_type)

    Returns:
        Configured Issue Worker agent
    """
    cfg = settings.agent
    model = model or cfg.fast_model
    provider = (provider_type or cfg.provider_type).lower()

    # Worker's name that sub-agents will transfer back to
    worker_name = f"IssueWorker_{worker_id}"
    developer_name = f"Developer_{worker_id}"
    qa_name = f"QA_Architect_{worker_id}"

    # Each worker gets its own Developer and QA instances
    # Per-role model/provider resolved inside their factories when not overridden here
    developer = create_developer_agent(
        model=developer_model,
        parent_agent_name=worker_name,
        name=developer_name,
        provider_type=provider_type,
    )
    qa_architect = create_qa_architect_agent(
        model=qa_model,
        parent_agent_name=worker_name,
        name=qa_name,
        provider_type=provider_type,
    )

    # Format prompt with this worker's names and assignment key
    assignment_key = f"issue_for_{worker_id}"
    worker_prompt = ISSUE_WORKER_PROMPT.format(
        assignment_key=assignment_key,
        worker_name=worker_name,
        developer_name=developer_name,
        qa_name=qa_name,
    )

    # IssueWorker gets READ-ONLY tools for investigation/coaching
    # These help it understand problems and guide Developer on retries
    investigation_tools = [
        get_file_content,  # Read files to understand the problem
        get_directory_tree,  # Explore repo structure
        get_issue_content,  # Re-read issue details if needed
    ]

    worker = Agent(
        name=worker_name,
        model=model,
        **(
            {"planner": BuiltInPlanner(thinking_config=types.ThinkingConfig(thinking_level="medium", include_thoughts=True))}
            if provider == "gemini"
            else {}
        ),
        instruction=worker_prompt,
        tools=investigation_tools,
        sub_agents=[developer, qa_architect],
        output_key=f"worker_{worker_id}_result",
    )

    log.info("issue_worker_created", worker_id=worker_id, model=model, tools=len(investigation_tools))
    return worker


# =============================================================================
# PARALLEL TECH LEAD - Dispatcher that manages parallel workers
# =============================================================================

PARALLEL_TECH_LEAD_PROMPT = """
You are the Autonomous Parallel Tech Lead for an autonomous development team.
You orchestrate PARALLEL development workflow, manage multiple squads, and ensure code quality.

## IDENTITY
- Name: Parallel Tech Lead Agent
- Role: Swarm Manager & Quality Gate (PARALLEL MODE)
- Authority: Final decision on merge readiness
- Philosophy: "Ship fast, but never ship broken code. Be strict - mediocre code creates technical debt."
- Style: **STRICT but constructive** - Don't approve subpar code. Give detailed feedback.
- Capability: Process up to {max_workers} issues in PARALLEL

## YOUR TEAM (Parallel Workers)
You have {max_workers} Issue Workers available. Each worker is a complete squad:
- Each Worker has: Developer (coding/PR) + QA_Architect (testing)
- Workers run in PARALLEL - all issues processed simultaneously
- Workers: {worker_names}

## AVAILABLE TOOLS

### Inbox Management
- `get_my_assigned_issues(repo_name)` - Check for assigned issues
- `get_issue_content(repo_name, issue_number)` - Read issue details

### Parallel Worker Management
- Call `transfer_to_agent(agent_name='ParallelWorkers')` to dispatch all workers
- Workers: {worker_names}

### PR Management (for final review)
- `get_pr_details(repo_name, pr_number)` - Check PR status, conflicts, CI results, and list of changed files
- `get_file_content(repo_name, file_path, ref)` - Read actual file content from PR branch for code review
- `add_pr_comment(repo_name, pr_number, comment)` - Comment on PRs
- `add_issue_comment(repo_name, issue_number, comment)` - Update issues

### Code Exploration & Direct Actions (Optional)
- `get_directory_tree(repo_name, path, ref)` - Explore repo structure
- `push_files_to_branch(repo_name, branch_name, file_changes, commit_message)` - Push quick fixes directly
- `run_tests_on_branch(repo_name, branch_name, test_command, docker_image, setup_commands)` - Run tests on a branch
- `lint_code_on_branch(repo_name, branch_name, lint_command, docker_image, setup_commands)` - Check code style

### Environment & Secrets Discovery (IMPORTANT for tests!)
- `get_env_template(repo_name, ref)` - Read .env.example to see what env vars are needed
- `get_repo_secrets_list(repo_name)` - List available GitHub secrets (names only, values are never exposed)
- `get_repo_variables(repo_name)` - List GitHub variables with their values
- `build_env_from_github(repo_name, ref)` - Build env config mapping .env.example to available secrets/variables

**Use these ONCE at the start** to build an env_config that workers need for running tests!

## EXECUTION PROTOCOL

### Step 1: Check Inbox & Discover Issues
1. Call `get_my_assigned_issues(repo_name)` to scan for work
2. If no issues: Reply "Inbox Zero. Standing by." and terminate
3. If issues exist: Prioritize by labels (P0 > P1 > P2 > unlabeled)
4. **CRITICAL: Filter out already-handled issues!**
   - Skip any issue that already has a linked PR (check issue body/comments)
   - Skip any issue you've already dispatched to a worker this session
   - Only dispatch NEW issues that haven't been worked on

### Step 1.5: Gather Environment Configuration (DO THIS ONCE!)
**Before assigning issues, get the env config that workers need for testing.**

1. Call `build_env_from_github(repo_name)` to build the environment mapping
   - This reads .env.example and maps vars to available GitHub secrets/variables
   - Includes: DB credentials, API keys, external service configs
2. Store this as `env_config` to include in EVERY worker assignment

**Why?** Developer and QA need this to run tests that require database connections,
external APIs, or other services. Without env_config, tests may fail!

### Step 2: Prepare Mission Briefs & Assign to Workers
For each issue (up to {max_workers} at a time):
1. Call `get_issue_content` to get full context
2. **CHECK FOR EXISTING PR FIRST!** Look for:
   - PR links in issue body ("PR #123", "closes #N")
   - Comments mentioning PR was created
   - If PR exists → DO NOT assign this issue, skip it!
3. Extract problem description, expected behavior, acceptance criteria
4. **IMPORTANT: Assign issues to workers by setting state keys**

**ASSIGNMENT MECHANISM:**
Before dispatching, you MUST assign issues to workers by outputting:
```
ASSIGNING_ISSUES_TO_STATE:
- state['issue_for_worker_1'] = {{
    issue: #101,
    repo: 'owner/repo',
    title: '...',
    env_config: {{  // <-- INCLUDE THIS!
      from_secrets: {{'DATABASE_URL': 'GITHUB_SECRET:DATABASE_URL', ...}},
      from_variables: {{'API_BASE_URL': 'https://api.example.com', ...}}
    }}
  }}
- state['issue_for_worker_2'] = {{issue: #102, repo: '...', env_config: {{...}}}}
```

**env_config tells workers which secrets/variables to use for tests!**

Workers WITHOUT an assignment will automatically skip (output WORKER_IDLE).
This means if you have 2 issues and 3 workers, only 2 workers will do actual work.

**CRITICAL: CREATE AN ASSIGNMENT TRACKING TABLE!**
You MUST output this table and remember it for Step 3:
```
═══════════════════════════════════════════════════════════════
MY_ASSIGNMENT_TRACKER (SAVE THIS FOR STEP 3!)
═══════════════════════════════════════════════════════════════
| Worker              | Issue Assigned | State Key to Check        |
|---------------------|----------------|---------------------------|
| IssueWorker_worker_1| Issue #101     | worker_worker_1_result    |
| IssueWorker_worker_2| Issue #13      | worker_worker_2_result    |
| IssueWorker_worker_3| (none - idle)  | worker_worker_3_result    |
═══════════════════════════════════════════════════════════════
```

Then call `transfer_to_agent(agent_name='ParallelWorkers')` to run all workers simultaneously.

### Step 3: Collect Worker Results

After ParallelWorkers returns, check the conversation for results from each worker.

Each worker outputs a result in this format:
```
══════════════════════════════════════════════════════════════
RESULT FROM IssueWorker_worker_N:
STATUS: WORKER_COMPLETE / WORKER_FAILED / WORKER_IDLE / WORKER_SKIP
...
══════════════════════════════════════════════════════════════
```

**If you don't see a result from a worker yet:**
- The worker is still working - just wait
- Do NOT assume failure or try workarounds
- Say "Waiting for workers to complete..." and stop

**Once you have results from ALL workers**, record them:
```
WORKER RESULTS:
| Worker | Status | PR # |
|--------|--------|------|
| ...    | ...    | ...  |
```

Then proceed to Step 4.

### Step 4: Final Review - YOU MUST CALL THE TOOLS!
**IMPORTANT: This step is YOUR responsibility as Tech Lead!**
**BE STRICT! Don't approve code that isn't production-ready.**
**ACTUALLY CALL THE TOOLS - don't just describe what you would do!**

**For EACH PR from WORKER_COMPLETE results, do this:**

**4.1. Call `get_pr_details(repo_name, pr_number)` NOW:**
```
Reviewing PR #<number>:
- Mergeable: <yes/no>
- CI Status: <passed/failed/pending>
- Changed Files: <list from tool response>
```

**4.2. Read the actual code - Call `get_file_content` for each changed file:**
For each file in the changed files list, CALL the tool:
```python
get_file_content(repo_name, "<file_path>", ref="<pr_branch>")
```

**4.3. Review the code you just read - check for:**
- Proper error handling (no silent failures, meaningful error messages)
- Type hints and docstrings (ALL functions must have them)
- Obvious bugs or anti-patterns (magic numbers, code duplication, etc.)
- Security issues (hardcoded secrets, SQL injection, XSS, etc.)
- Code readability (clear variable names, proper formatting)
- Proper logging and observability
- Edge case handling
- REDUNDANT code/files (unused imports, dead code, orphaned files)
- Old files that should have been deleted but weren't

**4.4. If you find issues - Call `add_pr_comment`:**
```python
add_pr_comment(repo_name, pr_number, "## Code Review Feedback\n\n<detailed issues>")
```

**Decision Matrix (BE STRICT!):**
- If QA PASSED + No Conflicts + CI Passed + **Code Quality EXCELLENT** → Mark as APPROVED
- If **ANY Code Quality Issues** → Leave PR comment with specific feedback, mark as NEEDS_WORK
- If Merge Conflicts → Mark as NEEDS_WORK
- If CI Failed → Mark as NEEDS_WORK

**DON'T BE LENIENT!** If you see missing type hints, poor error handling, no docstrings,
code smells, unclear variable names, redundant code → REJECT and leave feedback!

**TRACK YOUR REVIEWS:**
```
PR REVIEWS COMPLETED:
| PR #   | Issue # | Mergeable | CI    | Code Quality | Decision    |
|--------|---------|-----------|-------|--------------|-------------|
| PR #45 | #101    | Yes       | Pass  | Good         | ✅ APPROVED  |
| PR #46 | #13     | Yes       | Pass  | Issues found | ⚠️ NEEDS_WORK |
```

### Step 5: Report Summary

**⚠️ BEFORE WRITING THE REPORT, VERIFY YOUR DATA! ⚠️**

Copy and paste this verification checklist FIRST:
```
PRE-REPORT VERIFICATION:
═══════════════════════════════════════════════════════════════════════════════
[ ] I searched for "RESULT FROM IssueWorker_worker_1" and found: ____________
[ ] I searched for "RESULT FROM IssueWorker_worker_2" and found: ____________
[ ] I searched for "RESULT FROM IssueWorker_worker_3" and found: ____________

VERIFIED RESULTS:
| Worker   | Assigned Issue | STATUS Found        | PR # Found |
|----------|----------------|---------------------|------------|
| Worker 1 | Issue #___     | WORKER_________     | PR #___    |
| Worker 2 | Issue #___     | WORKER_________     | PR #___    |
| Worker 3 | (none)         | WORKER_IDLE         | N/A        |
═══════════════════════════════════════════════════════════════════════════════
```

**ONLY workers with STATUS: WORKER_FAILED or WORKER_ESCALATE are failures!**
**WORKER_COMPLETE = Success (has PR), WORKER_IDLE = Expected, WORKER_SKIP = Expected**

**NOW write the final report based on your VERIFIED data above:**
```
PARALLEL_SESSION_COMPLETE:
══════════════════════════════════════════════════════════════════════════════
ISSUE TRACKING:
- Issue #<N> (assigned to Worker 1): <COMPLETED with PR #X / FAILED / SKIPPED>
- Issue #<M> (assigned to Worker 2): <COMPLETED with PR #Y / FAILED / SKIPPED>

SUMMARY:
- Total Issues Assigned: <N>
- Successful (WORKER_COMPLETE): <N>
- Failed (WORKER_FAILED/ESCALATE only): <N>
- Skipped (WORKER_SKIP): <N>
- Idle Workers (no assignment): <N>

COMPLETED_PRS:
- PR #<num>: Issue #<issue> - ✅ Completed
- PR #<num>: Issue #<issue> - ✅ Completed

ACTUAL_FAILURES (only if WORKER_FAILED or WORKER_ESCALATE):
- Issue #<num>: <explicit error from worker>
══════════════════════════════════════════════════════════════════════════════
```

**REMINDER:** If a worker returned WORKER_COMPLETE with a PR number, that issue is DONE - include it in COMPLETED_PRS!

HUMAN_ACTION_REQUIRED:
- Please review and merge the following PRs: <list from COMPLETED_PRS above>

## PARALLEL EXECUTION RULES
1. Dispatch up to {max_workers} issues at once
2. Workers run independently - no shared state
3. **ParallelAgent WAITS for ALL workers** - when it returns, ALL are done
4. **Every worker outputs "RESULT FROM [worker_name]"** - search conversation for these!
5. **ONLY `STATUS: WORKER_FAILED` or `STATUS: WORKER_ESCALATE` means failure**
6. **WORKER_IDLE from Worker 3 does NOT mean Workers 1 & 2 failed!**
7. **YOU are the final quality gate** - check PR status yourself
8. Never merge without human approval
9. Never approve a PR with failing tests

## ⛔ FORBIDDEN PHRASES - NEVER SAY THESE:
- "Workers failed to provide results" ← WRONG! Results ARE in conversation!
- "No messages from Worker X" ← WRONG! Messages ARE there!
- "Worker X did not complete" ← WRONG unless STATUS says WORKER_FAILED!
- "Assuming failure" ← NEVER assume! Only explicit WORKER_FAILED = failure!

## HOW TO FIND WORKER RESULTS
Search the conversation for these EXACT strings:
```
"RESULT FROM IssueWorker_worker_1"  ← FIND THIS! It exists!
"RESULT FROM IssueWorker_worker_2"  ← FIND THIS! It exists!
"RESULT FROM IssueWorker_worker_3"  ← Usually WORKER_IDLE (no assignment)
```

Each result has a STATUS line - ONLY these matter:
- `STATUS: WORKER_COMPLETE` → Success! Has PR to review
- `STATUS: WORKER_FAILED` → Actual failure
- `STATUS: WORKER_ESCALATE` → Failed after 3 attempts
- `STATUS: WORKER_IDLE` → No issue assigned (expected for extra workers)
- `STATUS: WORKER_SKIP` → Issue already had PR

**You MUST find and copy the STATUS from each worker before concluding ANYTHING!**
"""


def create_parallel_tech_lead(
    model: str | None = None,
    developer_model: str | None = None,
    max_parallel_workers: int = 3,
    provider_type: str | None = None,
) -> Agent:
    """
    Creates a Tech Lead that can dispatch issues to parallel workers.

    Args:
        model: Model for Tech Lead coordinator (defaults to config model_name)
        developer_model: Model for Developer agents in workers (defaults to config model_name)
        max_parallel_workers: Maximum concurrent issue workers (default: 3)
        provider_type: Model provider (defaults to config provider_type)

    Returns:
        Configured Parallel Tech Lead agent
    """
    cfg = settings.agent
    model = model or cfg.model_name
    developer_model = developer_model or cfg.developer_model or cfg.model_name
    provider = (provider_type or cfg.provider_type).lower()

    # Create worker pool
    workers = []
    worker_names = []

    for i in range(max_parallel_workers):
        worker_id = f"worker_{i + 1}"
        issue_worker = create_issue_worker(
            worker_id=worker_id,
            model=cfg.fast_model,
            developer_model=developer_model,
            qa_model=cfg.fast_model,
            provider_type=provider,
        )
        workers.append(issue_worker)
        worker_names.append(f"IssueWorker_{worker_id}")

    # Create ParallelAgent that runs all workers simultaneously
    parallel_workers = ParallelAgent(
        name="ParallelWorkers",
        sub_agents=workers,
    )

    # Format prompt with worker info
    prompt = PARALLEL_TECH_LEAD_PROMPT.format(
        max_workers=max_parallel_workers,
        worker_names=", ".join(worker_names),
    )

    # Tech Lead tools
    tools = [
        get_my_assigned_issues,
        get_issue_content,
        get_pr_details,
        get_file_content,
        get_directory_tree,
        push_files_to_branch,
        add_pr_comment,
        add_issue_comment,
        run_tests_on_branch,
        lint_code_on_branch,
        # Environment & secrets discovery
        get_repo_secrets_list,
        get_repo_variables,
        get_env_template,
        build_env_from_github,
    ]

    # Build planner conditionally based on provider
    planner = None
    if provider == "gemini":
        planner = BuiltInPlanner(thinking_config=types.ThinkingConfig(thinking_level="high", include_thoughts=True))
    elif provider == "claude":
        planner = BuiltInPlanner(
            thinking_config=types.ThinkingConfig(
                thinkingBudget=cfg.thinking_budget,
                includeThoughts=True,
            )
        )

    tech_lead = Agent(
        name="Parallel_Tech_Lead",
        model=model,
        **({"planner": planner} if planner else {}),
        instruction=prompt,
        tools=tools,
        sub_agents=[parallel_workers],
    )

    log.info(
        "parallel_tech_lead_created",
        model=model,
        worker_count=max_parallel_workers,
        developer_model=developer_model,
    )
    return tech_lead


# =============================================================================
# ASYNC ORCHESTRATOR - For more control over parallel execution
# =============================================================================


class ParallelOrchestrator:
    """
    Async orchestrator for fine-grained control over parallel issue processing.

    Use this when you need:
    - Dynamic worker allocation
    - Progress monitoring
    - Graceful error handling
    - Rate limiting

    Example:
        orchestrator = ParallelOrchestrator(max_workers=3)
        results = await orchestrator.process_issues(repo_name, issues)
    """

    def __init__(
        self,
        max_workers: int = 3,
        model: str = "gemini-3-flash-preview",
        developer_model: str = "gemini-3-pro-preview",
    ):
        """Initialize the parallel orchestrator.

        Args:
            max_workers: Maximum number of concurrent issue workers.
            model: LLM model for orchestration.
            developer_model: LLM model for developer agents.
        """
        self.max_workers = max_workers
        self.model = model
        self.developer_model = developer_model
        self.active_workers: dict[str, IssueTask] = {}
        self.results: list[WorkerResult] = []
        self.semaphore = asyncio.Semaphore(max_workers)

    async def process_issue(self, task: IssueTask) -> WorkerResult:
        """Process a single issue with a worker."""
        async with self.semaphore:
            worker_id = f"async_worker_{task.issue_number}"
            task.worker_id = worker_id
            task.status = IssueStatus.IN_PROGRESS
            self.active_workers[worker_id] = task

            log.info(
                "worker_started",
                worker_id=worker_id,
                issue=task.issue_number,
            )

            try:
                # Create worker for this issue
                create_issue_worker(
                    worker_id=worker_id,
                    model=self.model,
                    developer_model=self.developer_model,
                )

                # TODO: Actually run the worker agent
                # This requires ADK's async runner which may need more setup
                # For now, return a placeholder

                result = WorkerResult(
                    issue_number=task.issue_number,
                    success=True,
                    pr_number=None,  # Would be filled by actual execution
                    qa_passed=False,
                )

                task.status = IssueStatus.COMPLETED
                return result

            except Exception as e:
                log.error(
                    "worker_failed",
                    worker_id=worker_id,
                    issue=task.issue_number,
                    error=str(e),
                )
                task.status = IssueStatus.FAILED
                task.error = str(e)

                return WorkerResult(
                    issue_number=task.issue_number,
                    success=False,
                    error=str(e),
                )
            finally:
                del self.active_workers[worker_id]

    async def process_issues(
        self,
        repo_name: str,
        issues: list[dict[str, Any]],
    ) -> list[WorkerResult]:
        """
        Process multiple issues in parallel.

        Args:
            repo_name: Repository name
            issues: List of issue dicts with 'number', 'title', 'priority'

        Returns:
            List of WorkerResults
        """
        tasks = [
            IssueTask(
                issue_number=issue["number"],
                repo_name=repo_name,
                title=issue.get("title", f"Issue #{issue['number']}"),
                priority=issue.get("priority", "normal"),
            )
            for issue in issues
        ]

        log.info(
            "parallel_processing_started",
            total_issues=len(tasks),
            max_workers=self.max_workers,
        )

        # Process all issues in parallel (limited by semaphore)
        results = await asyncio.gather(
            *[self.process_issue(task) for task in tasks],
            return_exceptions=True,
        )

        # Convert exceptions to failed results
        final_results: list[WorkerResult] = []
        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                final_results.append(
                    WorkerResult(
                        issue_number=tasks[i].issue_number,
                        success=False,
                        error=str(result),
                    )
                )
            else:
                final_results.append(result)

        self.results = final_results

        # Log summary
        successful = sum(1 for r in final_results if r.success)
        failed = len(final_results) - successful

        log.info(
            "parallel_processing_complete",
            total=len(final_results),
            successful=successful,
            failed=failed,
        )

        return final_results

    def get_summary(self) -> dict[str, Any]:
        """Get summary of processing results."""
        return {
            "total": len(self.results),
            "successful": sum(1 for r in self.results if r.success),
            "failed": sum(1 for r in self.results if not r.success),
            "prs_created": [r.pr_number for r in self.results if r.pr_number],
            "failed_issues": [{"issue": r.issue_number, "error": r.error} for r in self.results if not r.success],
        }


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================


def create_parallel_sdlc_team(
    max_workers: int = 3,
    tech_lead_model: str = "gemini-3-pro-preview",
    developer_model: str = "gemini-3-pro-preview",
) -> Agent:
    """
    Convenience function to create a complete parallel SDLC team.

    Args:
        max_workers: Maximum parallel issue workers
        tech_lead_model: Model for Tech Lead
        developer_model: Model for Developer agents

    Returns:
        Configured Parallel Tech Lead agent
    """
    return create_parallel_tech_lead(
        model=tech_lead_model,
        developer_model=developer_model,
        max_parallel_workers=max_workers,
    )


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    "IssueStatus",
    "IssueTask",
    "ParallelOrchestrator",
    "WorkerResult",
    "create_issue_worker",
    "create_parallel_sdlc_team",
    "create_parallel_tech_lead",
]

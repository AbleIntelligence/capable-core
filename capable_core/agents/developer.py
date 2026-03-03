"""
Developer Agent - Pure Coding Specialist.

Responsible for reading code, implementing fixes, and managing PRs.
Follows Steps 4-8 in the workflow diagram.
"""

import os
from typing import Any

import structlog
from google.adk.agents import Agent, LlmAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.planners import BuiltInPlanner
from google.genai import types
from google.genai.types import HttpRetryOptions

from capable_core.config import settings
from capable_core.tools.ci_tools import monitor_ci_for_pr
from capable_core.tools.github_tools import (
    create_branch_with_files,
    create_pr,
    delete_files_from_branch,
    get_branch_info,
    get_directory_tree,
    get_file_content,
    push_files_to_branch,
    update_pr_with_changes,
)
from capable_core.tools.sandbox_tools import (
    lint_code_on_branch,
    run_command_on_branch,
    run_tests_on_branch,
    validate_syntax,
)


log = structlog.get_logger()

# =============================================================================
# SYSTEM PROMPT
# =============================================================================

DEVELOPER_SYSTEM_PROMPT = """
You are a Senior Python Backend Engineer on an autonomous development team.
Your role is to ship production-ready, clean, typed, and well-tested code.

## IDENTITY
- Name: Developer Agent
- Specialization: Python backend development, debugging, code optimization
- Philosophy: "Code is read more than written. Clarity over cleverness."

## CAPABILITIES
You have access to the following tools:

### Code Reading
1. `get_file_content(repo_name, file_path, ref)` - Read files from the repository
2. `get_directory_tree(repo_name, path, ref)` - Explore repo structure

### Branch Management (USE THIS FOR CODE CHANGES)
3. `create_branch_with_files(repo_name, branch_name, file_changes, commit_message, base_branch)`
   - **PRIMARY TOOL** - Creates branch AND pushes code in ONE call
   - file_changes is a dict: {{"path/to/file.py": "complete file content..."}}
   - **USE ONCE PER ISSUE** - Only create ONE branch per issue!
4. `push_files_to_branch(repo_name, branch_name, file_changes, commit_message)`
   - Push more files to EXISTING branch
   - **ALL 4 PARAMS REQUIRED:** repo_name, branch_name, file_changes, commit_message
   - Example: `push_files_to_branch("owner/repo", "branch", {{"file.py": "code..."}}, "Fix X")`
5. `delete_files_from_branch(repo_name, branch_name, file_paths, commit_message)`
   - Delete files from EXISTING branch (for removing deprecated/unnecessary files)
   - file_paths is a list: ["path/to/old_file.py", "config/deprecated.yaml"]
   - Example: `delete_files_from_branch("owner/repo", "branch", ["old.py"], "Remove deprecated module")`
6. `get_branch_info(repo_name, branch_name)` - Check branch status

### Local Testing (RUN THESE BEFORE OPENING PR!)
7. `validate_syntax(code_content, filename, language)` - Quick syntax validation
8. `run_tests_on_branch(repo_name, branch_name, test_command, docker_image, setup_commands)` - Clone branch and run tests
9. `lint_code_on_branch(repo_name, branch_name, lint_command, docker_image, setup_commands)` - Clone branch and check code style
10. `run_command_on_branch(repo_name, branch_name, commands, docker_image, setup_commands)` - Run ANY CLI commands on branch for debugging

**IMPORTANT: Always run tests and lint LOCALLY before opening a PR!**
Don't rely on CI/CD to catch issues - it wastes time and resources.

### PR Management
11. `create_pr(repo_name, branch_name, title, description)` - Create PR from existing branch
12. `update_pr_with_changes(repo_name, pr_number, file_changes, commit_message)` - Update existing PR
13. `monitor_ci_for_pr(repo_name, pr_number)` - Monitor CI pipeline (only after tests & lint pass locally!)

## WORKFLOW

### Step 1: Thoroughly Understand the Codebase (CRITICAL - DO THIS FIRST!)
**Don't start coding until you fully understand the existing code!**

1. **Read the issue/mission description** carefully - understand WHAT needs to change and WHY
2. **Explore the project structure** with `get_directory_tree`:
   - Map out the entire repository layout
   - Identify source directories, test directories, config locations
3. **Read ALL relevant files** using `get_file_content`:
   - **Source files** that need modification
   - **Related source files** that interact with the code you'll change (imports, dependencies)
   - **Config files** - `pyproject.toml`, `setup.py`, `requirements.txt`, `package.json`, etc.
   - **Test files** - existing tests for the code you're modifying
   - **Documentation** - README, docstrings, comments that explain design decisions
4. **Understand the patterns** used in the codebase:
   - Coding style, naming conventions
   - How similar features are implemented
   - Error handling patterns
   - Logging and monitoring patterns
5. **Identify ALL files that may need changes**:
   - Direct changes (the main fix)
   - Config updates (new dependencies, settings)
   - Test updates (new tests, updated tests)
   - Documentation updates if needed

**WHY THIS MATTERS:**
- Changes often require updates to multiple files (e.g., adding a dependency requires updating requirements.txt)
- Existing code patterns should be followed for consistency

### Step 1.5: THINK Before You Code (Design First!)
**STOP! Don't rush into coding. Take a moment to DESIGN your solution.**

After reading the codebase, ask yourself:
1. **What is the ROOT CAUSE?** - Not just the symptom, but WHY is this happening?
2. **What's the SIMPLEST fix?** - Avoid over-engineering. The best code is no code.
3. **What are the SIDE EFFECTS?** - Will this change break anything else?
4. **Is there EXISTING code I can reuse?** - Don't reinvent the wheel.
5. **What's the MINIMAL change set?** - Touch as few files as possible.
6. **How will I TEST this?** - Think about test cases BEFORE writing code.

**AVOID "Head Against the Wall" Coding:**
- DON'T just start writing code and hope it works
- DON'T copy-paste without understanding
- DON'T make random changes to see what happens
- DO have a clear mental model of your solution BEFORE touching code
- DO sketch out the change in your mind: "I will modify X to do Y because Z"

**Output your plan (briefly):**
```
SOLUTION_DESIGN:
- Root cause: <why the bug/issue exists>
- Approach: <how you'll fix it>
- Files to change: <list>
- Risks: <potential side effects>
```

THEN proceed to implementation.
**CRITICAL: Do NOT write code in your text response!**
1. Immediately call `create_branch_with_files` with COMPLETE file content
2. Put ALL code directly in the tool call - not in your message
3. Provide ALL 4 required arguments: repo_name, branch_name, file_changes, commit_message

**ONE branch per issue! Call the tool directly:**
```python
create_branch_with_files(
    repo_name="owner/repo",
    branch_name="fix-issue-123",
    file_changes={{
        "src/utils.py": "COMPLETE FILE CONTENT HERE",
        "tests/test_utils.py": "COMPLETE TEST CONTENT HERE"
    }},
    commit_message="Fix issue #123: description"
)
```

**NEVER** describe what code you would write - WRITE IT DIRECTLY IN THE TOOL CALL!

### Step 3: Test on Branch (DO THIS BEFORE OPENING PR!)
**DON'T RELY ON CI/CD - Run tests locally first to save time!**
1. Use `run_tests_on_branch` to clone and test in Docker
2. This tests your code in the full repository context
3. Specify the test_command, docker_image, and setup_commands based on the project
4. Fix any failures BEFORE moving on - don't waste CI/CD cycles!

### Step 4: Lint Check (IMPORTANT - CI will fail if you skip this!)
**Linting runs in CI/CD pipeline. Check lint BEFORE opening a PR to avoid CI failures!**
1. Use `lint_code_on_branch` to check code style on your branch
2. Fix any lint errors with `push_files_to_branch`
3. Do NOT proceed to PR until lint passes

### Step 5: Self-Correction Loop (USE SAME BRANCH!)
- **If tests FAIL** → Analyze errors, fix the code, then push fixes:
```python
# ALL 4 PARAMETERS ARE REQUIRED!
push_files_to_branch(
    repo_name="owner/repo",              # REQUIRED
    branch_name="fix-issue-123",          # REQUIRED - same branch!
    file_changes={{"src/file.py": "..."}},  # REQUIRED - complete file content
    commit_message="Fix test failures"    # REQUIRED - describe what you fixed
)
```
- **If lint FAILS** → Fix style issues, push fixes with `push_files_to_branch` (same format above), re-lint
- **If tests AND lint PASS** → Proceed to create PR

### Step 5.5: Debug with `run_command_on_branch` (USE WHEN STUCK!)
**If you can't understand WHY tests fail, imports break, or code doesn't work as expected:**

Use `run_command_on_branch` to run arbitrary commands on the branch in Docker - like a remote shell.
This is your debugger! Use it to add prints, check imports, inspect the environment, etc.

**When to use it:**
- Tests fail but error message is unclear
- Import errors you don't understand
- Need to check which packages are installed
- Need to inspect file structure or content at runtime
- Need to verify environment variables, Python version, etc.
- CI fails and you can't reproduce locally

```python
# Example: Debug import errors
run_command_on_branch(
    repo_name="owner/repo",
    branch_name="fix-issue-123",
    commands=[
        "python -c 'import sys; print(sys.path)'",
        "pip list | grep flask",
        "python -c 'from src.app import create_app; print(create_app)'",
    ],
    docker_image="python:3.12-slim",
    setup_commands=["pip install -r requirements.txt"]
)

# Example: Debug test failures with print statements
run_command_on_branch(
    repo_name="owner/repo",
    branch_name="fix-issue-123",
    commands=[
        "python -c 'from src.config import Settings; s = Settings(); print(vars(s))'",
        "python -m pytest tests/test_api.py -v -s 2>&1 | tail -50",
    ],
    docker_image="python:3.12-slim",
    setup_commands=["pip install -r requirements.txt", "pip install pytest"]
)

# Example: Check file structure
run_command_on_branch(
    repo_name="owner/repo",
    branch_name="fix-issue-123",
    commands=[
        "find . -name '*.py' -not -path './.git/*' | sort",
        "cat src/__init__.py",
        "grep -rn 'def main' src/",
    ],
    docker_image="python:3.12-slim"
)
```

**Tips:**
- `commands` is a LIST - you can run multiple commands in one call
- Each command's output is labeled so you can tell which is which
- Use `python -c '...'` to run quick Python snippets
- Use `grep`, `find`, `cat` to inspect files
- Use `pip list`, `npm list`, etc. to check installed packages

### Step 6: Create PR (FROM EXISTING BRANCH)
1. Use `create_pr` to open a PR from your existing branch:
```python
create_pr(
    repo_name="owner/repo",
    branch_name="fix-issue-123",  # SAME branch you created in Step 3!
    title="Fix issue #123: Brief description",
    description="Detailed explanation of changes..."
)
```
2. Use `monitor_ci_for_pr` to wait for CI
3. **If CI FAILS** → Use `push_files_to_branch` to fix (SAME branch), then monitor again
4. **If CI PASSES** → Report success

## OUTPUT FORMAT
**CRITICAL: `transfer_to_agent` is a FUNCTION you must CALL, not text to write!**

When you complete ALL steps (code pushed, tests passed, PR created, CI passed):
1. Output the status report below
2. **IMMEDIATELY call `transfer_to_agent(agent_name='{parent_agent}')`** - this is a function call!

**DO NOT** just write "Transferring" or "HANDOFF" - you must CALL the transfer_to_agent function!

```
DEVELOPMENT_COMPLETE:
- Branch: <branch_name>
- PR: #<number>
- URL: <pr_url>
- Tests: PASSED
- CI: PASSED

CHANGES_SUMMARY:
- <what was changed and why>
```

Then CALL: `transfer_to_agent(agent_name='{parent_agent}')`

If something failed and you cannot fix it after 3 attempts:

```
DEVELOPMENT_BLOCKED:
- Branch: <branch_name>
- Issue: <what failed>
- Attempts: <what you tried>
```

Then CALL: `transfer_to_agent(agent_name='{parent_agent}')`

**IMPORTANT:**
- `transfer_to_agent` is a TOOL/FUNCTION - you must invoke it like any other tool!
- Without calling this function, control will NOT return to your parent agent!

## CONSTRAINTS
- **Complete Files Only:** Never return diffs or partial code
- **Test Before PR:** Always run tests before creating PR
- **Self-Contained:** Every file must have all required imports
- **Type Safety:** Use type hints everywhere
- **Docstrings:** Include docstrings for all functions

## TEST QUALITY REQUIREMENTS (MUTATION TESTING!)
**Your tests will be run through MUTATION TESTING by QA!**
Mutation testing modifies your code to check if tests catch the changes. Write tests that will SURVIVE mutation testing:

### DO:
- **Assert specific values** - Don't just check `is not None`, check the EXACT expected value
- **Test edge cases** - Empty inputs, boundary values, error conditions
- **Test each code path** - If/else branches, loops, error handling
- **Use precise assertions** - `assertEqual(result, 42)` not `assertTrue(result > 0)`
- **Test return values explicitly** - Every function output should be validated
- **Keep tests fast** - Each test should complete in < 1 second
- **Clean up after each test** - Reset global state, close connections, clear caches
- **Assert exact quantities** - When testing search/filter, assert `toHaveLength(N)` for expected count
- **Verify specific content** - Check that rendered elements contain expected text, not just that they exist

### DON'T:
- **NO sleep() or time delays** - These cause timeouts in mutation testing
- **NO flaky tests** - Tests must be 100% deterministic
- **NO external dependencies** - Mock all network calls, file I/O, databases
- **NO weak assertions** - `assertTrue(len(x) > 0)` is weak, `assertEqual(len(x), 3)` is strong
- **NO testing implementation details** - Test behavior, not internal state
- **NO global state leakage** - Tests must not depend on other tests' side effects
- **NO infinite loops or recursion** - Add max iteration limits
- **NO existence-only checks** - Don't just check if element exists, verify its content

### PREVENT MUTATION SCORE POLLUTION (Frontend/React)
**Wrap styling and non-logic constants with Stryker disable comments:**

```typescript
// Stryker disable all: styling does not affect logic
const styles = {{
  container: {{ padding: '16px', margin: '8px' }},
  header: {{ fontSize: '24px', fontWeight: 'bold' }}
}};
// Stryker restore all

// Stryker disable all: static configuration
const CONFIG = {{
  API_TIMEOUT: 5000,
  MAX_RETRIES: 3
}};
// Stryker restore all
```

This prevents mutation testing from wasting time on non-logic code!

### HARDEN DATA VERIFICATION IN TESTS
```typescript
// BAD - only checks existence
expect(screen.getByTestId('item-1')).toBeInTheDocument();

// GOOD - verifies actual content
expect(screen.getByTestId('item-1')).toHaveTextContent('Expected Value');

// BAD - doesn't verify count
const items = screen.getAllByTestId(/list-item/);
expect(items.length).toBeGreaterThan(0);

// GOOD - asserts exact quantity
expect(screen.getAllByTestId(/list-item/)).toHaveLength(3);

// When testing search/filter with 3 mocked items where 1 matches query:
expect(screen.getAllByTestId(/list-item/)).toHaveLength(1);
```

### MUTATION TESTING TIMEOUT PREVENTION (CRITICAL!)
Mutation tests run your test suite HUNDREDS of times. Tests that are slow will cause TIMEOUTS!

### Example of GOOD vs BAD tests:
```python
# BAD - weak assertion, mutation will survive
def test_bad():
    result = calculate_price(100, 0.1)
    assert result > 0  # Mutant changing 0.1 to 0.2 still passes!

# GOOD - precise assertion, catches mutations
def test_good():
    result = calculate_price(100, 0.1)
    assert result == 110.0  # Exact value catches any mutation!

# BAD - no cleanup, state leaks between tests
def test_bad_global():
    global_cache['key'] = 'value'
    assert process_with_cache() == 'result'

# GOOD - cleanup after test
def test_good_global():
    global_cache['key'] = 'value'
    try:
        assert process_with_cache() == 'result'
    finally:
        global_cache.clear()  # Always cleanup!
```

## CRITICAL RULES - DO NOT VIOLATE
1. **ONE BRANCH PER ISSUE** - Never create multiple branches for the same issue!
2. **USE `create_branch_with_files`** - This creates branch AND pushes code in ONE call
3. **USE `create_pr` (not create_pr_with_changes)** - Create PR from your existing branch
4. **NEVER return without pushing code** - If you read files, you must push a fix
5. **NEVER ask for permission** - just do the work
6. **ALWAYS include COMPLETE file content** - not descriptions or placeholders
7. **file_changes must be a dict** - Example: {{"path/file.py": "actual code content"}}
8. **COMPLETE THE FULL WORKFLOW** - Branch → Test → PR → CI → Transfer back
9. **CALL transfer_to_agent** - It's a function! Don't write text - CALL `transfer_to_agent(agent_name='{parent_agent}')`!
"""

# =============================================================================
# AGENT DEFINITION
# =============================================================================


def create_developer_agent(
    model: str | None = None,
    use_litellm: bool = True,
    additional_tools: list | None = None,
    parent_agent_name: str = "Tech_Lead",
    name: str = "Developer",
    provider_type: str | None = None,
) -> Agent | LlmAgent:
    """
    Factory function to create a Developer Agent instance.

    Args:
        model: The LLM model to use.
               - For GitHub Models (via LiteLLM): "github/openai/gpt-5", "github/meta/llama-4-maverick"
               - For Hugging Face Inference API (via LiteLLM): "huggingface/Qwen/Qwen3-Coder-480B-A35B-Instruct"
               - For Hugging Face local models: "hf-local/Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8"
               - For Together AI: "together_ai/Qwen/Qwen3-Coder-480B-A35B-Instruct"
               - For Gemini: "gemini-3-pro-preview", "gemini-2.5-flash"
               - For Claude via Vertex AI: "claude-opus-4-5" or "claude-sonnet-4"
        use_litellm: If True and model starts with supported prefixes, use LiteLLM.
        additional_tools: Extra tools to add to the agent.
        parent_agent_name: Name of the parent agent to transfer back to (default: "Tech_Lead").
                          For parallel mode, use the IssueWorker name (e.g., "IssueWorker_worker_1").
        name: The name of the agent (default: "Developer"). For parallel mode, use e.g., "Developer_worker_1".

    Returns:
        Configured Agent instance (LlmAgent for LiteLLM models, Agent for native models).

    Note:
        - GitHub Models (via LiteLLM) uses https://models.github.ai/inference.
            Prefer setting GITHUB_API_KEY (kept separate from GITHUB_TOKEN used by GitHub tools).
            If GITHUB_API_KEY is not set, it will fall back to GITHUB_TOKEN.
        - Hugging Face Inference API requires HUGGINGFACE_API_KEY environment variable.
        - Hugging Face local models (hf-local/) download and run locally using transformers.
            Requires: pip install transformers torch accelerate
            Example: model="hf-local/Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8"
            This will download the model on first use and run inference locally.
        - Together AI requires TOGETHER_API_KEY or TOGETHERAI_API_KEY environment variable.
        - Claude models via Vertex AI require location="global" in the ADK client configuration.
        - For Gemini, ThinkingConfig with thinking_level="high" is used.
    """
    # Core tools for the developer
    tools = [
        # Code reading
        get_file_content,
        get_directory_tree,
        # Branch management - create_branch_with_files is the PRIMARY tool
        create_branch_with_files,
        push_files_to_branch,
        delete_files_from_branch,
        get_branch_info,
        # Testing - use run_tests_on_branch (easier to debug)
        validate_syntax,
        run_tests_on_branch,
        lint_code_on_branch,
        # Debugging - run arbitrary commands on branch in Docker
        run_command_on_branch,
        # PR management - use create_pr (NOT create_pr_with_changes!)
        create_pr,
        update_pr_with_changes,
        monitor_ci_for_pr,
    ]

    if additional_tools:
        tools.extend(additional_tools)

    # Format the system prompt with the parent agent name
    formatted_prompt = DEVELOPER_SYSTEM_PROMPT.format(parent_agent=parent_agent_name)

    # Resolve model and provider from config when not explicitly supplied
    cfg = settings.agent
    model = model or cfg.developer_model or cfg.model_name
    provider = (provider_type or cfg.developer_provider or cfg.provider_type).lower()

    if provider == "hf-local":
        # Local Hugging Face model - download and run locally using transformers
        # Format: hf-local/Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8
        # This uses LiteLLM's huggingface provider with local inference
        hf_model_name = model[9:]  # Remove "hf-local/" prefix
        print(f"Using local Hugging Face model: {hf_model_name}")
        log.info("hf_local_model", model=hf_model_name)

        # LiteLLM supports local HuggingFace models via the "huggingface" provider
        # with device_map="auto" for automatic GPU placement
        litellm_kwargs: dict[str, Any] = {
            "device_map": "auto",  # Automatically use GPU if available
            "torch_dtype": "auto",  # Use appropriate dtype (FP16/BF16)
            "trust_remote_code": True,  # Required for some models like Qwen
        }

        agent = LlmAgent(
            name=name,
            model=LiteLlm(model=f"huggingface/{hf_model_name}", **litellm_kwargs),
            description="Senior engineer who reads code, implements fixes, creates branches, runs tests, and creates PRs. Handles all coding tasks.",
            instruction=formatted_prompt,
            tools=tools,
            output_key="developer_result",
        )
    elif provider == "litellm":
        # GitHub Models, Hugging Face, or Together AI models via LiteLLM
        # GitHub Models: Prefer GITHUB_API_KEY (fallback: GITHUB_TOKEN), endpoint: https://models.github.ai/inference
        # Hugging Face: Uses HUGGINGFACE_API_KEY
        # Together AI: Uses TOGETHER_API_KEY
        # Supports: GPT-5, Llama 4, Qwen, Mistral, DeepSeek, and many more
        print(f"Using LiteLLM model: {model}")
        log.info("litellm_model", model=model)

        litellm_kwargs_ext: dict[str, Any] = {}
        if model.lower().startswith("github/"):
            # Keep model auth separate from GitHub repo tooling token.
            # Prefer GITHUB_API_KEY for GitHub Models; fall back to GITHUB_TOKEN.
            github_models_token = os.getenv("GITHUB_API_KEY") or os.getenv("GITHUB_TOKEN")
            if github_models_token:
                litellm_kwargs_ext["api_key"] = github_models_token
            # GitHub Models uses the Azure AI Inference endpoint.
            litellm_kwargs_ext["api_base"] = "https://models.github.ai/inference"

        agent = LlmAgent(
            name=name,
            model=LiteLlm(model=model, **litellm_kwargs_ext),
            description="Senior engineer who reads code, implements fixes, creates branches, runs tests, and creates PRs. Handles all coding tasks.",
            instruction=formatted_prompt,
            tools=tools,
            output_key="developer_result",
        )
    elif provider == "claude":
        print("Using Claude model via Vertex AI")
        # Claude models via Vertex AI Model Garden
        # Use ThinkingConfig with thinkingBudget for extended thinking (Thought Signatures)
        # This helps Claude maintain "train of thought" across multi-step SDLC workflows
        # Note: Ensure ADK client is configured with location="global" for Claude
        agent = Agent(
            name=name,
            model=model,
            planner=BuiltInPlanner(
                thinking_config=types.ThinkingConfig(
                    thinkingBudget=cfg.thinking_budget,
                    includeThoughts=True,  # Include thoughts in response for debugging
                )
            ),
            description="Senior engineer who reads code, implements fixes, creates branches, runs tests, and creates PRs. Handles all coding tasks.",
            instruction=formatted_prompt,
            tools=tools,
            output_key="developer_result",
        )
    else:
        # Gemini models (default): Use ThinkingConfig with thinking_level
        print("Using Gemini model")

        # Retry configuration for Vertex AI Gemini to handle transient errors
        retry_config = HttpRetryOptions(
            attempts=15,  # Try 15 times before giving up
            initial_delay=1.0,  # Wait 1 second first
            max_delay=60.0,  # Max wait of 60 seconds
            exp_base=2.0,  # Double the wait time each failure (1s, 2s, 4s...)
            http_status_codes=[429, 500, 503],  # Only retry on these errors
        )

        agent = Agent(
            name=name,
            model=model,
            planner=BuiltInPlanner(thinking_config=types.ThinkingConfig(thinking_level="high", include_thoughts=True)),
            description="Senior engineer who reads code, implements fixes, creates branches, runs tests, and creates PRs. Handles all coding tasks.",
            instruction=formatted_prompt,
            tools=tools,
            output_key="developer_result",
            generate_content_config=types.GenerateContentConfig(
                http_options=types.HttpOptions(
                    retry_options=retry_config,
                ),
            ),
        )

    log.info("developer_agent_created", model=model, tool_count=len(tools), parent_agent=parent_agent_name, name=name)
    return agent


# Default instance
developer_agent = create_developer_agent()


# =============================================================================
# CALLBACK FUNCTIONS (For State Management)
# =============================================================================


def on_developer_start(context: dict[str, Any]) -> dict[str, Any]:
    """Callback when developer agent starts.

    Extracts mission context from shared state.
    """
    mission = context.get("mission", {})
    feedback = context.get("feedback", "")
    # Build the prompt
    prompt_parts = []
    if mission:
        prompt_parts.append(f"## Mission\n{mission.get('description', 'No description')}")
        if mission.get("issue_number"):
            prompt_parts.append(f"Issue: #{mission['issue_number']}")
        if mission.get("repo_name"):
            prompt_parts.append(f"Repository: {mission['repo_name']}")
    if feedback:
        prompt_parts.append(f"\n## Previous Feedback (Retry)\n{feedback}")
    context["developer_prompt"] = "\n".join(prompt_parts)
    return context


def on_developer_complete(context: dict[str, Any], result: str) -> dict[str, Any]:
    """Callback when developer agent completes.

    Parses output and updates shared state.
    """
    # Store the raw output
    context["developer_output"] = result
    # Parse structured output
    if "TEST_STATUS: PASS" in result or "Tests: PASSED" in result:
        context["local_tests_passed"] = True
    else:
        context["local_tests_passed"] = False

    if "DEVELOPMENT_COMPLETE" in result:
        context["ready_for_pr"] = True
        context["development_status"] = "complete"
    elif "DEVELOPMENT_BLOCKED" in result:
        context["ready_for_pr"] = False
        context["development_status"] = "blocked"
    else:
        context["ready_for_pr"] = False
        context["development_status"] = "unknown"

    # Check for proper handoff
    if "HANDOFF:" in result:
        context["handoff_received"] = True
    else:
        context["handoff_received"] = False
        log.warning("developer_no_handoff", message="Developer did not include HANDOFF in response")

    # Extract file changes (simplified parsing)
    context["file_changes"] = _parse_file_changes(result)

    return context


def _parse_file_changes(output: str) -> dict[str, str]:
    """
    Parses developer output to extract file changes.

    Expected format:
    FILE: path/to/file.py
    ---
    content
    ---
    """
    files = {}
    current_file = None
    content_lines: list[str] = []
    in_content = False

    for line in output.split("\n"):
        if line.startswith("FILE:"):
            if current_file and content_lines:
                files[current_file] = "\n".join(content_lines)
            current_file = line.replace("FILE:", "").strip()
            content_lines = []
            in_content = False
        elif line.strip() == "---":
            in_content = not in_content
        elif in_content:
            content_lines.append(line)

    # Don't forget the last file
    if current_file and content_lines:
        files[current_file] = "\n".join(content_lines)

    return files

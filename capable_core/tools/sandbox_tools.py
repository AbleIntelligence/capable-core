"""Sandbox Execution Tools for the CapAble-Core multi-agent system.

Provides isolated code execution using Docker containers or Vertex AI Code Execution.
"""

import io
import json
import os
import tarfile
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import docker
import structlog


log = structlog.get_logger()


class ExecutionStatus(Enum):
    """Execution result status."""

    SUCCESS = "success"
    FAILURE = "failure"
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass
class TestResult:
    """Structured test execution result."""

    status: ExecutionStatus
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    tests_passed: int = 0
    tests_failed: int = 0
    tests_skipped: int = 0
    coverage_percent: float | None = None

    def to_prompt(self) -> str:
        """Format result for LLM consumption."""
        status_icon = "✅" if self.status == ExecutionStatus.SUCCESS else "❌"

        return f"""
## Test Execution Result {status_icon}

**Status:** {self.status.value.upper()}
**Exit Code:** {self.exit_code}
**Duration:** {self.duration_seconds:.2f}s

### Test Summary
- Passed: {self.tests_passed}
- Failed: {self.tests_failed}
- Skipped: {self.tests_skipped}
{f"- Coverage: {self.coverage_percent:.1f}%" if self.coverage_percent else ""}

### Output
```
{self.stdout[:3000]}
```
{f"### Errors{chr(10)}```{chr(10)}{self.stderr[:1500]}{chr(10)}```" if self.stderr else ""}
"""


@dataclass
class MutationTestResult:
    """Mutation testing result."""

    status: ExecutionStatus
    total_mutants: int
    killed_mutants: int
    survived_mutants: int
    timeout_mutants: int
    mutation_score: float
    survived_details: list[dict[str, Any]] = field(default_factory=list)

    def to_prompt(self) -> str:
        """Format mutation result for LLM."""
        return f"""
## Mutation Testing Result

**Status:** {self.status.value.upper()}
**Mutation Score:** {self.mutation_score:.1f}%

### Mutant Summary
- Total: {self.total_mutants}
- Killed: {self.killed_mutants} ✅
- Survived: {self.survived_mutants} ⚠️
- Timeout: {self.timeout_mutants}

### Survived Mutants (Potential Test Gaps)
{self._format_survived()}
"""

    def _format_survived(self) -> str:
        if not self.survived_details:
            return "None - all mutants were killed!"

        details = ""
        for m in self.survived_details[:10]:  # Limit to 10
            details += f"- **{m.get('file', 'unknown')}:{m.get('line', '?')}** - {m.get('description', 'mutation survived')}\n"
        return details


class DockerSandbox:
    """Isolated Docker-based code execution environment.

    Language-agnostic - supports any Docker image and command.
    """

    def __init__(self, image: str, timeout: int = 300, memory_limit: str = "512m", cpu_limit: float = 1.0):
        """
        Initialize a Docker sandbox.

        Args:
            image: Docker image to use (REQUIRED). Use latest stable versions:
                   - Python: "python:3.12-slim"
                   - Node.js: "node:22-slim"
                   - Java: "maven:3-eclipse-temurin-21" or "gradle:8-jdk21"
                   - Go: "golang:1.23-alpine"
                   - Rust: "rust:1.83-slim"
                   - .NET: "mcr.microsoft.com/dotnet/sdk:9.0"
            timeout: Max execution time in seconds.
            memory_limit: Container memory limit.
            cpu_limit: CPU cores to allocate.
        """
        self.image = image
        self.timeout = timeout
        self.memory_limit = memory_limit
        self.cpu_limit = cpu_limit
        self.client = None
        self._container = None
        self._docker_available = False

        # Try to connect to Docker
        try:
            self.client = docker.from_env()
            self.client.ping()  # Verify connection
            self._docker_available = True
            log.info("docker_sandbox_initialized", image=image)
        except Exception as e:
            log.warning("docker_not_available", error=str(e))
            self._docker_available = False

    def _create_tar_stream(self, files: dict[str, str]) -> io.BytesIO:
        """Creates a TAR archive from file dict."""
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode="w") as tar:
            for filename, content in files.items():
                data = content.encode("utf-8")
                info = tarfile.TarInfo(name=filename)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        tar_buffer.seek(0)
        return tar_buffer

    def execute(self, command: str, code_files: dict[str, str], env_vars: dict[str, str] | None = None) -> TestResult:
        """
        Executes a command in an isolated container.

        Args:
            command: Shell command to run. Can be any valid shell command.
            code_files: Dict of {filepath: content} to inject.
            env_vars: Optional environment variables.

        Returns:
            TestResult with execution details.
        """
        # Check if Docker is available
        if not self._docker_available:
            return TestResult(
                status=ExecutionStatus.ERROR,
                exit_code=-1,
                stdout="",
                stderr="Docker is not available. Please ensure Docker Desktop is running on your system.",
                duration_seconds=0,
                tests_passed=0,
                tests_failed=0,
                tests_skipped=0,
            )

        container = None
        start_time = time.time()

        try:
            # Create container
            container = self.client.containers.run(
                self.image,
                command="tail -f /dev/null",  # Keep alive
                detach=True,
                working_dir="/app",
                mem_limit=self.memory_limit,
                nano_cpus=int(self.cpu_limit * 1e9),
                environment=env_vars or {},
                network_mode="bridge",
            )

            log.info("sandbox_container_started", container_id=container.short_id)

            # Inject code files
            if code_files:
                tar_stream = self._create_tar_stream(code_files)
                container.put_archive("/app", tar_stream)

            # Execute the main command (mask any tokens in logs)
            github_token = os.getenv("GITHUB_TOKEN", "")
            log_command = command.replace(github_token, "***") if github_token else command
            log.info("sandbox_executing", command=log_command)
            exit_code, output = container.exec_run(
                f"sh -c '{command}'",
                demux=True,  # Separate stdout/stderr
            )

            duration = time.time() - start_time
            stdout = output[0].decode("utf-8") if output[0] else ""
            stderr = output[1].decode("utf-8") if output[1] else ""

            # Parse test results from multiple frameworks
            result = self._parse_test_output(stdout, stderr)
            result.exit_code = exit_code
            result.duration_seconds = duration
            result.status = ExecutionStatus.SUCCESS if exit_code == 0 else ExecutionStatus.FAILURE

            return result

        except docker.errors.ContainerError as e:
            return TestResult(
                status=ExecutionStatus.ERROR,
                exit_code=-1,
                stdout="",
                stderr=f"Container error: {e!s}",
                duration_seconds=time.time() - start_time,
            )
        except Exception as e:
            log.error("sandbox_execution_failed", error=str(e))
            return TestResult(
                status=ExecutionStatus.ERROR,
                exit_code=-1,
                stdout="",
                stderr=f"Sandbox error: {e!s}",
                duration_seconds=time.time() - start_time,
            )
        finally:
            if container:
                try:
                    container.stop(timeout=5)
                    container.remove()
                    log.info("sandbox_container_cleaned", container_id=container.short_id)
                except Exception:
                    log.warning("sandbox_container_cleanup_failed", container_id=container.short_id)

    def _parse_test_output(self, stdout: str, stderr: str) -> TestResult:
        """Parses test output from multiple testing frameworks.

        Supports: pytest, jest, mocha, JUnit/Maven, go test, cargo test, dotnet test.
        """
        passed = failed = skipped = 0
        coverage = None
        combined = stdout + "\n" + stderr

        import re

        # ============== Python (pytest) ==============
        # Format: "X passed, Y failed, Z skipped"
        match = re.search(r"(\d+) passed", combined)
        if match:
            passed += int(match.group(1))
        match = re.search(r"(\d+) failed", combined)
        if match:
            failed += int(match.group(1))
        match = re.search(r"(\d+) skipped", combined)
        if match:
            skipped += int(match.group(1))

        # ============== JavaScript (Jest/Mocha) ==============
        # Jest: "Tests: X passed, Y failed, Z total"
        match = re.search(r"Tests:\s*(\d+) passed", combined)
        if match:
            passed += int(match.group(1))
        match = re.search(r"Tests:.*?(\d+) failed", combined)
        if match:
            failed += int(match.group(1))

        # Mocha: "X passing", "Y failing"
        match = re.search(r"(\d+) passing", combined)
        if match:
            passed += int(match.group(1))
        match = re.search(r"(\d+) failing", combined)
        if match:
            failed += int(match.group(1))
        match = re.search(r"(\d+) pending", combined)
        if match:
            skipped += int(match.group(1))

        # ============== Java (JUnit/Maven/Gradle) ==============
        # Maven: "Tests run: X, Failures: Y, Errors: Z, Skipped: W"
        match = re.search(r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)", combined)
        if match:
            total_run = int(match.group(1))
            failures = int(match.group(2))
            errors = int(match.group(3))
            skip = int(match.group(4))
            passed += total_run - failures - errors - skip
            failed += failures + errors
            skipped += skip

        # ============== Go (go test) ==============
        # Format: "ok" or "FAIL", count "--- PASS:" and "--- FAIL:"
        pass_matches = re.findall(r"--- PASS:", combined)
        passed += len(pass_matches)
        fail_matches = re.findall(r"--- FAIL:", combined)
        failed += len(fail_matches)
        skip_matches = re.findall(r"--- SKIP:", combined)
        skipped += len(skip_matches)

        # ============== Rust (cargo test) ==============
        # Format: "test result: ok. X passed; Y failed; Z ignored"
        match = re.search(r"test result:.*?(\d+) passed;\s*(\d+) failed;\s*(\d+) ignored", combined)
        if match:
            passed += int(match.group(1))
            failed += int(match.group(2))
            skipped += int(match.group(3))

        # ============== .NET (dotnet test) ==============
        # Format: "Passed: X, Failed: Y, Skipped: Z"
        match = re.search(r"Passed:\s*(\d+)", combined)
        if match:
            passed += int(match.group(1))
        match = re.search(r"Failed:\s*(\d+)", combined)
        if match:
            failed += int(match.group(1))
        match = re.search(r"Skipped:\s*(\d+)", combined)
        if match:
            skipped += int(match.group(1))

        # ============== Coverage (multiple formats) ==============
        # Python coverage: "TOTAL ... XX%"
        match = re.search(r"TOTAL\s+\d+\s+\d+\s+(\d+)%", combined)
        if match:
            coverage = float(match.group(1))
        # Jest/Istanbul table: "All files |   85.71 |" (first number after All files)
        if coverage is None:
            match = re.search(r"All files\s*\|\s*([\d.]+)", combined)
            if match:
                coverage = float(match.group(1))
        # Jest text-summary: "Statements   : 85.71% ( 6/7 )"
        if coverage is None:
            match = re.search(r"Statements\s*:\s*([\d.]+)%", combined)
            if match:
                coverage = float(match.group(1))
        # Jest text-summary: "Lines        : 85.71% ( 6/7 )"
        if coverage is None:
            match = re.search(r"Lines\s*:\s*([\d.]+)%", combined)
            if match:
                coverage = float(match.group(1))
        # Go coverage: "coverage: XX.X% of statements"
        if coverage is None:
            match = re.search(r"coverage:\s*(\d+(?:\.\d+)?)%", combined)
            if match:
                coverage = float(match.group(1))
        # Rust tarpaulin: "XX.XX% coverage"
        if coverage is None:
            match = re.search(r"(\d+(?:\.\d+)?)%\s*coverage", combined)
            if match:
                coverage = float(match.group(1))

        return TestResult(
            status=ExecutionStatus.SUCCESS,
            exit_code=0,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=0,
            tests_passed=passed,
            tests_failed=failed,
            tests_skipped=skipped,
            coverage_percent=coverage,
        )


# =============================================================================
# TOOL FUNCTIONS (For Agent Use)
# =============================================================================


def run_in_sandbox(
    command: str,
    docker_image: str,
    code_files: dict[str, str] | None = None,
    setup_commands: list[str] | None = None,
    env_vars: dict[str, str] | None = None,
    timeout: int = 300,
    memory_limit: str = "1g",
) -> str:
    """
    Executes any command in an isolated Docker container.

    This is a flexible, language-agnostic tool for running code in isolation.
    Use the latest stable Docker image for your language.

    Args:
        command: The main command to execute (e.g., "pytest -v", "npm test", "go test ./...").
        docker_image: Docker image to use. Use official images with latest stable versions:
                      - Python: "python:3.12-slim"
                      - Node.js: "node:22-slim"
                      - Java: "eclipse-temurin:21"
                      - Go: "golang:1.23-alpine"
                      - Rust: "rust:1.83-slim"
                      - .NET: "mcr.microsoft.com/dotnet/sdk:9.0"
                      - Ruby: "ruby:3.3-slim"
                      - PHP: "php:8.3-cli"
        code_files: Optional dict of {filepath: content} to inject into /app directory.
        setup_commands: Optional list of commands to run before the main command
                        (e.g., ["pip install -r requirements.txt", "npm install"]).
        env_vars: Optional environment variables for the container.
        timeout: Maximum execution time in seconds (default: 300).
        memory_limit: Container memory limit (default: "1g").

    Returns:
        Execution result with stdout, stderr, exit code, and duration.

    Examples:
        # Python tests
        >>> run_in_sandbox(command="pytest -v", docker_image="python:3.12-slim", setup_commands=["pip install -r requirements.txt"])

        # Node.js tests
        >>> run_in_sandbox(command="npm test", docker_image="node:22-slim", setup_commands=["npm install"])

        # Go tests
        >>> run_in_sandbox(command="go test -v ./...", docker_image="golang:1.23-alpine")
    """
    sandbox = DockerSandbox(image=docker_image, timeout=timeout, memory_limit=memory_limit)

    if not sandbox._docker_available:
        return """
❌ **Docker Not Available**

Docker Desktop is not running. Please start Docker Desktop and try again.
"""

    # Build full command with setup
    full_command = command
    if setup_commands:
        full_command = " && ".join(setup_commands) + f" && {command}"

    result = sandbox.execute(command=full_command, code_files=code_files or {}, env_vars=env_vars)

    is_success = result.status == ExecutionStatus.SUCCESS and result.exit_code == 0
    status_icon = "✅" if is_success else "❌"
    status_text = "PASSED" if is_success else "FAILED"

    return f"""
## Sandbox Execution Result {status_icon}

**STATUS: {status_text}**
**Exit Code:** {result.exit_code}
**Duration:** {result.duration_seconds:.2f}s
**Image:** {docker_image}

### Output
```
{result.stdout[:4000]}
```
{f"### Errors{chr(10)}```{chr(10)}{result.stderr[:2000]}{chr(10)}```" if result.stderr else ""}
"""


def run_tests_in_sandbox(
    code_files: dict[str, str],
    test_command: str,
    docker_image: str,
    setup_commands: list[str] | None = None,
    timeout: int = 300,
) -> str:
    """
    Runs tests on provided code files in an isolated Docker sandbox.

    Use this when you have the actual code content and want to test it
    without cloning from a repository.

    Args:
        code_files: Dict of {filepath: content} representing the code to test.
                    Must include both source files and test files.
        test_command: The test command to run (e.g., "pytest -v", "npm test", "go test ./...").
        docker_image: Docker image to use. Choose based on language:
                      - Python: "python:3.12-slim"
                      - Node.js: "node:22-slim"
                      - Java: "eclipse-temurin:21" or "maven:3-eclipse-temurin-21"
                      - Go: "golang:1.23-alpine"
                      - Rust: "rust:1.83-slim"
                      - .NET: "mcr.microsoft.com/dotnet/sdk:9.0"
        setup_commands: Optional commands to run first (e.g., ["pip install pytest"]).
        timeout: Maximum execution time in seconds.

    Returns:
        Formatted test result for agent consumption.

    Examples:
        # Python
        >>> run_tests_in_sandbox(
        ...     code_files={"app.py": "def add(a,b): return a+b", "test_app.py": "..."},
        ...     test_command="pytest -v",
        ...     docker_image="python:3.12-slim",
        ...     setup_commands=["pip install pytest"],
        ... )

        # Node.js
        >>> run_tests_in_sandbox(
        ...     code_files={"index.js": "...", "test/index.test.js": "...", "package.json": "..."},
        ...     test_command="npm test",
        ...     docker_image="node:22-slim",
        ...     setup_commands=["npm install"],
        ... )
    """
    sandbox = DockerSandbox(image=docker_image, timeout=timeout)

    if not sandbox._docker_available:
        return """
❌ **Docker Not Available**

Docker Desktop is not running. Please start Docker Desktop and try again.
"""

    # Build command with setup
    full_command = test_command
    if setup_commands:
        full_command = " && ".join(setup_commands) + f" && {test_command}"

    result = sandbox.execute(command=full_command, code_files=code_files)

    return result.to_prompt()


def run_tests_with_coverage(
    code_files: dict[str, str],
    test_command: str,
    coverage_command: str,
    docker_image: str,
    setup_commands: list[str] | None = None,
    min_coverage: float = 80.0,
) -> str:
    """
    Runs tests with coverage reporting in any language.

    Args:
        code_files: Dict of {filepath: content}.
        test_command: Base test command without coverage (used as fallback info).
        coverage_command: Full command that runs tests WITH coverage reporting.
                          Examples:
                          - Python: "pytest --cov=. --cov-report=term-missing"
                          - Node.js: "npx nyc npm test" or "npm run test:coverage"
                          - Go: "go test -cover ./..."
                          - Java: "mvn test jacoco:report"
                          - .NET: "dotnet test --collect:'XPlat Code Coverage'"
        docker_image: Docker image to use (latest stable for your language).
        setup_commands: Commands to install dependencies and coverage tools.
        min_coverage: Minimum required coverage percentage (default: 80%).

    Returns:
        Coverage report with pass/fail status.

    Examples:
        # Python coverage
        >>> run_tests_with_coverage(
        ...     code_files={...},
        ...     test_command="pytest",
        ...     coverage_command="pytest --cov=. --cov-report=term-missing --cov-fail-under=80",
        ...     docker_image="python:3.12-slim",
        ...     setup_commands=["pip install pytest pytest-cov"],
        ... )

        # Node.js coverage
        >>> run_tests_with_coverage(
        ...     code_files={...},
        ...     test_command="npm test",
        ...     coverage_command="npx nyc --reporter=text npm test",
        ...     docker_image="node:22-slim",
        ...     setup_commands=["npm install", "npm install -D nyc"],
        ... )
    """
    sandbox = DockerSandbox(
        image=docker_image,
        timeout=600,  # Coverage can take longer
    )

    if not sandbox._docker_available:
        return "❌ **Docker Not Available** - Please start Docker Desktop."

    # Build command with setup
    full_command = coverage_command
    if setup_commands:
        full_command = " && ".join(setup_commands) + f" && {coverage_command}"

    result = sandbox.execute(command=full_command, code_files=code_files)

    prompt = result.to_prompt()

    # Try to parse coverage from output (works for many formats)
    import re

    coverage = None

    # Common coverage output patterns
    patterns = [
        r"TOTAL\s+\d+\s+\d+\s+(\d+)%",  # Python pytest-cov
        r"All files\s*\|\s*[\d.]+\s*\|\s*[\d.]+\s*\|\s*[\d.]+\s*\|\s*([\d.]+)",  # NYC/Istanbul
        r"Coverage:\s*([\d.]+)%",  # Generic
        r"coverage:\s*([\d.]+)%",  # Generic lowercase
        r"(\d+(?:\.\d+)?)\s*%\s*coverage",  # "XX% coverage"
    ]

    for pattern in patterns:
        match = re.search(pattern, result.stdout, re.IGNORECASE)
        if match:
            coverage = float(match.group(1))
            break

    # Add coverage gate result
    if coverage is not None:
        if coverage >= min_coverage:
            prompt += f"\n\n✅ **COVERAGE_STATUS: PASSED** - {coverage:.1f}% >= {min_coverage}%"
        else:
            prompt += f"\n\n❌ **COVERAGE_STATUS: FAILED** - {coverage:.1f}% < {min_coverage}%"
    else:
        prompt += "\n\n⚠️ **COVERAGE_STATUS: UNKNOWN** - Could not parse coverage percentage from output."

    # Add test command info for context
    prompt += f"\n\n**Test Command:** `{test_command}`\n**Coverage Command:** `{coverage_command}`"

    return prompt


def run_mutation_tests(
    code_files: dict[str, str],
    mutation_command: str,
    docker_image: str,
    setup_commands: list[str] | None = None,
    test_command: str = "pytest",
) -> str:
    """
    Runs mutation testing to verify test quality.

    Mutation testing modifies your code in small ways and checks if tests catch the changes.
    This helps identify weak tests that don't properly verify behavior.

    Args:
        code_files: Dict of {filepath: content}.
        mutation_command: Full mutation testing command (include paths to mutate).
                          Examples:
                          - Python: "mutmut run --paths-to-mutate=src/"
                          - JavaScript: "npx stryker run"
                          - Java: "mvn pitest:mutationCoverage"
                          - .NET: "dotnet stryker"
        docker_image: Docker image to use.
        setup_commands: Commands to install mutation testing tools.
        test_command: Base test command for baseline verification.

    Returns:
        Mutation testing report with score.

    Example:
        >>> run_mutation_tests(
        ...     code_files={...},
        ...     mutation_command="mutmut run --paths-to-mutate=src/",
        ...     docker_image="python:3.12-slim",
        ...     setup_commands=["pip install pytest mutmut"],
        ... )
    """
    sandbox = DockerSandbox(
        image=docker_image,
        timeout=900,  # Mutation tests take much longer
    )

    if not sandbox._docker_available:
        return "❌ **Docker Not Available** - Please start Docker Desktop."

    # First run normal tests to establish baseline
    baseline_cmd = test_command
    if setup_commands:
        baseline_cmd = " && ".join(setup_commands) + f" && {test_command}"

    test_result = sandbox.execute(command=baseline_cmd, code_files=code_files)

    if test_result.status != ExecutionStatus.SUCCESS:
        return f"""
## Mutation Testing Skipped ❌

**Baseline tests failed.** Cannot run mutation tests until all tests pass.

{test_result.to_prompt()}
"""

    # Run mutation tests
    mutation_cmd = mutation_command
    if setup_commands:
        mutation_cmd = " && ".join(setup_commands) + f" && {mutation_command}"

    mutation_result = sandbox.execute(command=mutation_cmd, code_files=code_files)

    # Parse mutation output (try common patterns)
    import re

    total = killed = survived = 0
    score = 0.0

    output = mutation_result.stdout + mutation_result.stderr

    # Try various mutation testing output patterns
    patterns = {
        "total": [r"(\d+)\s*mutants", r"Total:\s*(\d+)", r"(\d+)\s*mutations"],
        "killed": [r"Killed:\s*(\d+)", r"(\d+)\s*killed", r"(\d+)\s*detected"],
        "survived": [r"Survived:\s*(\d+)", r"(\d+)\s*survived", r"(\d+)\s*undetected"],
        "score": [r"Mutation\s*[sS]core:\s*([\d.]+)%?", r"Score:\s*([\d.]+)%?"],
    }

    for key, pattern_list in patterns.items():
        for pattern in pattern_list:
            match = re.search(pattern, output, re.IGNORECASE)
            if match:
                if key == "total":
                    total = int(match.group(1))
                elif key == "killed":
                    killed = int(match.group(1))
                elif key == "survived":
                    survived = int(match.group(1))
                elif key == "score":
                    score = float(match.group(1))
                break

    # Calculate score if not found but we have killed/total
    if score == 0 and total > 0:
        score = (killed / total) * 100

    status = "✅ PASSED" if survived == 0 else "⚠️ NEEDS IMPROVEMENT"

    return f"""
## Mutation Testing Result

**MUTATION_STATUS: {status}**
**Mutation Score:** {score:.1f}%

### Mutant Summary
- Total Mutants: {total}
- Killed (detected by tests): {killed} ✅
- Survived (missed by tests): {survived} ⚠️

### Raw Output
```
{output[:3000]}
```

---
**INTERPRETATION:**
- High mutation score (>80%) = Tests are thorough
- Low mutation score = Tests may miss bugs
- Survived mutants indicate code paths that aren't properly tested
"""


def validate_syntax(code_content: str, filename: str, language: str = "python") -> str:
    """
    Validates code syntax without executing.

    For quick syntax checking before pushing code. Supports multiple languages.

    Args:
        code_content: Code to validate.
        filename: Filename for error reporting.
        language: Programming language. Supports:
                  - "python" (uses compile())
                  - "javascript" / "typescript" (uses esprima/tsc via Docker)
                  - "json" (uses json.loads)
                  - "yaml" (uses yaml.safe_load)

    Returns:
        Validation result with any syntax errors.

    Examples:
        >>> validate_syntax("def foo():\\n  return 1", "app.py", "python")
        >>> validate_syntax("const x = {", "app.js", "javascript")
    """
    language = language.lower()

    if language == "python":
        try:
            compile(code_content, filename, "exec")
            return f"✅ **Syntax Valid:** {filename} has no Python syntax errors."
        except SyntaxError as e:
            return f"""
❌ **Syntax Error in {filename}**

**Line {e.lineno}:** {e.msg}
```python
{e.text}
{"^".rjust(e.offset or 1) if e.offset else ""}
```
"""

    elif language == "json":
        try:
            json.loads(code_content)
            return f"✅ **Syntax Valid:** {filename} is valid JSON."
        except json.JSONDecodeError as e:
            return f"""
❌ **JSON Syntax Error in {filename}**

**Line {e.lineno}, Column {e.colno}:** {e.msg}
"""

    elif language == "yaml":
        try:
            import yaml

            yaml.safe_load(code_content)
            return f"✅ **Syntax Valid:** {filename} is valid YAML."
        except Exception as e:
            return f"❌ **YAML Syntax Error in {filename}:** {e!s}"

    elif language in ["javascript", "js", "typescript", "ts"]:
        # Use Docker for JS/TS syntax check
        sandbox = DockerSandbox(image="node:22-slim", timeout=30, memory_limit="256m")
        if not sandbox._docker_available:
            return "⚠️ Cannot validate JS/TS syntax - Docker not available."

        # Use Node.js to check syntax
        check_script = f"""
try {{
    new Function({json.dumps(code_content)});
    console.log('SYNTAX_OK');
}} catch (e) {{
    console.error('SYNTAX_ERROR:', e.message);
    process.exit(1);
}}
"""
        result = sandbox.execute(command=f"node -e {json.dumps(check_script)}", code_files={})

        if result.exit_code == 0:
            return f"✅ **Syntax Valid:** {filename} has no JavaScript syntax errors."
        else:
            return f"❌ **JavaScript Syntax Error in {filename}:** {result.stderr or result.stdout}"

    else:
        return f"⚠️ Syntax validation not implemented for language: {language}. File: {filename}"


def lint_code(code_files: dict[str, str], lint_command: str, docker_image: str, setup_commands: list[str] | None = None) -> str:
    """
    Runs linting/static analysis on code files.

    Args:
        code_files: Dict of {filepath: content}.
        lint_command: The linting command to run. Examples:
                      - Python: "ruff check ." or "flake8 ." or "pylint *.py"
                      - JavaScript: "npx eslint ." or "npm run lint"
                      - TypeScript: "npx tsc --noEmit"
                      - Go: "go vet ./..." or "golangci-lint run"
                      - Rust: "cargo clippy"
                      - Java: "mvn checkstyle:check"
        docker_image: Docker image with linting tools. Use latest stable:
                      - Python: "python:3.12-slim"
                      - Node: "node:22-slim"
                      - Go: "golangci/golangci-lint:latest"
        setup_commands: Commands to install linting tools (e.g., ["pip install ruff"]).

    Returns:
        Linting report with issues found.

    Examples:
        # Python with ruff
        >>> lint_code(
        ...     code_files={"app.py": "import os\\nx=1"},
        ...     lint_command="ruff check . --output-format=text",
        ...     docker_image="python:3.12-slim",
        ...     setup_commands=["pip install ruff"],
        ... )

        # JavaScript with ESLint
        >>> lint_code(
        ...     code_files={"app.js": "const x = 1", "package.json": "..."},
        ...     lint_command="npx eslint .",
        ...     docker_image="node:22-slim",
        ...     setup_commands=["npm install", "npm install -D eslint"],
        ... )
    """
    sandbox = DockerSandbox(image=docker_image, timeout=120)

    if not sandbox._docker_available:
        return "❌ **Docker Not Available** - Cannot run linting."

    # Build command with setup
    full_command = lint_command
    if setup_commands:
        full_command = " && ".join(setup_commands) + f" && {lint_command}"

    result = sandbox.execute(command=full_command, code_files=code_files)

    if result.exit_code == 0:
        return "✅ **Linting Passed:** No issues found."

    return f"""
## Linting Report ⚠️

**LINT_STATUS: ISSUES_FOUND**

### Issues
```
{result.stdout[:3000]}
```
{f"### Errors{chr(10)}```{chr(10)}{result.stderr[:1000]}{chr(10)}```" if result.stderr else ""}

**ACTION:** Please fix the linting issues above.
"""


def lint_code_on_branch(
    repo_name: str,
    branch_name: str,
    lint_command: str,
    docker_image: str,
    setup_commands: list[str] | None = None,
    timeout: int = 300,
) -> str:
    """
    Clones a GitHub branch into Docker and runs linting/static analysis.

    Use this to check code quality on a branch before approving a PR.

    Args:
        repo_name: Repository in "owner/repo" format.
        branch_name: The branch to clone and lint.
        lint_command: The linting command to run. Examples:
                      - Python: "ruff check ." or "flake8 ." or "pylint **/*.py"
                      - Node.js: "npx eslint ." or "npm run lint"
                      - TypeScript: "npx tsc --noEmit && npx eslint ."
                      - Go: "go vet ./..." or "golangci-lint run"
                      - Rust: "cargo clippy"
                      - Java: "mvn checkstyle:check" or "./gradlew checkstyleMain"
        docker_image: Docker image to use. Use latest stable versions:
                      - Python: "python:3.12-slim"
                      - Node.js: "node:22-slim"
                      - Go: "golang:1.23" or "golangci/golangci-lint:latest"
                      - Rust: "rust:1.83"
                      - Java: "maven:3-eclipse-temurin-21"
        setup_commands: Commands to run after cloning (install deps, linting tools).
                        Examples:
                        - Python: ["pip install ruff"] or ["pip install flake8"]
                        - Node.js: ["npm install", "npm install -D eslint"]
                        - Go: ["go install github.com/golangci/golangci-lint/cmd/golangci-lint@latest"]
        timeout: Maximum execution time in seconds (default: 300).

    Returns:
        Linting report with PASS/FAIL status and issues found.

    Examples:
        # Python project with ruff
        >>> lint_code_on_branch(
        ...     repo_name="owner/repo",
        ...     branch_name="fix-issue-123",
        ...     lint_command="ruff check . --output-format=text",
        ...     docker_image="python:3.12-slim",
        ...     setup_commands=["pip install ruff"],
        ... )

        # Node.js project with ESLint
        >>> lint_code_on_branch(
        ...     repo_name="owner/frontend",
        ...     branch_name="feature-gallery",
        ...     lint_command="npx eslint . --format stylish",
        ...     docker_image="node:22-slim",
        ...     setup_commands=["npm install"],
        ... )

        # Go project
        >>> lint_code_on_branch(
        ...     repo_name="owner/backend",
        ...     branch_name="main",
        ...     lint_command="go vet ./... && golangci-lint run",
        ...     docker_image="golangci/golangci-lint:latest",
        ... )
    """
    import os

    github_token = os.getenv("GITHUB_TOKEN", "")

    sandbox = DockerSandbox(image=docker_image, timeout=timeout, memory_limit="1g")

    if not sandbox._docker_available:
        return "❌ **Docker Not Available** - Please start Docker Desktop."

    # Build clone URL
    clone_url = f"https://{github_token}@github.com/{repo_name}.git" if github_token else f"https://github.com/{repo_name}.git"

    # Build commands
    commands = [
        "apt-get update -qq && apt-get install -y -qq git > /dev/null 2>&1 || apk add --no-cache git > /dev/null 2>&1 || true",
        f"git clone --depth 1 --branch {branch_name} {clone_url} /app/repo",
        "cd /app/repo",
    ]

    if setup_commands:
        commands.extend(setup_commands)

    full_command = " && ".join(commands) + f" && cd /app/repo && {lint_command}"

    log.info("running_lint_on_branch", repo=repo_name, branch=branch_name)

    result = sandbox.execute(command=full_command, code_files={}, env_vars={"GITHUB_TOKEN": github_token} if github_token else {})

    # Clean token from output
    stdout = result.stdout.replace(github_token, "***") if github_token else result.stdout
    stderr = result.stderr.replace(github_token, "***") if github_token else result.stderr

    # Determine status
    is_success = result.exit_code == 0
    status_icon = "✅" if is_success else "⚠️"
    status_text = "PASSED" if is_success else "ISSUES_FOUND"

    if is_success:
        return f"""
## Linting Report for Branch: {branch_name} {status_icon}

**LINT_STATUS: {status_text}**

**Repository:** {repo_name}
**Branch:** {branch_name}
**Docker Image:** {docker_image}
**Duration:** {result.duration_seconds:.2f}s

✅ **No linting issues found!**

### Output
```
{stdout[:2000] if stdout.strip() else "(no output)"}
```
"""

    return f"""
## Linting Report for Branch: {branch_name} {status_icon}

**LINT_STATUS: {status_text}**

**Repository:** {repo_name}
**Branch:** {branch_name}
**Docker Image:** {docker_image}
**Exit Code:** {result.exit_code}
**Duration:** {result.duration_seconds:.2f}s

### Linting Issues Found
```
{stdout[:4000]}
```
{f"### Errors{chr(10)}```{chr(10)}{stderr[:2000]}{chr(10)}```" if stderr else ""}

---
**REQUIRED_ACTION:** Fix the linting issues above before the PR can be approved.
"""


def run_command_on_branch(
    repo_name: str,
    branch_name: str,
    commands: list[str],
    docker_image: str,
    setup_commands: list[str] | None = None,
    timeout: int = 300,
) -> str:
    """
    Clones a GitHub branch into Docker and runs arbitrary CLI commands.

    This is a general-purpose debugging tool. Use it when you need to:
    - Debug why code fails (add print statements, check imports, inspect files)
    - Check the runtime environment (Python version, installed packages, etc.)
    - Run any CLI command on the codebase (grep, find, cat, etc.)
    - Verify file structure, imports, or configurations
    - Run multiple commands in sequence to investigate an issue

    Unlike run_tests_on_branch, this tool accepts MULTIPLE commands and does
    not assume any specific purpose - use it for any investigation.

    Args:
        repo_name: Repository in "owner/repo" format.
        branch_name: The branch to clone.
        commands: List of commands to run IN ORDER. Each command runs sequentially.
                  Examples:
                  - ["python -c 'import sys; print(sys.version)'"]
                  - ["cat requirements.txt", "pip list", "python -c 'import mymodule; print(dir(mymodule))'"]
                  - ["find . -name '*.py' | head -20", "grep -rn 'def main' src/"]
                  - ["python -c 'from app import create_app; print(create_app)'"]
        docker_image: Docker image to use. Match the project language:
                      - Python: "python:3.12-slim"
                      - Node.js: "node:22-slim"
                      - Java: "maven:3-eclipse-temurin-21"
                      - Go: "golang:1.23"
        setup_commands: Commands to run after cloning (install deps, etc).
                        Examples: ["pip install -r requirements.txt"]
        timeout: Maximum execution time in seconds (default: 300).

    Returns:
        Combined output from all commands.

    Examples:
        # Debug Python imports
        >>> run_command_on_branch(
        ...     repo_name="owner/repo",
        ...     branch_name="fix-issue-123",
        ...     commands=[
        ...         "pip list",
        ...         "python -c 'from src.models import User; print(User.__mro__)'",
        ...     ],
        ...     docker_image="python:3.12-slim",
        ...     setup_commands=["pip install -r requirements.txt"],
        ... )

        # Check file structure and content
        >>> run_command_on_branch(
        ...     repo_name="owner/repo",
        ...     branch_name="fix-issue-123",
        ...     commands=[
        ...         "find . -name '*.py' -not -path './.git/*'",
        ...         "cat src/config.py",
        ...         "grep -rn 'DATABASE_URL' .",
        ...     ],
        ...     docker_image="python:3.12-slim",
        ... )
    """
    import os

    github_token = os.getenv("GITHUB_TOKEN", "")

    sandbox = DockerSandbox(image=docker_image, timeout=timeout, memory_limit="2g")

    if not sandbox._docker_available:
        return """
❌ **Docker Not Available**

Docker Desktop is not running. Please start Docker Desktop and try again.
"""

    # Build clone URL
    clone_url = f"https://{github_token}@github.com/{repo_name}.git" if github_token else f"https://github.com/{repo_name}.git"

    # Build command sequence: install git, clone, setup, then run each user command with output labels
    base_commands = [
        """
        apt-get update -qq && apt-get install -y -qq git > /dev/null 2>&1 ||
        apk add --no-cache git > /dev/null 2>&1 || yum install -y git > /dev/null 2>&1 || true
        """,
        f"git clone --depth 1 --branch {branch_name} {clone_url} /app/repo",
        "cd /app/repo",
    ]

    if setup_commands:
        base_commands.extend(setup_commands)

    # Run each user command with a labeled separator so output is clear
    user_commands = []
    for i, cmd in enumerate(commands, 1):
        user_commands.append(f"echo '\\n══════ COMMAND {i}: {cmd} ══════'")
        user_commands.append(cmd)
        user_commands.append("echo '══════ EXIT CODE: '$?' ══════'")

    full_command = " && ".join(base_commands) + " && cd /app/repo && " + " ; ".join(user_commands)

    log.info("running_command_on_branch", repo=repo_name, branch=branch_name, image=docker_image, num_commands=len(commands))

    result = sandbox.execute(command=full_command, code_files={}, env_vars={"GITHUB_TOKEN": github_token} if github_token else {})

    # Clean token from output
    stdout = result.stdout.replace(github_token, "***") if github_token else result.stdout
    stderr = result.stderr.replace(github_token, "***") if github_token else result.stderr

    return f"""
## Debug Output for Branch: {branch_name}

**Repository:** {repo_name}
**Docker Image:** {docker_image}
**Commands Run:** {len(commands)}
**Duration:** {result.duration_seconds:.2f}s

### Output
```
{stdout[:6000]}
```
{f"### Errors / Warnings{chr(10)}```{chr(10)}{stderr[:3000]}{chr(10)}```" if stderr else ""}
"""


def run_tests_on_branch(
    repo_name: str,
    branch_name: str,
    test_command: str,
    docker_image: str,
    setup_commands: list[str] | None = None,
    timeout: int = 600,
) -> str:
    """
    Clones a GitHub branch into Docker and runs tests.

    This is the primary tool for testing code changes in the context of the full
    repository, including all dependencies and project structure.

    Args:
        repo_name: Repository in "owner/repo" format.
        branch_name: The branch to clone and test.
        test_command: The test command to run. Choose based on project:
                      - Python: "pytest -v --tb=short"
                      - Node.js: "npm test" or "yarn test"
                      - Java Maven: "mvn test"
                      - Java Gradle: "./gradlew test"
                      - Go: "go test -v ./..."
                      - Rust: "cargo test"
                      - .NET: "dotnet test"
        docker_image: Docker image to use. Use latest stable versions:
                      - Python: "python:3.12-slim"
                      - Node.js: "node:22-slim"
                      - Java: "maven:3-eclipse-temurin-21" or "gradle:8-jdk21"
                      - Go: "golang:1.23"
                      - Rust: "rust:1.83"
                      - .NET: "mcr.microsoft.com/dotnet/sdk:9.0"
        setup_commands: Commands to run after cloning (install deps, build, etc).
                        Examples:
                        - Python: ["pip install -r requirements.txt"]
                        - Node.js: ["npm install"]
                        - Java Maven: [] (mvn handles deps)
                        - Go: ["go mod download"]
        timeout: Maximum execution time in seconds (default: 600).

    Returns:
        Test execution results with clear PASS/FAIL status.
    Examples:
        # Python project
        >>> run_tests_on_branch(
        ...     repo_name="owner/repo",
        ...     branch_name="fix-issue-123",
        ...     test_command="pytest -v",
        ...     docker_image="python:3.12-slim",
        ...     setup_commands=["pip install -r requirements.txt", "pip install pytest"],
        ... )
        # Node.js project
        >>> run_tests_on_branch(
        ...     repo_name="owner/frontend",
        ...     branch_name="feature-gallery",
        ...     test_command="npm test",
        ...     docker_image="node:22-slim",
        ...     setup_commands=["npm install"],
        ... )
        # Java Maven project
        >>> run_tests_on_branch(
        ...     repo_name="owner/backend",
        ...     branch_name="main",
        ...     test_command="mvn test -q",
        ...     docker_image="maven:3-eclipse-temurin-21",
        ... )
    """
    import os

    github_token = os.getenv("GITHUB_TOKEN", "")
    sandbox = DockerSandbox(image=docker_image, timeout=timeout, memory_limit="2g")
    # Check Docker availability
    if not sandbox._docker_available:
        return """
❌ **Docker Not Available**

Docker Desktop is not running. Please start Docker Desktop and try again.

Alternatively, you can:
1. Use `run_tests_in_sandbox` with individual files
2. Use `validate_syntax` for quick syntax checks
"""

    # Build clone URL
    clone_url = f"https://{github_token}@github.com/{repo_name}.git" if github_token else f"https://github.com/{repo_name}.git"

    # Build setup commands - install git first, then clone
    commands = [
        # Install git (works on debian/alpine/etc)
        """
        apt-get update -qq && apt-get install -y -qq git > /dev/null 2>&1 ||
        apk add --no-cache git > /dev/null 2>&1 || yum install -y git > /dev/null 2>&1 || true
        """,
        f"git clone --depth 1 --branch {branch_name} {clone_url} /app/repo",
        "cd /app/repo",
    ]

    # Add user-specified setup commands
    if setup_commands:
        commands.extend(setup_commands)

    # Add the test command
    full_command = " && ".join(commands) + f" && cd /app/repo && {test_command}"

    log.info("running_tests_on_branch", repo=repo_name, branch=branch_name, image=docker_image)

    result = sandbox.execute(command=full_command, code_files={}, env_vars={"GITHUB_TOKEN": github_token} if github_token else {})

    # Clean token from output if present
    stdout = result.stdout.replace(github_token, "***") if github_token else result.stdout
    stderr = result.stderr.replace(github_token, "***") if github_token else result.stderr

    # Determine clear PASS/FAIL status
    is_success = result.status == ExecutionStatus.SUCCESS and result.exit_code == 0
    status_icon = "✅" if is_success else "❌"
    status_text = "PASSED" if is_success else "FAILED"

    return f"""
## Test Results for Branch: {branch_name} {status_icon}

**TEST_STATUS: {status_text}**

**Repository:** {repo_name}
**Docker Image:** {docker_image}
**Exit Code:** {result.exit_code}
**Duration:** {result.duration_seconds:.2f}s

### Test Summary
- Passed: {result.tests_passed}
- Failed: {result.tests_failed}
- Skipped: {result.tests_skipped}

### Output
```
{stdout[:4000]}
```
{f"### Errors{chr(10)}```{chr(10)}{stderr[:2000]}{chr(10)}```" if stderr else ""}

---
**NEXT_ACTION:** {"Proceed to create PR - all tests passed!" if is_success else "Fix the failing tests and push again using push_files_to_branch"}
"""


def run_coverage_on_branch(
    repo_name: str,
    branch_name: str,
    coverage_command: str,
    docker_image: str,
    setup_commands: list[str] | None = None,
    min_coverage: float = 80.0,
    timeout: int = 600,
) -> str:
    """
    Clones a GitHub branch into Docker and runs tests with coverage analysis.

    Use this to verify test coverage on a branch before approving a PR.

    Args:
        repo_name: Repository in "owner/repo" format.
        branch_name: The branch to clone and test.
        coverage_command: Command to run tests with coverage. Examples:
                          - Python: "pytest --cov=. --cov-report=term-missing"
                          - Node.js (Jest): "npm test -- --coverage --coverageReporters=text"
                          - Node.js (NYC/Mocha): "npx nyc --reporter=text npm test"
                          - Go: "go test -cover -coverprofile=coverage.out ./... && go tool cover -func=coverage.out"
                          - Java: "mvn test jacoco:report"
        docker_image: Docker image to use. Use latest stable versions:
                      - Python: "python:3.12-slim"
                      - Node.js: "node:22-slim"
                      - Go: "golang:1.23"
                      - Java: "maven:3-eclipse-temurin-21"
        setup_commands: Commands to run after cloning (install deps, coverage tools).
                        Examples:
                        - Python: ["pip install -r requirements.txt", "pip install pytest-cov"]
                        - Node.js: ["npm install"] (Jest has built-in coverage, no extra deps needed)
        min_coverage: Minimum required coverage percentage (default: 80%).
        timeout: Maximum execution time in seconds (default: 600).

    Returns:
        Coverage report with PASS/FAIL status.

    Examples:
        # Python project
        >>> run_coverage_on_branch(
        ...     repo_name="owner/repo",
        ...     branch_name="fix-issue-123",
        ...     coverage_command="pytest --cov=. --cov-report=term-missing",
        ...     docker_image="python:3.12-slim",
        ...     setup_commands=["pip install -r requirements.txt", "pip install pytest pytest-cov"],
        ... )

        # Node.js project with Jest (React, etc.)
        >>> run_coverage_on_branch(
        ...     repo_name="owner/frontend",
        ...     branch_name="feature-gallery",
        ...     coverage_command="npm test -- --coverage --coverageReporters=text",
        ...     docker_image="node:22-slim",
        ...     setup_commands=["npm install"],
        ... )

        # Node.js project with Mocha/NYC
        >>> run_coverage_on_branch(
        ...     repo_name="owner/backend",
        ...     branch_name="feature-api",
        ...     coverage_command="npx nyc --reporter=text npm test",
        ...     docker_image="node:22-slim",
        ...     setup_commands=["npm install", "npm install -D nyc"],
        ... )
    """
    import os
    import re

    github_token = os.getenv("GITHUB_TOKEN", "")

    sandbox = DockerSandbox(image=docker_image, timeout=timeout, memory_limit="2g")

    if not sandbox._docker_available:
        return "❌ **Docker Not Available** - Please start Docker Desktop."

    # Build clone URL
    clone_url = f"https://{github_token}@github.com/{repo_name}.git" if github_token else f"https://github.com/{repo_name}.git"

    # Build commands
    commands = [
        "apt-get update -qq && apt-get install -y -qq git > /dev/null 2>&1 || apk add --no-cache git > /dev/null 2>&1 || true",
        f"git clone --depth 1 --branch {branch_name} {clone_url} /app/repo",
        "cd /app/repo",
    ]

    if setup_commands:
        commands.extend(setup_commands)

    full_command = " && ".join(commands) + f" && cd /app/repo && {coverage_command}"

    log.info("running_coverage_on_branch", repo=repo_name, branch=branch_name)

    result = sandbox.execute(command=full_command, code_files={}, env_vars={"GITHUB_TOKEN": github_token} if github_token else {})

    # Clean token from output
    stdout = result.stdout.replace(github_token, "***") if github_token else result.stdout
    stderr = result.stderr.replace(github_token, "***") if github_token else result.stderr

    # Parse coverage from output
    coverage = None
    combined = stdout + "\n" + stderr

    patterns = [
        # Python pytest-cov: "TOTAL    100    20    80%"
        r"TOTAL\s+\d+\s+\d+\s+(\d+)%",
        # Jest/Istanbul table: "All files |   85.71 |" (first number after All files)
        r"All files\s*\|\s*([\d.]+)",
        # Jest text-summary: "Statements   : 85.71% ( 6/7 )"
        r"Statements\s*:\s*([\d.]+)%",
        # Jest text-summary: "Lines        : 85.71% ( 6/7 )"
        r"Lines\s*:\s*([\d.]+)%",
        # NYC text reporter: "Lines        : 85.71%"
        r"All files\s*\|\s*[\d.]+\s*\|\s*[\d.]+\s*\|\s*[\d.]+\s*\|\s*([\d.]+)",  # NYC table with 4 columns
        # Go: "total:    (statements)  85.0%"
        r"total:\s*\(statements\)\s*([\d.]+)%",
        # Go coverage: "coverage: 85.0% of statements"
        r"coverage:\s*([\d.]+)%\s*of\s*statements",
        # Generic patterns
        r"Coverage:\s*([\d.]+)%",
        r"coverage:\s*([\d.]+)%",
        r"(\d+(?:\.\d+)?)\s*%\s*coverage",
        # JaCoCo
        r"Line coverage:\s*([\d.]+)%",
        # C8/V8 coverage: "Lines: 85.71%"
        r"Lines:\s*([\d.]+)%",
    ]

    for pattern in patterns:
        match = re.search(pattern, combined, re.IGNORECASE)
        if match:
            coverage = float(match.group(1))
            break

    # Determine status
    tests_passed = result.exit_code == 0
    coverage_passed = coverage is not None and coverage >= min_coverage

    if tests_passed and coverage_passed:
        status = "PASSED"
        status_icon = "✅"
    else:
        status = "FAILED"
        status_icon = "❌"

    coverage_str = f"{coverage:.1f}%" if coverage else "UNKNOWN"
    coverage_status = "✅" if coverage_passed else "❌"

    return f"""
## Coverage Report for Branch: {branch_name} {status_icon}

**COVERAGE_STATUS: {status}**

**Repository:** {repo_name}
**Branch:** {branch_name}
**Docker Image:** {docker_image}

### Coverage Analysis
- **Coverage:** {coverage_str} {coverage_status}
- **Required:** {min_coverage}%
- **Tests Exit Code:** {result.exit_code}
- **Duration:** {result.duration_seconds:.2f}s

### Test Summary
- Passed: {result.tests_passed}
- Failed: {result.tests_failed}
- Skipped: {result.tests_skipped}

### Output
```
{stdout[:4000]}
```
{f"### Errors{chr(10)}```{chr(10)}{stderr[:2000]}{chr(10)}```" if stderr else ""}

---
**COVERAGE_GATE:** {
        f"PASSED - Coverage meets {min_coverage:.0f}% threshold" if coverage_passed else f"FAILED - Coverage below {min_coverage:.0f}% threshold"
    }
"""


def run_mutation_tests_on_branch(
    repo_name: str,
    branch_name: str,
    mutation_command: str,
    docker_image: str,
    setup_commands: list[str] | None = None,
    test_command: str = "pytest",
    min_mutation_score: float = 60.0,
    timeout: int = 900,
) -> str:
    """
    Clones a GitHub branch into Docker and runs mutation testing.

    Mutation testing modifies code in small ways to verify tests catch the changes.
    Use this to verify test quality on a branch before approving a PR.

    Args:
        repo_name: Repository in "owner/repo" format.
        branch_name: The branch to clone and test.
        mutation_command: Command to run mutation tests. Examples:
                          - Python: "mutmut run --paths-to-mutate=src/ && mutmut results"
                          - Node.js (Jest): "npx stryker run --testRunner jest --mutate 'src/**/*.ts,src/**/*.tsx,!src/**/*.test.*'"
                          - Node.js (Mocha): "npx stryker run --testRunner mocha"
                          - Java: "mvn pitest:mutationCoverage"
        docker_image: Docker image to use.
                      IMPORTANT: For Node.js mutation testing, use "node:22" (NOT slim!)
                      Stryker requires the `ps` command which is missing in slim images.
        setup_commands: Commands to run after cloning (install deps, mutation tools).
                        IMPORTANT: For Node.js, you MUST install @stryker-mutator/core!
                        Examples:
                        - Python: ["pip install -r requirements.txt", "pip install pytest mutmut"]
                        - Node.js (Jest): ["npm install", "npm install -D @stryker-mutator/core @stryker-mutator/jest-runner"]
                        - Node.js (Mocha): ["npm install", "npm install -D @stryker-mutator/core @stryker-mutator/mocha-runner"]
        test_command: Base test command to verify tests pass first.
        min_mutation_score: Minimum required mutation score (default: 60%).
        timeout: Maximum execution time (default: 900s - mutation tests are slow).

    Returns:
        Mutation testing report with score and survived mutants.

    Examples:
        # Python project
        >>> run_mutation_tests_on_branch(
        ...     repo_name="owner/repo",
        ...     branch_name="fix-issue-123",
        ...     mutation_command="mutmut run --paths-to-mutate=src/ && mutmut results",
        ...     docker_image="python:3.12-slim",
        ...     setup_commands=["pip install -r requirements.txt", "pip install pytest mutmut"],
        ...     test_command="pytest",
        ... )
        # Node.js project with Jest (React, etc.)
        # IMPORTANT: Use node:22 (NOT slim) - Stryker needs `ps` command
        >>> run_mutation_tests_on_branch(
        ...     repo_name="owner/frontend",
        ...     branch_name="feature-gallery",
        ...     mutation_command="npx stryker run --testRunner jest --mutate 'src/**/*.tsx,!src/**/*.test.*'",
        ...     docker_image="node:22",
        ...     setup_commands=["npm install", "npm install -D @stryker-mutator/core @stryker-mutator/jest-runner"],
        ...     test_command="npm test",
        ... )
    """
    import os
    import re

    github_token = os.getenv("GITHUB_TOKEN", "")

    sandbox = DockerSandbox(image=docker_image, timeout=timeout, memory_limit="2g")

    if not sandbox._docker_available:
        return "❌ **Docker Not Available** - Please start Docker Desktop."

    # Build clone URL
    clone_url = f"https://{github_token}@github.com/{repo_name}.git" if github_token else f"https://github.com/{repo_name}.git"

    # Build commands - first run baseline tests
    commands = [
        "apt-get update -qq && apt-get install -y -qq git > /dev/null 2>&1 || apk add --no-cache git > /dev/null 2>&1 || true",
        f"git clone --depth 1 --branch {branch_name} {clone_url} /app/repo",
        "cd /app/repo",
    ]

    if setup_commands:
        commands.extend(setup_commands)

    # First verify baseline tests pass
    baseline_command = " && ".join(commands) + f" && cd /app/repo && {test_command}"

    log.info("running_baseline_tests", repo=repo_name, branch=branch_name)

    baseline_result = sandbox.execute(command=baseline_command, code_files={}, env_vars={"GITHUB_TOKEN": github_token} if github_token else {})

    if baseline_result.exit_code != 0:
        stdout = baseline_result.stdout.replace(github_token, "***") if github_token else baseline_result.stdout
        stderr = baseline_result.stderr.replace(github_token, "***") if github_token else baseline_result.stderr
        return f"""
## Mutation Testing Skipped ❌

**MUTATION_STATUS: SKIPPED**

**Reason:** Baseline tests failed. Cannot run mutation tests until all tests pass.

### Baseline Test Output
```
{stdout[:3000]}
```
{f"### Errors{chr(10)}```{chr(10)}{stderr[:1500]}{chr(10)}```" if stderr else ""}

**REQUIRED_ACTION:** Fix the failing tests first, then retry mutation testing.
"""

    # Now run mutation tests
    full_command = " && ".join(commands) + f" && cd /app/repo && {mutation_command}"

    log.info("running_mutation_tests_on_branch", repo=repo_name, branch=branch_name)

    result = sandbox.execute(command=full_command, code_files={}, env_vars={"GITHUB_TOKEN": github_token} if github_token else {})

    # Clean token from output
    stdout = result.stdout.replace(github_token, "***") if github_token else result.stdout
    stderr = result.stderr.replace(github_token, "***") if github_token else result.stderr
    combined = stdout + "\n" + stderr

    # Parse mutation results
    total = killed = survived = timed_out = no_coverage = 0
    score = 0.0

    # ============== Stryker (JavaScript/TypeScript) ==============
    # Format: "Instrumented 9 source file(s) with 425 mutant(s)"
    match = re.search(r"with\s+(\d+)\s+mutant", combined)
    if match:
        total = int(match.group(1))

    # Format: "422/425 tested (0 survived, 21 timed out)"
    match = re.search(r"(\d+)/(\d+)\s+tested\s*\((\d+)\s+survived,?\s*(\d+)?\s*timed\s*out\)", combined)
    if match:
        tested = int(match.group(1))
        total = total if total else int(match.group(2))
        survived = int(match.group(3))
        timed_out = int(match.group(4)) if match.group(4) else 0
        killed = tested - survived - timed_out

    # Stryker final summary: "Mutation score: 94.35%"
    match = re.search(r"Mutation\s+score[:\s]+(\d+(?:\.\d+)?)\s*%", combined, re.IGNORECASE)
    if match:
        score = float(match.group(1))

    # Count [NoCoverage] mutants
    no_coverage = len(re.findall(r"\[NoCoverage\]", combined))

    # ============== Mutmut (Python) ==============
    # Format: "X passed, Y failed, Z skipped"
    if total == 0:
        patterns = {
            "total": [r"(\d+)\s*mutants", r"Total:\s*(\d+)", r"(\d+)\s*mutations"],
            "killed": [r"Killed:\s*(\d+)", r"(\d+)\s*killed", r"(\d+)\s*detected"],
            "survived": [r"Survived:\s*(\d+)", r"(\d+)\s*survived", r"(\d+)\s*undetected"],
            "score": [r"Mutation\s*[sS]core:\s*([\d.]+)%?", r"Score:\s*([\d.]+)%?"],
        }

        for key, pattern_list in patterns.items():
            for pattern in pattern_list:
                match = re.search(pattern, combined, re.IGNORECASE)
                if match:
                    if key == "total":
                        total = int(match.group(1))
                    elif key == "killed":
                        killed = int(match.group(1))
                    elif key == "survived":
                        survived = int(match.group(1))
                    elif key == "score":
                        score = float(match.group(1))
                    break

    # Calculate score if not found
    if score == 0 and total > 0:
        # For mutation score: killed / (total - no_coverage) if we have no_coverage info
        effective_total = total - no_coverage if no_coverage > 0 else total
        if effective_total > 0:
            score = (killed / effective_total) * 100

    # Determine status
    mutation_passed = score >= min_mutation_score

    if mutation_passed:
        status = "PASSED"
        status_icon = "✅"
    else:
        status = "NEEDS_IMPROVEMENT"
        status_icon = "⚠️"

    # Build detailed summary
    no_coverage_str = f"\n- **Not Covered by Tests:** {no_coverage} ⚠️" if no_coverage > 0 else ""
    timed_out_str = f"\n- **Timed Out:** {timed_out}" if timed_out > 0 else ""

    return f"""
## Mutation Testing Report for Branch: {branch_name} {status_icon}

**MUTATION_STATUS: {status}**

**Repository:** {repo_name}
**Branch:** {branch_name}
**Mutation Score:** {score:.1f}% {"✅" if mutation_passed else "❌"}
**Required Score:** {min_mutation_score}%

### Mutant Summary
- **Total Mutants:** {total}
- **Killed (detected by tests):** {killed} ✅
- **Survived (missed by tests):** {survived} {"⚠️" if survived > 0 else "✅"}{no_coverage_str}{timed_out_str}

### Raw Output
```
{combined[:4000]}
```

---
**INTERPRETATION:**
- **High mutation score (>{min_mutation_score:.0f}%):** Tests are thorough ✅
- **Low mutation score:** Tests may miss bugs ⚠️
- **Survived mutants:** Indicate code paths not properly tested

**MUTATION_GATE:** {
        f"PASSED - Score meets {min_mutation_score:.0f}% threshold"
        if mutation_passed
        else f"NEEDS IMPROVEMENT - Score below {min_mutation_score:.0f}% threshold"
    }
"""

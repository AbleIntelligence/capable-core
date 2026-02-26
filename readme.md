<p align="center">
  <h1 align="center">CapAble-Core</h1>
  <p align="center">
    <strong>Autonomous Multi-Agent Development System powered by Google ADK</strong>
  </p>
  <p align="center">
    <a href="#quickstart">Quickstart</a> •
    <a href="#architecture">Architecture</a> •
    <a href="#configuration">Configuration</a> •
    <a href="#deployment">Deployment</a> •
    <a href="#contributing">Contributing</a>
  </p>
  <p align="center">
    <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License">
    <img src="https://img.shields.io/badge/version-0.2.0-orange" alt="Version 0.2.0">
    <img src="https://img.shields.io/badge/ADK-Google%20Agent%20Development%20Kit-4285F4" alt="Google ADK">
  </p>
</p>

---

CapAble Core is an autonomous software development system that uses a coordinated swarm of AI agents to resolve GitHub issues end-to-end — from reading the issue, understanding the codebase, writing code, running tests in isolated Docker sandboxes, to opening a pull request — all without human intervention.

## Key Capabilities

- **Autonomous Issue Resolution** — Assign an issue, get a tested PR back.
- **Strict Quality Gates** — 80 % minimum test coverage, mutation testing, CI verification.
- **Isolated Sandbox Execution** — All code runs in ephemeral Docker containers. Never on your host.
- **Parallel Processing** — Process multiple issues simultaneously with isolated Dev + QA worker pairs.
- **Multi-Provider LLMs** — Gemini, Claude (Vertex AI), LiteLLM, or HuggingFace local models.
- **Per-Role Model Overrides** — Assign different models to the Developer and QA Architect agents.

---

## Quickstart

### Prerequisites

| Requirement       | Minimum  |
| ----------------- | -------- |
| Python            | 3.11+    |
| Docker Engine     | 20.10+   |
| GitHub PAT        | `repo` scope |
| Google AI API Key **or** GCP Project | — |

### Install

```bash
# Clone the repository
git clone https://github.com/<your-org>/capable_core.git
cd capable_core

# Install the package
pip install -e ".[dev]"
```

### Minimal `.env`

```env
GITHUB_TOKEN=ghp_your_personal_access_token
GOOGLE_API_KEY=your_google_ai_api_key
```

### Run

```bash
# Fix a specific issue
capable-run --repo "owner/repo" --issue 42

# Scan your inbox for assigned issues
capable-run --repo "owner/repo"

# Dry run — validate config without executing
capable-run --repo "owner/repo" --dry-run
```

Or launch the ADK interactive UI:

```bash
adk web capable_core      # Browser UI on localhost:8000
adk api_server capable_core  # REST API on port 8080
```

---

## Architecture

### Agent Hierarchy

```
┌─────────────────────────────────────────────────────┐
│                    Tech Lead (Root)                  │
│            Orchestrator & Quality Gate               │
│                                                     │
│   Reads issues → delegates work → reviews results   │
│                                                     │
│   ┌──────────────────┐   ┌──────────────────────┐   │
│   │   Developer      │   │   QA Architect       │   │
│   │   Agent          │   │   Agent              │   │
│   │                  │   │                      │   │
│   │ • Read codebase  │   │ • Coverage analysis  │   │
│   │ • Write code     │   │ • Mutation testing   │   │
│   │ • Run tests      │   │ • Security checks    │   │
│   │ • Create PRs     │   │ • Approve / reject   │   │
│   └──────────────────┘   └──────────────────────┘   │
└─────────────────────────────────────────────────────┘
```

**Tech Lead** — The root orchestrator agent. Reads the issue, analyses the repository, delegates implementation to the Developer, then routes the PR to the QA Architect for verification. Holds final merge authority.

**Developer** — Explores the repo tree, edits files, pushes branches, runs sandbox tests, and opens PRs. Iterates up to `AGENT_DEV_MAX_ITERATIONS` times to satisfy quality gates.

**QA Architect** — Runs coverage analysis and mutation testing inside sandboxed containers. Approves or rejects the PR with detailed feedback. Iterates up to `AGENT_QA_MAX_ITERATIONS` times.

### Parallel Mode

When `FOUNDRY_PARALLEL_MODE=true`, the Tech Lead becomes a **dispatcher** that spawns isolated `IssueWorker` squads (each containing its own Developer + QA pair) to process multiple issues concurrently:

```
Tech Lead (Dispatcher)
    │
    ├──► IssueWorker #1  (Issue #101)
    │       Developer → QA Architect
    │
    ├──► IssueWorker #2  (Issue #102)
    │       Developer → QA Architect
    │
    └──► IssueWorker #3  (Issue #103)
            Developer → QA Architect
```

Control concurrency with `FOUNDRY_MAX_WORKERS` (default: 3).

### Nightwatch Workflow

The `NightwatchWorkflow` orchestrator drives the end-to-end pipeline:

```
Issue Detected
    │
    ▼
Tech Lead reads issue & repo structure
    │
    ▼
Developer: implement fix → push branch → run sandbox tests
    │            ▲
    │ fail       │ iterate (up to AGENT_DEV_MAX_ITERATIONS)
    └────────────┘
    │ pass
    ▼
Developer opens Pull Request
    │
    ▼
QA Architect: coverage ≥ 80% ? mutation testing passes ?
    │            ▲
    │ fail       │ iterate (up to AGENT_QA_MAX_ITERATIONS)
    └────────────┘
    │ pass
    ▼
Tech Lead reviews & approves PR
```

### Tools

| Tool | Description |
| ---- | ----------- |
| `get_my_assigned_issues` | Fetch issues assigned to the bot from GitHub |
| `get_issue_content` | Read issue title, body, labels, and comments |
| `get_directory_tree` | Explore repository file structure |
| `get_file_content` | Read file contents from any branch |
| `push_files_to_branch` | Commit and push file changes |
| `get_pr_details` | Inspect PR status, CI results, changed files |
| `add_pr_comment` / `add_issue_comment` | Post comments on PRs or issues |
| `run_tests_on_branch` | Execute tests in a Docker sandbox container |
| `lint_code_on_branch` | Run linter inside a Docker sandbox container |
| `run_command_on_branch` | Run arbitrary commands in a sandbox container |
| `monitor_ci_for_pr` | Poll GitHub Actions CI status until resolved |

---

## Sandbox & Security

All code execution happens inside **ephemeral Docker sibling containers** — never on the host.

```
┌────────────────────────────────────────┐
│  Host Machine                          │
│                                        │
│  ┌──────────────────────────────────┐  │
│  │  CapAble-Core Container    │  │
│  │                                  │  │
│  │  sandbox_tools.py                │  │
│  │      │                           │  │
│  │      │ docker.from_env()         │  │
│  │      ▼                           │  │
│  │  Docker Socket (/var/run/...)    │──┼──► Docker Engine
│  └──────────────────────────────────┘  │        │
│                                        │        ▼
│  ┌─────────────┐  ┌─────────────┐      │   Sibling
│  │ Sandbox #1  │  │ Sandbox #2  │      │   Containers
│  │ pytest      │  │ ruff lint   │      │   (ephemeral)
│  └─────────────┘  └─────────────┘      │
└────────────────────────────────────────┘
```

> **⚠️ Critical:** When running inside Docker, you **must** mount the host Docker socket:
>
> ```bash
> docker run -v /var/run/docker.sock:/var/run/docker.sock ...
> ```

### Sandbox Resource Limits

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `SANDBOX_DEFAULT_IMAGE` | `python:3.11-slim` | Base image for sandbox containers |
| `SANDBOX_TIMEOUT` | `300` | Container timeout (seconds) |
| `SANDBOX_MEMORY_LIMIT` | `512m` | Memory cap per container |
| `SANDBOX_CPU_LIMIT` | `1.0` | CPU core limit per container |

Each sandbox container:
- Clones the repo branch under test
- Installs project dependencies
- Runs the requested command (`pytest`, `ruff`, etc.)
- Captures stdout / stderr and exit code
- Is destroyed after execution — no persistent state

---

## Configuration

All configuration is managed through environment variables (or a `.env` file). Powered by Pydantic Settings with full validation.

### Authentication

| Variable | Required | Description |
| -------- | -------- | ----------- |
| `GITHUB_TOKEN` | **Yes** | GitHub PAT with `repo` scope |
| `GITHUB_DEFAULT_REPO` | No | Default `owner/repo` to target |
| `GITHUB_API_URL` | No | GitHub API base URL (default: `https://api.github.com`) |

### Google AI / Vertex AI

Choose **one** authentication path:

**Option A — Google AI API Key** (simplest):
```env
GOOGLE_API_KEY=your_key
```

**Option B — Vertex AI** (recommended for `gemini-3-pro-preview`):
```env
GOOGLE_CLOUD_PROJECT=my-gcp-project
GOOGLE_CLOUD_LOCATION=us-central1
GOOGLE_GENAI_USE_VERTEXAI=true
# Also run: gcloud auth application-default login
```

### Agent Behavior

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `AGENT_MODEL_NAME` | `gemini-3-pro-preview` | Global LLM model identifier |
| `AGENT_PROVIDER_TYPE` | `gemini` | Provider: `gemini`, `claude`, `litellm`, `hf-local` |
| `AGENT_THINKING_BUDGET` | `10000` | Token budget for Claude extended thinking |
| `AGENT_DEFAULT_MODEL` | `gemini-3-pro-preview` | Default model fallback |
| `AGENT_FAST_MODEL` | `gemini-2.0-flash` | Fast model for simple tasks |

### Per-Role Model Overrides

Assign different models to each agent role. Leave empty to use the global defaults.

```env
# Give the Developer a faster model
AGENT_DEVELOPER_MODEL=gemini-2.0-flash
AGENT_DEVELOPER_PROVIDER=gemini

# Give the QA Architect Claude for deeper analysis
AGENT_QA_MODEL=claude-sonnet-4-20250514
AGENT_QA_PROVIDER=claude
```

### Quality Gates & Iteration Limits

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `AGENT_DEV_MAX_ITERATIONS` | `3` | Max Developer retry loops |
| `AGENT_QA_MAX_ITERATIONS` | `2` | Max QA verification loops |
| `AGENT_CI_POLL_INTERVAL` | `30` | CI status polling interval (seconds) |
| `AGENT_CI_TIMEOUT` | `600` | CI monitoring timeout (seconds) |
| `AGENT_MIN_COVERAGE_PERCENT` | `80.0` | Minimum test coverage to pass QA |
| `AGENT_MAX_SURVIVING_MUTANTS` | `5` | Max surviving mutants allowed |

### Parallel Mode

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `FOUNDRY_PARALLEL_MODE` | `false` | Enable parallel issue processing (`true` / `false`) |
| `FOUNDRY_MAX_WORKERS` | `3` | Max concurrent issue workers |

### LLM Provider Examples

<details>
<summary><strong>Gemini (default)</strong></summary>

```env
AGENT_PROVIDER_TYPE=gemini
AGENT_MODEL_NAME=gemini-3-pro-preview
GOOGLE_API_KEY=your_key
```
</details>

<details>
<summary><strong>Claude via Vertex AI</strong></summary>

```env
AGENT_PROVIDER_TYPE=claude
AGENT_MODEL_NAME=claude-sonnet-4-20250514
AGENT_THINKING_BUDGET=10000
GOOGLE_CLOUD_PROJECT=my-project
GOOGLE_GENAI_USE_VERTEXAI=true
```
</details>

<details>
<summary><strong>LiteLLM — GPT-4o (OpenAI)</strong></summary>

```env
AGENT_PROVIDER_TYPE=litellm
AGENT_MODEL_NAME=openai/gpt-4o
AGENT_FAST_MODEL=openai/gpt-4o-mini
OPENAI_API_KEY=sk-...
```
</details>

<details>
<summary><strong>LiteLLM — Claude (Anthropic)</strong></summary>

```env
AGENT_PROVIDER_TYPE=litellm
AGENT_MODEL_NAME=anthropic/claude-sonnet-4-20250514
AGENT_FAST_MODEL=anthropic/claude-haiku-3-20250514
AGENT_THINKING_BUDGET=10000
ANTHROPIC_API_KEY=sk-ant-...
```
</details>

<details>
<summary><strong>LiteLLM — Mixed (per-role overrides)</strong></summary>

Use different providers for each agent role:

```env
AGENT_PROVIDER_TYPE=litellm

# Tech Lead + Developer use GPT-4o
AGENT_MODEL_NAME=openai/gpt-4o
AGENT_DEVELOPER_MODEL=openai/gpt-4o
AGENT_DEVELOPER_PROVIDER=litellm

# QA Architect uses Claude for deeper analysis
AGENT_QA_MODEL=anthropic/claude-sonnet-4-20250514
AGENT_QA_PROVIDER=litellm

# Both keys required
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
```
</details>

<details>
<summary><strong>HuggingFace Local</strong></summary>

```bash
pip install -e ".[hf-local]"
```

```env
AGENT_PROVIDER_TYPE=hf-local
AGENT_MODEL_NAME=mistralai/Mistral-7B-Instruct-v0.3
```
</details>

---

## Multi-Interface Usage

### CLI — `capable-run`

The `capable-run` entry point drives the full Nightwatch pipeline from the command line:

```bash
# Fix a specific issue
capable-run --repo "my-org/backend" --issue 123

# Auto-detect highest-priority assigned issue
capable-run --repo "my-org/backend"

# Dry run — build the mission prompt and validate config
capable-run --repo "my-org/backend" --dry-run

# Verbose logging
capable-run --repo "my-org/backend" --issue 42 --verbose
```

### ADK Web UI

Launch the Google ADK interactive session in your browser:

```bash
adk web capable_core
# Opens http://localhost:8000 with a chat interface to the Tech Lead
```

### ADK API Server

Expose the agent swarm as a REST API:

```bash
adk api_server --host 0.0.0.0 --port 8080 capable_core
```

### Python API

```python
from capable_core.flows.nightwatch import NightwatchWorkflow, WorkflowConfig

config = WorkflowConfig(
    repo_name="my-org/backend",
    max_dev_iterations=3,
    min_coverage=80.0,
)
workflow = NightwatchWorkflow(config)
result = workflow.execute(issue_number=42)

print(result.success)   # True / False
print(result.pr_url)    # https://github.com/my-org/backend/pull/99
```

---

## Deployment

### Docker (Recommended)

```bash
# Build the image
docker build -f capable_core/Dockerfile -t capable_core .

# Run with Docker socket access (required for sandboxes)
docker run -d \
  --name capable-core \
  -p 8080:8080 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e GITHUB_TOKEN \
  -e GOOGLE_API_KEY \
  -e AGENT_PROVIDER_TYPE=gemini \
  capable_core
```

### CI/CD Pipelines

Two GitHub Actions workflows gate every change:

| Workflow | Trigger | Jobs |
| -------- | ------- | ---- |
| [validate-pr.yml](.github/workflows/validate-pr.yml) | `pull_request → main` | `ruff check` → `ruff format --check` → `pytest` |
| [build-test.yml](.github/workflows/build-test.yml) | `push → main` | Lint → Test → Docker build + smoke test → tag `latest` |

### Multi-Cloud Hosting

The Docker image is platform-agnostic. Deploy it anywhere Docker runs with socket access:

<details>
<summary><strong>AWS ECS / Fargate</strong></summary>

```bash
# Push to ECR
aws ecr get-login-password | docker login --username AWS --password-stdin <account>.dkr.ecr.<region>.amazonaws.com
docker tag capable_core:latest <account>.dkr.ecr.<region>.amazonaws.com/capable_core:latest
docker push <account>.dkr.ecr.<region>.amazonaws.com/capable_core:latest

# Deploy with ECS task definition that includes:
#   - Docker socket bind mount (EC2 launch type only — Fargate does not support Docker socket)
#   - Environment variables for GITHUB_TOKEN, GOOGLE_API_KEY, etc.
```

> **Note:** Fargate does not support Docker socket mounting. Use EC2 launch type or run sandboxes via a remote Docker host.
</details>

<details>
<summary><strong>Azure Container Instances / ACI</strong></summary>

```bash
# Push to ACR
az acr login --name <registry>
docker tag capable_core:latest <registry>.azurecr.io/capable_core:latest
docker push <registry>.azurecr.io/capable_core:latest

# Deploy to ACI (requires Docker-in-Docker or VM-based compute for socket access)
az container create \
  --resource-group foundry-rg \
  --name foundry \
  --image <registry>.azurecr.io/capable_core:latest \
  --ports 8080 \
  --environment-variables GITHUB_TOKEN=<token> GOOGLE_API_KEY=<key>
```
</details>

<details>
<summary><strong>GCP Cloud Run / GCE</strong></summary>

```bash
# Push to Artifact Registry
gcloud auth configure-docker <region>-docker.pkg.dev
docker tag capable_core:latest <region>-docker.pkg.dev/<project>/foundry/capable_core:latest
docker push <region>-docker.pkg.dev/<project>/foundry/capable_core:latest

# GCE (full Docker socket access)
gcloud compute instances create-with-container foundry-vm \
  --container-image=<region>-docker.pkg.dev/<project>/foundry/capable_core:latest \
  --machine-type=e2-standard-4
```

> **Note:** Cloud Run does not support Docker socket access. Use GCE for the full sandbox experience, or point `DOCKER_HOST` to a remote Docker daemon.
</details>

---

## Project Structure

```
capable_core/
├── pyproject.toml                  # Package metadata, dependencies, entry points
├── LICENSE                         # MIT
├── readme.md                       # You are here
├── .ruff.toml                      # Ruff linter/formatter config
├── .pre-commit-config.yaml         # Pre-commit hooks
├── .github/workflows/
│   ├── validate-pr.yml             # PR gate: lint + format + test
│   └── build-test.yml              # Main: lint → test → Docker build
├── tests/
│   ├── conftest.py
│   └── integration/
│       └── test_agent_reasoning.py # 16 integration tests
└── capable_core/             # Main package
    ├── __init__.py                 # Version, exports
    ├── config.py                   # Pydantic Settings (all env vars)
    ├── run.py                      # CLI entry point (capable-run)
    ├── Dockerfile                  # Container build
    ├── start.sh                    # Generic ADK server launcher
    ├── internal_start.sh           # GCP-specific launcher + Chat bridge
    ├── agents/
    │   ├── __init__.py
    │   ├── agent.py                # Root agent (Tech Lead), ADK discovery
    │   ├── developer.py            # Developer agent + system prompt
    │   ├── qa_architect.py         # QA Architect agent + system prompt
    │   ├── parallel_squads.py      # Parallel IssueWorker squads
    │   └── squads.py               # Squad utilities
    ├── tools/
    │   ├── __init__.py
    │   ├── github_tools.py         # GitHub API (issues, PRs, files)
    │   ├── ci_tools.py             # CI monitoring
    │   └── sandbox_tools.py        # Docker sandbox execution
    └── flows/
        └── nightwatch/
            ├── __init__.py         # NightwatchWorkflow, WorkflowConfig
            └── agents.py           # Agent re-exports (deprecated)
```

---

## Contributing

### Setup

```bash
git clone https://github.com/<your-org>/capable_core.git
cd capable_core
pip install -e ".[dev]"
pre-commit install
```

### Tests

```bash
# Run all tests
pytest tests/ -v

# With coverage
pytest tests/ --cov=capable_core --cov-report=term-missing
```

### Linting & Formatting

```bash
# Lint
ruff check capable_core/

# Auto-fix
ruff check --fix capable_core/

# Format
ruff format capable_core/
```

Pre-commit hooks run Ruff lint, Ruff format, and mypy automatically on every commit.

### Commit Convention

- `feat:` — New feature
- `fix:` — Bug fix
- `docs:` — Documentation
- `refactor:` — Code restructuring
- `test:` — Test additions/changes
- `ci:` — CI/CD pipeline changes

---

## License

[MIT](LICENSE) — use it, fork it, ship it.

## Author

**Idan Asis** - *Lead Architect* - [GitHub Profile](https://github.com/idanasis)

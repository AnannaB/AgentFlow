# Installation and setup

## Prerequisites

- Python 3.13
- Git
- Windows PowerShell on Windows; the package itself uses cross-platform Python APIs
- Ollama and a locally available model for future inference features. Ollama is not required for the current domain, persistence, or test workflows.

## Create a development environment

From the repository root, in PowerShell:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

If PowerShell blocks activation, use the environment's interpreter directly:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
```

On macOS or Linux, create and activate the environment with `python3 -m venv .venv` and `source .venv/bin/activate`, then install with `python -m pip install -e '.[dev]'`.

## Configuration

Copy `.env.example` to `.env`. Settings use the `AGENTFLOW_` prefix and are validated at load time.

| Variable | Default | Purpose |
|---|---|---|
| `AGENTFLOW_OLLAMA_BASE_URL` | `http://localhost:11434` | Local Ollama base URL. Credentials in URLs are rejected. |
| `AGENTFLOW_OLLAMA_MODEL` | `llama3.1` | Local model name configured for future planning. |
| `AGENTFLOW_OLLAMA_TIMEOUT_SECONDS` | `60` | Positive request timeout, capped at 600 seconds. |
| `AGENTFLOW_OLLAMA_FALLBACK_ENABLED` | `true` | Whether future planner orchestration may use rule-based fallback. |
| `AGENTFLOW_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |

Do not commit `.env`, credentials, model data, or local databases. `.gitignore` excludes common local configuration and data files.

## Prepare Ollama (when needed)

Install Ollama using its official distribution, start the local service, and download a model supported by your machine. Set the model name and local base URL in `.env`. AgentFlow does not connect to Ollama yet; planner integration and connection diagnostics are planned for later phases.

## Run development checks

```powershell
python -m pytest
python -m ruff check .
python -m ruff format --check .
```

The test suite does not require a live Ollama service. The CI workflow installs the package and development extras on Windows and Ubuntu, then runs pytest and Ruff.

## Execute a persisted workflow (Phase 3)

The engine accepts an already validated `ExecutionPlan` and an existing registry; it does not generate plans from natural language.

```python
from agentflow.application.execution import WorkflowExecutionService
from agentflow.domain.models import ExecutionPlan, PlanStep
from agentflow.storage.sqlite import SQLiteDatabase, SQLiteRepository
from agentflow.tools.builtins import create_builtin_registry

database = SQLiteDatabase("data/agentflow.sqlite3")
database.initialize()
repository = SQLiteRepository(database)
service = WorkflowExecutionService(repository, create_builtin_registry())
plan = ExecutionPlan(
    objective_summary="Calculate a result",
    steps=[
        PlanStep(step_key="calculate", tool_name="calculator", arguments={"expression": "6 * 7"})
    ],
)
run = service.create_run(plan)
result = service.execute(run.id)
print(result.status)
```

Execution persists each attempt and pins every step to the selected tool version. Steps execute sequentially in plan order. Output references use only the exact `{"$agentflow_ref":{"step_key":"...","pointer":"/..."}}` object form with RFC 6901 JSON Pointer, and resolve only successful direct dependencies in the same run. Retries require retryable and idempotent tool metadata plus a classified transient/busy error. Optional failures remain recorded; their dependants are skipped, while independent steps may continue.

Approval requests are persisted and bound to run/step scope and a digest. A matching approved record with a resolution timestamp is required; execution has no caller-supplied approval flag. Process interruptions are recorded with `PAUSED` and `error_code=interrupted`; the engine does not automatically replay uncertain work and has no general resume/replay UI yet.

## Use the Phase 2 tool registry

```python
from pathlib import Path

from agentflow.tools.builtins import create_builtin_registry
from agentflow.tools.context import ToolExecutionContext

registry = create_builtin_registry()
context = ToolExecutionContext(workspace_roots=(Path("./workspace"),))
result = registry.invoke("calculator", {"expression": "(12 + 8) / 4"}, context=context)
print(result.result)
```

Create workspace directories before adding them to a context. CSV tools accept a path under one of those roots. JSON exporter additionally requires `ToolExecutionContext(..., approved=True)` and a `.json` destination; approval is an explicit gate at this phase, not yet a user approval workflow. Never set the flag based only on model output.

The registered tool interface consists of `ToolMetadata`, strict `ToolInput` and `ToolOutput` models, and a Python handler receiving validated input plus `ToolExecutionContext`. Register with `ToolRegistry.register(ToolDefinition(...))`. Exact name/version duplicates are rejected; versions coexist and discovery selects the latest semantic version unless all are requested. Pin invocation with `version=`. Errors are typed under `ToolError`; input values, outputs, and CSVs are bounded. Timeout is a caller wait bound plus cooperative cancellation, not forced termination of arbitrary Python.

Available tools:

- `calculator`: basic `+`, `-`, `*`, `/`, `//`, `%`, `**`, and unary signs through an arithmetic AST allowlist; function calls and names are rejected.
- `date_difference`: difference in days between ISO dates.
- `date_calculate`: add or subtract a bounded number of days.
- `csv_profiler`: shape, names, inferred types, missing counts, and basic numeric/categorical statistics.
- `missing_value_analyzer`: per-column and overall missing counts and rates.
- `duplicate_detector`: duplicate count and bounded row indices, optionally based on a configured subset.
- `outlier_detector`: per-numeric-column IQR bounds, counts, and bounded indices.
- `data_aggregator`: group by one or more columns and compute sum, mean, count, min, or max with explicit aliases.
- `json_exporter`: strict JSON export with atomic replace, SHA-256, path confinement, and approval requirement.
- `markdown_report_generator`: escaped Markdown from validated summaries and findings.


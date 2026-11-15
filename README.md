# AgentFlow

AgentFlow is a local-first agentic workflow orchestration platform. Users will describe an objective in natural language; AgentFlow will plan and execute validated workflows using registered tools, with local Ollama inference and a rule-based fallback.

> **Current status:** Phases 0–4 are implemented: foundation, domain and SQLite persistence, secure tool registry, workflow execution, and local Ollama planning with deterministic fallback. The dashboard is not implemented yet.

## Requirements

- Python 3.13
- Git
- Ollama and a local model for LLM planning; recognized objectives can still use rule-based fallback when Ollama is unavailable

## Install and configure

See [Installation and setup](docs/setup.md) for Windows and general development setup, environment configuration, and Ollama preparation.

Quick start from the repository root:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

## Development checks

```powershell
python -m pytest
python -m ruff check .
python -m ruff format --check .
```

## Architecture and roadmap

See [Architecture](docs/architecture.md) for component boundaries, data flow, persistence, execution behavior, security controls, and phased delivery.

Initialize the Phase 1 database and create a first run from Python:

```python
from agentflow.domain.models import WorkflowRun
from agentflow.storage.sqlite import SQLiteDatabase, SQLiteRepository

database = SQLiteDatabase("data/agentflow.sqlite3")
database.initialize()
repository = SQLiteRepository(database)
run = repository.create_run(WorkflowRun(objective="Review a local report"))
print(run.id)
```

Repository operations use short-lived connections. Use `with repository.transaction():` to group multiple repository writes atomically.

## Phase 2 tool registry

Tools are explicitly registered Python callables with a unique name and semantic version, description, strict Pydantic input and output models, risk, timeout, retry, and idempotency metadata. `ToolRegistry.invoke()` validates inputs, enforces approval and bounded concurrency, invokes only the registered callable, then validates and bounds JSON output. Discovery exposes schemas and metadata, never the callable. No model-generated Python, shell, SQL, or other code is executed.

Create a registry with `create_builtin_registry()` from `agentflow.tools.builtins`. Provide `ToolExecutionContext` with explicitly configured existing workspace roots for file tools. JSON export also requires `approved=True`; future workflow services must provide an actual verified approval decision.

Built-ins: `calculator` supports bounded basic arithmetic; `date_difference` and `date_calculate` provide date arithmetic; `csv_profiler` reports dataset shape, columns, missing values, types, and basic statistics; `missing_value_analyzer` reports null counts and percentages; `duplicate_detector` detects repeated rows or selected columns; `outlier_detector` uses configurable IQR bounds; `data_aggregator` groups and computes sum, mean, count, min, or max; `json_exporter` writes validated JSON atomically within an approved workspace; `markdown_report_generator` builds an escaped readable report.

Tool execution timeouts bound caller wait time and signal cooperative cancellation. Python cannot forcibly stop an already-running synchronous function, so registered implementations must be trusted, bounded, and call context checkpoints around longer work. File tools have explicit size limits and do not log their payloads.

## Phase 3 workflow execution

`WorkflowExecutionService` persists a validated execution plan and pinned tool versions with `create_run()`, then runs it with `execute(run_id)`. Steps are scheduled sequentially in plan order after their direct dependencies succeed. Inputs may contain only the exact `{"$agentflow_ref":{"step_key":"...","pointer":"/..."}}` marker using RFC 6901 JSON Pointer; references are limited to successful direct dependencies in the same run.

Every invocation has a durable attempt record. Bounded retries occur only for explicitly retryable, idempotent tools that raise classified `ToolBusyError` or `ToolTransientError`. Validation, permission, timeout, and uncertain outcomes are not retried. Optional failures remain in history and do not alone fail a run; dependants of any unsuccessful step are skipped, while independent steps continue.

Approval is verified from persisted requests matching exact run, step (or plan scope), and digest. The request must be approved and resolved before an approved execution context is created; execution accepts no approval boolean. Process interruption is recorded as `PAUSED` with `error_code=interrupted`; uncertain attempts are never automatically replayed. Recovery records the interruption but does not resume work. A user cancellation during an uncertain tool call is also paused for review because synchronous Python cannot be forcibly stopped safely.

Persisted tool results are limited to 256 KiB measured as compact JSON encoded in UTF-8. See [Installation and setup](docs/setup.md) for a minimal execution example.

## Phase 4 local planning

`PlanningService.plan(objective)` asks the configured Ollama model for a JSON-schema-constrained plan, then validates the plan structure, registered tool names, and each tool's Pydantic input contract. It returns an explicit `planner_kind` and model or fallback reason. It does not execute the plan. Call `PlanningService.create_run(result, execution_service)` to persist it, or `PlanningService.execute(...)` when execution is explicitly intended.

When Ollama is unavailable or returns an invalid plan, the configurable fallback recognizes only explicit arithmetic expressions and ISO date arithmetic. Unsupported objectives return a typed error instead of claiming a plan was made. For example, `Calculate 6 * 7` produces a one-step plan selecting the registered `calculator` tool with `{"expression":"6 * 7"}`. Neither planner can supply code or select unregistered tools.

## Local-first and safety principles

- Inference is intended to run through a local Ollama instance; no paid cloud LLM API is part of the design.
- Model output is untrusted data and must never be executed as code.
- Workflow execution will be limited to registered tools with validated inputs and explicit approval policies.
- Credentials and sensitive values must not be committed or written to logs.


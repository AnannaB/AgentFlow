# AgentFlow

AgentFlow is a local-first agentic workflow orchestration platform. Users will describe an objective in natural language; AgentFlow will plan and execute validated workflows using registered tools, with local Ollama inference and a rule-based fallback.

> **Current status:** Phase 1 domain and SQLite persistence are implemented. Planning, execution, the tool registry, and dashboard are not implemented yet.

## Requirements

- Python 3.13
- Git
- Ollama for local model inference (not required until Ollama planning is implemented)

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

See [Architecture](docs/architecture.md) for component boundaries, data flow, the Phase 1 persistence model, security controls, and phased delivery. The roadmap is incremental; workflow execution is not implemented yet.

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

## Local-first and safety principles

- Inference is intended to run through a local Ollama instance; no paid cloud LLM API is part of the design.
- Model output is untrusted data and must never be executed as code.
- Workflow execution will be limited to registered tools with validated inputs and explicit approval policies.
- Credentials and sensitive values must not be committed or written to logs.


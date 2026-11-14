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


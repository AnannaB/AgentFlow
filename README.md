# AgentFlow

AgentFlow is a local-first workflow application for turning natural-language objectives into reviewable plans, executing registered Python tools, and producing inspectable reports. It uses Ollama for local LLM planning and retains a deterministic planner for a small set of supported objectives. The dashboard keeps planning, review, saving, and execution as separate user actions.

## Features

- Local Ollama planning with JSON-schema-constrained output and strict validation.
- Deterministic, explicitly attributed fallback planning.
- Registered tools with Pydantic input/output contracts, risk controls, timeouts, and bounded retries.
- CSV profiling, missing-value analysis, duplicate and outlier detection, and group aggregations.
- Persisted workflow runs, attempts, approvals, and events in SQLite with explicit migrations.
- Streamlit pages for overview, upload and plan review, execution history, approvals, reports, and settings.
- Workspace-confined Markdown and approved JSON report exports.
- A deterministic sales demonstration that remains usable when local Ollama planning is unavailable.

## Architecture

The project follows a modular application structure:

| Area | Responsibility |
|---|---|
| `domain` | Strict workflow, plan, attempt, and state models |
| `storage` | SQLite schema migrations and repository persistence |
| `tools` | Registered built-ins, validation, policy, and workspace controls |
| `application` | Workflow execution, approvals, and reporting services |
| `planning` | Ollama client, tool-aware validation, and deterministic fallback |
| `dashboard` | Streamlit views and wiring to application services |

The model returns structured data only. AgentFlow validates the plan against the registered tool catalog and each tool's input schema before a run can be saved. It never executes generated Python, shell, or SQL. See [the architecture guide](docs/architecture.md) for lifecycle, persistence, and security details.

## Technology

Python 3.13, Ollama, Pydantic, SQLite, Streamlit, Pandas, NumPy, Plotly, pytest, Ruff, and GitHub Actions. Docker remains an intended deployment option, but this repository does not currently include a Dockerfile or Compose configuration. Inference runs through a local Ollama service; no paid cloud model API is integrated.

## Installation

On Windows PowerShell, from the repository root:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

If environment activation is restricted, use `.\.venv\Scripts\python.exe -m pip install -e ".[dev]"` and run commands with that interpreter. See [setup documentation](docs/setup.md) for other platforms, configuration, and checks.

## Ollama setup

Install and start Ollama, then pull the default local model:

```powershell
ollama pull llama3.2:1b
ollama list
```

The default endpoint is `http://localhost:11434` and the default model is `llama3.2:1b`. Override them with `AGENTFLOW_OLLAMA_BASE_URL` and `AGENTFLOW_OLLAMA_MODEL` in `.env`, or use the dashboard Settings page. The dashboard's connection check calls `/api/tags`; planning calls `POST /api/chat`, so a successful tags check alone does not verify planning access or the configured model. A live `/api/chat` request was not verified in the current development environment because Python received Windows socket error 10013. AgentFlow does not send prompts to a cloud provider.

## Launch the dashboard

```powershell
python -m streamlit run src/agentflow/dashboard/app.py
```

The local dashboard provides an overview, workflow planning, run history, approval management, and report exports. Data and generated reports remain under the configured workspace, which defaults to `workspace`.

## Run the sales demonstration

1. Launch the dashboard and open **New workflow**.
2. Upload [`examples/sales_demo.csv`](examples/sales_demo.csv).
3. Choose **Use deterministic sales demo plan**. This creates a validated plan using the registered CSV profiler and aggregator. It does not call Ollama or claim LLM provenance.
4. Review the plan and its registered tools, then choose **Save reviewed plan as workflow**.
5. Choose **Run workflow**. The profiler and aggregations calculate revenue by product and calendar month. Results identify the top product from the observed product totals.
6. Open **Reports** and export Markdown. For JSON, request approval, approve the request on **Approvals**, then return to **Reports** to export.

The demo preserves the uploaded source. Since the sample contains date, product, quantity, and unit price but no precomputed revenue or month fields, the deterministic demo prepares a separate workspace copy with those derived columns before the registered analysis tools run. Uploaded files are limited to 10 MiB, 100,000 rows, and 256 columns.

For ordinary planning, enter an objective and choose **Generate plan**. Ollama results are labeled as Ollama plans; fallback provenance is displayed when fallback is used. The general rule-based fallback currently recognizes arithmetic and ISO date calculations only.

## Screenshots

Screenshots are not included yet. Add current dashboard captures here after reviewing them for local paths, run identifiers, and dataset information that should not be published.

## Verification

```powershell
python -m pytest
python -m ruff check .
python -m ruff format --check .
git diff --check
```

Latest local verification: **160 passed, 2 skipped**. The live Ollama endpoint diagnostic is opt-in; the Windows symlink security test was skipped because symlink creation was unavailable. Ruff lint passed and all 72 Python files were already formatted. Live Ollama planning and browser click-through were not verified.

The regular test suite does not require a running Ollama server. To opt in to a local `/api/tags` diagnostic, set `$env:AGENTFLOW_DIAGNOSE_OLLAMA='1'` before running `python -m pytest tests/planning/test_ollama_endpoint.py -q`.

## Known limitations

- Live Ollama planning depends on local process networking and the selected installed model; this session could not verify `/api/chat` because the Python process received Windows error 10013.
- Browser click-through was not verified; the deterministic backend workflow and report exports were exercised through the application services.
- The general deterministic fallback is intentionally narrow and does not plan arbitrary CSV analyses. Use the dedicated deterministic sales demo for the supplied sales dataset.
- JSON report export requires an approval decision. The dashboard is intended for a trusted local operator and does not provide user authentication.
- Python cannot forcibly stop an already-running synchronous tool call; timeout cancellation is cooperative and uncertain side effects are not automatically replayed.
- Docker packaging is not included yet; run AgentFlow directly from the Python environment.

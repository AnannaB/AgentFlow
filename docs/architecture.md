# AgentFlow architecture

## Product scope and principles

AgentFlow is a local-first workflow orchestrator. Users provide objectives in natural language; the application creates structured plans, selects registered tools, executes validated steps, handles failures, validates results, and produces reports. All LLM inference is intended to use local Ollama. A deterministic rule-based planner provides fallback behavior. Model output is untrusted data and must never be executed as code.

The architecture is layered: Streamlit UI → application services → domain contracts and policies → infrastructure adapters. Dependencies point inward. Domain and application behavior must not depend on Streamlit or a specific storage/LLM vendor.

## Components and responsibilities

| Component | Responsibility |
|---|---|
| Streamlit dashboard | Objective input, templates, visual workflow graph and progress, approvals, history, replay inspection, analytics, loading/error/empty states. |
| Workflow application services | Create and coordinate runs, validate plans, enforce transitions, invoke approval and execution policies, assemble reports. |
| Planner interface | Translate objective and bounded tool catalog into a proposed plan. Ollama and rule-based implementations share this contract. |
| Plan validator and policy | Check step count, dependency graph, registered tool names, schemas, risk, and approval requirements. |
| Tool registry | Explicit allowlist of stable tool names, argument/result models, risk, idempotency, timeout, and implementation. |
| Executor | Execute dependency-ready validated steps; persist attempts and outcomes; apply bounded retry/cancellation behavior. |
| Repositories and SQLite adapter | Persist runs, templates, approvals, events, steps, and attempts using explicit migrations and transactional state changes. |
| Template service | Save, list, update, duplicate, and delete reusable validated workflow definitions. Templates contain no execution results or secrets. |
| Replay/history service | Provide bounded, paginated run queries and inspect persisted inputs, outputs, attempt history, and sanitized failure details. Replay creates a new run and revalidates; it never blindly repeats a side-effecting step. |
| Model diagnostics | Validate configured endpoint/model, report connectivity and timeout/fallback settings without exposing secrets. |
| Analytics/report service | Calculate success rate, duration, tool usage, failures, and retry frequency from persisted history; bound query windows and result sizes. |

Pandas/NumPy and Plotly are reserved for useful analysis and dashboard visualization. They must not be introduced into core domain state logic.

## Data flow

1. User submits an objective or selects a saved template.
2. Application creates a persisted run in `queued`; template variables are validated and expanded.
3. Planner receives objective and safe registry metadata. Ollama response is parsed as structured data; failures may route to rule planning when configured.
4. Validator checks schema, tools, arguments, limits, dependency acyclicity, and risk policy. Invalid plans never execute.
5. Required plan approval is recorded against a digest of the exact plan.
6. Executor runs eligible steps. Each attempt and state transition is stored transactionally. High-risk step approval is checked immediately before execution.
7. Results are validated and persisted; bounded retries follow tool idempotency metadata. Uncertain side effects pause for review.
8. Reporting and analytics query persisted data. History endpoints/UI use pagination and bounded date ranges.
9. Replay inspection displays historical data read-only by default. A retry/replay action creates a new run from selected safe inputs after policy and approval checks.

## Lifecycle and transactional state

Run states: `queued`, `planning`, `plan_review`, `awaiting_approval`, `executing`, `validating`, `paused`, `succeeded`, `failed`, `cancelled`.

Step states: `pending`, `ready`, `running`, `succeeded`, `failed`, `skipped`, `blocked`, `cancelled`.

Transitions are centralized and validated. A transition and corresponding event write occur in the same SQLite transaction. Terminal runs are immutable; retry starts a new run linked to its source. Startup recovery identifies interrupted active work and pauses it for policy-based recovery rather than replaying side effects.

## Planning and execution

Ollama is called with a bounded objective/context, registered tool descriptions and schemas, and output constraints. Pydantic validates responses; deterministic policy checks enforce limits and allowed capabilities. The rule planner supports only explicitly recognized intents and otherwise reports that it cannot safely plan.

The executor resolves names only in the registry, validates arguments immediately before invocation, bounds time and output, and validates results. It never evaluates model-generated code, shell, SQL, or scripts. Sequential execution is the initial default. Parallel scheduling requires later explicit design for concurrency and consistency.

## Tool registry

Each explicit registration includes name/version, description, strict Pydantic input/output models, implementation, risk, approval requirement, timeout, retryability/idempotency, and resource permissions. Duplicate names fail startup. Tools are small, real, and documented. Filesystem tools are scoped to configured roots; unsafe or uncertain side effects are not automatically repeated.

## Human approval

Policy can require plan-level or step-level approval. The dashboard displays exact objective, step graph, tool names, validated arguments, risks, and expected effects. Decisions bind to a plan/argument digest; any mutation invalidates approval. Rejection is persisted and causes a visible paused or terminal outcome. The initial local single-user workflow is not an identity system; multi-user deployment requires separate authorization design.

## SQLite schema (Phases 1 and 3)

The schema is implemented by ordered SQL migrations in `src/agentflow/storage/migrations/` and applied by `SQLiteDatabase.initialize()`. Future schema changes must use a new ordered migration. Connections enable foreign keys and a busy timeout, are scoped to each operation, and are always closed. Write transactions use `BEGIN IMMEDIATE`; repository state changes and their events commit or roll back together. Timestamps are stored as fixed precision UTC ISO-8601 text. JSON payloads are validated as Pydantic models; tool arguments are limited to 64 KiB, tool outputs to 256 KiB, event details to 16 KiB, and non-finite numbers and credential-like event keys are rejected.

### `workflow_runs`

`id` UUID primary key; `objective`; `status`; `planner_kind`; `plan_json`; `created_at`, `updated_at`, `started_at`, `finished_at`; optional `error_code`, sanitized `error_summary`; optional `source_run_id` for replay lineage.

### `workflow_steps`

`id` UUID primary key; `run_id` FK; `step_key`; `position`; `tool_name`; pinned `tool_version`; `arguments_json`; `depends_on_json`; `required`; `approval_required`; `status`; timestamps. Unique `(run_id, step_key)` and `(run_id, position)`. Migration `0002_workflow_step_tool_version.sql` adds the version column; new runs persist the exact selected registry version and reject unavailable versions at execution time.

### `step_attempts`

`id`; `step_id` FK; `attempt_number`; `status`; timestamps; `input_hash`; bounded `output_json`; sanitized error code/summary; `duration_ms`; unique `(step_id, attempt_number)`.

### `workflow_templates`

`id` UUID; unique case-insensitive `name`; `description`; validated `plan_json`; `version`; `created_at`, `updated_at`; optional `archived_at`. Templates contain no credentials or run data. Template changes are versioned or copied into a run snapshot so later edits do not mutate history.

### `approval_requests`

`id`; `run_id` FK; optional `step_id`; `purpose` (`workflow` or `report_export`); `status`; requested/resolved timestamps; `decision_note`; `plan_digest`. A composite foreign key ensures a step-level approval points to a step from the same run; deleting that step removes its approval request. Migration `0003_approval_purpose.sql` adds purpose and a pending-request index.

### `run_events`

`id`; `run_id` FK; `occurred_at`; `event_type`; bounded sanitized `details_json`. Useful for audit display, not a substitute for current state columns.

The initial migration indexes run history by `(created_at, id)` and status/date, plus run-step, step-attempt, approval, event, and template lookup paths. `list_runs()` uses a bounded page size and stable `(created_at, id)` ordering. Child-list operations also impose limits. Analytics over explicit date windows and bounded aggregates remains a later phase.

## Pydantic contracts

The domain uses strict frozen models with `extra="forbid"`, bounded fields/lists, timezone-aware UTC timestamps, and safe defaults (`Field(default_factory=list/dict)`). Contracts include `WorkflowRun`, `WorkflowStep`, `StepAttempt`, `ApprovalRequest`, `RunEvent`, `WorkflowTemplate`, `PlanStep`, `ExecutionPlan`, and `ToolResult`. Phase 3 adds `FailureDetail`, `StepExecutionResult`, and `ExecutionResult`. Plan validation rejects duplicate step keys, invalid dependency references, and dependency cycles. Repository writes revalidate JSON snapshots; persisted JSON is revalidated when loaded. `ToolResult` enforces a 256 KiB compact JSON UTF-8 data limit aligned with the registry.

`SQLiteRepository` provides CRUD for all six persisted entities. `transaction()` groups repository calls on one connection. Run/step state changes go through `transition_run()` / `transition_step()`; direct updates cannot change lifecycle state. A transition and its corresponding `RunEvent` are written in the same transaction.

## Configuration, diagnostics, and logging

Environment-based settings validate Ollama base URL, model name, positive bounded timeout, fallback enablement, and log level. Diagnostics should report reachability, model availability, and effective non-secret settings. Structured logs carry run/step correlation IDs but exclude objectives, raw tool payloads, credentials, and sensitive outputs by default. Redaction is defense in depth, not permission to log secrets.

## Error recovery

Classify transient infrastructure, validation/policy, tool, timeout/cancellation, and unexpected internal failures. Use bounded exponential backoff only for retryable operations. Persist each attempt. Retry side-effecting actions only when idempotent or protected by an idempotency key. If completion is uncertain, pause for human review. Do not restart whole workflows automatically.

## Security

- Local Ollama only; no paid cloud inference integration.
- No dynamic code execution and no arbitrary model-named tools.
- Validate all model, template, tool input, and tool output data.
- Bound sizes, durations, retries, history queries, and analytics windows.
- Scope filesystem access; use parameterized SQL and explicit migrations.
- Keep secrets out of source control, logs, events, and reports.
- Treat user documents and CSV cells as untrusted data, not instructions.
- Docker should run with least privilege and only required mounts.

## Testing strategy

Unit tests cover schemas, defaults, transitions, dependency graphs, digest invalidation, retry policy, analytics calculations, and pagination boundaries. Integration tests cover migration upgrades, SQLite transactions/rollback, repository bounds, templates, and execution attempts. Planner tests mock the Ollama boundary; optional local smoke tests may use a real Ollama model. Acceptance tests cover dashboard empty/loading/error states, graph/progress, approval, history/replay inspection, and analytics. CI should test Windows and Linux without model downloads or cloud credentials.

## Roadmap

### Phase 0 — foundation (completed)

Package metadata/dependencies, validated settings, structured redacting logger, pytest/Ruff config, GitHub Actions, `.gitignore`, `.env.example`, README, and setup/architecture docs. No engine, tools, database business logic, or dashboard.

### Phase 1 — domain and persistence (completed)

Domain contracts/state machine, initial SQLite migration/repository, transactional state changes, and unit/integration tests are implemented. The LLM, tool registry, application services, and dashboard remain out of scope. Files: `domain/models.py`, `states.py`, `transitions.py`, `errors.py`, `storage/protocols.py`, `storage/sqlite.py`, `storage/migrations/`, and corresponding tests.

### Phase 2 — tool registry (completed)

Implement a typed, versioned tool registry and deterministic built-ins. This phase does not include workflow planning, templates, or execution. The registry validates Pydantic inputs and outputs, enforces metadata-based approval, bounded concurrency, timeouts, JSON serializability, and tool version selection. Discovery returns metadata and schemas only. File tools require explicit existing workspace roots and enforce byte, row, column, and group limits. Registered implementations are trusted application code; model output can only select a registered name and validated data, never executable code.

Built-ins are `calculator`, `date_difference`, `date_calculate`, `csv_profiler`, `missing_value_analyzer`, `duplicate_detector`, `outlier_detector`, `data_aggregator`, `json_exporter`, and `markdown_report_generator`. See README and setup docs for supported operations. JSON exporter is medium risk, requires an explicit approval context, writes within a configured workspace via atomic replacement, and is not retryable/idempotent. Other built-ins are deterministic and idempotent.

Timeouts bound registry wait time and signal cooperative cancellation. Python cannot safely kill a synchronous worker; registry concurrency remains occupied until a timed-out callable exits. Future custom tools must use bounded operations and checkpoints. No arbitrary generated code, shell, or SQL is accepted.

The registry reports failures with typed, sanitized `ToolError` subclasses and does not log raw inputs or outputs. Persistent attempt and error history is implemented in Phase 3.

### Phase 3 — workflow execution (completed)

`application.WorkflowExecutionService` persists a validated plan snapshot, step records, attempts, events, and state transitions through the existing repository and registry. Scheduling is sequential and stable in plan order. Output references use only the exact `$agentflow_ref` object with `step_key` and RFC 6901 JSON Pointer. They resolve only from successful direct dependencies in the same run; malformed markers, pointer escapes/indices, or missing values fail before tool invocation.

Approval requests are matched against exact run, step scope and plan/argument digest. Only persisted `APPROVED` requests with `resolved_at` set unlock an approved context. The executor accepts no caller approval flag. Each attempt is persisted separately. Retries are bounded to explicitly retryable, idempotent tools with classified transient/busy failures. Validation, permission, timeout, and uncertain side effects are not retried. Optional failures remain in history and do not alone fail a run; dependants of any unsuccessful step are skipped while independent steps continue.

`recover_interrupted_run()` moves an active persisted run to `PAUSED`, records `run.interrupted` and `error_code=interrupted`, and marks an in-progress attempt uncertain/failed. It never invokes that tool again. Paused runs are not executable; explicit resolution or a new run is required. If a user cancels while a synchronous tool outcome is uncertain, the run is paused with a cancellation-specific uncertain outcome; cooperative cancellation cannot forcibly stop Python code. General resume and replay-as-new-run services are not implemented.

### Phase 4 — local Ollama planning (implemented)

`planning.OllamaClient` uses the local `/api/chat` endpoint with JSON-schema output, configured model and timeout. `PlanValidator` parses the existing `ExecutionPlan`, enforces registry allowlisting and each selected tool's Pydantic input schema, and raises risk to at least the registered metadata floor. `PlanningService` returns explicit Ollama or rule-based provenance and never executes during `plan()`. The deterministic fallback recognizes only explicit arithmetic and ISO date operations; unsupported objectives fail with a typed error. The existing execution service accepts the validated plan with persisted planner provenance. Tests mock HTTP; a live Ollama server is not required by CI.

### Phase 5 — approvals and workflow reporting (implemented)

`application.ApprovalService` lists bounded pending requests, checks exact run/step/purpose/digest scope, and persists decisions plus events transactionally. Execution uses the latest matching workflow decision so a stale approval cannot supersede a newer pending or rejected request. `WorkflowReportService` builds bounded reports from persisted run/step/attempt data, redacts sensitive fields and common credential strings, and reports optional/failed steps. Markdown output uses the registered generator; JSON output uses the registered exporter only after a persisted approval tied to the exact report and destination. Both paths are workspace-confined and size-bounded.

### Phase 6 — Streamlit dashboard (implemented)

`dashboard.app` presents overview metrics, validated CSV upload and preview, AI plan review, separate save/run actions, bounded execution history, approval decisions, report exports, and session-local Ollama/workspace configuration. `dashboard.services` composes the existing repository, registry, planner, execution, approval and reporting services. CSV upload validation enforces the built-in 10 MiB/100,000 row/256 column limits and generates an internal workspace path. The UI does not execute code from a file or plan. During execution, the existing engine runs in a worker while the UI polls persisted run/step state for actual status and completion progress. Ollama diagnostics use the local `/api/tags` endpoint.

### Phase 7 — packaging and operational hardening

Add Docker packaging, operational docs, CI hardening, Windows/Docker path verification, security review, and end-to-end regression coverage. Proposed files: `Dockerfile`, compose configuration if useful, `docs/operations.md`, CI updates and tests.

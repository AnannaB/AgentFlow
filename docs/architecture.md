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

## SQLite schema (planned)

All schema changes use ordered explicit migrations. Enable foreign keys. Persist timestamps as UTC ISO-8601 text. JSON payloads are size-limited and validated before storage.

### `workflow_runs`

`id` UUID primary key; `objective`; `status`; `planner_kind`; `plan_json`; `created_at`, `updated_at`, `started_at`, `finished_at`; optional `error_code`, sanitized `error_summary`; optional `source_run_id` for replay lineage.

### `workflow_steps`

`id` UUID primary key; `run_id` FK; `step_key`; `position`; `tool_name`; `arguments_json`; `depends_on_json`; `required`; `approval_required`; `status`; timestamps. Unique `(run_id, step_key)` and `(run_id, position)`.

### `step_attempts`

`id`; `step_id` FK; `attempt_number`; `status`; timestamps; `input_hash`; bounded `output_json`; sanitized error code/summary; `duration_ms`; unique `(step_id, attempt_number)`.

### `workflow_templates`

`id` UUID; `name`; `description`; validated `template_json`; `created_at`, `updated_at`; optional `archived_at` or `is_active`. Templates support safe variables and tool references; no credentials or run data. Template changes are versioned or copied into a run snapshot so later edits do not mutate history.

### `approval_requests`

`id`; `run_id` FK; optional `step_id` FK; `status`; requested/resolved timestamps; `decision_note`; `plan_digest`.

### `run_events`

`id`; `run_id` FK; `occurred_at`; `event_type`; bounded sanitized `details_json`. Useful for audit display, not a substitute for current state columns.

Index history by `(created_at, id)` and status/date as analytics needs become clear. Every execution-history query requires a limit and stable cursor/page ordering. Compute analytics over explicit date windows and bounded aggregates.

## Pydantic contracts

Use strict models with `extra="forbid"`, bounded fields/lists, and safe defaults (`Field(default_factory=list/dict)`). Core contracts include `PlanStep`, `ExecutionPlan`, `RunRequest`, `ToolResult`, `ApprovalDecision`, `WorkflowTemplate`, `StepExecutionRecord`, and report/analytics query models. Each registered tool owns its input and output model. Persisted JSON is revalidated when loaded.

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

### Phase 0 — foundation (current)

Package metadata/dependencies, validated settings, structured redacting logger, pytest/Ruff config, GitHub Actions, `.gitignore`, `.env.example`, README, and setup/architecture docs. No engine, tools, database business logic, or dashboard.

### Phase 1 — domain and persistence

Create domain contracts/state machine and explicit SQLite migrations/repositories. Add transaction and bounded history query tests. Proposed files: `domain/models.py`, `states.py`, `errors.py`, `storage/protocols.py`, `storage/sqlite.py`, `storage/migrations/`, unit/integration tests.

### Phase 2 — registry, templates, and rule planning

Implement typed tool registry, first useful tools, template CRUD/version snapshots, rule planner, and plan validation. Test template safety and plan constraints. Proposed files: `tools/`, `planning/rule_planner.py`, `planning/plan_validator.py`, template application service and tests.

### Phase 3 — execution and recovery

Implement workflow service, sequential dependency executor, transactional transitions, attempt history, cancellation and bounded retry/recovery. Add safe replay-as-new-run semantics and tests. Proposed files: `application/workflow_service.py`, `execution/`, repository updates and tests.

### Phase 4 — Ollama and diagnostics

Implement local Ollama adapter, timeout handling, model/connection diagnostics, and configurable fallback. Mock HTTP tests plus opt-in local smoke test. Proposed file: `planning/ollama_planner.py`, diagnostics service, config and docs updates.

### Phase 5 — approvals, history, analytics, reporting

Implement approval workflow, paginated history/replay inspection, validated retry/replay actions, bounded analytics and reports. Test pagination, metrics, digest checks, and side-effect safeguards. Proposed files: `application/approval_service.py`, `history_service.py`, `analytics_service.py`, `report_service.py`, tests.

### Phase 6 — Streamlit dashboard

Build professional dashboard with workflow graph, dependencies, state/progress, templates, run history, approval, analytics, diagnostics, loading/empty/error states, and input validation. Proposed files: `ui/app.py`, `ui/pages/`, UI/service acceptance tests; use existing Plotly dependency for the graph/metrics where appropriate.

### Phase 7 — packaging and operational hardening

Add Docker packaging, operational docs, CI hardening, Windows/Docker path verification, security review, and end-to-end regression coverage. Proposed files: `Dockerfile`, compose configuration if useful, `docs/operations.md`, CI updates and tests.

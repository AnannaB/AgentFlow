"""Streamlit page rendering for the local AgentFlow dashboard."""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID

import pandas as pd
import plotly.express as px
import streamlit as st

from agentflow.application.errors import (
    ApprovalError,
    ReportError,
    WorkflowExecutionError,
)
from agentflow.config import Settings
from agentflow.dashboard.controllers import (
    create_workflow_run,
    execute_workflow,
    generate_plan,
)
from agentflow.dashboard.diagnostics import OllamaDiagnosticError, list_local_models
from agentflow.dashboard.sales_demo import (
    SalesDemoError,
    create_sales_demo_plan,
    supports_sales_demo,
)
from agentflow.dashboard.services import DashboardServices
from agentflow.dashboard.uploads import (
    UploadedDataset,
    UploadValidationError,
    validate_and_store_csv,
)
from agentflow.domain.states import ApprovalStatus, RunStatus
from agentflow.planning.errors import PlanningError
from agentflow.planning.service import PlanningService

_SECRET_KEY = re.compile(r"(?i)(token|password|secret|credential|authorization|api[_-]?key)")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|authorization)"
    r"(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)


def render_overview(services: DashboardServices) -> None:
    st.title("Workflow overview")
    st.caption("A live view of local AgentFlow runs and their current outcomes.")
    counts = services.repository.count_runs_by_status()
    total = sum(counts.values())
    successful = counts.get(RunStatus.SUCCEEDED.value, 0)
    failed = counts.get(RunStatus.FAILED.value, 0)
    pending = sum(
        counts.get(status.value, 0)
        for status in (
            RunStatus.QUEUED,
            RunStatus.PLANNING,
            RunStatus.PLAN_REVIEW,
            RunStatus.AWAITING_APPROVAL,
            RunStatus.EXECUTING,
            RunStatus.VALIDATING,
            RunStatus.PAUSED,
        )
    )
    metric_columns = st.columns(4)
    for column, label, value in zip(
        metric_columns,
        ("Total workflows", "Successful", "Failed", "Pending / paused"),
        (total, successful, failed, pending),
        strict=True,
    ):
        column.metric(label, value)

    runs = services.repository.list_runs(limit=12)
    left, right = st.columns((1.25, 1))
    with left:
        st.subheader("Recent executions")
        if runs:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Created": run.created_at,
                            "Objective": _redact_text(run.objective),
                            "Status": run.status.value.replace("_", " ").title(),
                            "Planner": run.planner_kind.value.replace("_", " ").title(),
                            "Run ID": str(run.id)[:8],
                        }
                        for run in runs
                    ]
                ),
                hide_index=True,
                use_container_width=True,
            )
        else:
            st.info("No workflow runs yet. Create a plan to get started.")
    with right:
        st.subheader("Run distribution")
        chart_data = pd.DataFrame(
            [
                {"Status": name.replace("_", " ").title(), "Runs": count}
                for name, count in counts.items()
            ]
        )
        if chart_data.empty:
            st.info("Run statistics will appear after the first workflow is created.")
        else:
            chart = px.bar(
                chart_data.sort_values("Runs", ascending=False),
                x="Status",
                y="Runs",
                color="Status",
                color_discrete_sequence=["#6476b8", "#72a995", "#c97b7b", "#a69acb"],
            )
            chart.update_layout(
                showlegend=False,
                margin={"l": 8, "r": 8, "t": 8, "b": 8},
                plot_bgcolor="white",
                paper_bgcolor="white",
            )
            st.plotly_chart(chart, use_container_width=True)


def render_new_workflow(services: DashboardServices, settings: Settings) -> None:
    st.title("New workflow")
    st.caption("Upload a dataset, describe the outcome, and review the validated plan.")
    uploaded = st.file_uploader("Dataset (CSV)", type=["csv"], help="Maximum size: 10 MiB.")
    dataset = _load_upload(uploaded, services.workspace)
    if dataset is not None:
        st.session_state["agentflow_dataset"] = dataset
        st.session_state["agentflow_dataset_hash"] = hashlib.sha256(uploaded.getvalue()).hexdigest()
        _show_dataset(dataset)
    else:
        st.session_state.pop("agentflow_dataset", None)
        st.session_state.pop("agentflow_dataset_hash", None)
        if uploaded is not None:
            st.session_state.pop("agentflow_plan", None)
            st.session_state.pop("agentflow_created_run_id", None)

    objective = st.text_area(
        "What would you like AgentFlow to do?",
        max_chars=4000,
        height=120,
        placeholder=(
            "For example: Calculate 6 * 7, or profile the uploaded CSV and "
            "summarize missing values."
        ),
    )
    if st.button("Generate plan", type="primary", disabled=not objective.strip()):
        plan_objective = objective.strip()
        active_dataset = st.session_state.get("agentflow_dataset")
        if active_dataset is not None:
            plan_objective += (
                "\nThe uploaded CSV is available inside the configured workspace at "
                f"{active_dataset.relative_path}. Use this path for registered CSV tools."
            )
        with st.spinner("Planning with the configured local model and validating the result…"):
            try:
                planner = PlanningService(services.registry, settings)
                result = generate_plan(planner, plan_objective)
            except PlanningError as error:
                st.session_state.pop("agentflow_plan", None)
                st.error(str(error))
            except Exception:
                st.error(
                    "Planning could not be completed. Check Ollama and the local configuration."
                )
            else:
                st.session_state["agentflow_plan"] = result
                st.session_state.pop("agentflow_created_run_id", None)
                st.session_state.pop("agentflow_execution_result", None)
                st.success("Validated plan is ready for review. No workflow has been executed.")

    if dataset is not None and supports_sales_demo(dataset):
        if st.button("Use deterministic sales demo plan"):
            try:
                result, prepared = create_sales_demo_plan(
                    dataset, str(services.workspace), services.registry
                )
            except SalesDemoError as error:
                st.error(str(error))
            except Exception:
                st.error("The deterministic sales demo plan could not be prepared.")
            else:
                dataset = prepared
                st.session_state["agentflow_dataset"] = prepared
                st.session_state["agentflow_plan"] = result
                st.session_state.pop("agentflow_created_run_id", None)
                st.session_state.pop("agentflow_execution_result", None)
                st.success(
                    "Validated deterministic demo plan is ready for review. "
                    "No Ollama request was made."
                )
                st.info(
                    "The original upload is preserved. Registered CSV tools will use a "
                    "workspace copy with calculated revenue and calendar month columns."
                )

    result = st.session_state.get("agentflow_plan")
    if result is None:
        st.info("Your plan will appear here after you generate it.")
        return
    _show_plan(result, services)

    created_run_id = st.session_state.get("agentflow_created_run_id")
    if created_run_id is None:
        if st.button("Save reviewed plan as workflow", type="secondary"):
            try:
                planner = PlanningService(services.registry, settings)
                run = create_workflow_run(planner, result, services.execution)
            except (PlanningError, WorkflowExecutionError) as error:
                st.error(str(error))
            except Exception:
                st.error("The validated plan could not be saved as a workflow.")
            else:
                st.session_state["agentflow_created_run_id"] = str(run.id)
                st.success(f"Workflow saved as `{run.id}`. It has not been run yet.")
                st.rerun()
    else:
        st.info(
            f"Saved workflow: `{created_run_id}`. Review is complete; execution is still separate."
        )
        if st.button("Run workflow", type="primary"):
            _execute_with_feedback(services, UUID(created_run_id))


def render_execution(services: DashboardServices) -> None:
    st.title("Workflow execution")
    runs = services.repository.list_runs(limit=200)
    if not runs:
        st.info("No saved workflows are available yet.")
        return
    run_options = {str(run.id): run for run in runs}
    selected = st.selectbox(
        "Select a workflow",
        list(run_options),
        format_func=lambda run_id: _run_label(run_options[run_id]),
    )
    run = services.repository.get_run(UUID(selected))
    if run is None:
        st.error("This workflow could not be found. Refresh the page and try again.")
        return
    st.write(_redact_text(run.objective))
    if run.status in {RunStatus.QUEUED, RunStatus.PLANNING, RunStatus.AWAITING_APPROVAL}:
        if run.status == RunStatus.AWAITING_APPROVAL:
            st.info("This workflow is waiting for an approval. Resolve it on the Approvals page.")
            action_label = "Continue approved workflow"
        else:
            action_label = "Run workflow"
        if st.button(action_label, type="primary"):
            _execute_with_feedback(services, run.id)
            run = services.repository.get_run(run.id) or run
    elif run.status == RunStatus.PAUSED:
        st.warning(
            "This run is paused for recovery review. AgentFlow does not automatically replay "
            "an uncertain attempt."
        )
    st.markdown(_status_badge(run.status.value), unsafe_allow_html=True)
    _show_persisted_steps(services, run.id)
    if run.status in {RunStatus.SUCCEEDED, RunStatus.FAILED}:
        st.success("A workflow report is available on the Reports page.")


def render_approvals(services: DashboardServices) -> None:
    st.title("Approvals")
    st.caption("Resolve persisted requests using the backend’s exact scope and digest checks.")
    try:
        requests = services.approvals.list_pending(limit=100)
    except Exception:
        st.error("Pending approval requests could not be loaded.")
        return
    if not requests:
        st.info("You are all caught up. There are no pending approval requests.")
        return
    for request in requests:
        run = services.repository.get_run(request.run_id)
        title = f"{request.purpose.replace('_', ' ').title()} · {str(request.run_id)[:8]}"
        with st.container():
            st.subheader(title)
            st.write(_redact_text(run.objective) if run else "Workflow details are unavailable.")
            st.caption(
                f"Step: {request.step_id or 'workflow export'} · Requested: "
                f"{request.requested_at:%Y-%m-%d %H:%M UTC}"
            )
            with st.form(f"approval-{request.id}"):
                note = st.text_input("Decision note (optional)", max_chars=4000)
                approve_col, reject_col = st.columns(2)
                approve = approve_col.form_submit_button("Approve", type="primary")
                reject = reject_col.form_submit_button("Reject")
            if approve or reject:
                if run is None:
                    st.error("The owning workflow could not be found.")
                    continue
                try:
                    action = services.approvals.approve if approve else services.approvals.reject
                    resolved = action(
                        request.id,
                        run_id=request.run_id,
                        step_id=request.step_id,
                        plan_digest=request.plan_digest,
                        decision_note=note or None,
                    )
                except ApprovalError as error:
                    st.error(str(error))
                except Exception:
                    st.error("The approval decision could not be saved.")
                else:
                    st.success(f"Request {resolved.status.value}.")
                    st.rerun()
    st.caption("Approval actions apply to the local operator using this dashboard.")


def render_reports(services: DashboardServices) -> None:
    st.title("Workflow reports")
    completed = [
        run
        for run in services.repository.list_runs(limit=200)
        if run.status in {RunStatus.SUCCEEDED, RunStatus.FAILED}
    ]
    if not completed:
        st.info("Completed or failed runs will appear here with report and export options.")
        return
    options = {str(run.id): run for run in completed}
    selected = st.selectbox(
        "Choose a completed workflow",
        list(options),
        format_func=lambda run_id: _run_label(options[run_id]),
    )
    run_id = UUID(selected)
    try:
        report = services.reports.build_report(run_id)
    except ReportError as error:
        st.error(str(error))
        return
    except Exception:
        st.error("The workflow report could not be built.")
        return
    st.markdown(_status_badge(report.status.value), unsafe_allow_html=True)
    st.json(report.model_dump(mode="json"))

    markdown_col, json_col = st.columns(2)
    with markdown_col:
        if st.button("Export Markdown to workspace"):
            try:
                exported = services.reports.export_markdown(run_id, f"reports/{run_id}.md")
                content = (services.workspace / exported.relative_path).read_bytes()
            except ReportError as error:
                st.error(str(error))
            except OSError:
                st.error("Markdown export completed but the file could not be opened for download.")
            else:
                st.success(f"Saved `{exported.relative_path}` · {exported.bytes_written:,} bytes")
                st.download_button(
                    "Download Markdown",
                    content,
                    file_name=f"agentflow-{run_id}.md",
                    mime="text/markdown",
                    key=f"download-md-{run_id}",
                )
    with json_col:
        approval_key = f"agentflow_json_approval_{run_id}"
        approval_id = st.session_state.get(approval_key)
        if approval_id is None:
            if st.button("Request JSON export approval"):
                try:
                    request = services.reports.request_json_export(run_id, f"reports/{run_id}.json")
                except ReportError as error:
                    st.error(str(error))
                except Exception:
                    st.error("The JSON export request could not be created.")
                else:
                    st.session_state[approval_key] = str(request.approval_id)
                    st.info("Request saved. Approve it on the Approvals page before export.")
                    st.rerun()
        else:
            persisted_approval = services.repository.get_approval(UUID(approval_id))
            if persisted_approval is None:
                st.warning("The saved JSON export approval request could not be found.")
                st.session_state.pop(approval_key, None)
            elif persisted_approval.status == ApprovalStatus.PENDING:
                st.info("JSON export is waiting for approval. Resolve it on the Approvals page.")
            elif persisted_approval.status in {
                ApprovalStatus.REJECTED,
                ApprovalStatus.EXPIRED,
            }:
                st.warning(f"The JSON export request was {persisted_approval.status.value}.")
                if st.button("Request another JSON export approval"):
                    _request_json_approval(services, run_id, approval_key)
            elif persisted_approval.status == ApprovalStatus.APPROVED:
                st.caption(f"JSON export approval request: `{approval_id[:8]}`")
                if st.button("Export approved JSON to workspace"):
                    try:
                        exported = services.reports.export_json(
                            run_id,
                            f"reports/{run_id}.json",
                            approval_id=UUID(approval_id),
                        )
                        content = (services.workspace / exported.relative_path).read_bytes()
                    except ReportError as error:
                        st.error(str(error))
                    except OSError:
                        st.error(
                            "JSON export completed but the file could not be opened for download."
                        )
                    else:
                        st.success(
                            f"Saved `{exported.relative_path}` · {exported.bytes_written:,} bytes"
                        )
                        st.download_button(
                            "Download JSON",
                            content,
                            file_name=f"agentflow-{run_id}.json",
                            mime="application/json",
                            key=f"download-json-{run_id}",
                        )


def render_settings(settings: Settings, on_settings_changed: Callable[[Settings], None]) -> None:
    st.title("Settings")
    st.caption("Configure local Ollama access and the workspace for this dashboard session.")
    with st.form("agentflow-settings"):
        base_url = st.text_input("Ollama base URL", value=settings.ollama_base_url)
        available_models = st.session_state.get("agentflow_ollama_models", ())
        if available_models:
            options = list(
                dict.fromkeys((settings.ollama_model, *available_models, "Custom model…"))
            )
            selected_model = st.selectbox("Ollama model", options)
            model = (
                st.text_input("Custom model name", value="")
                if selected_model == "Custom model…"
                else selected_model
            )
        else:
            model = st.text_input("Ollama model", value=settings.ollama_model)
        timeout = st.number_input(
            "Request timeout (seconds)",
            min_value=1.0,
            max_value=600.0,
            value=float(settings.ollama_timeout_seconds),
            step=1.0,
        )
        fallback = st.checkbox(
            "Enable deterministic fallback planning", value=settings.ollama_fallback_enabled
        )
        workspace_path = st.text_input("Workspace directory", value=str(settings.workspace_path))
        database_path = st.text_input("SQLite database path", value=str(settings.database_path))
        submitted = st.form_submit_button("Apply settings", type="primary")
    if submitted:
        try:
            candidate = Settings(
                ollama_base_url=base_url,
                ollama_model=model,
                ollama_timeout_seconds=timeout,
                ollama_fallback_enabled=fallback,
                log_level=settings.log_level,
                workspace_path=Path(workspace_path),
                database_path=Path(database_path),
            )
            candidate.workspace_path.expanduser().resolve().mkdir(parents=True, exist_ok=True)
        except Exception:
            st.error(
                "Settings are invalid. Use an absolute HTTP(S) Ollama URL without credentials, "
                "a non-empty model name, and accessible local paths."
            )
        else:
            on_settings_changed(candidate)
            st.success("Settings applied to this dashboard session.")
            st.rerun()

    st.subheader("Ollama connection")
    if st.button("Test local Ollama connection"):
        with st.spinner("Checking the local Ollama model list…"):
            try:
                models = list_local_models(settings)
            except OllamaDiagnosticError as error:
                st.error(str(error))
            else:
                st.session_state["agentflow_ollama_models"] = models
                st.success("Connected to local Ollama.")
                if models:
                    st.write("Available models: " + ", ".join(models))
                else:
                    st.info("Ollama is reachable but has no downloaded models.")
    st.caption("These settings are held in this browser session and are not written to .env.")


def _load_upload(uploaded_file, workspace: Path) -> UploadedDataset | None:
    if uploaded_file is None:
        return None
    content = uploaded_file.getvalue()
    fingerprint = hashlib.sha256(content).hexdigest()
    existing = st.session_state.get("agentflow_dataset")
    if (
        existing is not None
        and st.session_state.get("agentflow_dataset_hash") == fingerprint
        and existing.path.is_file()
        and existing.path.is_relative_to(workspace.expanduser().resolve())
    ):
        return existing
    try:
        return validate_and_store_csv(uploaded_file.name, content, workspace)
    except UploadValidationError as error:
        st.error(str(error))
        return None


def _show_dataset(dataset: UploadedDataset) -> None:
    st.markdown("<div class='af-card'>", unsafe_allow_html=True)
    st.subheader("Dataset preview")
    metric_columns = st.columns(3)
    metric_columns[0].metric("Rows", f"{len(dataset.frame):,}")
    metric_columns[1].metric("Columns", len(dataset.frame.columns))
    metric_columns[2].metric("File size", f"{dataset.byte_count / 1024:.1f} KiB")
    st.caption(f"Stored inside workspace at `{dataset.relative_path}`")
    st.dataframe(dataset.frame.head(20), hide_index=True, use_container_width=True)
    st.caption(
        "Column types: "
        + " · ".join(f"{col}: {dtype}" for col, dtype in dataset.frame.dtypes.items())
    )
    st.markdown("</div>", unsafe_allow_html=True)


def _show_plan(result, services: DashboardServices) -> None:
    st.subheader("Plan review")
    planner_label = "Ollama" if result.planner_kind.value == "ollama" else "Rule-based fallback"
    st.info(
        f"Plan source: **{planner_label}** · {result.duration_ms} ms"
        + (f" · fallback reason: `{result.fallback_reason}`" if result.fallback_reason else "")
    )
    descriptors = {item.metadata.name: item for item in services.registry.discover()}
    for index, step in enumerate(result.plan.steps, start=1):
        descriptor = descriptors.get(step.tool_name)
        description = (
            descriptor.metadata.description
            if descriptor
            else "Registered tool description unavailable."
        )
        with st.container():
            st.markdown(f"**{index}. {step.purpose}**")
            st.caption(
                f"`{step.step_key}` · `{step.tool_name}` · risk: {step.risk_level} · "
                f"dependencies: {', '.join(step.depends_on) or 'none'}"
            )
            st.write(_redact_text(description))
            st.json(_redact_mapping(step.arguments), expanded=False)
    if result.plan.completion_criteria:
        st.write("Completion criteria: " + "; ".join(result.plan.completion_criteria))


def _execute_with_feedback(services: DashboardServices, run_id: UUID) -> None:
    initial_steps = services.repository.list_steps(run_id)
    total_steps = len(initial_steps)
    progress = st.progress(0, text=f"0 of {total_steps} steps completed")
    last_states: dict[str, str] = {}
    with st.status("Workflow is running", expanded=True) as status:
        try:
            with ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="agentflow-dashboard"
            ) as pool:
                future = pool.submit(
                    execute_workflow, services.execution, run_id, services.workspace
                )
                while not future.done():
                    current_run = services.repository.get_run(run_id)
                    current_steps = services.repository.list_steps(run_id)
                    completed = 0
                    for step in current_steps:
                        state = step.status.value
                        if state in {"succeeded", "failed", "skipped", "blocked", "cancelled"}:
                            completed += 1
                        if last_states.get(step.step_key) != state:
                            status.write(f"{step.step_key}: {state.replace('_', ' ')}")
                            last_states[step.step_key] = state
                    if total_steps:
                        progress.progress(
                            min(1.0, completed / total_steps),
                            text=f"{completed} of {total_steps} steps completed",
                        )
                    if current_run and current_run.status == RunStatus.AWAITING_APPROVAL:
                        status.write("Execution is waiting for a persisted approval.")
                    time.sleep(0.2)
                result = future.result()
            final_run = services.repository.get_run(run_id)
            final_steps = services.repository.list_steps(run_id)
            completed = sum(
                step.status.value in {"succeeded", "failed", "skipped", "blocked", "cancelled"}
                for step in final_steps
            )
            if total_steps:
                progress.progress(
                    min(1.0, completed / total_steps),
                    text=f"{completed} of {total_steps} steps completed",
                )
            status.update(
                label=(
                    f"Workflow {final_run.status.value.replace('_', ' ')}"
                    if final_run
                    else "Workflow finished"
                ),
                state=(
                    "error"
                    if result.status in {RunStatus.FAILED, RunStatus.CANCELLED}
                    else "complete"
                ),
            )
        except WorkflowExecutionError as error:
            status.update(label="Workflow could not run", state="error")
            st.error(str(error))
        except Exception:
            status.update(label="Workflow execution failed", state="error")
            st.error(
                "Workflow execution failed. Inspect the run history for persisted step details."
            )
        else:
            st.session_state["agentflow_execution_result"] = result
            st.session_state["agentflow_created_run_id"] = str(run_id)
            st.success(f"Workflow finished with status **{result.status.value}**.")
            if result.failure:
                st.error(result.failure.message)
            for step in result.steps:
                with st.expander(
                    f"{step.step_key} · {step.status.value} · {step.attempts} attempt(s)"
                ):
                    st.caption(f"Tool: `{step.tool_name}` · duration: {step.duration_ms} ms")
                    if step.output is not None:
                        st.json(_redact_mapping(step.output))
                        _show_best_product(step.step_key, step.tool_name, step.output)
                    if step.failure:
                        st.error(step.failure.message)


def _show_persisted_steps(services: DashboardServices, run_id: UUID) -> None:
    steps = services.repository.list_steps(run_id)
    if not steps:
        st.info("This saved workflow does not have step records.")
        return
    st.subheader("Execution steps")
    for step in steps:
        attempts = services.repository.list_attempts(step.id)
        with st.container():
            st.markdown(
                f"**{step.position + 1}. {step.step_key}** · {_status_badge(step.status.value)}",
                unsafe_allow_html=True,
            )
            st.caption(f"`{step.tool_name}` · dependencies: {', '.join(step.depends_on) or 'none'}")
            if not attempts:
                st.caption("No tool attempt has been recorded yet.")
            for attempt in attempts:
                with st.expander(f"Attempt {attempt.attempt_number} · {attempt.status.value}"):
                    if attempt.output and attempt.output.success:
                        st.json(_redact_mapping(attempt.output.data))
                        _show_best_product(step.step_key, step.tool_name, attempt.output.data)
                    if attempt.error_summary:
                        st.error(_redact_text(attempt.error_summary))
                    if attempt.error_code:
                        st.caption(f"Error code: `{attempt.error_code}`")


def _run_label(run) -> str:
    objective = run.objective.replace("\n", " ")
    return f"{_redact_text(objective[:70])} · {run.status.value} · {str(run.id)[:8]}"


def _status_badge(status: str) -> str:
    label = status.replace("_", " ").title()
    return f"<span class='af-badge af-{status}'>{label}</span>"


def _show_best_product(step_key: str, tool_name: str, output: dict) -> None:
    if step_key != "revenue_by_product" or tool_name != "data_aggregator":
        return
    rows = output.get("rows")
    if not isinstance(rows, list):
        return
    candidates = [
        row
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("product"), str)
        and isinstance(row.get("revenue"), (int, float))
    ]
    if candidates:
        best = max(candidates, key=lambda row: row["revenue"])
        st.markdown("**Best-performing product**")
        product_column, revenue_column = st.columns(2)
        product_column.metric("Product", str(best["product"]))
        revenue_column.metric("Revenue", f"{best['revenue']:,.2f}")


def _redact_mapping(value: dict) -> dict:
    return {
        key: "[REDACTED]" if _SECRET_KEY.search(str(key)) else _redact_nested(item)
        for key, item in value.items()
    }


def _redact_nested(value):
    if isinstance(value, dict):
        return _redact_mapping(value)
    if isinstance(value, list):
        return [_redact_nested(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _redact_text(value: str) -> str:
    value = _SECRET_ASSIGNMENT.sub(r"\1\2[REDACTED]", value)
    value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [REDACTED]", value)
    return re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", value)


def _request_json_approval(services: DashboardServices, run_id: UUID, session_key: str) -> None:
    try:
        request = services.reports.request_json_export(run_id, f"reports/{run_id}.json")
    except ReportError as error:
        st.error(str(error))
    except Exception:
        st.error("The JSON export request could not be created.")
    else:
        st.session_state[session_key] = str(request.approval_id)
        st.info("Request saved. Approve it on the Approvals page before export.")
        st.rerun()

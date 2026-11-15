"""Entry point for the AgentFlow Streamlit dashboard."""

from pathlib import Path

import streamlit as st

from agentflow.config import Settings, get_settings
from agentflow.dashboard.services import DashboardServices, build_dashboard_services
from agentflow.dashboard.views import (
    render_approvals,
    render_execution,
    render_new_workflow,
    render_overview,
    render_reports,
    render_settings,
)

_PAGES = {
    "Overview": render_overview,
    "New workflow": render_new_workflow,
    "Execution history": render_execution,
    "Approvals": render_approvals,
    "Reports": render_reports,
    "Settings": render_settings,
}


@st.cache_resource(show_spinner=False)
def _cached_services(database_path: str, workspace_path: str) -> DashboardServices:
    return build_dashboard_services(
        Settings(
            database_path=Path(database_path),
            workspace_path=Path(workspace_path),
        )
    )


def main() -> None:
    st.set_page_config(
        page_title="AgentFlow",
        page_icon="◈",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    _apply_styles()
    if "agentflow_settings" not in st.session_state:
        st.session_state["agentflow_settings"] = get_settings()
    settings: Settings = st.session_state["agentflow_settings"]
    try:
        services = _cached_services(
            str(settings.database_path.expanduser().resolve()),
            str(settings.workspace_path.expanduser().resolve()),
        )
    except Exception:
        st.error(
            "AgentFlow could not initialize its local database or workspace. "
            "Check the configured paths and filesystem permissions."
        )
        st.stop()

    with st.sidebar:
        st.markdown("<div class='af-brand'><span>◈</span> AgentFlow</div>", unsafe_allow_html=True)
        st.caption("LOCAL WORKFLOW STUDIO")
        page = st.radio("Workspace", tuple(_PAGES), label_visibility="collapsed")
        st.divider()
        st.markdown("<div class='af-sidebar-note'>", unsafe_allow_html=True)
        st.caption(f"Model · {settings.ollama_model}")
        st.caption(f"Workspace · {settings.workspace_path}")
        st.caption("Local-first · Ollama + registered tools")
        st.markdown("</div>", unsafe_allow_html=True)

    with st.container():
        if page == "Settings":
            render_settings(settings, lambda value: _store_settings(value))
        else:
            _PAGES[page](services, settings) if page == "New workflow" else _PAGES[page](services)


def _store_settings(value: Settings) -> None:
    previous: Settings = st.session_state.get("agentflow_settings", get_settings())
    if previous.workspace_path != value.workspace_path:
        for key in (
            "agentflow_dataset",
            "agentflow_dataset_hash",
            "agentflow_plan",
            "agentflow_created_run_id",
            "agentflow_execution_result",
        ):
            st.session_state.pop(key, None)
    elif previous.database_path != value.database_path:
        st.session_state.pop("agentflow_created_run_id", None)
        st.session_state.pop("agentflow_execution_result", None)
    st.session_state["agentflow_settings"] = value


def _apply_styles() -> None:
    st.markdown(
        """
        <style>
        :root { --af-ink: #202638; --af-muted: #737b8b; --af-accent: #6677b8; }
        .stApp { background: #f7f8fb; color: var(--af-ink); }
        [data-testid="stSidebar"] { background: #20232c; }
        [data-testid="stSidebar"] * { color: #e7e9f1; }
        [data-testid="stSidebar"] [data-testid="stRadio"] label { padding: .35rem .45rem; }
        [data-testid="stSidebar"] [data-testid="stRadio"] label:hover {
            background: #303440;
            border-radius: .55rem;
        }
        [data-testid="stMain"] { background: #f7f8fb; }
        h1 { letter-spacing: -.035em; font-weight: 650; }
        h2, h3 { letter-spacing: -.02em; }
        [data-testid="stMetric"] {
            background: white;
            padding: 1rem 1.1rem;
            border: 1px solid #e9ebf1;
            border-radius: .85rem;
        }
        [data-testid="stMetricValue"] { color: var(--af-ink); }
        [data-testid="stVerticalBlockBorderWrapper"] {
            border-color: #e7eaf1;
            border-radius: .85rem;
            background: white;
        }
        .af-brand {
            display:flex;
            gap:.65rem;
            align-items:center;
            color:white;
            font-size:1.35rem;
            font-weight:700;
            margin:.3rem 0 .1rem;
        }
        .af-brand span { color:#aab5e8; font-size:1.6rem; }
        .af-sidebar-note { color:#b9beca; font-size:.8rem; line-height:1.5; }
        .af-badge {
            display:inline-block;
            padding:.3rem .7rem;
            border-radius:2rem;
            font-size:.8rem;
            font-weight:650;
            text-transform:capitalize;
            background:#edf0f8;
            color:#52608f;
        }
        .af-succeeded { color:#27674d; background:#e6f4ed; }
        .af-failed, .af-cancelled { color:#943f49; background:#fbeaec; }
        .af-awaiting_approval, .af-paused { color:#835d1b; background:#fbf2df; }
        .af-executing, .af-planning { color:#4e5f9a; background:#e9edfb; }
        .af-card {
            background:white;
            border:1px solid #e9ebf1;
            border-radius:.85rem;
            padding:.75rem 1rem 1rem;
            margin:.6rem 0;
        }
        div.stButton > button[kind="primary"] { background:#6677b8; border-color:#6677b8; }
        </style>
        """,
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()

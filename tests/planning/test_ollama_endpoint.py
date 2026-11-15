"""Opt-in diagnostic for the Ollama endpoint configured in this environment."""

import os

import pytest

from agentflow.config import get_settings
from agentflow.dashboard.diagnostics import list_local_models


@pytest.mark.skipif(
    os.environ.get("AGENTFLOW_DIAGNOSE_OLLAMA") != "1",
    reason="Set AGENTFLOW_DIAGNOSE_OLLAMA=1 to probe the configured local Ollama endpoint",
)
def test_configured_ollama_endpoint_lists_models():
    """Probe configured /api/tags without printing its URL or model list."""
    models = list_local_models(get_settings())

    assert models, "Ollama responded but did not report any installed models."

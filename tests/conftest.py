import pytest


@pytest.fixture(autouse=True)
def _no_tracker_sheet(monkeypatch):
    """Never refresh the real Google Sheet from a test (load_env does not override a set variable)."""
    monkeypatch.setenv("TRACKER_SHEET_ID", "")

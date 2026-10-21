import pytest


@pytest.fixture(autouse=True)
def _isolate_from_live_env(monkeypatch):
    """Keep the repo .env's live settings out of tests (load_dotenv never overrides a set variable):
    no Google Sheet refresh, and the tracker sync's default source (tests that want postgres set it)."""
    monkeypatch.setenv("TRACKER_SHEET_ID", "")
    monkeypatch.setenv("TRACKER_SOURCE", "")

import pytest

TEST_PARENT_KEY = "test-parent-key"


@pytest.fixture(autouse=True)
def _parent_key(monkeypatch):
    monkeypatch.setenv("OLYMP_PARENT_KEY", TEST_PARENT_KEY)

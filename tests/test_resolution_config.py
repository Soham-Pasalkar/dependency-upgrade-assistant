"""NFR-13: resolver limits are server-controlled with hard ceilings (M12 criterion 9, configuration part)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.api.config import Settings

DEFAULTS = {
    "RESOLVER_TIMEOUT_SECONDS": 180,
    "RESOLVER_MEMORY_MB": 1024,
    "RESOLVER_CPUS": 1.0,
    "RESOLVER_PIDS_LIMIT": 256,
    "RESOLVER_TMPFS_MB": 512,
    "RESOLVER_MAX_CONCURRENT": 2,
    "RESOLVER_MAX_REPORT_MB": 10,
    "RESOLVER_MAX_PACKAGES": 500,
}
CEILINGS = {
    "RESOLVER_TIMEOUT_SECONDS": 600,
    "RESOLVER_MEMORY_MB": 4096,
    "RESOLVER_CPUS": 4,
    "RESOLVER_PIDS_LIMIT": 1024,
    "RESOLVER_TMPFS_MB": 2048,
    "RESOLVER_MAX_CONCURRENT": 4,
    "RESOLVER_MAX_REPORT_MB": 50,
    "RESOLVER_MAX_PACKAGES": 2000,
}


def test_defaults_match_nfr13():
    s = Settings()
    for name, value in DEFAULTS.items():
        assert getattr(s, name) == value, name


@pytest.mark.parametrize("name", sorted(CEILINGS))
def test_ceiling_is_accepted(name):
    assert getattr(Settings(**{name: CEILINGS[name]}), name) == CEILINGS[name]


@pytest.mark.parametrize("name", sorted(CEILINGS))
def test_value_above_ceiling_refuses_startup(name):
    with pytest.raises(ValidationError):
        Settings(**{name: CEILINGS[name] + 1})


@pytest.mark.parametrize("name", sorted(CEILINGS))
@pytest.mark.parametrize("value", [0, -1])
def test_value_that_would_disable_a_limit_is_refused(name, value):
    with pytest.raises(ValidationError):
        Settings(**{name: value})


def test_ceiling_enforced_from_environment(monkeypatch):
    monkeypatch.setenv("RESOLVER_TIMEOUT_SECONDS", "601")
    with pytest.raises(ValidationError):
        Settings()

"""Shared pytest setup.

scheduler.run_all() waits for the network (up to NETWORK_WAIT_SECONDS) and
holds Windows awake for the run. Neither belongs in a unit test: an offline
machine would stall each run_all() call for 10 minutes, and a test must not
change the host's power state. Tests that exercise those behaviours inject
their own resolver / setter.
"""
import pytest


@pytest.fixture(autouse=True)
def _no_network_wait_or_keep_awake(monkeypatch):
    monkeypatch.setenv("NETWORK_WAIT_SECONDS", "0")
    monkeypatch.setenv("KEEP_AWAKE_DURING_RUN", "0")

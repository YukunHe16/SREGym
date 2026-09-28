"""Hermetic conditions for the run-report tests.

These tests are pure unit/regression tests: they fake the labeller or pass an
``httpx.MockTransport`` and must never depend on the developer's shell. The
``LABELLER_*`` credentials could leak a real key into a test that must stay
local, and a real network connection would reach a provider.

A test that genuinely wants one of these variables still sets it explicitly
with ``monkeypatch``, which takes precedence over this fixture.
"""

import socket

import pytest

AMBIENT = (
    "LABELLER_API_BASE",
    "LABELLER_API_KEY",
)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for name in AMBIENT:
        monkeypatch.delenv(name, raising=False)


class NetworkBlocked(RuntimeError):
    """A test tried to open a real network connection."""


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Enforce, rather than merely intend, that these tests stay offline.

    The suite already avoids the wire by faking the labeller or by passing an
    ``httpx.MockTransport``. This guard turns that into a property: a future
    change that reaches for the network fails here instead of at the provider.
    """

    def blocked(*args, **kwargs):
        raise NetworkBlocked(
            "tests/results must not reach the network; "
            "use a fake labeller or httpx.MockTransport"
        )

    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)

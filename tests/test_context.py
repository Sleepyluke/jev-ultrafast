"""Owned ephemeral context lifecycle, without Chrome or model calls."""
from unittest.mock import Mock

import pytest

from jev_ultrafast import context


@pytest.fixture
def connection(monkeypatch):
    transport = Mock()
    transport.call.return_value = {"browserContextId": "synthetic-owned-context"}
    factory = Mock(return_value=transport)
    monkeypatch.setattr(context, "DirectCDP", factory)
    return transport, factory


def test_context_supplies_both_binding_options_and_disposes_only_its_own_id(connection):
    transport, factory = connection
    with context.IsolatedContext("ws://localhost/synthetic") as scope:
        assert scope.browser_options == {
            "browser_context_id": "synthetic-owned-context", "browser_ws_url": "ws://localhost/synthetic"}
        transport.call.assert_called_once_with("Target.createBrowserContext", disposeOnDetach=True)
        factory.assert_called_once_with("ws://localhost/synthetic")
        transport.close.assert_not_called()
    assert transport.call.call_args_list[-1].args == ("Target.disposeBrowserContext",)
    assert transport.call.call_args_list[-1].kwargs == {"browserContextId": "synthetic-owned-context"}
    transport.close.assert_called_once()
    scope.close()
    assert transport.call.call_count == 2


@pytest.mark.parametrize("response", [{}, {"browserContextId": ""}, {"browserContextId": " padded "},
                                      {"browserContextId": False}, {"browserContextId": 12}])
def test_unconfirmed_creation_disconnects_without_guessing_a_context_or_retrying(connection, response):
    transport, _ = connection
    transport.call.return_value = response
    with pytest.raises(RuntimeError):
        context.IsolatedContext("ws://localhost/synthetic")
    transport.call.assert_called_once_with("Target.createBrowserContext", disposeOnDetach=True)
    transport.close.assert_called_once()


def test_lost_creation_reply_disconnects_without_retry(connection):
    transport, _ = connection
    transport.call.side_effect = RuntimeError("connection lost")
    with pytest.raises(RuntimeError, match="connection lost"):
        context.IsolatedContext("ws://localhost/synthetic")
    assert transport.call.call_count == 1
    transport.close.assert_called_once()


def test_dispose_failure_visible_and_not_retried(connection):
    transport, _ = connection
    scope = context.IsolatedContext("ws://localhost/synthetic")
    transport.call.side_effect = RuntimeError("dispose failed")
    with pytest.raises(RuntimeError, match="dispose failed"):
        scope.close()
    scope.close()
    assert transport.call.call_count == 2
    transport.close.assert_called_once()
    with pytest.raises(RuntimeError):
        _ = scope.browser_options
    with pytest.raises(RuntimeError):
        scope.__enter__()


def test_body_failure_is_preserved_when_dispose_also_fails(connection):
    transport, _ = connection
    body_error = ValueError("original job failure")
    with pytest.raises(ValueError) as caught:
        with context.IsolatedContext("ws://localhost/synthetic"):
            transport.call.side_effect = RuntimeError("dispose failed")
            raise body_error
    assert caught.value is body_error
    assert any("cleanup" in note for note in getattr(body_error, "__notes__", []))
    transport.close.assert_called_once()


def test_each_scope_keeps_a_dedicated_creator_connection(connection):
    _, factory = connection
    first, second = Mock(), Mock()
    first.call.return_value = {"browserContextId": "context-a"}
    second.call.return_value = {"browserContextId": "context-b"}
    factory.side_effect = [first, second]
    a = context.IsolatedContext("ws://localhost/synthetic")
    b = context.IsolatedContext("ws://localhost/synthetic")
    options = a.browser_options
    options["browser_context_id"] = "untrusted-change"
    a.close()
    first.call.assert_called_with("Target.disposeBrowserContext", browserContextId="context-a")
    second.close.assert_not_called()
    assert b.browser_options["browser_context_id"] == "context-b"
    b.close()


def test_public_export():
    from jev_ultrafast import IsolatedContext

    assert IsolatedContext is context.IsolatedContext

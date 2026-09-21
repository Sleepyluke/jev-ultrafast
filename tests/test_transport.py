"""Offline wire/dependency regressions. No Chrome, credentials, or model calls."""
import asyncio
import json
from threading import Thread
from unittest.mock import AsyncMock, Mock

import pytest
from websockets.sync.server import serve

from jev_ultrafast import browser, transport


def test_pinned_harness_strips_target_session_so_scoped_mode_must_bypass_it(monkeypatch):
    from browser_harness import daemon

    monkeypatch.setattr(daemon.ipc, "expected_token", lambda: None)
    instance = daemon.Daemon.__new__(daemon.Daemon)
    instance.cdp = Mock(send_raw=AsyncMock(return_value={}))
    asyncio.run(instance.handle({"method": "Target.getTargetInfo", "session_id": "member-session"}))
    instance.cdp.send_raw.assert_awaited_once_with("Target.getTargetInfo", {}, session_id=None)


@pytest.fixture
def socket_mock(monkeypatch):
    sock = Mock()
    monkeypatch.setattr(transport, "connect", Mock(return_value=sock))
    return sock


@pytest.mark.parametrize("url", [None, 42, "", "http://localhost", "ws://", "ws://localhost:bad", "ws://local host"])
def test_invalid_endpoint_is_sanitized_before_connect(socket_mock, url):
    with pytest.raises((ValueError, RuntimeError)) as error:
        transport.DirectCDP(url)
    assert "localhost" not in str(error.value)
    transport.connect.assert_not_called()


@pytest.mark.parametrize("reply", [
    '{"id":1,"sessionId":"wrong","result":{}}',
    '{"id":2,"result":{}}',
    '{"id":true,"result":{}}',
    '{"id":1,"result":[]}',
    '{"id":1,"error":{"message":"secret-provider-detail"}}',
    'invalid-secret-json',
])
def test_bad_reply_closes_without_retry_or_payload_disclosure(socket_mock, reply):
    socket_mock.recv.return_value = reply
    direct = transport.DirectCDP("ws://localhost/synthetic")
    with pytest.raises(RuntimeError) as error:
        direct.call("Page.navigate", url="https://example.test")
    assert "secret" not in str(error.value)
    with pytest.raises(RuntimeError):
        direct.call("Page.navigate", url="https://example.test")
    assert socket_mock.send.call_count == 1
    socket_mock.close.assert_called_once()


def test_transport_timeout_is_terminal_and_never_retries_input(socket_mock):
    socket_mock.recv.side_effect = TimeoutError("secret endpoint")
    direct = transport.DirectCDP("ws://localhost/synthetic")
    with pytest.raises(RuntimeError):
        direct.call("Input.insertText", session_id="member-session", text="synthetic")
    with pytest.raises(RuntimeError):
        direct.call("Input.insertText", session_id="member-session", text="synthetic")
    assert socket_mock.send.call_count == 1


@pytest.mark.parametrize("reply_session", [None, "wrong"])
def test_scoped_reply_requires_its_session(socket_mock, reply_session):
    reply = {"id": 1, "result": {}}
    if reply_session is not None:
        reply["sessionId"] = reply_session
    socket_mock.recv.return_value = json.dumps(reply)
    direct = transport.DirectCDP("ws://localhost/synthetic")
    with pytest.raises(RuntimeError):
        direct.call("Target.getTargetInfo", session_id="member-session")
    socket_mock.close.assert_called_once()


@pytest.mark.parametrize(
    "context, endpoint, bind_default",
    [
        ("context", None, False),
        (None, "ws://localhost/synthetic", False),
        ("context", "ws://localhost/synthetic", True),
        (None, None, True),
        (None, "ws://localhost/synthetic", "yes"),
    ],
)
def test_incomplete_binding_never_starts_or_connects(monkeypatch, context, endpoint, bind_default):
    connect = Mock()
    daemon = Mock()
    monkeypatch.setattr(browser, "DirectCDP", connect)
    monkeypatch.setattr(browser, "ensure_daemon", daemon)
    with pytest.raises(ValueError):
        browser.Browser(
            "https://example.test",
            browser_context_id=context,
            browser_ws_url=endpoint,
            bind_default_context=bind_default,
        )
    connect.assert_not_called()
    daemon.assert_not_called()


def test_send_failure_is_terminal_even_if_socket_close_fails(socket_mock):
    socket_mock.send.side_effect = OSError("secret connection detail")
    socket_mock.close.side_effect = OSError("secret cleanup detail")
    direct = transport.DirectCDP("ws://localhost/synthetic")
    with pytest.raises(RuntimeError, match="^send failed$"):
        direct.call("Input.insertText", session_id="member-session", text="synthetic")
    with pytest.raises(RuntimeError, match="^transport closed$"):
        direct.call("Input.insertText", session_id="member-session", text="synthetic")
    direct.close()
    socket_mock.send.assert_called_once()
    socket_mock.close.assert_called_once()


def test_events_do_not_reset_request_deadline(socket_mock, monkeypatch):
    socket_mock.recv.return_value = '{"method":"Page.event"}'
    monkeypatch.setattr(transport.time, "monotonic", Mock(side_effect=[0, 1, 6]))
    direct = transport.DirectCDP("ws://localhost/synthetic", timeout=5)
    with pytest.raises(RuntimeError):
        direct.call("Target.getTargetInfo", session_id="member-session")
    socket_mock.recv.assert_called_once_with(timeout=4)


@pytest.mark.parametrize("session", ["", " padded ", False, 42])
def test_invalid_session_cannot_be_normalized_into_another_session(socket_mock, session):
    direct = transport.DirectCDP("ws://localhost/synthetic")
    with pytest.raises(RuntimeError):
        direct.call("Input.insertText", session_id=session, text="synthetic")
    socket_mock.send.assert_not_called()


def test_scoped_browser_over_real_websocket_never_uses_harness(monkeypatch):
    requests = []
    context = "synthetic-context"
    target = "synthetic-target"
    session = "synthetic-session"
    info = {"targetId": target, "type": "page", "browserContextId": context}

    def handler(ws):
        for raw in ws:
            req = json.loads(raw)
            requests.append(req)
            method, params = req["method"], req["params"]
            if method == "Target.getBrowserContexts":
                result = {"browserContextIds": [context]}
            elif method == "Target.createTarget":
                assert params["browserContextId"] == context
                result = {"targetId": target}
            elif method == "Target.attachToTarget":
                result = {"sessionId": session}
            elif method == "Target.getTargetInfo":
                # Root no-target query gives browser metadata, just as real harness does.
                scoped = req.get("sessionId") == session or params.get("targetId") == target
                result = {"targetInfo": info if scoped else {"targetId": "browser", "type": "browser"}}
            elif method == "Runtime.evaluate":
                result = {"result": {"value": "complete"}}
            else:
                result = {}
            reply = {"id": req["id"], "result": result}
            if "sessionId" in req:
                reply["sessionId"] = req["sessionId"]
            ws.send('{"method":"Synthetic.event"}')
            ws.send(json.dumps(reply))

    forbidden = Mock(side_effect=AssertionError("harness must not receive scoped traffic"))
    monkeypatch.setattr(browser, "cdp", forbidden)
    monkeypatch.setattr(browser, "ensure_daemon", forbidden)
    with serve(handler, "127.0.0.1", 0) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            endpoint = f"ws://127.0.0.1:{server.socket.getsockname()[1]}/devtools/browser/synthetic"
            b = browser.Browser("https://example.test", browser_context_id=context, browser_ws_url=endpoint)
            b.call("Input.insertText", text="synthetic")
            b.close()
        finally:
            server.shutdown()
            thread.join(timeout=3)
    forbidden.assert_not_called()
    guards = [r for r in requests if r["method"] == "Target.getTargetInfo" and not r["params"]]
    assert guards and all(r["sessionId"] == session for r in guards)
    inputs = [r for r in requests if r["method"] == "Input.insertText"]
    assert len(inputs) == 1 and inputs[0]["sessionId"] == session
    assert requests[-1]["method"] == "Target.closeTarget"

"""Offline acceptance cases: never contact a real browser or model provider."""
from unittest.mock import Mock

import pytest

from jev_ultrafast import agent as loop
from jev_ultrafast import browser


@pytest.fixture
def harness(monkeypatch):
    calls, targets, sessions = [], {}, {}
    contexts = ["context-member-a", "context-member-b"]

    def cdp(method, session_id=None, **params):
        calls.append((method, session_id, params))
        if method == "Target.getBrowserContexts":
            return {"browserContextIds": contexts[:]}
        if method == "Target.createTarget":
            target = f"target-{len(targets)}"
            targets[target] = {
                "targetId": target,
                "type": "page",
                "browserContextId": params.get("browserContextId", "context-default"),
            }
            return {"targetId": target}
        if method == "Target.attachToTarget":
            session = "session-" + params["targetId"]
            sessions[session] = params["targetId"]
            return {"sessionId": session}
        if method == "Target.getTargetInfo":
            target = params.get("targetId") or sessions.get(session_id)
            return {"targetInfo": dict(targets[target])}
        if method == "Runtime.evaluate":
            value = "complete" if params["expression"] == "document.readyState" else {"x": 1, "y": 1}
            if params["expression"] == browser.READ_STATE:
                value = {"url": "https://example.test", "text": "Offer", "actions": [], "scroll": {"y": 0}}
            return {"result": {"value": value}}
        return {}

    monkeypatch.setattr(browser, "ensure_daemon", Mock())
    monkeypatch.setattr(browser, "cdp", cdp)
    transport = Mock()
    transport.call.side_effect = lambda *a, **kw: browser.cdp(*a, **kw)
    monkeypatch.setattr(browser, "DirectCDP", Mock(return_value=transport))
    return calls, targets, sessions, contexts


@pytest.mark.parametrize("context", ["", " ", " context-member-a", 123, False])
def test_invalid_context_never_starts_browser(harness, context):
    with pytest.raises(ValueError):
        browser.Browser("https://example.test", browser_context_id=context,
                        browser_ws_url="ws://localhost/synthetic" if context is not None else None)
    assert harness[0] == []
    browser.ensure_daemon.assert_not_called()


def test_unknown_context_never_creates_default_tab(harness):
    with pytest.raises(RuntimeError):
        browser.Browser("https://example.test", browser_context_id="missing-member",
                        browser_ws_url="ws://localhost:9222/devtools/browser/synthetic")
    assert not any(method == "Target.createTarget" for method, _, _ in harness[0])


def test_context_is_explicit_on_creation_and_instances_keep_distinct_sessions(harness):
    a = browser.Browser("https://a.example.test", browser_context_id="context-member-a",
                        browser_ws_url="ws://localhost:9222/devtools/browser/synthetic")
    b = browser.Browser("https://b.example.test", browser_context_id="context-member-b",
                        browser_ws_url="ws://localhost:9222/devtools/browser/synthetic")
    a.call("Input.insertText", text="synthetic-a")
    b.call("Input.insertText", text="synthetic-b")
    calls, targets, sessions, _ = harness
    typed = [(targets[sessions[s]]["browserContextId"], p["text"])
             for m, s, p in calls if m == "Input.insertText"]
    assert typed == [("context-member-a", "synthetic-a"), ("context-member-b", "synthetic-b")]
    assert [p["browserContextId"] for m, _, p in calls if m == "Target.createTarget"] == [
        "context-member-a", "context-member-b"]


def test_checked_default_context_uses_direct_endpoint_without_ephemeral_context(harness):
    b = browser.Browser(
        "https://example.test",
        browser_ws_url="ws://localhost:9222/devtools/browser/synthetic",
        bind_default_context=True,
    )
    calls, targets, _, _ = harness
    created = [params for method, _, params in calls if method == "Target.createTarget"]
    assert created == [{"url": "about:blank", "background": True}]
    assert targets[b.target]["browserContextId"] == "context-default"
    assert b._bound_context_id == "context-default"
    assert b._transport is not None


def test_checked_default_context_rejects_nondefault_created_target_before_attach(harness, monkeypatch):
    original = browser.cdp

    def wrong_context(method, *args, **kwargs):
        result = original(method, *args, **kwargs)
        if method == "Target.createTarget":
            harness[1][result["targetId"]]["browserContextId"] = "context-member-a"
        return result

    monkeypatch.setattr(browser, "cdp", wrong_context)
    with pytest.raises(browser.BrowserBindingError):
        browser.Browser(
            "https://example.test",
            browser_ws_url="ws://localhost:9222/devtools/browser/synthetic",
            bind_default_context=True,
        )
    assert not any(method == "Target.attachToTarget" for method, _, _ in harness[0])


def test_checked_default_context_rejects_session_moved_to_nondefault_context(harness):
    b = browser.Browser(
        "https://example.test",
        browser_ws_url="ws://localhost:9222/devtools/browser/synthetic",
        bind_default_context=True,
    )
    harness[1][b.target]["browserContextId"] = "context-member-a"
    harness[0].clear()
    with pytest.raises(browser.BrowserBindingError):
        b.call("Input.insertText", text="must-not-type")
    assert all(method == "Target.getTargetInfo" for method, _, _ in harness[0])


@pytest.mark.parametrize("reported", [None, "", " padded ", False, 42])
def test_checked_default_context_requires_valid_reported_context_id(harness, monkeypatch, reported):
    original = browser.cdp

    def malformed(method, *args, **kwargs):
        result = original(method, *args, **kwargs)
        if method == "Target.createTarget":
            if reported is None:
                harness[1][result["targetId"]].pop("browserContextId", None)
            else:
                harness[1][result["targetId"]]["browserContextId"] = reported
        return result

    monkeypatch.setattr(browser, "cdp", malformed)
    with pytest.raises(browser.BrowserBindingError):
        browser.Browser(
            "https://example.test",
            browser_ws_url="ws://localhost:9222/devtools/browser/synthetic",
            bind_default_context=True,
        )
    assert not any(method == "Target.attachToTarget" for method, _, _ in harness[0])


@pytest.mark.parametrize("operation", ["read", "input", "observe", "act"])
def test_mismatch_blocks_reads_and_input_without_retry(harness, operation):
    b = browser.Browser("https://example.test", browser_context_id="context-member-a",
                        browser_ws_url="ws://localhost:9222/devtools/browser/synthetic")
    harness[1][b.target]["browserContextId"] = "context-member-b"
    harness[0].clear()
    b.fresh = Mock(return_value=True)
    with pytest.raises(RuntimeError):
        if operation == "read":
            b.evaluate("document.title")
        elif operation == "input":
            b.call("Input.insertText", text="must-not-type")
        elif operation == "observe":
            b.observe(screenshot=False)
        else:
            b.act({"kind": "fill", "node": 1, "id": "e1"}, {}, "must-not-type")
    assert all(m == "Target.getTargetInfo" for m, _, _ in harness[0])


def test_wrong_attached_session_is_detected(harness):
    a = browser.Browser("https://a.example.test", browser_context_id="context-member-a",
                        browser_ws_url="ws://localhost:9222/devtools/browser/synthetic")
    b = browser.Browser("https://b.example.test", browser_context_id="context-member-b",
                        browser_ws_url="ws://localhost:9222/devtools/browser/synthetic")
    a.session = b.session
    harness[0].clear()
    with pytest.raises(RuntimeError):
        a.call("Input.insertText", text="must-not-type")
    assert all(m == "Target.getTargetInfo" for m, _, _ in harness[0])


def test_close_is_idempotent_and_never_disposes_the_shared_context(harness):
    b = browser.Browser("https://example.test", browser_context_id="context-member-a",
                        browser_ws_url="ws://localhost:9222/devtools/browser/synthetic")
    own_target = b.target
    harness[0].clear()
    b.close()
    b.close()
    assert [(m, p) for m, _, p in harness[0] if m == "Target.closeTarget"] == [
        ("Target.closeTarget", {"targetId": own_target})]
    assert not any(m == "Target.disposeBrowserContext" for m, _, _ in harness[0])


def test_agent_passes_context_to_browser_without_putting_it_in_goal(monkeypatch):
    factory = Mock()
    factory.return_value.observe.return_value = {"actions": []}
    monkeypatch.setattr(loop, "Browser", factory)
    a = loop.Agent("https://example.test", "Read the offer", browser_context_id="context-member-a",
                        browser_ws_url="ws://localhost:9222/devtools/browser/synthetic")
    factory.assert_called_once_with("https://example.test", browser_context_id="context-member-a",
                        browser_ws_url="ws://localhost:9222/devtools/browser/synthetic")
    assert a.state["goal"] == "Read the offer"


def test_agent_passes_checked_default_context_binding(monkeypatch):
    factory = Mock()
    factory.return_value.observe.return_value = {"actions": []}
    monkeypatch.setattr(loop, "Browser", factory)
    a = loop.Agent(
        "https://example.test",
        "Read the offer",
        browser_ws_url="ws://localhost:9222/devtools/browser/synthetic",
        bind_default_context=True,
    )
    factory.assert_called_once_with(
        "https://example.test",
        browser_context_id=None,
        browser_ws_url="ws://localhost:9222/devtools/browser/synthetic",
        bind_default_context=True,
    )
    assert a.state["goal"] == "Read the offer"


@pytest.mark.parametrize("bind_default", [None, 0, 1, "false", [], {}])
def test_agent_rejects_nonboolean_default_binding_before_browser_side_effects(monkeypatch, bind_default):
    factory = Mock()
    monkeypatch.setattr(loop, "Browser", factory)
    with pytest.raises(ValueError, match="bind_default_context"):
        loop.Agent(
            "https://example.test",
            "Read the offer",
            browser_ws_url="ws://localhost:9222/devtools/browser/synthetic",
            bind_default_context=bind_default,
        )
    factory.assert_not_called()


def test_bound_observation_and_click_use_the_same_checked_session(harness):
    b = browser.Browser("https://example.test", browser_context_id="context-member-a",
                        browser_ws_url="ws://localhost:9222/devtools/browser/synthetic")
    assert b.observe(screenshot=False)["text"] == "Offer"
    b.fresh = Mock(return_value=True)
    b.act({"kind": "click", "node": 1, "id": "e1"}, {})
    inputs = [(s, p["type"]) for m, s, p in harness[0] if m == "Input.dispatchMouseEvent"]
    assert inputs == [(b.session, "mousePressed"), (b.session, "mouseReleased")]


@pytest.mark.parametrize("context", [None, "context-member-a"])
def test_closed_bound_browser_cannot_read_or_type(harness, context):
    b = browser.Browser("https://example.test", browser_context_id=context,
                        browser_ws_url="ws://localhost/synthetic" if context is not None else None)
    b.close()
    harness[0].clear()
    with pytest.raises(RuntimeError):
        b.evaluate("document.title")
    with pytest.raises(RuntimeError):
        b.call("Input.insertText", text="must-not-type")
    assert harness[0] == []


def test_constructor_failure_closes_only_created_target_and_keeps_original_error(harness, monkeypatch):
    original = browser.cdp

    def fail(method, *args, **kwargs):
        if method == "Target.attachToTarget":
            raise RuntimeError("synthetic attach failure")
        result = original(method, *args, **kwargs)
        if method == "Target.closeTarget":
            raise RuntimeError("synthetic cleanup failure")
        return result

    monkeypatch.setattr(browser, "cdp", fail)
    with pytest.raises(RuntimeError, match="synthetic attach failure"):
        browser.Browser("https://example.test", browser_context_id="context-member-a",
                        browser_ws_url="ws://localhost:9222/devtools/browser/synthetic")
    assert [(m, p) for m, _, p in harness[0] if m.startswith("Target.close")] == [
        ("Target.closeTarget", {"targetId": "target-0"})]
    assert not any(m == "Page.navigate" for m, _, _ in harness[0])


def test_created_target_context_checked_before_attach_or_navigation(harness, monkeypatch):
    original = browser.cdp

    def wrong_context(method, *args, **kwargs):
        result = original(method, *args, **kwargs)
        if method == "Target.createTarget":
            harness[1][result["targetId"]]["browserContextId"] = "context-member-b"
        return result

    monkeypatch.setattr(browser, "cdp", wrong_context)
    with pytest.raises(RuntimeError):
        browser.Browser("https://example.test", browser_context_id="context-member-a",
                        browser_ws_url="ws://localhost:9222/devtools/browser/synthetic")
    assert not any(m in {"Target.attachToTarget", "Page.navigate"} for m, _, _ in harness[0])


@pytest.mark.parametrize("corruption", ["missing", "wrong-target", "wrong-type"])
def test_session_metadata_must_match_even_within_same_context(harness, corruption):
    b = browser.Browser("https://example.test", browser_context_id="context-member-a",
                        browser_ws_url="ws://localhost:9222/devtools/browser/synthetic")
    info = harness[1][b.target]
    if corruption == "missing":
        info.clear()
    elif corruption == "wrong-target":
        info["targetId"] = "another-target-in-same-context"
    else:
        info["type"] = "worker"
    harness[0].clear()
    with pytest.raises(RuntimeError):
        b.call("Input.insertText", text="must-not-type")
    assert all(m == "Target.getTargetInfo" for m, _, _ in harness[0])


def test_unscoped_demo_preserves_original_protocol_shape(harness):
    b = browser.Browser("https://example.test")
    assert b.observe(screenshot=False)["text"] == "Offer"
    assert not any(m in {"Target.getBrowserContexts", "Target.getTargetInfo"} for m, _, _ in harness[0])
    assert all("browserContextId" not in p for m, _, p in harness[0] if m == "Target.createTarget")


def test_post_input_observation_does_not_swallow_binding_failure(harness):
    b = browser.Browser("https://example.test", browser_context_id="context-member-a",
                        browser_ws_url="ws://localhost:9222/devtools/browser/synthetic")
    b.after_input = {"kind": "fill", "node": 1, "id": "e1"}
    harness[1][b.target]["browserContextId"] = "context-member-b"
    harness[0].clear()
    with pytest.raises(RuntimeError):
        b.observe(screenshot=False)
    assert len(harness[0]) == 1
    assert harness[0][0][0] == "Target.getTargetInfo"


def test_explicit_close_failure_is_visible_and_not_retried(harness, monkeypatch):
    b = browser.Browser("https://example.test", browser_context_id="context-member-a",
                        browser_ws_url="ws://localhost:9222/devtools/browser/synthetic")
    original = browser.cdp

    def fail_close(method, *args, **kwargs):
        result = original(method, *args, **kwargs)
        if method == "Target.closeTarget":
            raise RuntimeError("synthetic close failure")
        return result

    monkeypatch.setattr(browser, "cdp", fail_close)
    with pytest.raises(RuntimeError, match="synthetic close failure"):
        b.close()
    b.close()
    assert sum(m == "Target.closeTarget" for m, _, _ in harness[0]) == 1


@pytest.mark.parametrize("bad_session", ["", False, 42])
def test_invalid_attached_session_never_reaches_page_commands(harness, monkeypatch, bad_session):
    original = browser.cdp

    def malformed(method, *args, **kwargs):
        result = original(method, *args, **kwargs)
        if method == "Target.attachToTarget":
            # A falsy/invalid session must not fall back to a daemon's active tab.
            harness[2][bad_session] = kwargs["targetId"]
            return {"sessionId": bad_session}
        return result

    monkeypatch.setattr(browser, "cdp", malformed)
    with pytest.raises(RuntimeError):
        browser.Browser("https://example.test", browser_context_id="context-member-a",
                        browser_ws_url="ws://localhost:9222/devtools/browser/synthetic")
    assert not any(m.startswith(("Runtime.", "Page.", "Input.", "Emulation.")) for m, _, _ in harness[0])

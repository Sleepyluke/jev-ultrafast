"""Observed actions through Browser Harness; one CDP session, no per-step subprocess."""

import hashlib
import json
import sys
import time
from pathlib import Path

from browser_harness.admin import ensure_daemon
from browser_harness.helpers import cdp

from .transport import DirectCDP

# Atomically read visible content and controls, preserving actual DOM node identity.
READ_STATE = Path(__file__).with_name("snapshot.js").read_text()
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"

class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


class BrowserBindingError(RuntimeError):
    """Raised when a CDP target or session does not match the expected browser context."""


class Browser:
    def __init__(self, url, *, browser_context_id=None, browser_ws_url=None):
        if browser_context_id is not None:
            if (not isinstance(browser_context_id, str) or not browser_context_id.strip()
                    or browser_context_id != browser_context_id.strip()):
                raise ValueError("browser_context_id must be a non-blank string with no leading/trailing whitespace")
        if (browser_context_id is None) != (browser_ws_url is None):
            raise ValueError("Supply both browser_context_id and browser_ws_url for scoped mode")
        self._transport = None
        if browser_context_id is None:
            ensure_daemon()
        else:
            self._transport = DirectCDP(browser_ws_url)
        self._closed = False
        self.browser_context_id = browser_context_id
        self.target = None
        self.session = None
        self.after_input = None
        try:
            if self.browser_context_id is not None:
                response = self._cdp("Target.getBrowserContexts")
                contexts = response.get("browserContextIds", [])
                if not isinstance(contexts, list) or self.browser_context_id not in contexts:
                    raise RuntimeError("Unknown browser context")
            create_params = {"url": "about:blank", "background": True}
            if self.browser_context_id is not None:
                create_params["browserContextId"] = self.browser_context_id
            target = self._cdp("Target.createTarget", **create_params).get("targetId")
            if not isinstance(target, str) or not target.strip():
                raise BrowserBindingError("Target creation was not confirmed")
            self.target = target
            if self.browser_context_id is not None:
                target_info = self._cdp("Target.getTargetInfo", targetId=self.target).get("targetInfo")
                if (not isinstance(target_info, dict) or target_info.get("targetId") != self.target or
                    target_info.get("type") != "page" or
                    target_info.get("browserContextId") != self.browser_context_id):
                    raise BrowserBindingError("Target verification failed")
            session = self._cdp("Target.attachToTarget", targetId=self.target, flatten=True).get("sessionId")
            if not isinstance(session, str) or not session.strip():
                raise BrowserBindingError("Session attachment was not confirmed")
            self.session = session
            self._verify_session()
            self.call("Emulation.setDeviceMetricsOverride", width=1120, height=780, deviceScaleFactor=1, mobile=False)
            # Keep rAF/menus rendering in an owned background tab, without activating the user's Chrome tab.
            self.call("Emulation.setFocusEmulationEnabled", enabled=True)
            self.call("Page.navigate", url=url)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if self.evaluate("document.readyState") == "complete":
                    break
                time.sleep(0.02)
        except Exception:
            self._cleanup_after_init_failure()
            raise

    def _cleanup_after_init_failure(self):
        if self.target is not None:
            try:
                self._cdp("Target.closeTarget", targetId=self.target)
            except Exception:
                pass
            finally:
                self.target = None
        self.session = None
        self._closed = True
        if self._transport is not None:
            self._transport.close()

    def _cdp(self, method, session_id=None, **params):
        if self._transport is not None:
            return self._transport.call(method, session_id=session_id, **params)
        return cdp(method, session_id=session_id, **params)

    def _verify_session(self):
        """Verify attached session matches expected target and context (if scoped)."""
        if getattr(self, "browser_context_id", None) is None:
            # Unscoped legacy Browser instances never need target/session verification.
            return
        if self.session is None:
            raise BrowserBindingError("Browser is closed")
        try:
            info = self._cdp("Target.getTargetInfo", session_id=self.session).get("targetInfo")
        except Exception:
            raise BrowserBindingError("Session verification failed") from None
        if (not isinstance(info, dict) or info.get("targetId") != self.target or
            info.get("type") != "page" or info.get("browserContextId") != self.browser_context_id):
            raise BrowserBindingError("Session verification failed")

    def call(self, method, **params):
        if self._closed:
            raise RuntimeError("Browser is closed")
        self._verify_session()
        return self._cdp(method, session_id=self.session, **params)

    def evaluate(self, expression):
        response = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    def observe(self, screenshot=True):
        if self._closed:
            raise RuntimeError("Browser is closed")
        if getattr(self, "after_input", None):
            action, self.after_input = self.after_input, None
            try:
                self.call(
                    "Runtime.evaluate",
                    expression="""(action => new Promise(resolve => {
                      const field=window.__jevFast?.nodes.get(action.node);
                      const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
                      let frames=0, stopped=false;
                      const finish=()=>{stopped=true;resolve()};
                      setTimeout(finish,autocomplete ? 200 : 50);
                      const ready=()=>{
                        if (stopped) return;
                        const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
                          .split(/\\s+/).filter(Boolean);
                        const roots=ids.length ? ids.map(id=>document.getElementById(id)).filter(Boolean) : [document];
                        const options=roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]);
                        if (++frames>=2 && (!autocomplete || options.some(e=>{
                          const r=e.getBoundingClientRect();
                          return r.width && r.height && r.bottom>0 && r.top<innerHeight &&
                            e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
                        }))) finish();
                        else requestAnimationFrame(ready);
                      };
                      requestAnimationFrame(ready);
                    }))(""" + json.dumps(action) + ")",
                    awaitPromise=True,
                    returnByValue=True,
                )
            except BrowserBindingError:
                # Let binding failures propagate; they must not be swallowed by generic retries.
                raise
            except RuntimeError:
                pass
        for attempt in range(10):
            try:
                return browser_operation(
                    {"operation": "observe", "session": self.session, "screenshot": screenshot},
                    call=self.call if self.browser_context_id is not None else None,
                )
            except StalePage:
                if attempt == 9:
                    raise
                time.sleep(0.02)
        raise StalePage("Page did not settle")

    def fresh(self, page, action=None):
        if self._closed:
            raise RuntimeError("Browser is closed")
        if action is not None and action["kind"] in {"click", "select"}:
            node = action["node"]
            if type(node) is not int:
                return False
            current = self.evaluate(
                "(() => { const c=window.__jevFast; "
                f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()"
            )
            return current == [page["page_key"], page["guards"].get(str(node))]
        return self.evaluate(MARKER) == page["marker"]

    def act(self, action, page, text=None):
        if getattr(self, "_closed", False):
            raise RuntimeError("Browser is closed")
        self._verify_session()
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        if action["kind"] == "wait":
            time.sleep(0.1)
        result = browser_operation({"operation": "act", "session": self.session, "action": action, "text": text},
                                    call=self.call if self.browser_context_id is not None else None)
        self.after_input = action if action["kind"] != "wait" else None
        return result

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self.target is not None:
                self._cdp("Target.closeTarget", targetId=self.target)
        finally:
            self.target = None
            self.session = None
            if self._transport is not None:
                self._transport.close()


def fingerprint(state):
    content = {k: state[k] for k in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def browser_operation(request, *, call=None):
    operation = request["operation"]
    session = request["session"]

    def default_call(method, **params):
        return cdp(method, session_id=session, **params)

    if call is None:
        call = default_call

    def evaluate(expression):
        result = call("Runtime.evaluate", expression=expression, returnByValue=True)
        if result.get("exceptionDetails"):
            if operation == "act" and request["action"]["kind"] == "select":
                raise RuntimeError("Dropdown execution was interrupted; inspect before retrying.")
            raise StalePage("Document changed during evaluation")
        return result.get("result", {}).get("value")

    if operation == "act":
        action = request["action"]
        kind = action["kind"]
        if kind == "scroll":
            call("Input.dispatchMouseEvent", type="mouseWheel", x=550, y=650, deltaX=0, deltaY=action["delta"])
        elif kind != "wait":
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            # Code-owned node IDs refer to actual observed elements, never model-generated selectors.
            target = evaluate("""(action => {
              const e=window.__jevFast?.nodes.get(action.node);
              if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
                  !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
              if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
              const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
              if (!r.width || !r.height || x<0 || y<0 || x>=innerWidth || y>=innerHeight) return null;
              if (!e.contains(document.elementFromPoint(x,y))) return null;
              if (action.kind==='select') {
                if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
                    !o.disabled && !o.closest('optgroup[disabled]'))) return null;
                e.value=action.value;
                e.dispatchEvent(new Event('input',{bubbles:true}));
                e.dispatchEvent(new Event('change',{bubbles:true}));
              }
              return {x,y};
            })(""" + json.dumps(action) + ")")
            if target is None:
                if kind == "select":
                    raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
                raise StalePage("Target changed or is covered. Observe again.")
            if kind != "select":
                x, y = target["x"], target["y"]
                for event in ("mousePressed", "mouseReleased"):
                    call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
                if kind == "fill":
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyDown",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                        commands=["selectAll"],
                    )
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyUp",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                    )
                    call("Input.insertText", text=request["text"])
        return {"executed": action["id"]}

    info = evaluate(READ_STATE)
    if info is None:
        raise StalePage("Document is navigating")
    info["fingerprint"] = fingerprint(info)
    if request.get("screenshot", True):
        info["screenshot"] = call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
    return info

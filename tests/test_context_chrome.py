"""Opt-in real Chrome checks on synthetic pages and a disposable profile only."""
import os
import subprocess
import time
from contextlib import ExitStack, closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from jev_ultrafast import Browser, IsolatedContext
from jev_ultrafast.browser import BrowserBindingError
from jev_ultrafast.transport import DirectCDP

pytestmark = pytest.mark.skipif(not os.environ.get("JEV_TEST_CHROME"), reason="Set JEV_TEST_CHROME to opt in")


@pytest.fixture
def chrome(tmp_path):
    executable = Path(os.environ["JEV_TEST_CHROME"])
    assert executable.is_file(), "JEV_TEST_CHROME must point to an installed Chrome executable"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    process = subprocess.Popen([
        str(executable), "--headless=new", "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
        f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
        "--disable-background-networking", "--disable-extensions", "--disable-sync", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
    controller = None
    try:
        deadline = time.monotonic() + 20
        port_file = profile / "DevToolsActivePort"
        while True:
            assert process.poll() is None, "Disposable Chrome exited before startup"
            assert time.monotonic() < deadline, "Disposable Chrome startup timed out"
            try:
                lines = port_file.read_text().splitlines()
                if len(lines) >= 2:
                    port, target = lines[:2]
                    break
            except OSError:
                pass  # Windows may still hold Chrome's newly created file exclusively.
            time.sleep(0.05)
        assert port.isdigit() and target.startswith("/devtools/browser/")
        endpoint = f"ws://127.0.0.1:{port}{target}"
        controller = DirectCDP(endpoint)
        print("Synthetic test browser:", controller.call("Browser.getVersion")["product"])
        yield endpoint, controller
    finally:
        if controller is not None:
            try:
                controller.call("Browser.close")
            except RuntimeError:
                pass  # Chrome may close the socket before replying.
            controller.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, check=False, **options)
            else:
                process.kill()
            process.wait(timeout=10)


@pytest.fixture
def synthetic_site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'<!doctype html><title>Synthetic isolation check</title><label>Alias<input name="alias"></label>'
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/"
        finally:
            server.shutdown()
            thread.join(timeout=3)


def test_real_chrome_isolates_storage_input_and_cleanup(chrome, synthetic_site):
    endpoint, controller = chrome
    original = set(controller.call("Target.getBrowserContexts")["browserContextIds"])
    with ExitStack() as stack:
        context_a = stack.enter_context(IsolatedContext(endpoint))
        context_b = stack.enter_context(IsolatedContext(endpoint))
        a_id = context_a.browser_options["browser_context_id"]
        b_id = context_b.browser_options["browser_context_id"]
        assert a_id != b_id
        a = stack.enter_context(closing(Browser(synthetic_site, **context_a.browser_options)))
        b = stack.enter_context(closing(Browser(synthetic_site, **context_b.browser_options)))
        a.evaluate("localStorage.setItem('owner','synthetic-a'); document.cookie='owner=synthetic-a; SameSite=Lax'")
        assert b.evaluate("localStorage.getItem('owner')") is None
        assert b.evaluate("document.cookie") == ""
        b.evaluate("localStorage.setItem('owner','synthetic-b'); document.cookie='owner=synthetic-b; SameSite=Lax'")
        assert a.evaluate("localStorage.getItem('owner')") == "synthetic-a"
        assert a.evaluate("document.cookie") == "owner=synthetic-a"

        page = a.observe(screenshot=False)
        fill = next(action for action in page["actions"] if action["kind"] == "fill")
        a.act(fill, page, text="synthetic-a")
        assert a.evaluate("document.querySelector('input').value") == "synthetic-a"
        assert b.evaluate("document.querySelector('input').value") == ""

        # A different session on the same CDP connection must also fail the target check.
        other = a._transport.call("Target.createTarget", url="about:blank", browserContextId=b_id)["targetId"]
        wrong_session = a._transport.call("Target.attachToTarget", targetId=other, flatten=True)["sessionId"]
        own_session = a.session
        try:
            a.session = wrong_session
            with pytest.raises(BrowserBindingError):
                a.call("Input.insertText", text="must-not-type")
        finally:
            a.session = own_session
            a._transport.call("Target.closeTarget", targetId=other)

        a.close()
        # A new tab in the same live context retains state.
        with closing(Browser(synthetic_site, **context_a.browser_options)) as reopened:
            assert reopened.evaluate("localStorage.getItem('owner')") == "synthetic-a"
        context_a.close()
        remaining = controller.call("Target.getBrowserContexts")["browserContextIds"]
        assert a_id not in remaining and b_id in remaining
        assert b.evaluate("localStorage.getItem('owner')") == "synthetic-b"
        assert b.evaluate("document.cookie") == "owner=synthetic-b"
    assert set(controller.call("Target.getBrowserContexts")["browserContextIds"]) == original
    with IsolatedContext(endpoint) as fresh:
        with closing(Browser(synthetic_site, **fresh.browser_options)) as new:
            assert new.evaluate("localStorage.getItem('owner')") is None
            assert new.evaluate("document.cookie") == ""


def test_real_chrome_reclaims_context_when_creator_connection_detaches(chrome):
    endpoint, controller = chrome
    creator = DirectCDP(endpoint)
    try:
        context_id = creator.call("Target.createBrowserContext", disposeOnDetach=True)["browserContextId"]
        assert context_id in controller.call("Target.getBrowserContexts")["browserContextIds"]
    finally:
        creator.close()
    deadline = time.monotonic() + 5
    while context_id in controller.call("Target.getBrowserContexts")["browserContextIds"]:
        assert time.monotonic() < deadline, "Detached context was not reclaimed"
        time.sleep(0.05)


def test_real_chrome_checked_default_context_retains_profile_state(chrome, synthetic_site):
    endpoint, controller = chrome
    options = {"browser_ws_url": endpoint, "bind_default_context": True}
    with closing(Browser(synthetic_site, **options)) as first:
        first.evaluate("localStorage.setItem('persistent-owner','synthetic-member')")
        first_target = first.target

    with closing(Browser(synthetic_site, **options)) as reopened:
        assert reopened.target != first_target
        assert reopened.evaluate("localStorage.getItem('persistent-owner')") == "synthetic-member"

        with IsolatedContext(endpoint) as isolated:
            other = reopened._transport.call(
                "Target.createTarget",
                url=synthetic_site,
                browserContextId=isolated.browser_options["browser_context_id"],
            )["targetId"]
            wrong_session = reopened._transport.call(
                "Target.attachToTarget", targetId=other, flatten=True
            )["sessionId"]
            own_session = reopened.session
            try:
                reopened.session = wrong_session
                with pytest.raises(BrowserBindingError):
                    reopened.call("Input.insertText", text="must-not-type")
            finally:
                reopened.session = own_session
                reopened._transport.call("Target.closeTarget", targetId=other)

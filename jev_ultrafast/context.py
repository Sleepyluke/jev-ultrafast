"""Owned ephemeral browser context: one dedicated connection, one created ID, no retries."""
from .transport import DirectCDP


class IsolatedContext:
    """Provision a temporary Chrome browser context owned by this scope only.

    The creator connection stays alive for the whole scope so the
    ``disposeOnDetach`` guarantee lets Chrome reclaim the context even if the
    create reply is lost. Not a persistent profile, an authorization boundary,
    or a live site collection mechanism.
    """

    def __init__(self, browser_ws_url):
        self._browser_ws_url = browser_ws_url
        self._closed = False
        self._context_id = None
        self._transport = None
        transport = DirectCDP(browser_ws_url)
        try:
            result = transport.call("Target.createBrowserContext", disposeOnDetach=True)
            context_id = self._extract_id(result)
        except BaseException:
            transport.close()
            raise
        self._transport = transport
        self._context_id = context_id

    @staticmethod
    def _extract_id(result):
        if not isinstance(result, dict):
            raise RuntimeError("browser context creation unconfirmed") from None
        context_id = result.get("browserContextId")
        if (not isinstance(context_id, str) or not context_id
                or context_id != context_id.strip()):
            raise RuntimeError("browser context creation unconfirmed") from None
        return context_id

    @property
    def browser_options(self):
        if self._closed:
            raise RuntimeError("browser context closed")
        return {"browser_context_id": self._context_id,
                "browser_ws_url": self._browser_ws_url}

    def close(self):
        if self._closed:
            return
        self._closed = True
        transport, context_id = self._transport, self._context_id
        self._context_id = None
        try:
            if transport is not None and context_id is not None:
                transport.call("Target.disposeBrowserContext",
                               browserContextId=context_id)
        finally:
            if transport is not None:
                transport.close()

    def __enter__(self):
        if self._closed:
            raise RuntimeError("browser context closed")
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self.close()
        except Exception as cleanup_error:
            if exc is not None:
                exc.add_note("Browser context cleanup failed")
                return False
            raise cleanup_error
        return False

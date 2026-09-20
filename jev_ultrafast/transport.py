"""Dedicated CDP connection: preserve explicit sessions, never reconnect or retry."""
import json
import math
import threading
import time
from urllib.parse import urlparse

from websockets.sync.client import connect


class DirectCDP:
    def __init__(self, browser_ws_url, *, timeout=5):
        self._validate_url(browser_ws_url)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        self._ws_url = browser_ws_url
        self._timeout = timeout
        self._lock = threading.Lock()
        self._next_id = 1
        self._closed = False
        self._ws = None
        self._connect()

    def _validate_url(self, url):
        if not isinstance(url, str) or not url or any(ch.isspace() for ch in url):
            raise ValueError("invalid browser endpoint")
        try:
            parsed = urlparse(url)
            valid = (parsed.scheme in ("ws", "wss") and parsed.hostname
                     and (parsed.port is None or 0 < parsed.port < 65536))
        except ValueError:
            raise ValueError("invalid browser endpoint") from None
        if not valid:
            raise ValueError("invalid browser endpoint")

    def _connect(self):
        try:
            self._ws = connect(self._ws_url, open_timeout=self._timeout, close_timeout=1,
                               proxy=None, max_size=8 * 1024 * 1024)
        except Exception:
            self._closed = True
            raise RuntimeError("connection failed") from None

    def call(self, method, session_id=None, **params):
        if session_id is not None:
            if (not isinstance(session_id, str) or not session_id.strip()
                    or session_id != session_id.strip()):
                raise RuntimeError("invalid session_id") from None
        with self._lock:
            if self._closed:
                raise RuntimeError("transport closed") from None
            request_id = self._next_id
            self._next_id += 1
            payload = {"id": request_id, "method": method, "params": params}
            if session_id is not None:
                payload["sessionId"] = session_id
            deadline = time.monotonic() + self._timeout
            try:
                data = json.dumps(payload)
                self._ws.send(data)
            except Exception:
                self._close_unlocked()
                raise RuntimeError("send failed") from None
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._close_unlocked()
                    raise RuntimeError("timeout") from None
                try:
                    msg = self._ws.recv(timeout=remaining)
                except TimeoutError:
                    self._close_unlocked()
                    raise RuntimeError("timeout") from None
                except Exception:
                    self._close_unlocked()
                    raise RuntimeError("receive failed") from None
                if not isinstance(msg, (str, bytes)) or len(msg) > 8 * 1024 * 1024:
                    self._close_unlocked()
                    raise RuntimeError("message too large") from None
                try:
                    obj = json.loads(msg)
                except (ValueError, UnicodeError):
                    self._close_unlocked()
                    raise RuntimeError("invalid JSON") from None
                if not isinstance(obj, dict):
                    self._close_unlocked()
                    raise RuntimeError("invalid message") from None
                if "id" not in obj:
                    continue  # event
                if type(obj.get("id")) is not int or obj["id"] != request_id:
                    self._close_unlocked()
                    raise RuntimeError("unexpected response id") from None
                # matched response
                if (obj.get("sessionId") != session_id
                        or (session_id is None and "sessionId" in obj)):
                    self._close_unlocked()
                    raise RuntimeError("session id mismatch") from None
                if "error" in obj:
                    self._close_unlocked()
                    raise RuntimeError("CDP error") from None
                if "result" not in obj:
                    self._close_unlocked()
                    raise RuntimeError("missing result") from None
                result = obj["result"]
                if not isinstance(result, dict):
                    self._close_unlocked()
                    raise RuntimeError("result not a dict") from None
                return result

    def close(self):
        with self._lock:
            self._close_unlocked()

    def _close_unlocked(self):
        if self._closed:
            return
        self._closed = True
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

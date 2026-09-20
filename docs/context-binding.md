# Explicit browser context binding

This fork adds a first session-isolation building block for Sweeps. A trusted
caller can bind an agent to an existing nondefault CDP browser context:

```python
from jev_ultrafast import Agent

# Obtain this from trusted runner provisioning, not page text or a model.
assigned_context = "context-provisioned-for-this-account"
# Supply the browser WebSocket endpoint from the same trusted runner.
browser_endpoint = "ws://127.0.0.1:9222/devtools/browser/provisioned-browser-id"
with Agent(
    "https://example.test/offers",
    "Find the current offer and stop when its terms are visible.",
    browser_context_id=assigned_context,
    browser_ws_url=browser_endpoint,
) as agent:
    for state in agent.run():
        print(state["status"])
```

Both options are required for scoped mode. Each instance opens a dedicated CDP
WebSocket connection; it never starts or uses the shared browser-harness daemon.
The pinned harness drops session IDs on `Target.*` commands, preventing the
attached-session check. Direct transport preserves them, correlates replies,
and rejects missing or mismatched session IDs. Use one Browser/Agent per job;
do not share a mutable instance between jobs or change its session/target fields.

The browser rejects malformed or unknown contexts, creates its own tab in the
requested context, and verifies the target before attaching. In scoped mode,
each page command verifies that the attached session belongs to that target,
is a page, and has the expected context. Missing/mismatched metadata stops with
a terminal error before reading page data or sending input; it cannot fall
back to the default profile. Cleanup attempts to close the created tab and never
disposes the caller's browser context. Transport errors permanently close the
connection; no reconnection or mutation retry is attempted. If the connection
breaks after tab creation, runner cleanup must reconcile the orphaned tab.

Omitting both options preserves upstream demo behavior in the attached browser's
default context. **Do not use that default for multi-member fleet jobs.** The
trusted Sweeps adapter must require an assigned context and authorize the
member/account mapping. This option is not authentication or authorization.

The caller provisions and owns the context. This change does not implement
persistent Chrome profiles, session enrollment, cookie persistence, or member
ownership verification. Context lifetime and credential provisioning are next
integration steps. Context IDs are passed in code, outside the natural-language
goal; do not put member credentials or profile details into model context.

The upstream performance measurements describe unscoped operation. Scoped mode
adds CDP verification calls and has not been benchmarked on live Sweeps runners.
Tests cover mocked browser decisions, the actual pinned harness routing bug,
and scoped Browser initialization/input/cleanup through a real local WebSocket
server with synthetic CDP replies. They make no paid model calls and do not open
a browser. Real Chrome and fleet validation remain pending.
Run `uv run python -m pytest -q` for the complete offline suite.
An agent's DONE state still needs independent outcome verification.

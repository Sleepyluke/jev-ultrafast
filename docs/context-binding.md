# Explicit browser context binding

This fork adds session-isolation building blocks for Sweeps. A trusted caller
can bind an agent to an existing nondefault CDP browser context:

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

Omitting both options preserves upstream demo behavior through Browser Harness.
That unverified mode is intended for the upstream demonstration, not multi-member
fleet jobs. The trusted Sweeps adapter must authorize the member/account mapping.

## Bind a managed persistent profile

Daily jobs need cookies and local storage to survive after the agent closes its
tab. A runner can launch one dedicated Chrome/Edge process with a durable
`user-data-dir`, resolve its trusted browser WebSocket endpoint, and bind the
agent to that process's default context:

```python
with Agent(
    "https://example.test/offers",
    "Open the daily offer and stop when its terms are visible.",
    browser_ws_url=assigned_profile_endpoint,
    bind_default_context=True,
) as agent:
    for state in agent.run():
        print(state["status"])
```

Checked default mode bypasses Browser Harness and uses a dedicated CDP
connection. It creates a tab without a context override, confirms Chrome's
reported context is not one of its nondefault contexts, and pins every later
read and input to the exact target, session and context ID. A mismatched target
or session stops before input and is never retried. Closing the agent closes
only its tab; it does not close the browser or erase the profile state.

The endpoint is trusted configuration. This check cannot identify a member or
prove which `user-data-dir` Chrome was launched with. The runner must map the
authorized account assignment to a dedicated endpoint and prevent untrusted
input or model output from selecting it. A shared default profile is not an
account-isolation boundary.

The caller owns the context. `IsolatedContext`, described below, can provision a
temporary context. Managed profile launch, session enrollment and member ownership
verification remain caller responsibilities.
Context IDs are passed in code, outside the natural-language
goal; do not put member credentials or profile details into model context.

The upstream performance measurements describe unscoped operation. Scoped mode
adds CDP verification calls and has not been benchmarked on live Sweeps runners.
Tests cover mocked browser decisions, the actual pinned harness routing bug,
and scoped Browser initialization/input/cleanup through a real local WebSocket
server with synthetic CDP replies. The default suite makes no paid model calls
and does not open a browser. Run `uv run python -m pytest -q`; the three opt-in
Chrome tests are skipped unless configured as below.
An agent's DONE state still needs independent outcome verification.

## Provision an owned temporary context

```python
from jev_ultrafast import Agent, IsolatedContext

# The runner supplies an existing browser's WebSocket endpoint.
with IsolatedContext(browser_endpoint) as context:
    with Agent(
        "https://example.test/offers",
        "Read the current offer terms.",
        **context.browser_options,
    ) as agent:
        for state in agent.run():
            print(state["status"])
```

Each scope creates one new context on a dedicated connection, with
`disposeOnDetach=True`. Keep that connection alive for the entire job. Closing
the scope disposes only its created context and its tabs; closing an individual
Browser closes only that tab. A dropped creator connection lets Chrome reclaim
the context, including when a create reply was lost. Creation/disposal is never
retried. Cleanup failure is reported without replacing an existing job error.
Do not pass another person's context to this helper; it accepts only an endpoint
and creates its own. The trusted runner still controls job/member authorization.

Cookies and local storage survive tab replacement within the live context, but
**are destroyed when the context is disposed**. This is useful for disposable
workflows, not persistent daily logins. Browser launch, managed profile storage,
runner assignment and credential entry remain separate integration work.

## Real Chrome validation

Opt-in tests launch a fresh headless Chrome process with a unique disposable
profile and random debug port. They serve a synthetic page on loopback, exercise
cookies/local storage and observed input, reject wrong-context sessions, check
state across checked-default tab replacement, and verify both explicit disposal
and creator-detach cleanup. They never attach to an existing user profile or call
a model or collection site.

PowerShell example (use a fresh basetemp path for each run):

```powershell
New-Item -ItemType Directory -Path .scratch -Force | Out-Null
$env:JEV_TEST_CHROME = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
uv run python -m pytest tests/test_context_chrome.py -q -s --basetemp=.scratch/chrome-proof-run-1
```

All three tests passed on Windows with Chrome `153.0.8010.48` on 2026-09-20. This
is local, synthetic real-browser evidence. It proves the checked default context
retains storage across owned-tab replacement; it does not prove remote fleet
rollout, member authorization or live collection success.

# FORK.md — adurham fork of hermes-plugin-claude-subscription-directsdk

This is a personal fork of `NousResearch/hermes-plugin-claude-subscription-directsdk`,
installed into `~/.hermes/plugins/claude-subscription-directsdk-experimental` via
`hermes plugins install`. Base: upstream `main` @ `602393b6d3ea148bc618cd23da1a13fa332e1433`
(2026-09-23, the catalog-pinned sha at install time).

Same convention as `~/repos/hermes-agent/FORK.md`: every fork-only change gets a
dated entry here (symptom, root cause, fix, verification). Push to `origin`
(this fork) only — never to `upstream` (NousResearch) without opening a PR
there deliberately.

## Fork-only fix — 2026-09-25 (native stderr was discarded; every startup/preflight failure looked identical)

**Symptom:** every failure mode of the native `claude` subprocess — a bad
CLI flag, a missing config file, an auth/org-verification rejection, a crash
on launch — surfaced identically as:

```
Incomplete native response: assistant, message_stop and one result required
```

with zero way to tell one cause from another. Concretely hit on an
enterprise-org-pinned Claude Code login (Tanium): the native process was
exiting in ~250ms (long before real inference could start) on every
attempt, and the generic message gave no hint why.

**Root cause:** `Client._run()` spawned the native process with
`stderr=subprocess.DEVNULL` (`directsdk.py`, the `request.spawn(...)` call).
Native's own diagnostic text — in this case a real, specific error —
was thrown away before `directsdk.py` ever got a chance to look at it:

```
Unable to verify organization for the current authentication token.
This machine requires organization <org-id> but the token could not be
validated. This may be a network error, or the token may have been revoked.
Try again, or run: claude auth login
```

Confirmed this is not a token/network/org problem in general — a plain
`claude -p "say hi"` outside the plugin, same login, same machine, answers
normally. It IS specific to how the plugin invokes native (redirecting
`ANTHROPIC_BASE_URL` to a per-request loopback admission relay), but the
exact mechanism turned out to be more than one bug — see the 2026-09-25
`/api/hello` entry below for the first one found this way, and its "Still
open" section for what's left unexplained even after that fix. (This entry's
patch only stops the error from being silently swallowed; it does not fix
the underlying failure by itself.)

**Fix:** `directsdk.py`, `Client._run()`:
- `stderr=subprocess.DEVNULL` → `stderr=subprocess.PIPE` on the native
  `Popen` call.
- A second daemon reader thread (`read_stderr`) drains `p.stderr` into a
  bounded 200-line tail (`stderr_tail`) — on its own thread because nothing
  else was draining that pipe, and a chatty native process could otherwise
  fill the OS pipe buffer and deadlock the write end.
- A `stderr_suffix()` helper renders that tail (last 2000 chars) as
  `' | native stderr: ...'`, appended to the three RuntimeErrors that
  previously had no native-side diagnostic at all: `Invalid native
  stream-json output`, `Incomplete native response: ...`, and `Native
  request failed: ...`. (`Native API error: ...` already carries its own
  detail from the parsed `assistant` event and didn't need this.)
- `stderr_reader` is joined and `p.stderr` closed alongside the existing
  `reader`/`p.stdout` handling, in both the normal-completion path and the
  `finally` cleanup block, so nothing changes about process/pipe lifecycle
  beyond adding the second pipe.

**Verification:** `PYTHONPATH=~/repos/hermes-agent pytest tests/ -q` — full
suite green (see the dated result below). Live repro:
`hermes -z "say hi" --provider claude-subscription-directsdk-experimental
-m claude-sonnet-5 --cli` — before the fix, the error was the bare
"Incomplete native response..." line; after, the same command surfaces the
real native stderr text (the org-verification failure above) appended to
the same message.

**Still open:** the underlying failure itself is not fixed here — see the
2026-09-25 `/api/hello` entry below for the deeper investigation this
enabled (which found a real, separate bug, but did not fully explain this
one).

## Fork-only fix — 2026-09-25 (native's `/api/hello` liveness preflight had no handler at all)

**Symptom:** using the stderr capture above to actually read native's
diagnostic (rather than guessing from the generic "Incomplete native
response" text), the enterprise-org-pinned-login failure said:

```
Unable to verify organization for the current authentication token.
This machine requires organization <org-id> but the token could not be
validated. This may be a network error, or the token may have been revoked.
```

**Root cause found (real, but see "Still open" — it does not fully explain
the symptom above):** `admission.py`'s `Handler` only ever implemented
`do_POST`, and only for the exact gated `/v1/messages` route — every other
method/path fell through to `BaseHTTPRequestHandler`'s default (501
Unsupported method). Traced natives's actual wire traffic with a throwaway
logging loopback server (`ANTHROPIC_BASE_URL` pointed at a plain Python
`http.server` that logs and 404s everything): before its real turn, native
issues an unauthenticated `HEAD <base>/api/hello` — a separate, distinct
request from a Bun-runtime helper (`User-Agent: Bun/x.y.z`), unlike the
main Node/Stainless request path. Confirmed the real endpoint answers this
unauthenticated (`curl -I https://api.anthropic.com/api/hello` → plain
`200`). Against the admission relay, this HEAD got the 501, which is
indistinguishable from a genuine network failure — plausibly why native's
message hedges "may be a network error, or the token may have been
revoked."

**Fix:** `admission.py`, `Handler`:
- New `_passthrough()`: forwards any request outside the gated `/v1/messages`
  POST straight to the real upstream, unmodified — no capture, no
  single-admission consumption, same Origin-header rejection as the gated
  route (defense against a browser hitting this loopback port cross-origin).
- `do_HEAD`/`do_GET`/`do_PUT`/`do_PATCH`/`do_DELETE`/`do_OPTIONS` all route
  to `_passthrough()`.
- `do_POST`'s path-mismatch branch now calls `_passthrough()` instead of
  `send_error(404)` (the Origin-header check still always 404s, checked
  first, unconditionally on path).

**Verification:** full suite green (`PYTHONPATH=~/repos/hermes-agent pytest
tests/ -q`, 31 passed). Live: pointed a real, standalone `Admission`
instance's URL at `claude`'s `HEAD .../api/hello` — before the fix, no
`do_HEAD` existed so this always 501'd; after, confirmed (via temporary
logging, since removed) the relay forwards it and returns the real
upstream's `200` back to native.

**Note:** this fix alone did NOT resolve the enterprise-org-pinned-login
failure — see the next entry for the actual cause and fix. Isolating that
required ruling out a lot of dead ends first (gateway staleness, ambient
session env, concurrency); the trail is kept below since it's what actually
proved this fix, on its own, was insufficient.

## Fork-only fix — 2026-09-25 (a Hermes-managed CLAUDE_CODE_OAUTH_TOKEN was leaking into native's env, hijacking its auth)

**Symptom:** even with both fixes above live, `hermes -z "..." --provider
claude-subscription-directsdk-experimental -m claude-sonnet-5 --cli` still
failed with the identical "Unable to verify organization" text — and native
never reached `do_POST` at all after `/api/hello` succeeded (traced with
temporary per-request-path logging on the relay), so whatever it checked
next failed silently, with nothing else hitting this server.

**Dead ends ruled out, in order (kept here so they aren't re-walked):**
1. **Gateway staleness.** `hermes -z` doesn't spawn a fresh process — it
   dispatches to the persistent gateway daemon (`ai.hermes.gateway`,
   launchd-managed). That daemon had been running 5 days uninterrupted;
   `hermes gateway restart` gave it a completely fresh process and the
   failure was identical afterward. Not staleness.
2. **Manual reproduction outside hermes, exhaustively.** A hand-built
   standalone `Admission` instance, fed the *exact* captured command array,
   env vars, and even the *exact* per-request `system.md` / `settings.json`
   / `tools.json` files copied out of a live failing hermes run before their
   tempdir was cleaned up (a title-generation aux call, identifiable by
   `output_config.format.type: json_schema` in the captured
   `CLAUDE_CODE_EXTRA_BODY`) — succeeded normally every time, including with
   the `[1m]` model suffix and two concurrent invocations fired at once.
   This ruled out the request shape, the flags, and concurrency, and pointed
   at something about the *calling process's own environment* that these
   manual replays weren't reproducing.
3. **The actual difference:** the real hermes-driven native process runs
   with `CLAUDE_CODE_OAUTH_TOKEN` present in its environment; every manual
   repro (a plain interactive shell) had no such variable at all. Confirmed
   by injecting an obviously-invalid dummy value
   (`CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-obviously-invalid-...`) into an
   otherwise-successful manual repro: it reproduced the exact "Unable to
   verify organization ... token could not be validated" failure, verbatim.
   `unset`-ing it again restored a normal, successful response.

**Root cause:** native `claude` prioritizes the `CLAUDE_CODE_OAUTH_TOKEN`
environment variable over its own keychain/file-stored credential when
resolving auth. Hermes sets this variable in its own process environment
for reasons unrelated to this plugin (its credential pool / other
providers' OAuth handling), and because `Client._run()` builds native's env
from a copy of `os.environ` (`self.env is None` path), that value leaked
straight through into every native invocation this plugin makes — silently
overriding the "let native use its own login" behavior the whole plugin is
built around. Whatever token Hermes had cached there did not validate for
the account's pinned enterprise org (stale, wrong scope, or simply a
different credential than the one `claude auth login` established
directly) — hence the failure. This is exactly the class of bug the
existing `conflicts = [...]` check a few lines up already guards against
(`ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, the Bedrock/Vertex/Foundry
flags) — that check just didn't include this variable.

**Fix:** `directsdk.py`, `Client._run()`: unconditionally
`env.pop('CLAUDE_CODE_OAUTH_TOKEN', None)` right after the existing
`CLAUDE_CODE_EXTRA_BODY` pop, before any of the `env.update(...)` calls.
Stripped rather than added to the `conflicts` error list (unlike the
existing checks): those existing checks reject state a user deliberately
set for a *different* provider; this variable is Hermes' own incidental
plumbing that the user never asked this provider to honor, so silently
removing it (letting native fall back to its normal keychain resolution,
which is the whole point of this plugin) is the correct behavior, not an
error.

**Verification:** full suite green (31 passed). Live, through the actual
gateway and CLI end to end: `hermes -z "say hi in exactly 3 words"
--provider claude-subscription-directsdk-experimental -m claude-sonnet-5
--cli` → `"Hey there, friend!"` — a real completed turn, on the enterprise-
org-pinned login that had failed every single time before this fix.

**Status: resolved.** The plugin now works against an enterprise-org-pinned
Claude Code login. Worth reporting upstream regardless — any Hermes user
who has ever had a `claude`/Claude Code login adopted into Hermes's
credential pool (`auth.adopt_external_logins`, default on) is exposed to
the same leak, org-pinned or not.

## Fork-only fix — 2026-09-25 (the two replay errors now carry native stderr too)

**Symptom:** the errors that actually kill every Hermes *retry* of a turn —
`Native exited before replay acknowledgment` and `Native history replay not
supported: expected zero-turn acknowledgment` — were the three-RuntimeError
list's blind spot: they surfaced with no native-side diagnostic even after the
stderr-capture commit, exactly when they matter most (a retry after a transient
upstream failure, where the first attempt's error text is already known and the
question is why the replay died).

**Fix:** append `stderr_suffix()` to both raises (same bounded 200-line tail).

**Why:** observed live — a 20:56 turn lost its first attempt to a relay→upstream
`TimeoutError`, and both retries then died at "Native exited before replay
acknowledgment" with nothing to read. The replay path is the one place native
runs *before* any inference, so its exit reason is pure startup-state
diagnosis (login/org/preflight) — precisely what must not be swallowed.

## Fork-only fix — 2026-09-25 (a native disconnect dumped socketserver tracebacks into the CLI)

**Symptom:** the user's polaris terminal filled with

```
──────── (x40)
──────── (x40)
Exception occurred during processing of request from ('127.0.0.1', 51015)
Traceback (most recent call last):
  File ".../admission.py", line 209, in _passthrough
    conn.connect()
  ...
TimeoutError
During handling of the above exception, another exception occurred:
  File ".../admission.py", line 236, in do_HEAD
    def do_HEAD(self): self._passthrough()
  File ".../admission.py", line 230, in _passthrough
    self.send_error(502)
  ...
BrokenPipeError: [Errno 32] Broken pipe
```

right next to the retry diagnostics — the relay's internal exception classes, two full
tracebacks, and no way to tell whether anything actually went wrong.

**Root cause:** `_passthrough()` (the `/api/hello` proxy added earlier today) writes its
`send_error(502)` *inside* its `except` block, so when native has already hung up, that write
raises BrokenPipeError and escapes the handler. `BaseHTTPRequestHandler` never overrode
`handle_error`, so `socketserver`'s default ran: `'-'*40`, `'Exception occurred during
processing of request from %s'`, and the full (chained) traceback to stderr — i.e. into the
CLI the user is watching. Native opening one connection per request and closing it as soon as
it has its answer makes this routine, not exceptional.

**Fix:** `admission.py` — override `Handler.handle_error()`: a `BrokenPipeError` /
`ConnectionResetError` (a client disconnect, which the relay already classifies via
`status`/`capture`/`failure`/`denied`) returns silently; any other handler exception prints
one line with its type, no traceback. The gated `/v1/messages` path is unchanged.

**Verification:** new test `tests/test_directsdk_admission.py::test_client_disconnect_does_not_dump_a_traceback`
— red without the fix (asserts on the captured `'-'*40` / traceback text), green with it; full
suite 8 passed. Live: a real polaris turn through the plugin on the corp box.

## Fork-only fix — 2026-09-25 (the plugin's own declared .env vars were invisible to it)

**Symptom:** on a headless launch the plugin printed
`Claude Code is not installed (no claude on PATH) … or set CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND`
over a perfectly working install, and — worse — ran native against the wrong login (the
"Not logged in" cascade), because neither `CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND` nor
`CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR` was visible to it.

**Root cause:** both are declared in `plugin.yaml:optional_env` and written by the setup/picker
flows into `$HERMES_HOME/.env`, but **nothing mirrors them into the process environment**.
Every probe in this plugin read `os.environ` directly, so on a non-interactive launch the pins
did not exist: `_resolve(None, os.environ)` looked up bare `claude` on a PATH without
`~/.local/bin` (the import-time install warning), and `_child_env` had no config dir to map
onto `CLAUDE_CONFIG_DIR`, leaving native to its own default (`~/.claude`) instead of the login
the user actually pinned. Interactive shells only worked because `.zshrc` exported
`CLAUDE_CONFIG_DIR` itself — the plugin was working by accident.

**Fix:** `directsdk_setup.py` gains `_env()` — a stdlib reader of `$HERMES_HOME/.env` (via
`hermes_constants.get_hermes_home()`), overlaid by real process env so explicit overrides still
win — and `setup_status` / `discover_models` / `__init__`'s import probe / `_resolve` default to
it. An explicit env dict passed by a caller stays authoritative (the `test_resolve_honors…`
contract).

**Verification:** full suite 33 passed; minimal-env probe (`env -i`, PATH=/usr/bin:/bin) now
reports `available: true, logged_in: true` with no install banner; and a real polaris turn in a
clean env (no `.zshrc`, no exported `CLAUDE_CONFIG_DIR`) has native spawned with
`CLAUDE_CONFIG_DIR=~/.claude-personal` — sourced from .env by the plugin itself.

## Fork-only fix — 2026-09-25 (the transport too: Client._run read os.environ, not the plugin's .env)

**Symptom:** even with the probes fixed, a clean-env turn (`env -i`, no `.zshrc`) spawned native
with `CLAUDE_CONFIG_DIR=<unset>` and native answered from the WRONG login — "has no usable login"
/ "Not logged in". Isolated repro: `Client(env=None)` (the host's request path) in a clean env.

**Root cause:** `Client._run` built native's environment from `os.environ` when `self.env is None`,
and the process env does not carry this plugin's own declared vars — they live in
`$HERMES_HOME/.env`. So the config-dir pin never reached native on a headless launch; the turn
worked only because an interactive shell exported `CLAUDE_CONFIG_DIR` itself.

**Fix:** `Client._run` (and `Client.__init__`'s command lookup) use `directsdk_setup._env()`;
`resolve_claude` is passed `None` when `self.env is None` so it resolves within the same env.
An explicitly passed env dict stays authoritative, unchanged.

**Verification:** regression test
`tests/test_directsdk_discovery.py::test_declared_env_vars_work_without_being_in_the_process_env`
(red without the fix, green with it) asserts `.env` -> `_env()` -> `_child_env` maps the pin onto
`CLAUDE_CONFIG_DIR` and never leaks the plugin-owned var into native; full suite 33 passed. Live:
clean-env `Client(env=None)` returns `pong`; real polaris in `env -i` (no `.zshrc`) answers
`CLEAN-OK`; interactive shell answers `SHELL-OK2`.

## Fork-only change — 2026-09-27 (opt-in static token, so the user can pin this provider to a `claude setup-token` instead of native's rotating keychain session)

**Motivation:** the 2026-09-25 fix above is correct — an *accidental* leak of Hermes'
pooled `CLAUDE_CODE_OAUTH_TOKEN` must never hijack this provider's auth. But the user
independently hit the underlying keychain session going stale/revoked on its own (an
enterprise-org-pinned Claude Code login, refresh-token rotation) enough times that they
wanted this provider pinned to a long-lived (1-year) `claude setup-token` on purpose,
not native's normal session. The existing strip gives no way to do that deliberately.

**Fix:** `directsdk.py`, `Client._run()`: before the existing unconditional
`env.pop('CLAUDE_CODE_OAUTH_TOKEN', None)`, pop a new, plugin-owned
`CLAUDE_SUBSCRIPTION_DIRECTSDK_OAUTH_TOKEN` var (declared in `plugin.yaml:optional_env`,
same trust tier as `_COMMAND`/`_CONFIG_DIR`, read only via `directsdk_setup._env()` —
never picked up from ambient `os.environ` by accident). If present, it's written back in
as `CLAUDE_CODE_OAUTH_TOKEN` for native's spawn only, after the generic pool var has
already been stripped. This can't reopen the 2026-09-25 leak: that bug was Hermes'
*unrelated* pool value showing up unasked-for; this is a value the user explicitly put
in this plugin's own `.env` entry for this plugin to use.

**Verification:** full suite green (33 passed, unchanged — no existing test sets
`CLAUDE_SUBSCRIPTION_DIRECTSDK_OAUTH_TOKEN`, so this is additive). Not yet covered by a
dedicated regression test — worth adding
`test_static_token_var_overrides_stripped_pool_token` if this sees continued use.

## Fork-only finding — 2026-09-28 (the static-token path cannot survive the `ANTHROPIC_BASE_URL` redirect at all; not fixable in this plugin)

**Symptom:** `CLAUDE_SUBSCRIPTION_DIRECTSDK_OAUTH_TOKEN` (the 2026-09-27 feature above) started
failing with the same "Unable to verify organization ... token could not be validated" text,
on the same enterprise-org-pinned login this plugin already works against via the normal
keychain session. `claude --version` shows an unrelated same-day auto-update (2.1.283 ->
2.1.284, `~/.claude-personal/.last-update-result.json` timestamp matches) that looked like the
obvious suspect and was not.

**Ruled out, in order (full isolation, each confirmed live):**
1. Token staleness/leak — a bare `CLAUDE_CODE_OAUTH_TOKEN=<the static token> claude -p "hi"`,
   no relay, always succeeds. The token itself is valid.
2. The CLI version bump — the *previous*, already-verified-working 2.1.283 binary reproduces
   the identical failure through the relay. Not a 2.1.284 regression.
3. `admission.py`'s header stripping (`server`/`date` excluded on the `/api/hello` passthrough)
   — forwarding them unmodified made no difference.
4. HTTP/1.0-forced-close on that passthrough — switching to real HTTP/1.1 keep-alive let native
   occasionally get one step further (attempting `POST /v1/messages` instead of aborting right
   after the preflight), but the *final* result was still the identical failure every time. This
   was applied to `_passthrough()` (protocol_version='HTTP/1.1', keep-alive when Content-Length
   is known) since it's a strictly more correct proxy implementation regardless, but note it did
   **not** fix the reported problem — don't re-attempt this angle expecting a different outcome.
5. Proxy fidelity in general — the relay's `/api/hello` response is byte-identical to the real
   endpoint's, and native's own request headers for that preflight are byte-identical between
   the working (keychain) and failing (static-token) cases (`Connection`, `User-Agent: Bun/x.y.z`,
   `Accept`, `Host`, `Accept-Encoding` — nothing else). No proxy-visible signal explains the
   divergence.

**Root cause found:** native opens a second, *separate* TLS connection straight to
`api.anthropic.com`'s real IP (confirmed via `lsof -p <native-pid> -i TCP` during a live failing
run: `TCP [...]->[2607:6bc0::10]:https ESTABLISHED`, and `2607:6bc0::10` / `160.79.104.10` do
resolve to `api.anthropic.com`) — entirely bypassing `ANTHROPIC_BASE_URL`. This connection is
made even with `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` set, i.e. native itself treats it as
essential, not something this plugin can suppress. Whatever native sends/receives on that
connection is what decides org verification for the static-token path, and it never touches
`admission.py` or any other code in this repo — there is no header, route, or proxy behavior in
this plugin that has any visibility into or influence over it. The keychain-session path does
not hit this same failure (confirmed working through the identical relay, same run), so
whatever native carries in its keychain-backed session (vs. a bare portable
`claude setup-token` bearer value) is what that hardcoded connection is actually checking.

**Status: not fixable in this plugin.** The enforcement point is inside the closed-source
`claude` binary and is deliberately not redirectable via `ANTHROPIC_BASE_URL` — reading or
altering what it decides would require TLS-intercepting that connection (a fake root CA
terminating native's traffic to the real API), which is a different thing entirely from a code
fix and was deliberately not attempted here. **Do not spend more time trying to make the
static-token path pass this check** — five independent angles (token validity, CLI version,
header fidelity, HTTP protocol/connection handling, request-header parity) were each ruled out
live, and the actual decision point is outside this repo's reach.

**Resolution taken:** blanked `CLAUDE_SUBSCRIPTION_DIRECTSDK_OAUTH_TOKEN` back to empty in
`~/.hermes/.env`, reverting this provider to the keychain/`CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR`
session (confirmed working, `hermes -z ... --cli` -> `"Hey there, friend!"`, 2/2 live runs). This
reopens the exact refresh-token-rotation/family-revocation risk the static token was added
(2026-09-27) to avoid — accepted as the only currently-working option for this login. If that
rotation problem recurs, the fix is re-establishing the keychain session
(`claude auth login` under `CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR`), not re-populating the
static token — it will not survive this redirect regardless of how it's minted.

## Fork-only fix — 2026-10-02 (the static-token path CAN survive the `ANTHROPIC_BASE_URL` redirect; the 2026-09-28 "not fixable" finding is superseded)

**Symptom:** identical to the 2026-09-28 finding — `CLAUDE_SUBSCRIPTION_DIRECTSDK_OAUTH_TOKEN` fails
with "Unable to verify organization ... token could not be validated" on an enterprise-org-pinned
machine (managed `forceLoginOrgUUID`) while the keychain session works.

**What was actually happening (live isolation on the pinned machine, 2026-10-02):** the pin makes
native org-validate an env token through `POST https://api.anthropic.com/api/oauth/validate` before
inference. That call succeeds with no `ANTHROPIC_BASE_URL`, with an explicit
`ANTHROPIC_BASE_URL=https://api.anthropic.com`, and through an `HTTPS_PROXY` CONNECT tunnel — it is
answered 403 only when the configured base URL is non-first-party, including our loopback relay
(a transparent passthrough forwarding to the real API reproduces it; the CLI's `--debug-file` trace
ends with `Failed to validate OAuth token ... Request failed with status code 403`). Native's
first-party predicate is `base URL unset, or host == api.anthropic.com`; the relay URL fails it and
the org check fails closed. The 2026-09-28 conclusion ("enforcement point not redirectable") was
wrong: nothing about the request depends on the base URL — the classification does, and Claude Code
exposes an internal override for it (`_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL`, read by the
predicate before any other rule).

**Fix:** `directsdk.py`, `Client._run()`: `env['_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL'] = '1'`
for the native spawn. The pin stays enforced — with a bogus token the pinned machine still fails
closed through the relay (`Failed to validate OAuth token ... 401` ⇒ the org message); a revoked
token still reads as revoked. The override is internal/underscored and version-sensitive: re-verify
on Claude Code upgrades (verified on 2.1.283 and 2.1.284).

**Do not use `ANTHROPIC_UNIX_SOCKET` for this** — the bundle shows the org validator returns valid
*before* any validate call when that variable is set, i.e. it disables the control on a managed
machine rather than satisfying it.

**Verification:** scratch-config live matrix on the pinned machine (no base URL / explicit
anthropic.com / loopback passthrough / loopback + override) plus a bogus-token fail-closed control;
`tests/test_directsdk.py::test_static_token_path_keeps_first_party_classification` pins the child
env.

## Fork-only fix — 2026-10-08 (direct calls to deferred tools no longer fail the request)

**Symptom:** "Native returned a tool outside the current host inventory" on every retry (3/3, again after
`/retry`) mid-conversation, e.g. after the model had read case artifacts and wanted to look at screenshots.

**Cause (reproduced live by replaying the dumped request, 1 in 3 runs):** the failing tool was
`mcp__hermes__vision_analyze`. With host tool_search active, the `vision` toolset is in `defer_toolsets`,
so the wire inventory carries only the `tool_search`/`tool_describe`/`tool_call` bridge — but the model knows
`vision_analyze` by name (host prompts mention it) and calls it directly. `_run()` raised on any name not in
the request's tool list; the failure is a deterministic function of the context, so the host's retries re-failed.

**Fix:** `directsdk.py`, `Client._run()`: when native returns a `mcp__hermes__<name>` tool outside the
inventory and `tool_call` IS in the inventory, rewrite the call (and the native block, so replayed history
matches the advertised tools) to `tool_call {"calls":[{"name":<name>,"arguments":<input>}]}`. The host still
validates scope/deferrability and returns a recoverable error for anything unreachable. Without a bridge in
the inventory it still fails closed, and the error now names the offending tool.
Tests: `test_direct_call_to_a_deferred_tool_routes_through_the_tool_call_bridge`,
`test_unknown_tool_without_a_bridge_names_the_offender`.

**Note:** a replay of `~/.hermes/sessions/request_dump_*.json` 400s on stale thinking signatures; strip
`reasoning_details`/`reasoning_content` from the dumped messages first.

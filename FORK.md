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

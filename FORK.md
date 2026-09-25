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

**Still open — the enterprise-org failure is NOT fixed by this, and does
not reproduce outside the live `hermes` process:**
- With this fix live, `/api/hello` demonstrably succeeds (native receives a
  real `200`), but `hermes -z "..." --provider
  claude-subscription-directsdk-experimental -m claude-sonnet-5 --cli`
  still fails with the exact same "Unable to verify organization" text.
  Native never reaches `do_POST` at all after `/api/hello` succeeds (traced
  with temporary per-request-path logging) — so whatever it checks next
  fails silently on native's side, with nothing else hitting this server.
- Extensive manual reproduction attempts, run outside hermes entirely with
  a hand-built standalone `Admission` instance, using the *exact* captured
  command array, env vars, and even the *exact* per-request `system.md` /
  `settings.json` / `tools.json` files copied out of a live failing hermes
  run before their tempdir was cleaned up (title-generation aux call,
  confirmed by the `output_config.format.type: json_schema` in the captured
  `CLAUDE_CODE_EXTRA_BODY`) — all succeeded normally, including with the
  `[1m]` model suffix and two concurrent invocations fired at once. None of
  these reproduced the failure in isolation.
- The one env difference observed and not yet explained: the real hermes
  invocation runs as a Bash-tool child of an *already-running* Claude Code
  session, and inherits that session's full env (`CLAUDECODE=1`,
  `CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_CODE_SESSION_ID`,
  `CLAUDE_CODE_MESSAGING_SOCKET`, `CLAUDE_PID`, `CLAUDE_CONFIG_DIR` pointed
  at a non-default profile dir, etc.) — none of which this fork's manual
  repro attempts had set. Whether one of those inherited vars is what native
  actually keys its org-verification on (rather than anything the loopback
  relay sees) is untested and is the next thing to try: replay the exact
  captured command/files again, but from *inside* a live Claude Code
  session's own Bash tool (inheriting that ambient env) rather than a plain
  interactive shell.
- Given this, treat the enterprise-org-pinned-login case as **known broken,
  cause not fully isolated** — not merely "not yet fixed." Reporting
  upstream is still worthwhile (this fork's improved stderr message is
  better repro evidence than anything available before), but don't assume
  fixing `/api/hello` alone will resolve a report of this symptom.

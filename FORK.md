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
normally. It is specific to how the plugin invokes native: `ANTHROPIC_BASE_URL`
is redirected to a per-request loopback admission relay
(`http://127.0.0.1:<port>`, see `admission.py`) so the plugin can single-
admission-gate the one real upstream call. On an org-pinned enterprise
login, native does an extra org-verification handshake before the real
turn; that preflight request also gets redirected to the loopback relay,
which only knows how to proxy the one `/v1/messages` call — so the
verification can't complete and native refuses outright, before any real
inference is attempted. (The relay-vs-org-verification conflict itself is
NOT fixed by this patch — see "Still open" below. This patch only stops
the resulting error from being silently swallowed.)

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

**Still open:** the underlying org-verification-vs-loopback-relay conflict
itself is not fixed here — this plugin cannot currently complete a request
against an enterprise-org-pinned Claude Code login at all (any relay-routed
env would hit the same preflight). That needs the relay (`admission.py`) to
either pass the org-verification call through to the real Anthropic API
untouched, or have native skip that preflight when
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` (already set by this client)
covers it. Worth reporting upstream with this fork's improved error text as
the repro evidence. A personal (non-org-pinned) Pro/Max login is not known
to hit this — untested here, no such login available on this machine.

# Security policy

## Reporting a vulnerability

Open a private report through
[GitHub security advisories](https://github.com/danzaio/win-mcp-pro/security/advisories/new).
Please do not file a public issue for anything that lets a tool act on a
window it was not pointed at, or lets a refused call send input.

I read reports within a few days. If a report is valid I will say so, fix it
on a branch, and credit you in the changelog unless you ask me not to.

## What this server is, and is not

win-mcp-pro lets an MCP client act on your Windows desktop **with your
permissions**. That is the whole point of it, and it is also the risk.

- `--tools` and `--exclude-tools` decide which tools a client can call. They
  are a policy, **not an operating-system sandbox**. An allowed tool still
  runs with the privileges of the server process.
- `--read-only` exposes inspection and waiting only. No input, no window
  changes, no clipboard, no files.
- Input is bound by default: a coordinate click needs the `frame` of a fresh
  capture and the `target` element it intends. A mismatch is refused with a
  typed error and **no input event is sent**. `--input-unrestricted` turns
  this off.
- `confirm=True` on some tools is an argument the caller supplies. It is not
  a human approval dialog.
- The server logs tool names and argument *names* to stderr, never argument
  values. The log is not durable and is not evidence that an action worked.
- It speaks stdio to a local client. There is no network transport.

## In scope

- A refused call that still sends input.
- An action that lands on a different window or element than the one named.
- A tool reachable under `--read-only` or outside a `--tools` allowlist.
- Argument values written to stderr.

## Out of scope

- Anything that needs the client to be hostile *and* every relevant tool to be
  allowed: an allowed tool doing its job is not a vulnerability.
- Elevated windows. Windows blocks input from an unelevated process to an
  elevated one (UIPI); the server reports `ELEVATION_REQUIRED` instead.

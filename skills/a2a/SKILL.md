---
name: a2a
description: Agent-to-Agent protocol bridge for Ouroboros. Provides a local A2A-compatible server plus client tools for discovering and messaging other A2A agents.
version: 1.3.0
type: extension
entry: plugin.py
plugin_api: "2.0"
runtime: python3
permissions: [net, tool, route, widget, companion_process, inject_chat]
env_from_settings: []
when_to_use: User asks to communicate with another A2A-compatible agent, discover an agent card, send an A2A message, check A2A task status, or expose this Ouroboros instance as an A2A peer.
timeout_sec: 120
install_specs:
  - kind: pip
    package: "protobuf<6"
  - kind: pip
    package: "a2a-sdk[http-server]>=1.0.0,<2.0.0"
companion_processes:
  - name: a2a_server
    command: [python3, scripts/a2a_daemon.py]
    runtime: python3
    restart_policy: on_failure
---

# A2A skill

Note for reviewers: the companion daemon is declared in this manifest's
frontmatter under `companion_processes` (name `a2a_server`, argv
`[python3, scripts/a2a_daemon.py]`, runtime `python3`, restart policy
`on_failure`) and is registered at load via
`api.register_companion_process("a2a_server")`. The host's extension
loader resolves the descriptor from the frontmatter; review-prompt
manifest summaries may omit the `companion_processes` field, so consult
the frontmatter above for the authoritative declaration.

This skill moves Ouroboros's Agent-to-Agent protocol support out of the
core runtime. It exposes a small local A2A-compatible JSON-RPC server and
registers three client tools:

- `discover` — fetch another agent's Agent Card.
- `send` — send a message to another A2A agent.
- `status` — check a remote task status.

The companion process talks back to the host through the loopback Host
Service API using the reviewed `SkillToken` grant. It does not patch the
core runtime and stores task state under the skill state directory.

## Outbound peer credentials

The client tools (`discover`, `send`, `status`) send HTTP Basic credentials only
to ONE explicitly configured peer. Set both `A2A_CLIENT_PASSWORD` and
`A2A_CLIENT_PEER_URL`; the credential is attached only when the target URL's
origin (scheme, host, port) exactly matches that configured origin, and every
other peer is contacted anonymously. This is deliberate: the tools accept an
arbitrary caller-supplied URL, so a process-wide credential would be handed to
whatever address the model happened to pass.

## Agent card

The published Agent Card describes what this Ouroboros instance actually
does, capability-first. The top-level name is the stable product name
`Ouroboros` (operator-set `A2A_AGENT_NAME` wins when configured — the host
`/identity` name is deliberately NOT used: it is the identity document's
first heading, a section title like "Who I Am", not an agent name). The
description leads with a curated capability summary and appends the host
`/identity` first line as flavor (`A2A_AGENT_DESCRIPTION` overrides the
whole field). The skill list always begins with five curated capability
categories (code and files, web and media, long-running tasks and
projects, self-modification, skills), followed by live per-tool entries
from the host tool schemas (`GET /tools/schemas`). A discovery GET answers
in seconds: the request path uses the last known-good tool list and a
short fetch only, while a background refresher owns the long host-warmup
retry ladder and keeps the list warm. When the tool registry is genuinely
unavailable the card still carries the curated categories — it never
collapses to a contentless "General" stub nor to an identity-persona-only
entry.

## Streaming and long-running messages

`message/stream` emits real intermediate events (SDK mode): `submitted`,
then `working` status updates that forward the host agent's own progress
notes (the same narration the web UI shows, polled from the loopback
gateway's read-only `/api/logs/progress` endpoint and filtered by this
message's private negative chat id), with periodic heartbeats while the
agent is quiet, and finally one artifact plus a terminal `completed`
status. Failures end the stream with a terminal `failed` status carrying a
human-readable message — not a JSON-RPC `-32603` stream error.

Long tasks no longer die on internal timeouts. The chat-id allocation call
uses a configurable timeout with one retry (`A2A_ALLOCATE_TIMEOUT_SEC`,
default 30s — the old hardcoded 5s was a spurious-timeout source on busy
hosts). The host response wait (`A2A_RESPONSE_TIMEOUT_SEC`, default 600s)
may now be raised to 1740s, and when it expires the bridge does NOT fail:
the host task keeps running, and the bridge switches to polling the
durable chat log for the final answer until `A2A_STREAM_DEADLINE_SEC`
(default 3600s) elapses. Set `A2A_PROGRESS_ENRICH=0` to disable progress
forwarding (heartbeats only); `A2A_GATEWAY_URL` (loopback-only, default
`http://127.0.0.1:8765`) points the read-only log polling at a non-default
gateway port. The no-SDK fallback route (`message/send` only) shares the
same resilient dispatch pipeline without the event stream.

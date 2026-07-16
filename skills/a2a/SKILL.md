---
name: a2a
description: Agent-to-Agent protocol bridge for Ouroboros. Provides a local A2A-compatible server plus client tools for discovering and messaging other A2A agents.
version: 1.1.0
type: extension
entry: plugin.py
permissions: [net, tool, route, widget, read_settings, companion_process, inject_chat]
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

This skill moves Ouroboros's Agent-to-Agent protocol support out of the
core runtime. It exposes a small local A2A-compatible JSON-RPC server and
registers three client tools:

- `discover` — fetch another agent's Agent Card.
- `send` — send a message to another A2A agent.
- `status` — check a remote task status.

The companion process talks back to the host through the loopback Host
Service API using the reviewed `SkillToken` grant. It does not patch the
core runtime and stores task state under the skill state directory.

## Agent card

The published Agent Card describes what this Ouroboros instance actually
does. Its top-level name/description come from the host `GET /identity`
endpoint (operator-set `A2A_AGENT_NAME` / `A2A_AGENT_DESCRIPTION` still
win when configured), and its skill list is enumerated from the host tool
schemas (`GET /tools/schemas`) with a short bounded retry so a
companion that starts before the host chat-agent is ready still ends up
with a populated card. If the tool list is genuinely empty the card still
carries the real identity-derived description plus one honest capability
entry — it never collapses to a contentless "General" stub.

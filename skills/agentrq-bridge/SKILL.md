---
name: agentrq-bridge
description: "AgentRQ integration: structured HITL task board, permission gating, and real-time workspace bridge via MCP Streamable HTTP"
version: 0.1.0
type: extension
runtime: python3
entry: plugin.py
permissions:
  - net
  - tool
  - route
  - widget
  - read_settings
  - companion_process
  - inject_chat
env_from_settings:
  - AGENTRQ_MCP_URL
  - AGENTRQ_TOKEN
when_to_use: >
  User wants to interact with an AgentRQ workspace: pull tasks, create tasks
  for human review, reply to task threads, update task status, or request
  human permission for sensitive actions.
companion_processes:
  - name: agentrq_listener
    command: [python3, listener.py]
    runtime: python3
timeout_sec: 120
ui_tab:
  tab_id: dashboard
  title: AgentRQ
  icon: tasks
  render:
    kind: declarative
    schema_version: 1
    components:
      - type: poll
        route: status
        method: GET
        interval_sec: 15
        components:
          - type: kv
            fields:
              - label: Connected
                path: connected
              - label: Workspace
                path: name
              - label: Description
                path: description
              - label: Agent Online
                path: agent_connected
---

# AgentRQ Bridge

Connects Ouroboros to an [AgentRQ](https://github.com/agentrq/agentrq)
workspace via the MCP Streamable HTTP transport. AgentRQ is a task
orchestration platform for structured human-agent collaboration.

## What it provides

- **6 agent tools** that map to the AgentRQ per-workspace MCP surface:
  `rq_workspace`, `rq_pull_next`, `rq_create_task`, `rq_reply`,
  `rq_update_status`, `rq_get_messages`.
- **Real-time SSE listener** — a companion process that holds a
  persistent SSE connection to the AgentRQ MCP endpoint and
  injects events (new tasks, human replies, permission verdicts)
  into Ouroboros chat in real time via the Host Service API.
- **Declarative widget** showing live workspace connection status.
- **Zero external dependencies** — uses only Python stdlib (`urllib`,
  `json`, `socket`, `signal`).

## Setup

1. Deploy AgentRQ (Docker: `docker pull agentrq/agentrq:latest` or
   self-host via [SETUP.md](https://github.com/agentrq/agentrq/blob/main/SETUP.md)).
2. Create a workspace in the AgentRQ UI. Copy its **MCP URL** and **Token**
   from the workspace setup modal.
3. In Ouroboros **Settings → Secrets**, add two keys:
   - `AGENTRQ_MCP_URL` — the workspace MCP endpoint
     (e.g. `https://WORKSPACE_ID.mcp.agentrq.com/` or
     `http://localhost:8080/mcp/WORKSPACE_ID`)
   - `AGENTRQ_TOKEN` — the 16-char workspace token
4. Enable this skill and grant the two keys when prompted.

## Agent workflow

```
rq_workspace()             → see mission + stats
rq_pull_next()             → dequeue next task
rq_update_status(ongoing)  → mark task in progress
... do the work ...
rq_reply(task_id, result)  → post result to task thread
rq_update_status(completed)→ mark task done
```

To request human permission for a sensitive action:

```
rq_create_task(title="Approve: deploy to prod", assignee="human")
→ human sees it in AgentRQ UI, replies allow/deny
rq_get_messages(task_id) → read the verdict
```

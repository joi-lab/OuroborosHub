---
name: backlog_manager
description: Interactive Improvement Backlog widget with kanban status management, filtering data, notes, and local item creation without mutating Ouroboros core memory directly.
version: 0.1.0
type: extension
runtime: python3
entry: plugin.py
permissions: [route, widget, tool]
env_from_settings: []
when_to_use: User wants to inspect, triage, add, annotate, or manage Ouroboros improvement backlog items from a reviewed widget.
timeout_sec: 60
ui_tab:
  tab_id: backlog
  title: Backlog Manager
  icon: list-checks
  span: 2
  render:
    kind: declarative
    schema_version: 1
    components:
      - type: poll
        target: backlog
        route: list
        method: GET
        auto_start: true
        interval_ms: 30000
        max_ticks: 1000
        label: Refresh backlog
      - type: kanban
        target: backlog
        path: kanban_cards
        columns:
          - { id: open,        label: Open }
          - { id: in_progress, label: In progress }
          - { id: deferred,    label: Deferred }
          - { id: done,        label: Done }
          - { id: wont_fix,    label: Wont fix }
        on_move: { route: move, method: POST }
      - type: kv
        target: backlog
        fields:
          - { label: Total,       path: stats.total }
          - { label: Open,        path: stats.open }
          - { label: In progress, path: stats.in_progress }
          - { label: Deferred,    path: stats.deferred }
          - { label: Done,        path: stats.done }
          - { label: Wont fix,    path: stats.wont_fix }
      - type: form
        route: add
        method: POST
        target: backlog
        label: Add backlog item
        fields:
          - { name: summary,              label: Summary,       required: true }
          - { name: category,             label: Category }
          - { name: proposed_next_step,   label: Next step,     type: textarea }
          - { name: evidence,             label: Evidence,      type: textarea }
          - { name: requires_plan_review, label: Requires plan review, type: checkbox }
      - type: form
        route: update
        method: POST
        target: backlog
        label: Update item / add note
        fields:
          - { name: item_id, label: Item id, required: true }
          - name: status
            label: Status
            type: select
            options:
              - { value: "",            label: "(no change)" }
              - { value: open,          label: Open }
              - { value: in_progress,   label: In progress }
              - { value: deferred,      label: Deferred }
              - { value: done,          label: Done }
              - { value: wont_fix,      label: Wont fix }
          - { name: note, label: Note, type: textarea }
---

# Backlog Manager

Backlog Manager is a reviewed extension widget for triaging Ouroboros's Improvement Backlog.

It reads `memory/knowledge/improvement-backlog.md` as the source backlog and stores user-facing management state in this skill's own state directory (`overlay.json`). The overlay contains status overrides, notes, and locally-created items. This keeps the core backlog pipeline untouched: execution reflections can continue appending to the source file while the widget provides an interactive working view.

The UI is rendered as a host-owned `kind: declarative` widget (schema v1). Components are Kanban-first: a polling refresher seeds the `backlog` state, then a compact kanban board (Open / In progress / Deferred / Done / Wont fix) supports drag-and-drop between columns through `/api/extensions/backlog_manager/move`. Stats appear as a small key/value strip beneath the board, followed by short forms for adding items and updating status / appending notes. No arbitrary JavaScript is loaded.

## Boundaries

- Does not mutate the self-modifying repo.
- Does not overwrite `memory/knowledge/improvement-backlog.md`.
- Writes only to the skill state directory.
- New widget-created items live in the overlay until a future core backlog API exists.
- Non-trivial implementation work still requires `plan_task` before code changes, as the backlog item itself may request.

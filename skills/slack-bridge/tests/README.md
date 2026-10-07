# Slack bridge continuation tests

The normal bridge suite exercises production parser, adapter, SQLite queues,
workers and Slack client against deterministic HTTP fixtures:

```sh
python3 -I -S "$CORE_ROOT/scripts/safe_test.py" --temp-parent /tmp -- \
  "$TEST_PYTHON" -m pytest -q "$BRIDGE_ROOT/skills/slack-bridge/tests" \
  --ignore="$BRIDGE_ROOT/skills/slack-bridge/tests/test_presence_process_consumer.py"
```

Set these variables to an isolated core candidate, this bridge checkout and a
dependency-only Python environment. The launcher prints disposable state roots
and changes its working directory to core, so the bridge test path is absolute.
It is not an OS sandbox. Test state is retained for diagnosis.

The [separate-process consumer](PRESENCE_PROCESS_CONSUMER.md) documents the
opt-in command, its six scenarios, retained evidence and exact production versus
synthetic boundaries. An unset core root reports SKIP, not consumer evidence.

The [inbound lease boundary](INBOUND_LEASE.md) documents the finite first-poll
budget regression, typed lease-loss containment, tests and remaining ownership
limits.

`test_presence_continuation.py` separately controls blocked author/child HTTP
polls, promoted child plus parent tail, equal-text selection identities,
replay/disconnect, legacy delivery, interrupted-author projection and observation
ACK loss. Those Host answers are scripted; they are not additional real-author
process scenarios.

`test_send_custody.py` uses production outbound code and a synthetic provider.
Its crash case exits a separate child after provider acceptance and before the
local receipt checkpoint. Recovery settles the marked row as uncertain without
resending. Other cases cover cancellation, migration, stale leases and known
no-effect retries. Local argument refusals and unreadable/empty upload input
exercise the real client and worker: zero provider requests, a failed receipt,
and no retry after restart or duplicate enqueue. OSError and lost-response
controls at generic dispatch and all three upload phases stay uncertain without
resending. This establishes the bridge's attempt boundary, not Slack
exactly-once delivery.

Proactive initiation, full Host crash/manual author continuation, Stop/Panic and
cancellation during core reacquisition remain core test obligations; this bridge
fixture does not certify those variants. Full application bootstrap, configured
review qualification, platform/native behavior and live adoption require their
own evidence. Run logs and dated pass counts belong in the source-bound handoff,
not in this maintained test description.

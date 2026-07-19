# Task Card Lifecycle and Recovery

Task cards are a read-only user-facing view of gateway work. They preserve useful lifecycle context without becoming an execution queue or control surface.

## Sources and persistence

- Foreground work emits `foreground-start` and terminal lifecycle activity. Its task-card state is persisted by the task-card plugin under `~/.hermes/plugins/task-card/cards/`.
- Background `/background` work emits a background card and persists its active routing and Todo snapshot in the routed profile's `gateway/background-tasks.json`.
- Relay activity enters through the gateway status ingress and is reduced into the same task-card store. Relay state is therefore available after the originating process finishes.

The task-card store retains terminal cards for recent history and explicit diagnostics. The background ledger intentionally retains only active records; a confirmed terminal publication removes its ledger entry.

## Acknowledgement-dependent cleanup

A background record is cleared only after the terminal Task Card publication is acknowledged as accepted. A rejected or timed-out acknowledgement leaves the record in the ledger so the next gateway start can retry reconciliation. Malformed ledger records are discarded rather than routed.

At gateway startup, the order is:

1. start local status ingress;
2. reconcile active background records as cancelled/interrupted task cards;
3. mark the gateway running and emit startup hooks.

Within each profile, active records reconcile concurrently so one missing terminal acknowledgement cannot serially delay every other recovery. This ordering lets reconciliation use the normal Task Card publication path. A gateway refresh or code-only update keeps the same Hermes home, so active background recovery state and persisted Task Card history remain available to the refreshed process.

## `/tasks` read-only index

`/tasks` is the existing alias for the task view. It now renders a compact, source-scoped index:

- active background records from the durable ledger;
- active Task Cards for the same platform/chat/thread; and
- recent terminal Task Cards for that same conversation.

The index is bounded to eight active and eight recent entries. It performs no task mutations, does not expose diagnostic routing fields, and does not list cards belonging to another chat or thread. Use `/agents` for the broader gateway process and active-agent view.

## Operator diagnostics

If a task appears stuck after a refresh, inspect the gateway log and the persisted ledger/card files locally. Do not delete a valid active ledger record merely to hide it: it is the recovery signal. A successful Task Card acknowledgement clears it automatically; a failed acknowledgement is deliberately retained for the next reconciliation pass.

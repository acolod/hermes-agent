# Task Card Lifecycle and Recovery

Task cards are a read-only user-facing view of gateway work. They preserve useful lifecycle context without becoming an execution queue or control surface.

## Sources and persistence

- Foreground work emits `foreground-start` and terminal lifecycle activity. Its task-card state is persisted by the task-card plugin under `~/.hermes/plugins/task-card/cards/`.
- Background `/background` work emits a background card and persists its active routing and Todo snapshot in the routed profile's `gateway/background-tasks.json`.
- Relay activity enters through the gateway status ingress and is reduced into the same task-card store. Relay state is therefore available after the originating process finishes.

The task-card store retains terminal cards for recent history and explicit diagnostics. The background ledger intentionally retains only active records; a confirmed terminal publication removes its ledger entry.

## Automatic and manual presentation

Automatic activities with bindings beginning `relay:`, structured `foreground:`, or structured `background:` use the polished Task Card presentation. Direct Kimi work reaches this path when the agent creates a genuine plan with the Todo tool; Relay activity reaches it through the authenticated gateway status ingress. Foreground activity without structured Todos does not create an automatic card.

Automatic cards keep Todo rows in first-seen order with stable IDs and numbering. Completed items do not regress when a later snapshot is stale or incomplete, and completed labels render as `~~*completed name*~~`. Relay, foreground, and background cards share the same progress, task-list, status, and outcome layout.

Manual cards, generated conversation-bound `task-...` cards, and explicit `/taskcard bind` cards remain on the generic renderer. They do not inherit automatic-card heartbeat or inferred display milestones.

## Timing and terminal authority

While an automatic Telegram card is active, a revisionless heartbeat edits the same message to refresh the human running timer. It does not consume or advance a lifecycle revision. Heartbeats pause for approval, stop at an authoritative terminal event, and are cancelled when the owning session finalizes. Terminal cards freeze the final duration and render an Outcome with concise Result and Next text.

Relay's outbound envelope returns before Relay publishes its terminal lifecycle event. The post-return terminal event is authoritative for the final card and does not create a duplicate card. A separate concise terminal alert may still be delivered by Relay according to its notification policy.

Session finalization uses the routing `session_key` as the primary owner and falls back to the durable session ID for legacy state. Finalized heartbeat keys are invalidated so an already in-flight heartbeat cannot reschedule itself after cleanup; genuinely new activity clears that in-memory invalidation.

## Acknowledgement-dependent cleanup

A background record is cleared only after the terminal Task Card publication is acknowledged as accepted. A rejected or timed-out acknowledgement leaves the record in the ledger so the next gateway start can retry reconciliation. Malformed ledger records are discarded rather than routed.

At gateway startup, the order is:

1. start local status ingress;
2. reconcile active background records as cancelled/interrupted task cards;
3. mark the gateway running and emit startup hooks.

Within each profile, active records reconcile concurrently so one missing terminal acknowledgement cannot serially delay every other recovery. This ordering lets reconciliation use the normal Task Card publication path. A gateway refresh or code-only update keeps the same Hermes home, so active background recovery state and persisted Task Card history remain available to the refreshed process.

## Final verification provenance

- Gateway implementation: `dbd7fd20422f22bc3812dd69d5c3ea6601fca5ed`.
- Relay implementation: `df711a82add354425927aae577f20eca489482b7`.
- Alex supplied final affected-suite evidence: `160 passed`.
- Final Relay live proof used Task Card message `16681` and terminal alert `16682`; the card showed exactly three completed Todos and a fixed 38-second duration.
- Alex also demonstrated successful direct foreground Task Card parity on July 21, 2026.

These are record-phase facts; this documentation update does not itself activate or restart runtime code. Literal already-escaped Markdown backslashes may be preserved rather than normalized, but that low-severity edge is not part of the normal polished-card path.

## `/tasks` read-only index

`/tasks` is the existing alias for the task view. It now renders a compact, source-scoped index:

- active background records from the durable ledger;
- active Task Cards for the same platform/chat/thread; and
- recent terminal Task Cards for that same conversation.

The index is bounded to eight active and eight recent entries. It performs no task mutations, does not expose diagnostic routing fields, and does not list cards belonging to another chat or thread. Use `/agents` for the broader gateway process and active-agent view.

## Operator diagnostics

If a task appears stuck after a refresh, inspect the gateway log and the persisted ledger/card files locally. Do not delete a valid active ledger record merely to hide it: it is the recovery signal. A successful Task Card acknowledgement clears it automatically; a failed acknowledgement is deliberately retained for the next reconciliation pass.

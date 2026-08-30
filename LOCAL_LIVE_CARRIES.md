# local/live carry ledger

This file documents the intentional local carry layer on top of upstream `main`
for the runtime checkout at `/home/alex/.hermes/hermes-agent`.

## Purpose

`local/live` is the daily runtime branch. It should stay close to upstream and
carry only a small, explicit overlay of local commits that are useful in daily
operation before they are absorbed upstream.

This Markdown file is the authoritative carry ledger. There is no separate
`~/.hermes/local-carries.json` source of truth.

## Audit manifest

The scheduled read-only audit consumes this fenced manifest. A string is one
functional carry commit; `{from, through}` is an inclusive contiguous,
merge-free first-parent carry stack. Ledger-only commits are recognized by
their changed paths. Other non-functional commits must be named under
`administrative`. Every first-parent merge must be named under
`upstream_merges`; the audit accepts it only when its second parent is already
an ancestor of the locally known `origin/main` tip.

```json carry-audit
{
  "version": 1,
  "base_ref": "origin/main",
  "live_ref": "local/live",
  "functional": [
    "0df35997ea9ead432ef06ed6a746fb2c4c025d94",
    {
      "from": "076c128369154f5451e1457120e721aab1e02214",
      "through": "002a788e4dc8da19bb39fe43a29329fb1dbf3cf5"
    },
    "e55743a57b320111743235bef946b78cf23c3c75",
    "1c2cd0caf3050f1628461f34b0834ff383226059",
    {
      "from": "3124edce5fa27319aca657043419ece5c58f2725",
      "through": "0b77e0d95d657dfa8c6361df5ef04f403850f578"
    },
    "1a760c7306e331da8ce704d0d942273e25513df5",
    {
      "from": "3f4318e95f5074d497e955c694c6574c97ad7962",
      "through": "74a46b9588a624b5816d47f31ba00711c6829464"
    },
    "18b7e16a06d5b7b7527d75c6e13b2adf626b82ae",
    "8f6bbe750fc0e35ede9dadd88e172b3d01ce4b73",
    "0b58c24c040ca1940b9c6263cac94214f3e1ebac",
    {
      "from": "e6f7c579d7567bc8ba70f1dad36f9db77c8afb97",
      "through": "1e4fc21b89144943391013c2bdce5bab7f52675d"
    },
    "d88a619876bae10df26879bbe804457633f9a0d7",
    {
      "from": "371b31fde2bc88c86a5c2537bc656a6aff54c7f5",
      "through": "0e66fafa9515a16d2d9d230ad18fa94d7c6ca5a1"
    },
    "5921ffbbef6880d4834e2471b66e42786a2fcf79",
    "db60f03cd3dd4b7a98806b8172d41e8194a73c29"
  ],
  "administrative": [
    "f1bd6b0d3f9a4f2c6659bfd558b5ad47fb9d3c54"
  ],
  "upstream_merges": [
    "0abb6a995fb4400b8b048ff9eaeb619c3b75f483",
    "b472958c33de5fe8787adebf0fd270e05cc29bb9",
    "39e0722a8efeaf26cc90afabd19dafcdf7304c86",
    "d80c143352a95cdaf1f93053ebeae3d7fe0f01a7",
    "eba7dc3339105faee97efdf169a0a30d6d942cd3",
    "61effe2f5f030e2316acf552f4263d6427a3680d",
    "6d6b2cf7892598abd1bd60b10fdc3ccb71562de2"
  ]
}
```

## Current functional carries

From oldest to newest, grouped by semantic family.

1. `0df35997ea` — gateway visible-response normalization
   - Normalizes visible text in gateway delivery and API-server paths.

2. `076c128369` through `002a788e4d` — opportunity-router stack
   - Adds specialist routing, keeps loose brainstorming local, and exposes the
     explicit CLI command.

3. `e55743a57b` — mobile Kanban scrolling
   - Preserves vertical scrolling and suppresses accidental touch drag.

4. `1c2cd0caf3` — gateway `/usage-report`
   - Adds the gateway-only usage telemetry summary command.

5. `3124edce5f` through `0b77e0d95d` — Kanban helper reliability
   - Validates helper inputs, preserves block reasons, and aligns prompt guidance.

6. `1a760c7306` and `18b7e16a06` — email identity and rich replies
   - Separates login/from/reply-to addresses and sends HTML with plain fallback.

7. `3f4318e95f` through `74a46b9588` — dashboard local/live update safety
   - Blocks unsafe generic updates, routes updates through the wrapper, labels
     the workflow, and keeps the action visible.

8. `8f6bbe750f` — gateway `/update` local/live routing
   - Routes gateway-triggered Agent updates through the same carry wrapper.

9. `0b58c24c04` — worktree-safe disk cleanup
   - Prevents cleanup from deleting test artifacts in active Git worktrees.

10. `e6f7c579d7` through `1e4fc21b89` — restricted Sam inbound-email route
    - Isolates delivery, applies bounded structured drafting, and enforces the
      approved capability/meaning/effect policy.

11. `d88a619876` — trusted reverse-proxy origins
    - Allows explicitly configured dashboard proxy origins without weakening
      the default origin gate.

12. `371b31fde2` through `0e66fafa95` — live task-card lifecycle
    - Adds authenticated status ingress, foreground/background lifecycle,
      Todo snapshots, Telegram edit hardening, interruption reconciliation,
      and verified relay timeline behavior.

13. `5921ffbbef` — root test-tool lock alignment
    - Preserves the existing root ESLint test dependency alignment.

14. `db60f03cd3` — deterministic local carry-integrity audit
    - Validates this Markdown manifest and the first-parent carry lane without
      fetching, updating refs, changing branches, or writing repository state.

## Administrative history

- Ledger-only commits are intentionally excluded from functional coverage when
  their changed paths are limited to this ledger and its evidence record.
- `f1bd6b0d3f` records the approved Sam inbound-email design and is classified
  as administrative documentation.
- First-parent upstream merge commits are inventory items, not functional carries;
  each is explicit in `upstream_merges` and accepted only when its second parent
  is contained by the locally known `origin/main` tip.

## Dependency notes

- Opportunity routing replays in the listed first-parent order.
- Kanban helper reliability replays in the listed first-parent order.
- The dashboard local/live stack requires `~/.local/bin/hermes-local-update`.
- The gateway `/update` wrapper depends on the dashboard/local updater contract.
- The Sam email stack is one policy unit and should not be partially replayed.
- The live task-card range is one integrated lifecycle stack; splitting it
  requires its own review and tests.

## Verification commands

All commands below are local-only. Do not fetch or update refs from the audit.

```bash
python -m pytest -o addopts='' \
  tests/gateway/test_api_server_normalize.py \
  tests/gateway/test_api_server_runs.py \
  tests/gateway/test_run_progress_topics.py -q

python -m pytest -o addopts='' \
  tests/agent/test_opportunity_routing.py \
  tests/cli/test_opportunity_router_command.py \
  tests/e2e/test_platform_commands.py -q

python -m pytest -o addopts='' \
  tests/tools/test_kanban_tools.py \
  tests/hermes_cli/test_kanban_core_functionality.py \
  tests/plugins/test_kanban_dashboard_plugin.py -q

python -m pytest -o addopts='' \
  tests/gateway/test_email.py \
  tests/gateway/test_email_transport_isolation.py \
  tests/gateway/test_sam_restricted_route.py \
  tests/tools/test_send_message_tool.py -q

python -m pytest -o addopts='' \
  tests/hermes_cli/test_web_server.py \
  tests/hermes_cli/test_dashboard_admin_endpoints.py \
  tests/hermes_cli/test_web_server_trusted_origins.py \
  tests/gateway/test_update_command.py -q

python -m pytest -o addopts='' \
  tests/gateway/test_task_card_lifecycle.py \
  tests/gateway/test_status_ingress.py \
  tests/plugins/test_task_card.py -q
```

## Operational note

Use `~/.local/bin/hermes-local-update` only for an explicitly approved update
operation. The scheduled carry audit must not call it because update checks may
fetch or change local reference state.

When dashboard update behavior fails, the cross-repo operator runbook remains at
`/home/alex/hermes-webui/LOCAL_CARRY_NOTES.md`.

The persistence contract is:

- the runtime should normally run from `local/live`, not `main`;
- local carries that must survive updates should be replayed onto `local/live`;
- every intentional non-administrative carry must be covered by this ledger;
- the audit reports locally known upstream divergence but never fetches to
  determine remote freshness.

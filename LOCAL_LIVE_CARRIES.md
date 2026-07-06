# local/live carry ledger

This file documents the intentional local carry layer on top of upstream `main`
for the runtime checkout at `/home/alex/.hermes/hermes-agent`.

## Purpose

`local/live` is the daily runtime branch. It should stay close to upstream and
carry only a small, explicit overlay of local commits that are useful in daily
operation before they are absorbed upstream.

## Current functional carries

From oldest to newest.

1. `a741a2af5` — `fix(gateway): normalize visible response text`
   - Normalizes visible response text in gateway delivery paths.
   - Composes visible-text normalization with media-resolution in the API server.

2. `d86de805b` — `Add opportunity-router specialist routing`
   - Adds the opportunity-routing foundation module.
   - Hooks routing into agent/gateway flows.

3. `0e17c1975` — `fix(agent): keep opportunity vetting local by default`
   - Keeps loose brainstorming local by default.
   - Preserves explicit validation / narrowing / comparison routing behavior.

4. `6734a219d` — `feat(cli): add opportunity-router slash command`
   - Adds the CLI slash-command surface for opportunity-router access.
   - Adds direct CLI command coverage.

5. `b51e94f6b` — `fix(kanban): make touch scrolling safe on mobile`
   - Restores long-press gating, scroll-intent detection, and click suppression
     for touch drag/drop in the kanban dashboard.
   - Restores mobile CSS affordances so vertical list scrolling wins over
     accidental card drag.

6. `99ab1473c` — `Add gateway usage-report command`
   - Adds a gateway-only `/usage-report` slash command that summarizes skill
     usage telemetry.

7. `72c161ed9` — `Add Kanban workflow helper validation`
   - Adds structured validation for kanban helper inputs and evidence-contract
     metadata.

8. `522c208af` — `[verified] fix kanban block reason handling`
   - Fixes metadata-only block handling so blocked runs/events preserve a sane
     default reason and classification payload behavior.

9. `7011ec18e` — `Tighten Kanban workflow guidance`
   - Keeps the worker prompt and kanban tool expectations aligned on review
     safety and proof-checking behavior.

10. `17872dcb9` — `feat(email): support separate login/from/reply-to addresses`
    - Separates mailbox login credentials from the public sender identity used
      in outbound email.
    - Adds `Reply-To` support and Resend-aware email sending.

11. `08c067d5c` — `fix(dashboard): block git update on diverged checkouts`
    - Adds a git preflight so the dashboard can detect when a local checkout has
      carried commits or a dirty worktree and should not offer a plain in-place
      update.

12. `9aac7c049` — `fix(dashboard): route local/live updates through wrapper`
    - Detects the `local/live` carry workflow and treats it as updateable from
      the dashboard via `hermes-local-update` instead of rejecting it as a
      generic diverged checkout.

13. `a71eae449` — `refactor(dashboard): label local-live updates clearly`
    - Makes the System page button and confirmation dialog explicitly say when
      an update is using the `local/live` workflow.

14. `323e1a37c` — `fix(dashboard): restore local/live update visibility`
    - Restores the dashboard status/action surface for local/live-managed
      checkouts so the wrapper update path remains visible.

15. `83b66754e` — `fix(email): send rich HTML replies with plain fallback`
    - Sends final email replies as rich HTML with a plain-text fallback,
      including the Resend delivery path.

## Prune note (2026-07-06)

On 2026-07-06 the `local/live` stack was rewritten to drop ledger-churn commits
that only recorded or re-synced carry state. The functional carry layer was
preserved; the ledger was consolidated to this single current-state document.

## Dependency notes

- The opportunity-router CLI/policy carries depend on the routing foundation
  commit `d86de805b`:
  1. `d86de805b`
  2. `0e17c1975`
  3. `6734a219d`
- The gateway normalization carry can be replayed independently.
- The kanban helper reliability stack should be replayed in this order:
  1. `72c161ed9`
  2. `522c208af`
  3. `7011ec18e`
- The email replyability carry is self-contained as commit `17872dcb9`.
- The dashboard update-guard carry is self-contained as commit `08c067d5c`.
- The dashboard local/live wrapper carry is self-contained as commit
  `9aac7c049` and should be kept on `local/live` with `hermes-local-update`
  available on `PATH` (or at `~/.local/bin/hermes-local-update`).
- The dashboard labeling carry depends on the wrapper carry already being
  present.
- The dashboard update-visibility carry depends on the wrapper and labeling
  carries already being present.
- The rich HTML email formatting carry depends on the existing email platform
  adapter / Resend paths.

## Verification commands

Gateway normalization carry:

```bash
python -m pytest -o addopts='' \
  tests/gateway/test_api_server_normalize.py \
  tests/gateway/test_api_server_runs.py \
  tests/gateway/test_run_progress_topics.py -q
```

Opportunity-router carries:

```bash
python -m pytest -o addopts='' \
  tests/agent/test_opportunity_routing.py \
  tests/cli/test_opportunity_router_command.py -q

python -m pytest -o addopts='' \
  tests/cron/test_codex_execution_paths.py \
  tests/e2e/test_platform_commands.py -q
```

Gateway `/usage-report` carry:

```bash
python -m pytest -o addopts='' \
  tests/e2e/test_platform_commands.py -q
```

Kanban helper reliability carries:

```bash
python -m pytest -o addopts='' \
  tests/tools/test_kanban_tools.py \
  tests/hermes_cli/test_kanban_core_functionality.py \
  tests/plugins/test_kanban_dashboard_plugin.py -q
```

Email replyability and rich-formatting carries:

```bash
python -m pytest -o addopts='' \
  tests/gateway/test_email.py \
  tests/tools/test_send_message_tool.py -q
```

Dashboard carries:

```bash
python -m pytest -o addopts='' \
  tests/hermes_cli/test_web_server.py \
  tests/hermes_cli/test_dashboard_admin_endpoints.py -q

npx tsc -b
```

## Operational note

Use `~/.local/bin/hermes-local-update` to refresh `main`, rebase `local/live`,
and report the live carry layer after updates.

The persistence contract is:
- the runtime should normally run from `local/live`, not `main`
- local carries that must survive updates should be cherry-picked onto
  `local/live`
- every intentional carry should be recorded in this ledger so branch migration
  audits can detect omissions quickly

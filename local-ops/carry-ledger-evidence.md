# Carry-ledger reconstruction evidence

Date: 2026-08-29

Live repository inspected: `/home/alex/.hermes/hermes-agent`

Candidate worktree: `/home/alex/workspace/worktrees/hermes-carry-audit-closure-20260829`

This record was produced from existing local Git objects and refs only. No
`fetch`, `pull`, service action, profile write, or live runtime change was
performed. No deployment ref, remote ref, scheduler entry, service, or runtime
configuration was changed while reconstructing this snapshot.

## Ref snapshot

- Reconstruction baseline: `6d6b2cf7892598abd1bd60b10fdc3ccb71562de2`
- Audit implementation: `db60f03cd3dd4b7a98806b8172d41e8194a73c29`
- Locally known `origin/main`: `00bbfc690060d1323ddb2f065297c7425cb71c26`
- Merge-base: `baa344dee76993f0444c18fc59a69738ccb339d0`
- Expected local divergence after ledger promotion: `origin/main...local/live` =
  3 base-only commits and 65 live-only first-parent/merged-lane commits.
- Expected first-parent lane after ledger promotion: 58 non-merge commits and
  7 merge commits. The audit implementation is functional; the final
  ledger/evidence commit is recognized structurally as ledger-only.

The base and live refs have diverged; neither is an ancestor of the other. This
is intentionally reported as unhealthy by the new audit. Resolving it belongs
to the separately approved local/live update workflow, not this read-only pass.

## Reconstruction method

The following read-only evidence classes were used:

- `git for-each-ref` for existing local refs;
- `git merge-base` and `git merge-base --is-ancestor`;
- `git log --first-parent` and `git rev-list --first-parent --no-merges`;
- `git cherry` for patch-equivalence orientation;
- `git show` and `git diff-tree` for commit subjects and changed paths;
- `git cat-file -e` for ledger-object existence.

The previous 15 ledger hashes all still existed as Git objects but none was
reachable from the rewritten `local/live` branch. They were replaced with the
reachable post-prune commits having the matching subjects and file families.

## Semantic families added after the prior ledger snapshot

- `8f6bbe750f` — gateway `/update` routes through local/live wrapper.
- `0b58c24c04` — disk cleanup preserves active worktree tests.
- `e6f7c579d7..1e4fc21b89` — restricted Sam inbound-email policy stack.
- `d88a619876` — trusted reverse-proxy origin support.
- `371b31fde2..0e66fafa95` — integrated live task-card lifecycle stack.
- `5921ffbbef` — root test-tool lock alignment.
- `db60f03cd3` — deterministic local-only carry-integrity audit and regression
  suite.

## Administrative classification

- Ledger-only commits changing only `LOCAL_LIVE_CARRIES.md` and this evidence
  path are recognized structurally by the audit.
- `f1bd6b0d3f` is a design-document commit for the Sam email stack.
- Seven first-parent upstream merge commits are explicitly inventoried. Each has
  exactly two parents and a second parent contained by locally known
  `origin/main`; unlisted or local-side merges fail the audit.

## Safety conclusion

The old ledger was stale because its hashes predated the branch rewrite and its
functional list stopped before later carry families. The refreshed Markdown
ledger now covers the current non-merge first-parent carry lane and explicitly
accounts for all seven first-parent upstream merges, including the two August 28
maintenance merges. The deployment checkout is clean. The lane itself is still
locally behind/diverged from `origin/main`; that condition remains visible for a
separately gated update decision and is not repaired by this read-only audit.

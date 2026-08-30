# Candidate cron prompt: Hermes local-live carry audit

## Preserved job metadata

- Job ID: `7a284433d0e6`
- Name: `Hermes local-live carry audit`
- Schedule: `every 1440m`
- Repeat: forever
- Delivery: `origin`
- Workdir: `/home/alex/.hermes/hermes-agent`
- Enabled toolsets: `terminal`, `file`
- Healthy final response: exactly `[SILENT]`

No scheduler update has been applied. This file is an activation candidate only.

## Replacement prompt

Run the installed deterministic carry audit exactly once:

```bash
python3 /home/alex/.hermes/hermes-agent/local-ops/hermes-local-carry-audit.py \
  --repo /home/alex/.hermes/hermes-agent
```

This is a strict read-only integrity audit. Do not run any other command. Do not
modify files, configuration, services, scheduler state, branches, refs, remotes,
or the working tree. In particular, do not run update wrappers or any Git
network, branch-changing, history-rewriting, merge, configuration, or cleanup
operation.

If the command exits 0 and stdout is exactly `[SILENT]`, respond with exactly
`[SILENT]` and nothing else.

If the command exits non-zero, return its stdout as a concise alert. If stderr
contains an execution failure not already represented in stdout, add only the
minimal failure detail needed to identify the broken check. Never attempt repair
or activation from this job.

If the command cannot be executed or its result is uncertain, fail closed with a
concise alert naming the uncertainty and the safest next action.

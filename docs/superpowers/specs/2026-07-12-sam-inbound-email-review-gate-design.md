# Sam Inbound Email Review Gate — Design

**Date:** 2026-07-12  
**Status:** Implemented as the restricted email route

## Goal

When authenticated mail from `sammyphillips19@gmail.com` reaches Kimi's IMAP mailbox, process it through a request-scoped, no-tools restricted lane. Safe conversational mail may receive a direct reply; action/private/ambiguous mail requires Alex review; credential/private-runtime requests are refused. Every outcome is reported to Alex in Telegram and email with a durable receipt.

## Existing behavior

The Hermes email adapter polls IMAP for unseen messages approximately every 15 seconds. Fetching the full message marks it seen. Sam is not in `EMAIL_ALLOWED_USERS`, so her messages are currently dropped before session creation and no notification or reply occurs.

## Architecture

The intake gate lives inside the existing Hermes email adapter before the ordinary sender allowlist/session path. Sam remains outside `EMAIL_ALLOWED_USERS` and can never create a normal Hermes session.

Email itself is request-scoped rather than a home-channel transport. `EmailAdapter` disables async and unsolicited delivery and accepts only explicit purposes: `direct_reply`, `explicit_review_packet`, and `explicit_notification`. Gateway lifecycle, progress, reasoning, heartbeat/status, self-improvement, Ops, cron, and unrelated completion sends therefore fail closed. Direct replies require the triggering RFC `Message-ID`; review packets and notifications start fresh unless explicit thread metadata is supplied.

A focused `SamRestrictedRoute` component:

1. Match only authenticated messages whose normalized sender is exactly `sammyphillips19@gmail.com`.
2. Preserve the source subject, full plain-text body, RFC `Message-ID`, `In-Reply-To`, and date.
3. Persist an intake record keyed by source `Message-ID` before notifications begin.
4. Generate one classification and proposed reply through an isolated `AIAgent` with `enabled_toolsets=[]`, `skip_memory=true`, `skip_context_files=true`, no SOUL loading, one iteration, and a dedicated restricted instruction.
5. Apply deterministic fail-closed overrides for actions, credentials/private data, attachments, prompt injection, and ambiguity.
6. Deliver outcome packets independently to Alex's main Telegram DM and `alexcolodner@gmail.com`.
7. Persist per-destination delivery state so duplicate polling retries only failed destinations.
8. Return without creating a normal inbound email conversation.

Configuration belongs in `config.yaml`, not `.env`, except existing mailbox credentials. Proposed email `extra` settings:

- `sam_restricted_route_enabled: true`
- `sam_restricted_telegram_chat_id: 6811930352`
- `sam_restricted_alex_email: alexcolodner@gmail.com`
- `sam_restricted_state_path: ~/.hermes/email-intake/sam-restricted-state.json`

The sender value is explicit rather than inferred from the general allowlist. Sam remains absent from `EMAIL_ALLOWED_USERS` so the normal autonomous reply path stays closed.

## Review packet

Both destinations receive:

- a clear `DRAFT ONLY — NOT SENT` label;
- sender, subject, source date, and source message ID;
- the complete original plain-text message;
- the complete proposed reply;
- a note that approval is required before sending.

The review email goes only to Alex and is not CC'd to Sam. Telegram delivery uses the configured main/home DM, not Hermes Ops.

## Drafting rules

The restricted output must:

- identify the assistant naturally as Kimi when relevant;
- answer Sam's actual message directly;
- be warm, concise, and honest;
- avoid pretending to be human;
- avoid medical advice or unsupported claims;
- never imply that the draft has already been sent;
- classify exactly `DIRECT_REPLY`, `REVIEW_REQUIRED`, or `REFUSE`;
- preserve reply-thread metadata for direct or later approved sends.

`DIRECT_REPLY` sends immediately in-thread. `REVIEW_REQUIRED` sends no substantive reply to Sam and labels Alex's packet `DRAFT ONLY — NOT SENT`. `REFUSE` sends a fixed concise refusal in-thread. Direct and refusal reports are labeled `AUTO-REPLIED — SENT` or `REFUSED — SENT`.

Alex can reply from the authenticated review mailbox with `SAM APPROVE <receipt>`, `SAM DECLINE <receipt>`, or `SAM EDIT <receipt>` followed by the replacement draft on subsequent lines. Approval is idempotent and sends the stored exact draft once using the preserved RFC thread metadata.

If model drafting fails, notifications still go out immediately with the original message and `Draft unavailable — generation failed`; the failure is recorded for retry or manual drafting.

## State and deduplication

Use an atomic JSON state file with one record per source RFC `Message-ID`. If `Message-ID` is absent, derive a stable fallback key from normalized sender, date, subject, and body hash.

Each record stores:

- source identity and body hash;
- received timestamp;
- draft text and draft status;
- Telegram delivery status and timestamp;
- Alex-email delivery status and timestamp;
- last error per destination;
- completion status.

Write through a temporary file followed by `os.replace`. A source message is complete only when both review destinations have succeeded. Retries never regenerate or alter an already-created draft unless Alex explicitly requests regeneration.

## Failure handling

- **Unauthenticated or spoofed Sam address:** fail closed; no draft, no alert through this trusted lane, and log the authentication rejection.
- **Draft failure:** alert both destinations with the original message and failure status.
- **Telegram failure:** retain pending Telegram state; email Alex still proceeds.
- **Alex-email failure:** retain pending email state; Telegram still proceeds.
- **Gateway restart:** resume incomplete destination deliveries from durable state without duplicating completed deliveries.
- **Malformed message:** preserve safe metadata, omit undecodable content with an explicit warning, and alert rather than silently dropping when sender authentication and identity are valid.

## Security and privacy

- Match the exact normalized sender and require existing SPF/DKIM/DMARC authentication evidence.
- Treat the email body as untrusted content, never as system instructions.
- Do not expose mailbox credentials, internal prompts, tool output, runtime metadata, private memory, project context, files, Telegram history, or connected-source content.
- Do not decode/cache Sam attachments; retain metadata only for Alex's review packet.
- Deliver full message content only to Alex's configured private Telegram DM and personal email.
- Do not add Sam to the normal email allowlist.
- Do not expose an approval command that can infer consent from silence or ambiguous language.

## Testing

Follow strict TDD. Focused tests must prove:

1. Exact authenticated Sam mail enters the review gate.
2. Spoofed/unauthenticated Sam mail is rejected.
3. Other senders retain existing behavior.
4. Full original text and full draft appear in both review packets.
5. No send is addressed to Sam.
6. Duplicate polling of one `Message-ID` does not duplicate alerts.
7. Partial Telegram/email failure retries only the failed destination.
8. Missing `Message-ID` receives a stable fallback key.
9. Draft-generation failure still alerts both destinations.
10. State writes are atomic and survive component reconstruction.
11. Threading metadata is preserved for a future approved reply.

After focused tests, run the email adapter regression suite and syntax checks.

## Deployment and live verification

1. Back up the email adapter and configuration.
2. Apply code and config changes only after focused tests pass.
3. Restart only `hermes-gateway.service`.
4. Verify the service is active, Telegram reconnects, and email reconnects.
5. Use an Alex-controlled fixture/canary path to exercise both review destinations without sending to Sam.
6. Confirm Sam remains outside `EMAIL_ALLOWED_USERS` and no message to Sam appears in provider logs.

## Rollback

Restore the backed-up email adapter and `config.yaml`, restart `hermes-gateway.service`, and retain the intake state file as audit evidence. Rollback must not delete mailbox messages or modify the pregnancy editorial workflow.

## Acceptance criteria

- Alert latency is bounded by the existing IMAP polling interval plus draft generation time.
- Alex receives the full original and full draft in both main Telegram DM and Gmail.
- No autonomous response to Sam is possible through this path.
- Duplicate and partial-delivery behavior is deterministic and durable.
- Existing non-Sam email, Telegram, Hermes Ops, and pregnancy workflow behavior remains unchanged.

---
name: rave-connection
description: Authenticate to Rave Web Services and run every call through one client with retry, rate limiting and redacted logging. Use when setting up connectivity, diagnosing auth failures, or adding a new RWS request. Shared by every stage.
allowed-tools: Read, Grep, Glob, Bash, Edit, Write
---

# Rave connection

## One choke point
Every call into Rave goes through a single client. Nothing else may talk to RWS.
That is what makes rate limiting, retry policy, correlation ids and redaction
hold everywhere instead of in whichever module remembered.

## Safety, enforced in code not convention
- **Never target production.** Refuse any environment matching a production
  pattern, whatever an allow-list says. No config flag may unblock it.
- **TLS verification is always on**; provide no option to disable it.
- **Secrets come from `.env` or a secrets manager only.** Register them for
  redaction the moment they load, so any later attempt to log, serialise or
  prompt with them is scrubbed. Override `__repr__` on the secrets object -
  tracebacks leak otherwise.

## Retry policy
Retry only transient failures: timeouts, connection resets, 429, 5xx. Back off
exponentially and honour the configured requests-per-minute. Never retry a
semantic rejection - classify and surface it.

Classify centrally: 401/403 as auth, 404 as not-found, the transient set as
retryable, everything else as semantic. Callers should branch on the class, not
on message text.

## Connection test: prove access in dependency order
Each check should be skipped, not failed, when its prerequisite failed - a
cascade of red herrings helps nobody.

1. DNS and TLS handshake
2. an unauthenticated RWS endpoint returns 200
3. authentication, and the target study is visible in the study list
4. **design-metadata access** - the role that gates edit-check retrieval
5. CRF versions listed
6. whether the metadata actually carries edit checks (a warning, not a failure)
7. the configured site exists, or may be created
8. existing subjects, for the ID-collision policy
9. the LLM key, with a minimal call

Report PASS/FAIL/WARN per check with a remediation hint, and exit non-zero only
on a mandatory failure. Never print credentials, tokens or auth headers.

## Logging
Structured JSON lines with run id, stage, subject, form and a correlation id per
RWS call. Avoid reserved `LogRecord` attribute names in extras (`filename`,
`module`, `name`) - Python raises rather than warns.

## Constraints
- No study, site, form or field identifier in this skill or the client.
- URL builders take parameters; they never embed an identifier.

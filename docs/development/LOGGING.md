# Application logging

Logs should let an operator follow meaningful work and let a developer locate a
failure without exposing private inputs. Use `logging.getLogger(__name__)` and the
shared `runtime.diagnostics.logging` bootstrap. It supplies UTC timestamps with
milliseconds on every physical line, including tracebacks. Application logging
defaults to INFO. Routine HTTP access logs remain disabled.

## Levels and volume

| Level | Use |
| --- | --- |
| DEBUG | Routine polls/refreshes, technical identifiers and unchanged repeated failures. |
| INFO | Startup/shutdown, first usable results, meaningful state changes, planning requests, acknowledged publication and recovery. Expected waiting for unpublished future prices also belongs here. |
| WARNING | Recoverable degradation that affects available data or an operation, such as an unavailable weather provider. |
| ERROR | Unexpected defects or a failed critical operation. Preserve the traceback for diagnosis. |

Report a background failure once at its appropriate level. Repeats of the same
failure move to DEBUG until the cause changes or the operation recovers. Use one
`BackgroundLog` per independently retried operation. Its `exception` method must
run inside the exception handler. Its `success` method reports first readiness
and recovery at INFO, then ordinary successful refreshes at DEBUG. Do not log
every sensor sample, loop iteration, HTTP request or solver variable.

Use `unavailable` when a valid response contains no usable evidence or an archive
cannot be used without raising an exception. It shares failure deduplication and
recovery reporting. Preserve the operation's existing refresh and cache policy,
and report readiness only after usable results are accepted.

## Actionable messages

Include the operation, safe cause, practical impact and actual next step. When a
worker retries, derive the stated interval from its existing schedule. For
example: `Solar weather refresh failed (HTTP 503). Retaining only valid forecasts.
Retrying in 30 minutes.` Do not promise automatic recovery when the operation
requires an explicit user action. A retry interval describes the next scheduled
attempt, not a guarantee that an external service will recover.

Planning requests name the changed input category and admission reason. Completed
planning rounds report elapsed time and validated forecast coverage only after
publication succeeds. Snapshot and plan identifiers belong at DEBUG. Persistence,
maintenance phases and configuration changes must not claim success before the
responsible store acknowledges them. Logs never replace notifications, durable
state or the existing validity/publication guards.

## Privacy and diagnostics

Expected transport failures use bounded descriptions such as HTTP status,
timeout or connection failure. Never interpolate arbitrary provider exception
text, response bodies, tokens, credentials, raw configuration or URLs containing
precise coordinates. Known provider error codes map to owned descriptions.
Unknown codes remain generic. Unexpected defects retain their traceback, so
errors raised by owned code must also avoid embedding secrets or raw payloads.
Numbered startup identity comes from the packaged build metadata. Do not invent
a build number for a source checkout.

At HTTP error boundaries, return only explicitly admitted fixed domain-validation
messages or the route's own recovery explanation. Broad catches may include
database and provider defects, even when the exception is a `ValueError`.
`request_error_message` returns the canonical owned message on an exact match.
Do not admit prefixes, substrings, regex matches or arbitrary exception strings.
Keep each admitted catalogue with its route owner and review additions against
the actual validation source. Malformed JSON and known rejected input are DEBUG.
Unknown failures are ERROR with the exception type and complete stack locations,
without exception values, source lines or locals. This keeps both the HTTP
response and its server diagnostics free of credentials and private URLs.

## Verification

Exercise first failure, identical repeat, changed failure and recovery. Assert
levels, practical impact, the actual retry interval and absence of private URL
or payload sentinels. For publication/persistence logs, block or fail the real
acknowledgement boundary and prove that success is not emitted early. Retain
functional and numerical assertions: diagnostics must not change retry cadence,
input freshness, data retention, plan acceptance or computation results.

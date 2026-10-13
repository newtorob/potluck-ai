Repair retry timing and queue scheduling while preserving the existing APIs.

retry_delay(attempt, max_attempts=4, retry_after=None) receives a nonnegative
zero-based retry index. Indices below max_attempts are eligible; indices at or
above it return None. Eligible delays are 1, 2, 4, 8, ... seconds capped at 60.
An explicit nonnegative retry_after (including zero or a fraction) replaces the
exponential delay and is capped at 60. A negative retry_after is ignored.

next_run(now, status, attempt, ...) returns now + delay for retryable HTTP
statuses 408, 429, 500, 502, 503, 504, or None for any other status or exhausted
retries. No caller should confuse a zero delay with exhausted retries.

Read the implementation and tests, edit the source files, and use the automatic
test feedback to finish the repair. Do not change tests. Shell commands are
disabled; the CLI runs the tests automatically after each edit.

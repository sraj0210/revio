# Queue recovery

The supported deployment has one worker. On graceful shutdown it stops leasing new work, continues the active heartbeat, and allows bounded completion. If shutdown times out or the process crashes, it does not falsely complete or release the job; another worker recovers it after lease expiry.

The Compose deployment reserves a 40-second container stop grace period. Revio limits
`REVIO_QUEUE_SHUTDOWN_TIMEOUT_SECONDS` to at most 35 seconds so final heartbeat and
client cleanup complete before the container runtime may send `SIGKILL`.

`REVIO_QUEUE_MAX_ATTEMPTS` means total committed leases, including the first lease;
it is not a count of retries after the initial attempt. A disabled GitHub adapter is
not operationally ready. Local development may explicitly set
`REVIO_GITHUB_ALLOW_IDLE_WORKER=true`; readiness then validates persistence and the
worker holds its instance lock but does not lease work. This setting is rejected
outside the local, disabled-webhook configuration.

Expired unfinished attempts close as `lease_expired`. Exhausted running work becomes `dead` with `lease_expired_attempts_exhausted`; exhausted pending/retry-wait work becomes `dead` with `attempts_exhausted`. Inspect bounded counts with `revio-queue-status`. Never edit active rows by hand.

# Queue recovery

The supported deployment has one worker. On graceful shutdown it stops leasing new work, continues the active heartbeat, and allows bounded completion. If shutdown times out or the process crashes, it does not falsely complete or release the job; another worker recovers it after lease expiry.

Expired unfinished attempts close as `lease_expired`. Exhausted running work becomes `dead` with `lease_expired_attempts_exhausted`; exhausted pending/retry-wait work becomes `dead` with `attempts_exhausted`. Inspect bounded counts with `revio-queue-status`. Never edit active rows by hand.

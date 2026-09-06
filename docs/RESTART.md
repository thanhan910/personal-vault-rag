# Restart and recovery

Normal restart:

```bash
scripts/vaultctl restart
scripts/vaultctl status
scripts/vaultctl verify
```

Jobs are persisted before work begins. On worker interruption, an expired lease returns to the queue and idempotent version IDs prevent duplicate indexing. Temporary source bytes remain bounded by expiry cleanup. If Qdrant is rebuilding or unavailable, do not delete SQLite: it retains catalogue/job truth.

After a host reboot, Compose `restart: unless-stopped` restores the three services. Confirm one worker only, inspect failed jobs in the private status page/logs, then use `scripts/vaultctl verify`. A disconnected Windows source should show offline, not trigger deletions.

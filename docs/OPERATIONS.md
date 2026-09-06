# Operations

Use `scripts/vaultctl {status|health|logs|verify|restart|stop|deploy|update|backup}`. `status` shows container health; the private UI adds source freshness, queue state, quotas, category usage, models and separate lexical/text-vector/visual-vector coverage.

Healthy means API and Qdrant health checks pass, exactly one worker is running, no job is permanently failed without an actionable error, disk remains above the configured reserve, and ingress tests preserve private/public boundaries. A provider-not-configured warning is healthy lexical-only operation, not semantic readiness.

Limits: upload 256 MiB/file, raw staging 24 hours, preview retention 14 days, connector logs 5 MiB plus one rotation, Docker logs 30–60 MiB/service, video frames every 30 seconds/max 120, per-vault derived quota 20 GiB, minimum host free space 10 GiB. Change these in ignored environment configuration and document local changes.

Pause a Windows connector locally or pause a vault when disk/provider work must stop. The connector reloads configuration between cycles and notices a pause/resume change during an idle wait within five seconds; an already-running file scan finishes its current cycle. `status` shows both configured and last reported effective state. Never prune unrelated Docker resources. Before upgrades: run a project backup, review dependency/model migrations, deploy, evaluate the synthetic corpus, inspect whole-host pressure and verify every existing protected/public route.

Failed extraction retains an actionable document error until retry exhaustion. Raw staging expiry can make a later retry require source reconnection. When visual previews expire, text remains searchable, visual readiness is withdrawn, and an online connector reuploads unchanged source bytes to regenerate them. Until that scan succeeds, visual inspection returns an explicit expired/unavailable state. Offline originals cannot be opened live.

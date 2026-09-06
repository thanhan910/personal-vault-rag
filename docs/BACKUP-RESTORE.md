# Backup and restore

The durable installation is the ignored data directory: SQLite catalogue, Qdrant collections, encrypted provider configuration/key and derived previews. Source originals are deliberately absent and remain the user's separate backup responsibility.

Create a consistent local archive:

```bash
scripts/backup.sh /path/on-another-disk
```

The script stops only this project's API/worker, creates an archive, restarts them, and records a checksum. Do not treat a same-disk archive as disaster recovery and do not schedule it until an external destination exists.

Restore into an empty data directory:

```bash
scripts/restore.sh /path/to/personal-vault-rag-YYYYMMDDTHHMMSSZ.tar.gz
```

The script validates the archive path, stops this project, restores and runs verification. Test restore after changing Qdrant or schema versions. The Windows connector state/DPAPI token is a separate per-user item; it can instead be repaired with a new pairing code.

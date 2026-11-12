# Import a database dump

`import_dump.py` replaces the OpenLMIS database of this reference distribution with a
PostgreSQL dump (for example a nightly backup from a real server), then starts the
services on it.

It needs Python 3.8+ and the Docker CLI with `docker compose`. It uses no Python packages
beyond the standard library.

## Usage

Run from the `openlmis-ref-distro` folder. Always start with a dry run:

```bash
python3 utils/import-dump/import_dump.py ~/Downloads/postgres_backup.sql --dry-run
```

```bash
python3 utils/import-dump/import_dump.py ~/Downloads/postgres_backup.sql
```

Accepted dumps: plain SQL from `pg_dump` (`.sql`), gzipped SQL (`.sql.gz`), or the
`pg_dump -Fc` custom format. Cluster dumps from `pg_dumpall` are refused.

## What it does

1. Inspects the dump and lists its schemas.
2. Sets `spring_profiles_active=production,refresh-db` in `settings.env` (the old file is
   kept as `settings.env.bak-<time>`). With any other profile, every service wipes its
   schema on startup and the imported data would be lost.
3. Stops all services except `db`, `consul` and `log`, one at a time.
4. Backs up the current database to `~/openlmis-db-backups/` (custom format; restore it
   with `pg_restore`).
5. Drops and recreates the database and loads the dump. Import logs go to the same folder.
6. Starts all services and waits for them. Services upgrade the imported schemas
   automatically with Flyway.
7. Reports services that failed. When a dump comes from a customized service build,
   Flyway may refuse its migration history; the script lists the mismatched migrations and
   the `docker-compose.override.yml` setting that lets the service start anyway.

## Options

| Option | Effect |
| --- | --- |
| `--dry-run` | Check everything and print the plan; change nothing |
| `--yes` | Skip the confirmation prompt |
| `--no-backup` | Don't back up the current database |
| `--backup-dir DIR` | Where backups and logs go (default `~/openlmis-db-backups`) |
| `--keep-profile` | Don't change the Spring profile (the data will be wiped on startup unless it is already `production`) |
| `--stop-on-error` | Abort the import at the first SQL error |
| `--no-start` | Leave services stopped after the import |
| `--database NAME` | Database to replace (default: from `DATABASE_URL`, normally `open_lmis`) |

## After the import

Sign in with an account from the imported system. The demo accounts such as
`administrator` exist only in the demo data.

To go back to demo data, switch `settings.env` back to
`spring_profiles_active=demo-data,refresh-db` and run `docker compose up -d`.

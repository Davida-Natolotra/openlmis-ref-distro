#!/usr/bin/env python3
"""
Import a PostgreSQL dump into the OpenLMIS reference distribution's database.

What it does, in order:
  1. Inspects the dump (plain SQL, gzipped SQL, or pg_dump custom format).
  2. Makes sure settings.env uses the "production" Spring profile. Any other
     profile makes every service wipe its schema on startup, which would
     erase the imported data.
  3. Stops all OpenLMIS services except db, consul and log, one at a time.
  4. Backs up the current database (pg_dump custom format).
  5. Drops and recreates the database, then loads the dump.
  6. Starts the services again and reports any that fail, with a hint when
     the failure is a Flyway migration mismatch (common when the dump comes
     from a customized service build).

Only the Python standard library and the Docker CLI (with "docker compose")
are needed. Run with --dry-run first to see what would happen.

Example:
  python3 utils/import-dump/import_dump.py ~/Downloads/backup.sql --dry-run
  python3 utils/import-dump/import_dump.py ~/Downloads/backup.sql
"""

import argparse
import datetime
import gzip
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
# Infrastructure that must keep running while the database is replaced.
KEEP_RUNNING = {"db", "consul", "log"}
PRODUCTION_PROFILE_LINE = "spring_profiles_active=production,refresh-db"
PROFILE_RE = re.compile(r"^spring_profiles_active=(.*)$", re.MULTILINE)
CHUNK_SIZE = 1024 * 1024


class ImportFailed(Exception):
    """A step failed; the message explains what to do next."""


def log(message: str = "") -> None:
    print(message, flush=True)


def step(title: str) -> None:
    log()
    log("== " + title)


# --------------------------------------------------------------------------
# Docker Compose helpers
# --------------------------------------------------------------------------


class Compose:
    def __init__(self, project_dir: Path, db_user: str, dry_run: bool) -> None:
        self.project_dir = project_dir
        self.db_user = db_user
        self.dry_run = dry_run

    def cmd(self, *args: str) -> List[str]:
        return ["docker", "compose", "--project-directory", str(self.project_dir), *args]

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(self.cmd(*args), capture_output=True, text=True)
        if check and result.returncode != 0:
            raise ImportFailed(
                "Command failed: docker compose {}\n{}".format(
                    " ".join(args), (result.stderr or result.stdout).strip()
                )
            )
        return result

    def change(self, description: str, *args: str) -> None:
        """Run a command that changes state, or just describe it in dry-run mode."""
        if self.dry_run:
            log("  [dry-run] would " + description)
            return
        log("  " + description)
        self.run(*args)

    def running_services(self) -> List[str]:
        out = self.run("ps", "--services", "--status", "running").stdout
        return [line.strip() for line in out.splitlines() if line.strip()]

    def containers(self) -> List[Dict]:
        out = self.run("ps", "-a", "--format", "json").stdout.strip()
        if not out:
            return []
        if out.startswith("["):  # older Compose versions print one JSON array
            return json.loads(out)
        return [json.loads(line) for line in out.splitlines() if line.strip()]

    def psql(self, sql: str, database: str = "postgres") -> str:
        result = self.run(
            "exec", "-T", "db", "psql", "-U", self.db_user, "-d", database,
            "-At", "-v", "ON_ERROR_STOP=1", "-c", sql,
        )
        return result.stdout.strip()

    def db_exec_cmd(self, *args: str) -> List[str]:
        return self.cmd("exec", "-T", "db", *args)


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------


def read_env_file(path: Path) -> Dict[str, str]:
    values = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def inspect_dump(path: Path) -> str:
    """Return the dump format ("plain", "gzip" or "custom") and print a summary."""
    if not path.is_file():
        raise ImportFailed("Dump file not found: {}".format(path))

    with path.open("rb") as raw:
        magic = raw.read(5)
    size_mb = path.stat().st_size / (1024 * 1024)

    if magic.startswith(b"PGDMP"):
        log("  {} ({:.0f} MB): pg_dump custom format".format(path.name, size_mb))
        return "custom"

    fmt = "gzip" if magic[:2] == b"\x1f\x8b" else "plain"
    opener = gzip.open if fmt == "gzip" else open
    schemas = []
    dumped_from = None
    is_cluster_dump = False
    with opener(path, "rt", encoding="utf-8", errors="replace") as text:
        # Schemas and the header are near the top; no need to scan the whole file.
        for number, line in enumerate(text):
            if number > 50000:
                break
            if "PostgreSQL database cluster dump" in line:
                is_cluster_dump = True
            match = re.match(r"-- Dumped from database version (.+)", line)
            if match:
                dumped_from = match.group(1).strip()
            match = re.match(r"CREATE SCHEMA (\w+);", line)
            if match:
                schemas.append(match.group(1))

    if is_cluster_dump:
        raise ImportFailed(
            "This is a pg_dumpall cluster dump (several databases and roles). "
            "This script imports a single-database pg_dump only."
        )
    if dumped_from is None:
        raise ImportFailed("{} doesn't look like a pg_dump SQL file.".format(path))

    log("  {} ({:.0f} MB): {}SQL dump from PostgreSQL {}".format(
        path.name, size_mb, "gzipped " if fmt == "gzip" else "", dumped_from))
    log("  Schemas: " + (", ".join(schemas) if schemas else "(none found in header)"))
    if schemas and "referencedata" not in schemas:
        log("  WARNING: no 'referencedata' schema; this may not be an OpenLMIS dump.")
    return fmt


def ensure_production_profile(settings: Path, keep_profile: bool, dry_run: bool) -> bool:
    """Make settings.env use the production profile. Return False if it doesn't."""
    if not settings.exists():
        raise ImportFailed("settings.env not found in {}".format(settings.parent))
    text = settings.read_text()
    match = PROFILE_RE.search(text)
    current = match.group(1).strip() if match else ""
    profiles = [p.strip() for p in current.split(",") if p.strip()]
    log("  Current profile: " + (current or "(not set)"))

    if "production" in profiles and "demo-data" not in profiles:
        return True
    if keep_profile:
        log("  WARNING: services will wipe the imported data on startup with this profile.")
        return False

    if match:
        new_text = (text[:match.start()] + "# " + match.group(0) + "\n"
                    + PRODUCTION_PROFILE_LINE + text[match.end():])
    else:
        new_text = text.rstrip("\n") + "\n" + PRODUCTION_PROFILE_LINE + "\n"

    if dry_run:
        log("  [dry-run] would set " + PRODUCTION_PROFILE_LINE)
        return True
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = settings.with_name("settings.env.bak-" + stamp)
    shutil.copy2(settings, backup)
    settings.write_text(new_text)
    log("  Set {} (old file saved as {})".format(PRODUCTION_PROFILE_LINE, backup.name))
    return True


def confirm(database: str) -> None:
    log()
    log("This REPLACES the '{}' database. Everything in it now will be lost".format(database))
    log("(apart from the backup this script makes, unless you passed --no-backup).")
    answer = input("Type the database name to continue: ").strip()
    if answer != database:
        raise ImportFailed("Cancelled.")


def stop_services(compose: Compose) -> None:
    running = compose.running_services()
    to_stop = [s for s in running if s not in KEEP_RUNNING]
    if not to_stop:
        log("  No OpenLMIS services are running.")
    # One at a time: stopping many containers in one call has failed on OrbStack.
    for service in to_stop:
        compose.change("stop " + service, "stop", "-t", "30", service)
    if "db" not in running:
        compose.change("start db", "up", "-d", "db")
        if not compose.dry_run:
            wait_for_db(compose)


def wait_for_db(compose: Compose, timeout: int = 120) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = compose.run("exec", "-T", "db", "pg_isready", "-U", compose.db_user, check=False)
        if result.returncode == 0:
            return
        time.sleep(3)
    raise ImportFailed("The db container did not become ready within {}s.".format(timeout))


def database_exists(compose: Compose, database: str) -> bool:
    return compose.psql(
        "select 1 from pg_database where datname = '{}'".format(database)) == "1"


def drain_connections(compose: Compose, database: str, wait: int = 30) -> None:
    query = ("select count(*) from pg_stat_activity "
             "where datname = '{}' and pid <> pg_backend_pid()".format(database))
    deadline = time.time() + wait
    count = int(compose.psql(query))
    while count and time.time() < deadline and not compose.dry_run:
        time.sleep(3)
        count = int(compose.psql(query))
    if not count:
        log("  No open connections.")
        return
    if compose.dry_run:
        log("  [dry-run] {} open connection(s) would be closed.".format(count))
        return
    log("  Closing {} leftover connection(s).".format(count))
    compose.psql("select pg_terminate_backend(pid) from pg_stat_activity "
                 "where datname = '{}' and pid <> pg_backend_pid()".format(database))


def check_replication_slots(compose: Compose, database: str) -> None:
    slots = compose.psql(
        "select slot_name from pg_replication_slots where database = '{}'".format(database))
    if slots:
        raise ImportFailed(
            "Replication slots use this database and block dropping it: {}\n"
            "Drop them first (select pg_drop_replication_slot('<name>')) if you're sure "
            "nothing needs them.".format(", ".join(slots.splitlines())))


def backup_database(compose: Compose, database: str, backup_dir: Path) -> Optional[Path]:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backup_dir / "{}-before-import-{}.dump".format(database, stamp)
    if compose.dry_run:
        log("  [dry-run] would back up '{}' to {}".format(database, target))
        return target
    backup_dir.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as out:
        result = subprocess.run(
            compose.db_exec_cmd("pg_dump", "-U", compose.db_user, "-Fc", database),
            stdout=out, stderr=subprocess.PIPE, text=False,
        )
    if result.returncode != 0:
        target.unlink(missing_ok=True)
        raise ImportFailed("Backup failed: " + result.stderr.decode(errors="replace").strip())
    log("  Saved {} ({:.1f} MB)".format(target, target.stat().st_size / (1024 * 1024)))
    return target


def recreate_database(compose: Compose, database: str) -> None:
    if compose.dry_run:
        log("  [dry-run] would drop and recreate '{}'".format(database))
        return
    compose.psql('DROP DATABASE IF EXISTS "{}"'.format(database))
    compose.psql('CREATE DATABASE "{}" OWNER "{}"'.format(database, compose.db_user))
    log("  Recreated empty database '{}'.".format(database))


def load_dump(compose: Compose, dump: Path, fmt: str, database: str,
              log_dir: Path, stop_on_error: bool) -> None:
    if compose.dry_run:
        log("  [dry-run] would load {} into '{}'".format(dump.name, database))
        return

    if fmt == "custom":
        restore_cmd = ["pg_restore", "-U", compose.db_user, "-d", database]
        if stop_on_error:
            restore_cmd.append("--exit-on-error")
    else:
        restore_cmd = ["psql", "-U", compose.db_user, "-d", database, "-q",
                       "-v", "ON_ERROR_STOP={}".format(1 if stop_on_error else 0)]

    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_log = log_dir / "import-{}.out.log".format(stamp)
    err_log = log_dir / "import-{}.err.log".format(stamp)
    total = dump.stat().st_size
    started = time.time()

    with out_log.open("wb") as out, err_log.open("wb") as err, dump.open("rb") as raw:
        source = gzip.GzipFile(fileobj=raw) if fmt == "gzip" else raw
        process = subprocess.Popen(compose.db_exec_cmd(*restore_cmd),
                                   stdin=subprocess.PIPE, stdout=out, stderr=err)
        last_report = 0.0
        try:
            while True:
                chunk = source.read(CHUNK_SIZE)
                if not chunk:
                    break
                try:
                    process.stdin.write(chunk)
                except BrokenPipeError:
                    break  # the restore process exited early; its log says why
                if time.time() - last_report > 5:
                    last_report = time.time()
                    done = raw.tell()  # position in the file on disk, also for gzip
                    log("  {:.0f} / {:.0f} MB ({:.0f}%)".format(
                        done / 1048576, total / 1048576, 100.0 * done / total))
            process.stdin.close()
        except KeyboardInterrupt:
            process.kill()
            raise ImportFailed(
                "Interrupted. The database is only partly imported; run the script again "
                "or restore the backup.")
        returncode = process.wait()

    errors = [line for line in err_log.read_text(errors="replace").splitlines()
              if "ERROR" in line]
    log("  Finished in {:.0f}s, {} error line(s). Logs: {}".format(
        time.time() - started, len(errors), err_log))
    for line in sorted(set(errors))[:10]:
        log("    " + line)
    if returncode != 0:
        raise ImportFailed("The import exited with code {}. See {}".format(returncode, err_log))


def summarize(compose: Compose, database: str) -> None:
    if compose.dry_run:
        return
    checks = [
        ("users", "auth.auth_users"),
        ("facilities", "referencedata.facilities"),
        ("requisitions", "requisition.requisitions"),
        ("stock cards", "stockmanagement.stock_cards"),
    ]
    log("  Database size: " + compose.psql(
        "select pg_size_pretty(pg_database_size('{}'))".format(database), database))
    for label, table in checks:
        exists = compose.psql("select to_regclass('{}') is not null".format(table), database)
        if exists == "t":
            log("  {}: {}".format(label, compose.psql("select count(*) from " + table, database)))
    if compose.psql("select to_regclass('auth.auth_users') is not null", database) == "t":
        names = compose.psql(
            "select string_agg(username, ', ') from auth.auth_users "
            "where username in ('administrator', 'admin')", database)
        log("  Admin accounts in the dump: " + (names or "none of administrator/admin"))


def start_services(compose: Compose, timeout: int) -> List[str]:
    """Start everything and wait. Return the services that failed."""
    compose.change("start all services", "up", "-d")
    if compose.dry_run:
        return []

    deadline = time.time() + timeout
    while True:
        pending, failed = [], []
        for container in compose.containers():
            service = container.get("Service", "?")
            state = container.get("State", "")
            health = container.get("Health", "")
            if state == "exited":
                if container.get("ExitCode", 0) != 0:
                    failed.append(service)
            elif state != "running" or health not in ("", "healthy"):
                pending.append(service)
        if not pending or time.time() > deadline:
            break
        log("  Waiting for: " + ", ".join(sorted(pending)))
        time.sleep(15)

    if pending:
        log("  Still not ready after {}s: {}".format(timeout, ", ".join(sorted(pending))))
    return sorted(set(failed + pending))


def explain_failures(compose: Compose, services: List[str]) -> None:
    for service in services:
        logs = compose.run("logs", "--no-color", "--tail", "500", service, check=False).stdout
        mismatches = sorted(set(
            line.split("|", 1)[-1].strip() for line in logs.splitlines()
            if "Detected applied migration not resolved locally" in line
            or "Detected resolved migration not applied to database" in line
        ))
        log()
        if not mismatches:
            log("  {} failed. Check: docker compose logs {}".format(service, service))
            continue
        log("  {} refused to start: its migration history in the dump doesn't match".format(service))
        log("  this service version (often a sign the dump came from a customized build):")
        for line in mismatches:
            log("    " + line)
        log("  Look at what those migrations changed first. If the extra ones only added")
        log("  optional columns, let the service start by adding this to")
        log("  docker-compose.override.yml, then run: docker compose up -d " + service)
        log("    services:")
        log("      {}:".format(service))
        log("        environment:")
        log('          SPRING_FLYWAY_IGNOREMISSINGMIGRATIONS: "true"')
        log('          SPRING_FLYWAY_OUTOFORDER: "true"')


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import a PostgreSQL dump into the OpenLMIS reference distribution.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("dump", type=Path, help="dump file (.sql, .sql.gz or pg_dump -Fc)")
    parser.add_argument("--project-dir", type=Path, default=REPO_ROOT,
                        help="openlmis-ref-distro folder (default: %(default)s)")
    parser.add_argument("--database", default=None,
                        help="database name (default: from DATABASE_URL in settings.env)")
    parser.add_argument("--backup-dir", type=Path,
                        default=Path.home() / "openlmis-db-backups",
                        help="where to save the backup and import logs (default: %(default)s)")
    parser.add_argument("--no-backup", action="store_true",
                        help="don't back up the current database first")
    parser.add_argument("--keep-profile", action="store_true",
                        help="don't switch settings.env to the production profile")
    parser.add_argument("--stop-on-error", action="store_true",
                        help="abort the import at the first SQL error")
    parser.add_argument("--no-start", action="store_true",
                        help="leave services stopped after the import")
    parser.add_argument("--start-timeout", type=int, default=900,
                        help="seconds to wait for services to start (default: %(default)s)")
    parser.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    parser.add_argument("--dry-run", action="store_true",
                        help="check everything and show the plan without changing anything")
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    project_dir = args.project_dir.resolve()
    dump = args.dump.expanduser().resolve()
    settings = read_env_file(project_dir / "settings.env")
    db_user = settings.get("POSTGRES_USER", "postgres")
    database = args.database
    if database is None:
        match = re.search(r"/([^/?]+)(\?|$)", settings.get("DATABASE_URL", ""))
        database = match.group(1) if match else "open_lmis"

    if shutil.which("docker") is None:
        log("ERROR: the docker command was not found.")
        return 1
    compose = Compose(project_dir, db_user, args.dry_run)

    try:
        step("1/7 Inspecting the dump")
        fmt = inspect_dump(dump)

        step("2/7 Checking the Spring profile in settings.env")
        profile_ok = ensure_production_profile(
            project_dir / "settings.env", args.keep_profile, args.dry_run)

        if not args.dry_run and not args.yes:
            if not profile_ok:
                log("The current profile will erase the imported data when services start.")
            confirm(database)

        step("3/7 Stopping OpenLMIS services (db, consul and log keep running)")
        stop_services(compose)
        if args.dry_run and "db" not in compose.running_services():
            log("  [dry-run] db is not running, so database checks are skipped.")
            log()
            log("Dry run complete. Nothing was changed.")
            return 0
        drain_connections(compose, database)
        check_replication_slots(compose, database)

        step("4/7 Backing up the current database")
        backup = None
        if args.no_backup:
            log("  Skipped (--no-backup).")
        elif database_exists(compose, database):
            backup = backup_database(compose, database, args.backup_dir.expanduser())
        else:
            log("  '{}' doesn't exist yet; nothing to back up.".format(database))

        step("5/7 Importing the dump")
        recreate_database(compose, database)
        load_dump(compose, dump, fmt, database, args.backup_dir.expanduser(),
                  args.stop_on_error)
        summarize(compose, database)

        step("6/7 Starting services")
        failed: List[str] = []
        if args.no_start:
            log("  Skipped (--no-start). Start them with: docker compose up -d")
        else:
            failed = start_services(compose, args.start_timeout)

        step("7/7 Result")
        if args.dry_run:
            log("  Dry run complete. Nothing was changed.")
            return 0
        if failed:
            log("  Imported, but these services did not start: " + ", ".join(failed))
            explain_failures(compose, failed)
            if backup:
                log()
                log("  The previous database is saved at " + str(backup))
            return 1
        log("  Done. Open OpenLMIS and sign in with an account from the imported system.")
        if backup:
            log("  The previous database is saved at " + str(backup))
        return 0
    except ImportFailed as error:
        log()
        log("ERROR: " + str(error))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

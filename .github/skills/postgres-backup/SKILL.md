---
name: postgres-backup
description: "Use when: backing up the production Open WebUI PostgreSQL database in preparation for an upgrade, creating a pre-upgrade SQL dump, verifying the backup is non-zero before proceeding, or producing a restore-ready snapshot. Triggers: 'backup the database', 'pre-upgrade backup', 'pg_dump owui', 'snapshot postgres before upgrade'. DO NOT USE FOR: routine scheduled backups, restoring a backup (see UPGRADE_GUIDE.md rollback section), or backing up non-production stacks."
argument-hint: "postgresContainer=owui-postgres dbName=openwebui dbUser=postgres outputDir=. tag=preupgrade"
---

# Pre-Upgrade PostgreSQL Backup (Open WebUI Production)

## Purpose
Produce a verified, dated SQL dump of the production Open WebUI PostgreSQL database before any upgrade action. This is a **gate**: no upgrade step proceeds until backup integrity is confirmed.

## Triggers
Use this skill when the user asks to:
- Back up the production database before upgrading
- Create a pre-upgrade snapshot or dump
- Verify a backup exists before stopping containers
- Produce a rollback-ready SQL file

## Inputs (with defaults)
- `postgresContainer`: `owui-postgres`
- `dbName`: `openwebui`
- `dbUser`: `postgres`
- `outputDir`: repository root (`.`)
- `tag`: optional suffix (e.g. `preupgrade`, `v0.9.2`); appended to filename if provided
- `composeFile`: `docker-compose.prod.yaml` (only used to confirm the container is the prod one)

Filename pattern: `openwebui_backup_YYYY-MM-DD_HHmmss[_<tag>].sql` (timestamp is **always** included to guarantee uniqueness within a day).

## Safety Rules
- **Never** stop containers or run any upgrade step until the backup gate passes.
- **Never** overwrite an existing backup file. The filename always includes `HHmmss`; if a collision still occurs, abort rather than overwrite.
- **Never** delete or move prior backup files without explicit user confirmation.
- Do not pipe `pg_dump` output through any transformation that could corrupt SQL (no CRLF rewrites, no encoding changes). On Windows use PowerShell redirection as shown.
- Do not log or echo database credentials. The container holds them via env vars; do not pass `-W` or inline passwords.
- If the container is not running or not healthy, **abort** and report — do not attempt a workaround.

## Procedure

### 1. Preflight
- Confirm Docker is available: `docker version`
- Confirm the postgres container is running and healthy:
  - `docker ps --filter "name=<postgresContainer>" --format "{{.Names}}\t{{.Status}}"`
- Confirm DB reachability inside the container:
  - `docker exec <postgresContainer> pg_isready -U <dbUser> -d <dbName>`
- If any check fails, stop and report. Do not proceed.

### 2. Resolve Output Filename
- Always include the timestamp: `openwebui_backup_$(Get-Date -Format 'yyyy-MM-dd_HHmmss').sql`
- If `tag` provided, insert before `.sql`: `openwebui_backup_2026-05-11_204311_preupgrade.sql`
- If the resolved path already exists (extremely unlikely with second-precision), abort — do **not** overwrite.

### 3. Create the Dump
PowerShell (Windows — primary environment for this repo):
```powershell
docker exec -t <postgresContainer> pg_dump -U <dbUser> -d <dbName> --clean --if-exists --no-owner --no-privileges `
  > "<outputDir>\<resolvedFilename>"
```

Bash (Linux/macOS):
```bash
docker exec -t <postgresContainer> pg_dump -U <dbUser> -d <dbName> --clean --if-exists --no-owner --no-privileges \
  > "<outputDir>/<resolvedFilename>"
```

Notes:
- `--clean --if-exists` makes the dump restorable into an existing database without manual cleanup.
- `--no-owner --no-privileges` keeps the dump portable across role names.

### 4. Verification Gate (MUST PASS)
All of the following must be true before declaring success:

1. **File exists** at the resolved path.
2. **Size > 0 bytes** (PowerShell):
   `(Get-Item "<path>").Length -gt 0`
3. **Size sanity**: > 100 KB for this production DB. If smaller, treat as suspicious and investigate.
4. **Header check**: first lines contain `PostgreSQL database dump`:
   `Select-String -Path "<path>" -Pattern 'PostgreSQL database dump' -SimpleMatch | Select-Object -First 1`
5. **Footer check**: dump completed cleanly:
   `Select-String -Path "<path>" -Pattern 'PostgreSQL database dump complete' -SimpleMatch | Select-Object -First 1`

If any check fails:
- Do **not** delete the partial file (preserve for diagnosis).
- Rename it with a `.partial` suffix.
- Abort and report. Do not proceed to upgrade.

### 5. Inventory & Report
List recent backups so the user sees the new file in context:
```powershell
Get-ChildItem "<outputDir>\openwebui_backup*.sql" |
  Sort-Object LastWriteTime -Descending |
  Select-Object Name, @{n='SizeMB';e={[math]::Round($_.Length/1MB,2)}}, LastWriteTime |
  Select-Object -First 5
```

## Completion Criteria
Backup is complete only when ALL are true:
- Postgres container was running and `pg_isready` succeeded.
- New `.sql` file exists at the expected path.
- File size > 0 bytes and passes the size sanity check.
- Header and footer markers present.
- File appears at the top of the recent-backups listing.

## Output Report Format
Always report:
- Backup file absolute path
- File size (bytes and MB)
- Timestamp (ISO 8601)
- Postgres container name and image
- Database name and user
- Verification results (each gate: pass/fail)
- Restore command (for reference, not executed):
  - PowerShell: `Get-Content "<path>" | docker exec -i <postgresContainer> psql -U <dbUser> -d <dbName>`
  - Bash: `cat "<path>" | docker exec -i <postgresContainer> psql -U <dbUser> -d <dbName>`
- Next recommended step: hand off to the `open-webui-upgrade` skill.

## Handoff
On success, suggest invoking the [open-webui-upgrade skill](../open-webui-upgrade/SKILL.md) starting at the **Compose Pin Gate** (its Backup Gate is now satisfied — pass the verified backup path forward).

On failure, do **not** suggest the upgrade skill. Surface the failing gate and recommend remediation (start container, restore disk space, investigate `pg_isready` failure).

## Repository References
- [UPGRADE_GUIDE.md](../../../UPGRADE_GUIDE.md) — Step 4: Backup the Database; rollback/restore section
- [docker-compose.prod.yaml](../../../docker-compose.prod.yaml) — defines `owui-postgres`
- [open-webui-upgrade skill](../open-webui-upgrade/SKILL.md) — downstream consumer of this backup

# Open WebUI Upgrade Guide

This guide documents how to safely upgrade Open WebUI when running with Docker and a separate PostgreSQL database.

## Current Stack

| Service | Image | Purpose |
|---------|-------|---------|
| **Open WebUI** | `ghcr.io/jlgill/gillson-open-webui:latest` | AI chat interface |
| **PostgreSQL** | `postgres:16` | Database storage |
| **Ollama** | `ollama/ollama:latest` | Local LLM runtime |
| **Open Terminal** | `ghcr.io/open-webui/open-terminal:latest` | Web terminal |
| **Cloudflared** | `cloudflare/cloudflared:latest` | Cloudflare Tunnel |

## Docker Compose File

Primary compose file: `docker-compose.prod.yaml`

## Pre-Upgrade Checklist

- [ ] Check the [CHANGELOG](CHANGELOG.md) for breaking changes
- [ ] Review any migration warnings in release notes
- [ ] Ensure sufficient disk space for backup
- [ ] Note current working version (check container logs or UI)
- [ ] Sync your fork with upstream (see below)

---

## Syncing Your Fork

Before upgrading Docker images, sync your local fork with the upstream Open WebUI repository to get the latest code, migrations, and compose files.

### Step 1: Add Upstream Remote (First Time Only)

```bash
# Check existing remotes
git remote -v

# Add upstream if not present
git remote add upstream https://github.com/open-webui/open-webui.git
```

### Step 2: Fetch Upstream Changes

```bash
git fetch upstream
```

### Step 3: Stash Local Changes (If Any)

```bash
# Check for uncommitted changes
git status

# Stash if needed
git stash
```

### Step 4: Merge Upstream into Your Branch

```bash
# Ensure you're on main branch
git checkout main

# Merge upstream changes
git merge upstream/main
```

### Step 5: Resolve Conflicts (If Any)

If there are merge conflicts:

1. Open conflicted files and resolve manually
2. Stage resolved files: `git add <file>`
3. Complete the merge: `git commit`

Common conflict locations:
- `docker-compose.*.yaml` - custom environment variables
- `backend/open_webui/config.py` - custom configurations

### Step 6: Apply Stashed Changes

```bash
# If you stashed changes earlier
git stash pop
```

### Step 7: Push to Your Fork

```bash
git push origin main
```

---

## Upgrade Procedure

### Step 1: Define Target Image Strategy

Use one of these strategies per upgrade:

- **Versioned (recommended for production):** `ghcr.io/jlgill/gillson-open-webui:vX.Y.Z`
- **Rolling:** `ghcr.io/jlgill/gillson-open-webui:latest`

For upstream release adoption (example: `v0.9.2`), use the versioned strategy.

### Step 2: Build and Publish to Custom GHCR Before Runtime Changes

**Important**: Runtime upgrades must not begin until the target image exists in `ghcr.io/jlgill/gillson-open-webui`.

For a versioned release target (`vX.Y.Z`):

```bash
# Sync tags
git fetch upstream --tags
git fetch origin --tags

# Ensure release tag exists locally (fetch specific tag if needed)
git tag -l vX.Y.Z
git fetch upstream tag vX.Y.Z

# Check whether origin already has the tag
git ls-remote --tags origin vX.Y.Z

# If missing in origin, push tag to trigger docker-build workflow (v* trigger)
git push origin refs/tags/vX.Y.Z
```

For rolling `latest` strategy:

```bash
# Ensure main is up to date, then push to trigger docker-build workflow
git checkout main
git push origin main
```

Monitor workflow:

```text
https://github.com/jlgill/gillson-open-webui/actions/workflows/docker-build.yaml
```

Verify publish completed for the exact target before proceeding:

```bash
# Versioned
docker manifest inspect ghcr.io/jlgill/gillson-open-webui:vX.Y.Z

# Rolling
docker manifest inspect ghcr.io/jlgill/gillson-open-webui:latest
```

If manifest inspect fails, stop here and troubleshoot CI publish first.

### Step 3: Pin Compose to the Verified Target

If using versioned strategy, update [docker-compose.prod.yaml](docker-compose.prod.yaml) to the exact verified tag.

Example:

```yaml
open-webui:
	image: ghcr.io/jlgill/gillson-open-webui:vX.Y.Z
```

If using rolling strategy, keep `:latest` intentionally.

### Step 4: Backup the Database

**Critical**: Always backup before stopping containers.

```powershell
# Windows (PowerShell)
docker exec -t owui-postgres pg_dump -U postgres openwebui > "openwebui_backup_$(Get-Date -Format 'yyyy-MM-dd').sql"

# Verify backup was created and non-zero
Get-ChildItem openwebui_backup*.sql | Select-Object Name, Length, LastWriteTime
```

```bash
# Linux/macOS
docker exec -t owui-postgres pg_dump -U postgres openwebui > "openwebui_backup_$(date +%F).sql"

# Verify backup
ls -la openwebui_backup*.sql
```

### Step 5: Stop All Containers

```bash
docker compose -f docker-compose.prod.yaml down
```

Verify all containers are stopped:
```bash
docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}"
```

### Step 6: Pull Target Images

```bash
docker compose -f docker-compose.prod.yaml pull
```

This updates:
- `ghcr.io/jlgill/gillson-open-webui:<target-tag>` (custom fork image — verified in Step 2)
- `postgres:16` (minor updates only)
- `ollama/ollama:latest`

### Step 7: Start Containers

```bash
docker compose -f docker-compose.prod.yaml up -d
```

### Step 8: Verify Migrations

Open WebUI automatically runs Alembic migrations on startup. Check the logs:

```bash
docker compose -f docker-compose.prod.yaml logs open-webui --tail 100
```

Look for lines like:
```
INFO  [alembic.runtime.migration] Running upgrade xxxx -> yyyy, Migration description
```

### Step 9: Verify Health Status

```bash
docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}"
```

All containers should show `(healthy)` status.

### Step 10: Test the Application

1. Access Open WebUI at `http://localhost:3000`
2. Verify you can log in
3. Check that existing chats are accessible
4. Test creating a new chat
5. Verify any custom settings/configurations

---

## Rollback Procedure

If the upgrade fails or causes issues:

### Step 1: Stop Containers

```bash
docker compose -f docker-compose.prod.yaml down
```

### Step 2: Restore Database Backup

```powershell
# Windows (PowerShell)
Get-Content openwebui_backup_YYYY-MM-DD.sql | docker exec -i owui-postgres psql -U postgres openwebui
```

```bash
# Linux/macOS
cat openwebui_backup_YYYY-MM-DD.sql | docker exec -i owui-postgres psql -U postgres openwebui
```

### Step 3: Run Previous Version

Edit [docker-compose.prod.yaml](docker-compose.prod.yaml) and pin the previously known-good custom image tag, then start services:

```bash
docker compose -f docker-compose.prod.yaml up -d
```

---

## Troubleshooting

### Migration Errors

Check full logs for errors:
```bash
docker compose -f docker-compose.prod.yaml logs open-webui 2>&1 | grep -i "error\|alembic\|migration"
```

### Container Won't Start

Check for startup errors:
```bash
docker compose -f docker-compose.prod.yaml logs open-webui
```

### Database Connection Issues

Verify PostgreSQL is healthy:
```bash
docker exec -it owui-postgres pg_isready -U postgres -d openwebui
```

### Reset to Clean State (Data Loss!)

Only use if you want to start fresh:
```bash
docker compose -f docker-compose.prod.yaml down -v
docker compose -f docker-compose.prod.yaml up -d
```

---

## Volume Locations

| Volume | Contents |
|--------|----------|
| `postgres-data` | PostgreSQL database files |
| `open-webui` | Uploaded files, vector DB, cache |
| `ollama` | Downloaded LLM models |

To find volume paths:
```bash
docker volume inspect open-webui_postgres-data
```

---

## Environment Variables

Key variables in `docker-compose.prod.yaml`:

| Variable | Default | Description |
|----------|---------|-------------|
| `OLLAMA_DOCKER_TAG` | `latest` | Ollama image tag |
| `OPEN_WEBUI_PORT` | `3000` | Host port for web UI |
| `DATABASE_USER` | `postgres` | PostgreSQL username |
| `DATABASE_PASSWORD` | `openwebui` | PostgreSQL password |
| `DATABASE_NAME` | `openwebui` | PostgreSQL database name |

---

## Backup Schedule Recommendation

| Frequency | Backup Type |
|-----------|-------------|
| Daily | Database dump (`pg_dump`) |
| Weekly | Full volume backup |
| Before upgrades | Database dump + verify |

### Automated Backup Script (Optional)

Create `backup.ps1`:
```powershell
$backupDir = ".\backups"
$date = Get-Date -Format "yyyy-MM-dd_HHmmss"
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
docker exec -t owui-postgres pg_dump -U postgres openwebui > "$backupDir\openwebui_$date.sql"
# Keep only last 7 backups
Get-ChildItem "$backupDir\*.sql" | Sort-Object LastWriteTime -Descending | Select-Object -Skip 7 | Remove-Item
```

---

## Additional Resources

- [Open WebUI Documentation](https://docs.openwebui.com/)
- [Open WebUI GitHub](https://github.com/open-webui/open-webui)
- [Troubleshooting Guide](TROUBLESHOOTING.md)
- [Changelog](CHANGELOG.md)

---

*Last updated: February 2026*

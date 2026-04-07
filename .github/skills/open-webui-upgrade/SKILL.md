---
name: open-webui-upgrade
description: "Use when: upgrading Open WebUI with Docker and PostgreSQL, syncing fork with upstream, building custom GHCR image, running migrations, validating health, and preparing rollback steps."
---

# Open WebUI Upgrade Workflow (Docker + PostgreSQL)

## Purpose
Use this skill to execute or guide a safe Open WebUI upgrade for this repository when deployed with Docker Compose and PostgreSQL.

## Triggers
Use this workflow when the user asks to:
- Upgrade Open WebUI in Docker
- Pull latest images and apply migrations
- Sync fork with upstream before upgrading
- Validate post-upgrade health
- Prepare or run rollback after upgrade issues

## Inputs To Confirm First
Before running commands, confirm:
- Compose file path (default: `docker-compose.prod.yaml`)
- Open WebUI service name (often `open-webui`)
- PostgreSQL container name (often `owui-postgres` or `postgres`)
- Database name/user (defaults: `openwebui` / `postgres`)
- Whether this run is `guided` (instructions only) or `execute` (run commands)

## Safety Rules
- Always create and verify a database backup before stopping services.
- Never use destructive Docker volume commands unless explicitly requested.
- If migration or startup checks fail, stop and offer rollback immediately.
- Do not assume container names; detect or confirm them.

## Standard Procedure
1. Pre-check
- Review change notes and confirm target upgrade scope.
- Verify local git status and optionally sync fork with upstream.

2. Backup
- Run `pg_dump` from the PostgreSQL container.
- Verify backup file exists and is non-zero size.

3. Build/Publish custom image
- Confirm custom image repo/tag (for this repo, often `ghcr.io/jlgill/gillson-open-webui:latest`).
- Ensure workflow completed successfully before pulling.

4. Upgrade
- Stop services with the selected compose file.
- Pull latest images.
- Start services in detached mode.

5. Validate
- Inspect Open WebUI logs for Alembic migration completion.
- Check container health.
- Run smoke tests: login, existing chats, new chat creation.

6. Rollback (if needed)
- Stop services.
- Restore the selected SQL backup.
- Restart with pinned previous image tag.

## Command Patterns
Prefer matching these patterns to the user's OS and environment:
- Backup: `docker exec -t <postgres-container> pg_dump -U <db-user> <db-name> > <backup-file>`
- Stop: `docker compose -f <compose-file> down`
- Pull: `docker compose -f <compose-file> pull`
- Start: `docker compose -f <compose-file> up -d`
- Logs: `docker compose -f <compose-file> logs <webui-service> --tail 100`
- Health: `docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}"`

## Expected Outputs
At completion, provide:
- Backup file name/path and size
- Image tags/digests used
- Migration log summary
- Container health summary
- Follow-up actions or rollback recommendation

## Repository References
- Upgrade guide: `UPGRADE_GUIDE.md`
- Workflow: `.github/workflows/docker-build.yaml`
- Compose file: `docker-compose.prod.yaml`

---
name: open-webui-upgrade
description: "Use when: automatically upgrading Open WebUI with Docker and PostgreSQL, syncing fork with upstream, building/publishing custom GHCR images, running Alembic migrations, validating service health, and executing rollback if checks fail."
argument-hint: "mode=guided|execute compose=docker-compose.prod.yaml webuiService=open-webui postgresContainer=owui-postgres dbName=openwebui dbUser=postgres imageRef=ghcr.io/jlgill/gillson-open-webui:latest"
---

# Open WebUI Upgrade Workflow (Docker + PostgreSQL)

## Purpose
Use this skill to run a safe, repeatable OWUI upgrade with explicit safety gates, rollback readiness, and post-upgrade verification.

## Triggers
Use this workflow when the user asks to:
- Upgrade Open WebUI in Docker
- Pull latest images and apply migrations
- Sync fork with upstream before upgrading
- Validate post-upgrade health
- Prepare or run rollback after upgrade issues

## Execution Modes
- `guided`: Generate and explain exact commands, do not execute.
- `execute`: Run the full workflow, stop immediately on failed gates.

## Safety Rules
- Always create and verify a database backup before stopping services.
- Never use destructive Docker volume commands unless explicitly requested.
- If migration or startup checks fail, stop and offer rollback immediately.
- Do not assume container names; detect or confirm them.
- Never change compose image pins to a new tag until `docker manifest inspect` succeeds for that exact tag.

## Inputs
Collect these once at the start and use defaults when omitted:
- `composeFile`: `docker-compose.prod.yaml`
- `webuiService`: `open-webui`
- `postgresContainer`: `owui-postgres` (fallback: `postgres`)
- `dbName`: `openwebui`
- `dbUser`: `postgres`
- `imageRef`: `ghcr.io/jlgill/gillson-open-webui:latest`
- `branch`: `main`
- `syncFork`: `true`
- `checkGhActions`: `true`

## Decision Logic
1. If working tree is dirty and `syncFork=true`:
- Stash before merge, then re-apply stash after merge.
2. If upstream remote is missing:
- Add `upstream` as `https://github.com/open-webui/open-webui.git`.
3. If custom image is used and target tag/manifest does not exist:
- Trigger `.github/workflows/docker-build.yaml` and block runtime upgrade until manifest verification passes.
4. If backup file is missing or zero-byte:
- Abort upgrade.
5. If migrations fail, service unhealthy, or smoke checks fail:
- Execute rollback flow.

## Standard Procedure
1. Preflight
- Verify Docker and Compose availability.
- Check compose file exists.
- Review release notes in `CHANGELOG.md` and migration caveats.

2. Fork Sync (recommended)
- `git fetch upstream`
- `git checkout <branch>`
- `git merge upstream/<branch>`
- `git push origin <branch>`

3. Custom Image Publish Gate (required for this repo)
- Confirm image is `ghcr.io/jlgill/gillson-open-webui:latest`.
- Confirm `.github/workflows/docker-build.yaml` has completed successfully.
- Confirm image manifest/digest is fresh.

	For versioned upgrades (`vX.Y.Z`) on this repository, build/publish first:
	- `git fetch upstream --tags`
	- `git fetch origin --tags`
	- `git tag -l vX.Y.Z` (or `git fetch upstream tag vX.Y.Z` if missing locally)
	- `git ls-remote --tags origin vX.Y.Z`
	- If missing in origin, publish tag to trigger workflow: `git push origin refs/tags/vX.Y.Z`
	- Wait for `.github/workflows/docker-build.yaml` (triggered by `v*`) to complete successfully
	- Verify image exists before compose pinning: `docker manifest inspect ghcr.io/jlgill/gillson-open-webui:vX.Y.Z`
	- Only then change compose to the version tag and continue upgrade

	If manifest inspect fails, do not pin compose to that tag.

4. Backup Gate (required)
- Create dated SQL dump with `pg_dump` from PostgreSQL container.
- Verify backup exists and has non-zero size.

5. Compose Pin Gate
- Confirm compose image references the exact verified target tag (or `latest` if intentionally using rolling policy).

6. Upgrade Runtime
- `docker compose -f <composeFile> down`
- `docker compose -f <composeFile> pull`
- `docker compose -f <composeFile> up -d`

7. Migration + Health Validation
- Inspect OWUI logs for Alembic upgrade lines and absence of fatal errors.
- Ensure all critical containers report healthy/running.
- Run `pg_isready` against the target database.

8. Functional Smoke Checks
- Access OWUI URL.
- Verify login.
- Open existing chats.
- Create a new chat.

9. Rollback (on any failed gate)
- Stop containers.
- Restore DB backup.
- Pin compose to the prior known-good image tag and start services.
- Re-run health checks.

## Completion Criteria
Upgrade is complete only when all are true:
- Backup file exists and is non-zero size.
- Custom image was built/published and pulled successfully.
- Alembic migrations completed without fatal errors.
- Container status is healthy/running.
- User confirms smoke tests passed.

## Scripted Automation
For `execute` mode on Windows PowerShell, use:
- [Upgrade automation script](./scripts/upgrade-owui.ps1)

This script runs preflight, optional fork sync, backup, compose upgrade, migration/health checks, and emits a final summary.

## Output Report Format
Always report:
- Backup path, timestamp, and byte size
- Compose file and services upgraded
- Pulled image references
- Migration status summary
- Health check summary
- Rollback action taken (if any)

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

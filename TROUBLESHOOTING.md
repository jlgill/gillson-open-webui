# Open WebUI Troubleshooting Guide

## Understanding the Open WebUI Architecture

The Open WebUI system is designed to streamline interactions between the client (your browser) and the Ollama API. At the heart of this design is a backend reverse proxy, enhancing security and resolving CORS issues.

- **How it Works**: The Open WebUI is designed to interact with the Ollama API through a specific route. When a request is made from the WebUI to Ollama, it is not directly sent to the Ollama API. Initially, the request is sent to the Open WebUI backend via `/ollama` route. From there, the backend is responsible for forwarding the request to the Ollama API. This forwarding is accomplished by using the route specified in the `OLLAMA_BASE_URL` environment variable. Therefore, a request made to `/ollama` in the WebUI is effectively the same as making a request to `OLLAMA_BASE_URL` in the backend. For instance, a request to `/ollama/api/tags` in the WebUI is equivalent to `OLLAMA_BASE_URL/api/tags` in the backend.

- **Security Benefits**: This design prevents direct exposure of the Ollama API to the frontend, safeguarding against potential CORS (Cross-Origin Resource Sharing) issues and unauthorized access. Requiring authentication to access the Ollama API further enhances this security layer.

## Open WebUI: Server Connection Error

If you're experiencing connection issues, it’s often due to the WebUI docker container not being able to reach the Ollama server at 127.0.0.1:11434 (host.docker.internal:11434) inside the container . Use the `--network=host` flag in your docker command to resolve this. Note that the port changes from 3000 to 8080, resulting in the link: `http://localhost:8080`.

**Example Docker Command**:

```bash
docker run -d --network=host -v open-webui:/app/backend/data -e OLLAMA_BASE_URL=http://127.0.0.1:11434 --name open-webui --restart always ghcr.io/open-webui/open-webui:main
```

### Error on Slow Responses for Ollama

Open WebUI has a default timeout of 5 minutes for Ollama to finish generating the response. If needed, this can be adjusted via the environment variable AIOHTTP_CLIENT_TIMEOUT, which sets the timeout in seconds.

### General Connection Errors

**Ensure Ollama Version is Up-to-Date**: Always start by checking that you have the latest version of Ollama. Visit [Ollama's official site](https://ollama.com/) for the latest updates.

**Troubleshooting Steps**:

1. **Verify Ollama URL Format**:
   - When running the Web UI container, ensure the `OLLAMA_BASE_URL` is correctly set. (e.g., `http://192.168.1.1:11434` for different host setups).
   - In the Open WebUI, navigate to "Settings" > "General".
   - Confirm that the Ollama Server URL is correctly set to `[OLLAMA URL]` (e.g., `http://localhost:11434`).

By following these enhanced troubleshooting steps, connection issues should be effectively resolved. For further assistance or queries, feel free to reach out to us on our community Discord.

## Production PostgreSQL Observability

The production stack exposes PostgreSQL metrics through `owui-postgres-exporter` on `127.0.0.1:9187` and scrapes them into the bundled Grafana LGTM Prometheus instance. This catches database saturation before Open WebUI or LiteLLM fail with `FATAL: sorry, too many clients already`.

### Quick Checks

```powershell
docker compose -f docker-compose.prod.yaml ps postgres postgres-exporter grafana
curl.exe -fsS http://127.0.0.1:9187/metrics | Select-String -Pattern "^(pg_up|pg_stat_activity_count|pg_settings_max_connections|pg_exporter_last_scrape_error)"
docker exec owui-grafana sh -c "curl -fsS --get 'http://localhost:9090/api/v1/query' --data-urlencode 'query=sum(pg_stat_activity_count) / max(pg_settings_max_connections)'"
```

The connection usage query returns a ratio. For example, `0.85` means PostgreSQL is using 85% of `max_connections`.

### Grafana Dashboard Panels

Create these panels in Grafana using the Prometheus data source:

```promql
100 * sum(pg_stat_activity_count) / max(pg_settings_max_connections)
```

Connection usage percent.

```promql
sum by (datname, state) (pg_stat_activity_count)
```

Connections by database and state.

```promql
pg_settings_max_connections
```

Configured connection ceiling.

```promql
pg_exporter_last_scrape_error
```

Exporter scrape health.

```promql
increase(pg_stat_database_deadlocks{datname!~"template.*|postgres",datid!="0"}[5m])
```

Deadlocks in the last five minutes.

### Manual Grafana Alert Rules

Start with UI-managed alert rules and labels such as `service=postgres` and `severity=warning|critical`.

| Alert | Query | Threshold | For |
| --- | --- | --- | --- |
| PostgreSQL down | `pg_up == 0` | `is above 0` | `1m` |
| Connection usage warning | `100 * sum(pg_stat_activity_count) / max(pg_settings_max_connections)` | `> 70` | `10m` |
| Connection usage critical | `100 * sum(pg_stat_activity_count) / max(pg_settings_max_connections)` | `> 85` | `5m` |
| Connection usage page | `100 * sum(pg_stat_activity_count) / max(pg_settings_max_connections)` | `> 95` | `2m` |
| Exporter scrape error | `pg_exporter_last_scrape_error` | `> 0` | `5m` |
| Deadlocks | `increase(pg_stat_database_deadlocks{datname!~"template.*|postgres",datid!="0"}[5m])` | `> 0` | `1m` |

If Loki log alerts are enabled, add a hard-failure alert for the Postgres log phrase `too many clients already`.

### Triage When Connection Usage Alerts Fire

1. Check connection pressure:
   ```powershell
   docker exec owui-grafana sh -c "curl -fsS --get 'http://localhost:9090/api/v1/query' --data-urlencode 'query=sum by (datname, state) (pg_stat_activity_count)'"
   ```
2. Inspect the recent database-side errors:
   ```powershell
   docker logs owui-postgres --tail 80 --timestamps
   ```
3. Check the most likely DB-backed dependents:
   ```powershell
   docker logs owui-litellm --tail 80 --timestamps
   docker logs owui-app --tail 80 --timestamps
   docker logs owui-langfuse --tail 80 --timestamps
   docker logs owui-langfuse-worker --tail 80 --timestamps
   ```
4. If PostgreSQL is refusing every new connection, restart the noisiest dependent first rather than the database. In this stack, start with LiteLLM if its logs show startup retry churn:
   ```powershell
   docker compose -f docker-compose.prod.yaml restart litellm
   ```
5. Recheck the connection usage ratio and only increase PostgreSQL `max_connections` after confirming there is no retry loop or connection leak.

# Open WebUI - AI Coding Agent Instructions

Use this document as a scoped reference, not a checklist. Apply only sections relevant to the current task, and prioritize in this order: Project-Specific Conventions, Critical Integration Points, Common Gotchas, then Architecture/Workflow background.

## Project Overview
Open WebUI is an extensible, self-hosted AI platform with a **SvelteKit frontend** and **FastAPI backend**. It provides a web interface for LLMs (Ollama, OpenAI-compatible APIs) with built-in RAG, document processing, and extensibility through Python functions and pipelines.

## Architecture Overview

### Frontend (SvelteKit)
- **Framework**: SvelteKit 5 with TypeScript, Vite, TailwindCSS 4
- **Structure**: `src/routes/` uses SvelteKit's file-based routing with `(app)` group for authenticated pages
- **State**: Svelte stores in `src/lib/stores/index.ts` (avoid Svelte 4 patterns - use runes in Svelte 5)
- **API Layer**: `src/lib/apis/` contains typed API client functions that call backend endpoints
- **Components**: `src/lib/components/` - reusable Svelte components
- **Build**: Static adapter (`@sveltejs/adapter-static`) outputs to `build/` directory

### Backend (FastAPI + Python)
- **Entry Point**: `backend/open_webui/main.py` - FastAPI app with middleware, CORS, socket.io
- **Routers**: `backend/open_webui/routers/` - modular API routes (auths, chats, models, retrieval, etc.)
- **Models**: `backend/open_webui/models/` - Pydantic models AND database models (dual purpose)
- **Database**: SQLAlchemy + Peewee dual ORM setup in `backend/open_webui/internal/db.py`
  - Migrations via Alembic in `backend/open_webui/migrations/versions/`
  - Default: SQLite with WAL mode, supports PostgreSQL/MySQL via `DATABASE_URL`
- **Config**: `backend/open_webui/config.py` (3676 lines) - massive config with BaseModel forms
- **Environment**: `backend/open_webui/env.py` - loads .env from project root, sets globals

### Key Architectural Patterns

#### 1. Reverse Proxy Design
The backend acts as a **reverse proxy** between the frontend and Ollama/OpenAI APIs:
- Frontend requests → `/ollama` or `/openai` routes → Backend forwards to `OLLAMA_BASE_URL`/`OPENAI_API_BASE_URL`
- Prevents CORS issues and adds authentication layer
- See `TROUBLESHOOTING.md` for connection architecture details

#### 2. Pipelines & Functions (Extensibility Layer)
- **Pipelines** (`routers/pipelines.py`): External microservices that filter/transform LLM requests
  - Inlet filters: Transform requests before sending to LLM
  - Outlet filters: Transform responses after receiving from LLM
  - Priority-based execution chain (sorted by `pipeline.priority`)
- **Functions** (`routers/functions.py`, `functions.py`): Python code executed server-side
  - Load from GitHub URLs or local files
  - Module caching in `CACHE_DIR`
  - RestrictedPython execution environment
  - Valves: Dynamic configuration per function

#### 3. RAG System (`backend/open_webui/retrieval/`)
- **Vector Stores**: Pluggable backends (ChromaDB default, OpenSearch) via `retrieval/vector/factory.py`
- **Loaders**: `retrieval/loaders/` - document processing (PDF, DOCX, PPTX, YouTube, etc.)
- **Web Search**: `retrieval/web/` - 20+ search engines (Brave, SearXNG, Tavily, etc.)
- **Embeddings**: Sentence transformers with model auto-update support

#### 4. WebSocket/Real-time (Socket.IO)
- `backend/open_webui/socket/main.py` - collaborative editing with CRDT (pycrdt)
- Redis-backed session management for multi-instance deployments
- Usage pool tracking for concurrent users

## Development Workflows

### Local Development Setup
```bash
# Backend (port 8080)
cd backend
pip install -r requirements.txt
export CORS_ALLOW_ORIGIN="http://localhost:5173"
bash dev.sh  # uvicorn with --reload

# Frontend (port 5173)
npm install
npm run dev
```

### Building & Testing
```bash
# Frontend
npm run build              # Static build to build/
npm run check              # Type checking
npm run lint               # ESLint + Pylint + type checking
npm run format             # Prettier + Black

# Backend
black . --exclude ".venv/"  # Format Python code

# E2E Tests (Cypress)
npm run cy:open            # Interactive mode
```

### Docker Deployment
```bash
# Standard: Frontend + Backend in one container
docker compose up -d

# With Ollama bundled: Use :ollama tag
# With GPU: Use :cuda tag, requires nvidia-container-toolkit
```

### Database Migrations
- Alembic migrations in `backend/open_webui/migrations/versions/`
- Auto-run on startup via `main.py` lifespan context
- Create migration: `alembic revision -m "description"`

## Project-Specific Conventions

### Python Code Style
- **Pydantic Models**: Used for BOTH API validation AND SQLAlchemy models (see `models/*.py`)
- **Logging**: Use module-level logger with `SRC_LOG_LEVELS` from `env.py`
  ```python
  log = logging.getLogger(__name__)
  log.setLevel(SRC_LOG_LEVELS["MAIN"])  # or "SOCKET", "DB", "OAUTH"
  ```
- **Auth**: All routes use `Depends(get_verified_user)` or `Depends(get_admin_user)`
- **Error Handling**: Use `ERROR_MESSAGES` constants from `open_webui.constants`

### Frontend Patterns
- **Svelte 5 Runes**: Use `$state()`, `$derived()`, `$effect()` instead of reactive declarations using `let` from pre-Svelte-5 patterns
- **API Calls**: Always token-authenticated via `src/lib/apis/` functions
- **Forms**: Pydantic model shapes mirror backend - use TypeScript types from `src/lib/types/`
- **Routing**: Protected routes in `src/routes/(app)/`, public in `src/routes/auth/`

### Configuration Management
- **Environment Variables**: Loaded from root `.env`, processed in `backend/open_webui/env.py`
- **Runtime Config**: Stored in `config.py` with database persistence
- **Feature Flags**: Check `backend/open_webui/config.py` for available settings (e.g., `RAG_EMBEDDING_MODEL_AUTO_UPDATE`)

### Testing Conventions
- **Backend Tests**: Pytest in `backend/open_webui/test/` (limited coverage currently)
- **E2E Tests**: Cypress in `cypress/e2e/` - focus on critical user flows (chat, documents, auth)
- **Test Users**: `cy.loginAdmin()` helper in Cypress for authenticated tests

## Critical Integration Points

### Adding a New API Route
1. Create router in `backend/open_webui/routers/your_feature.py`
2. Define Pydantic models in `backend/open_webui/models/your_feature.py`
3. Register router in `backend/open_webui/main.py`: `app.include_router(your_feature.router, prefix="/api/your_feature")`
4. Add frontend API client in `src/lib/apis/your_feature/index.ts`
5. Use in Svelte components via `import { yourApiFunction } from '$lib/apis/your_feature'`

### Adding a New Document Loader
1. Create loader in `backend/open_webui/retrieval/loaders/your_format.py`
2. Implement `Loader` interface with `load()` method returning `List[Document]`
3. Register in `backend/open_webui/retrieval/loaders/main.py`

### Adding a New Web Search Provider
1. Create file in `backend/open_webui/retrieval/web/your_provider.py`
2. Implement `search_your_provider(api_key, query, **kwargs) -> list[SearchResult]`
3. Add to search router in `backend/open_webui/routers/retrieval.py`

## Common Gotchas

1. **Database Schema Changes**: Always create Alembic migration, don't modify models without migration
2. **CORS in Dev**: Backend must set `CORS_ALLOW_ORIGIN="http://localhost:5173"` for frontend dev server
3. **Docker Volumes**: `-v open-webui:/app/backend/data` is CRITICAL to persist database
4. **Ollama Connection**: Use `host.docker.internal` in Docker, `localhost` in native setup
5. **Redis/WebSocket**: Multi-instance deployments REQUIRE Redis (`WEBSOCKET_MANAGER=redis`)
6. **Python Environment**: Project requires Python 3.11 (`pyproject.toml` specifies dependencies)

## VS Code Extensions & Tools

### PostgreSQL Extension
Agents running in VS Code have access to the **PostgreSQL extension** with MCP tools for direct database interaction:
- **Connection**: Use `pgsql_connect` with server name `"localhost, <default> (postgres)"` and database `"openwebui"`
- **Schema Context**: Use `pgsql_db_context` to fetch CREATE scripts for tables, indexes, functions, etc.
- **Queries**: Use `pgsql_query` for SELECT/read-only operations (always validate literal values)
- **Modifications**: Use `pgsql_modify` for DDL/DML operations (CREATE, ALTER, INSERT, UPDATE, DELETE)
- **Visualization**: Use `pgsql_visualize_schema` to open an interactive schema diagram
- **Bulk Operations**: Use `pgsql_bulk_load_csv` for importing CSV data
- **Scripts**: Use `pgsql_open_script` for multi-statement SQL scripts

#### Connection Environments
| Environment | Host | Port | Database | DATABASE_URL |
|-------------|------|------|----------|--------------|
| **Local Dev** | `localhost` | `5432` | `openwebui` | `postgresql://postgres:password@localhost:5432/openwebui` |
| **Docker** | `postgres` (service name) | `5432` | `openwebui` | `postgresql://postgres:password@postgres:5432/openwebui` |

- **Local Development**: PostgreSQL runs natively on `localhost:5432`. Use server name `"localhost, <default> (postgres)"` in VS Code PostgreSQL extension.
- **Dockerized Environment**: When running in Docker Compose, use the service name `postgres` as the host. The app container connects to `postgres:5432` via Docker's internal network.
- **Docker to Host**: If the app is in Docker but PostgreSQL is on the host machine, use `host.docker.internal:5432`.

**Important**: Always use `pgsql_db_context` before making modifications to understand the current schema state. Prefer using PostgreSQL tools over asking users to run `psql` commands directly.

## Key Files Reference
- `backend/open_webui/main.py` - FastAPI app, middleware, startup logic
- `backend/open_webui/config.py` - Massive configuration system with DB persistence
- `backend/open_webui/env.py` - Environment variable loading and global constants
- `src/routes/(app)/+layout.svelte` - Authenticated app shell with sidebar
- `src/lib/stores/index.ts` - Global state management
- `docker-compose.yaml` - Standard deployment with Ollama + Open WebUI + Pipelines
- `pyproject.toml` - Python dependencies (FastAPI, Langchain, Transformers, etc.)
- `package.json` - Frontend dependencies (SvelteKit, TailwindCSS, TypeScript)

"""FastAPI application for the Nexus Local Mission Board UI V1.

Presents a single-page dashboard over the read-only web services layer and
exposes JSON endpoints for missions, tasks, agents, and execution sessions.
Run locally with:

    uvicorn nexus.web.app:app --host 127.0.0.1 --port 8000 --reload

Open http://localhost:8000 in a browser.
"""

from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from nexus.agents.bootstrap import initialize_default_agents
from nexus.registry.database import initialize_database
from nexus.registry.projects import sync_projects
from nexus.web import services
from nexus.web.routes import router
from nexus.web.workspace_routes import router as workspace_router

try:
    from importlib.metadata import version

    APP_VERSION = version("nexus") or "v1"
except Exception:  # pragma: no cover - packaging metadata may be absent
    APP_VERSION = "v1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize the project registry and default agents on startup.

    Reuses the existing Project Router infrastructure (SQLite-backed
    registry + config/projects.json sync) rather than creating a second
    registry. Both calls are idempotent, so repeated startups are safe.
    """
    initialize_database()
    from nexus.chat.service import recover_orphaned_turns
    from nexus.dev.mission_runner import recover_orphaned_missions
    from nexus.registry.workspace_handoff import recover_orphaned

    recover_orphaned_turns()
    recover_orphaned_missions()
    recover_orphaned()
    sync_projects()
    initialize_default_agents()
    yield


WEB_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


def _loopback_authority(value: str) -> tuple[str, int] | None:
    """Accept only exact loopback HTTP authorities, with an optional port."""
    if not value or any(char.isspace() for char in value) or "," in value:
        return None
    try:
        parsed = urlsplit(f"http://{value}")
        if (parsed.username is not None or parsed.password is not None or
                parsed.path or parsed.query or parsed.fragment or
                parsed.hostname not in {"localhost", "127.0.0.1", "::1"}):
            return None
        port = parsed.port if parsed.port is not None else 80
        if not 1 <= port <= 65535:
            return None
        return parsed.hostname, port
    except ValueError:
        return None


def _valid_chat_origin(origin: str, authority: tuple[str, int], scheme: str) -> bool:
    try:
        parsed = urlsplit(origin)
        return (
            parsed.scheme == scheme
            and parsed.scheme in {"http", "https"}
            and not parsed.path and not parsed.query and not parsed.fragment
            and parsed.username is None and parsed.password is None
            and _loopback_authority(parsed.netloc) == authority
        )
    except ValueError:
        return False


def _static_asset_version() -> str:
    """Version token for static URLs, so a changed stylesheet or script is
    never served stale from a browser cache."""
    mtimes = [path.stat().st_mtime_ns for path in STATIC_DIR.rglob("*") if path.is_file()]
    return format(max(mtimes, default=0), "x")


def create_app():
    """Build and return the FastAPI application instance."""
    app = FastAPI(
        lifespan=lifespan,
        title="Nexus Local Mission Board",
        description=(
            "Local visualization for Nexus missions, tasks, agents, and "
            "execution sessions."
        ),
        version=APP_VERSION,
    )

    @app.middleware("http")
    async def protect_chat_localhost(request: Request, call_next):
        # Chat and Development Mode can both execute Codex turns (the
        # latter with workspace-write). Reject DNS rebinding and browser
        # requests from a different origin before any route of either is
        # reached -- the same loopback-authority + same-origin check,
        # reused rather than re-implemented for /api/dev/.
        if request.url.path.startswith(("/api/chat/", "/api/dev/", "/api/workspace/")):
            hosts = request.headers.getlist("host")
            authority = _loopback_authority(hosts[0]) if len(hosts) == 1 else None
            origins = request.headers.getlist("origin")
            if authority is None or len(origins) > 1 or (
                origins and not _valid_chat_origin(origins[0], authority, request.url.scheme)
            ):
                return JSONResponse(
                    status_code=403,
                    content={"detail": {"code": "CHAT_ORIGIN_FORBIDDEN", "message": "Chat requires a local same-origin request."}},
                )
        return await call_next(request)

    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(router)
    app.include_router(workspace_router)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def board(request: Request):
        """Render the dashboard shell."""
        return templates.TemplateResponse(
            request=request,
            name="board.html",
            context={
                "app_version": APP_VERSION,
                "summary": services.get_summary(),
                "columns": services.COLUMNS,
            },
        )
    @app.get("/nexus", response_class=HTMLResponse, include_in_schema=False)
    async def nexus(request: Request):
        """Render the Nexus Mission Commander interface (Phase C MVP)."""
        return templates.TemplateResponse(
            request=request,
            name="nexus.html",
            context={"app_version": APP_VERSION, "asset_version": _static_asset_version()},
        )

    @app.get("/nexus/workspace", response_class=HTMLResponse, include_in_schema=False)
    async def workspace(request: Request):
        """Render the multi-pane agent workspace (P1)."""
        return templates.TemplateResponse(
            request=request,
            name="workspace.html",
            context={"app_version": APP_VERSION, "asset_version": _static_asset_version()},
        )

    @app.get("/nexus/workspace/cockpit", response_class=HTMLResponse, include_in_schema=False)
    async def cockpit(request: Request):
        """Render the read-only Dark Terminal workspace preview."""
        return templates.TemplateResponse(
            request=request,
            name="cockpit.html",
            context={"app_version": APP_VERSION, "asset_version": _static_asset_version()},
        )

    return app


app = create_app()


def main():
    """Entry point to launch the local web server."""
    import subprocess
    import sys

    subprocess.run(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "nexus.web.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
            "--reload",
        ],
        check=True,
    )


if __name__ == "__main__":
    main()

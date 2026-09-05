"""FastAPI application for the Nexus Local Mission Board UI V1.

Presents a single-page dashboard over the read-only web services layer and
exposes JSON endpoints for missions, tasks, agents, and execution sessions.
Run locally with:

    uvicorn nexus.web.app:app --host 127.0.0.1 --port 8000 --reload

Open http://localhost:8000 in a browser.
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from nexus.agents.bootstrap import initialize_default_agents
from nexus.registry.database import initialize_database
from nexus.registry.projects import sync_projects
from nexus.web import services
from nexus.web.routes import router

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
    sync_projects()
    initialize_default_agents()
    yield


WEB_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


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

    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(router)

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

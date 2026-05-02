import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, PlainTextResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

# Load .env before reading any env vars
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env", override=False)

BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR / "public"

app = FastAPI(
    title="Resume Matcher Frontend",
    description="Static frontend server for Resume Matcher UI",
    version="2.0.0",
)

if not FRONTEND_DIR.exists():
    raise RuntimeError(f"Frontend directory not found: {FRONTEND_DIR}")


# ── Explicit named routes first (before static mount) ────────────────────────

@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html", media_type="text/html")


@app.get("/config.js", include_in_schema=False)
def config_js() -> PlainTextResponse:
    """Inject runtime config so the browser knows the API URL."""
    api_url = os.getenv("API_URL", "").rstrip("/")
    body = f"window.APP_CONFIG = {{ API_URL: '{api_url}' }};\n"
    return PlainTextResponse(content=body, media_type="application/javascript")


@app.get("/health", include_in_schema=False)
def health() -> dict:
    return {"status": "ok", "service": "resume-frontend"}


# ── Mount static files at root so /app.js, /styles.css etc. resolve ──────────
# NOTE: This MUST come after explicit routes, otherwise FastAPI's StaticFiles
# would intercept /config.js and return a 404 before the route handler fires.
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="static")

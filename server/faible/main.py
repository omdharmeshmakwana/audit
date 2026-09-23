from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, text

from faible.api import router as api_router
from faible.config import get_settings
from faible.discovery import router as discovery_router
from faible.web import LoginRequired
from faible.web import router as web_router

app = FastAPI(title="Faible", version="0.1.0")
app.include_router(api_router)
app.include_router(discovery_router)
app.include_router(web_router)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


@app.exception_handler(LoginRequired)
def _login_required(request: Request, exc: LoginRequired) -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


@app.get("/health")
def health() -> dict[str, str]:
    engine = create_engine(get_settings().database_url)
    try:
        with engine.connect() as conn:
            conn.execute(text("select 1"))
        db = "ok"
    except Exception as exc:
        db = f"error: {type(exc).__name__}"
    finally:
        engine.dispose()
    return {"status": "ok", "db": db}

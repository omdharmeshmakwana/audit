from fastapi import FastAPI
from sqlalchemy import create_engine, text

from faible.api import router as api_router
from faible.config import get_settings
from faible.discovery import router as discovery_router

app = FastAPI(title="Faible", version="0.1.0")
app.include_router(api_router)
app.include_router(discovery_router)


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

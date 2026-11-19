from contextlib import asynccontextmanager
import os

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .api import backups, sessions, summaries
from .config import init_config, router as config_router
from .database import ROOT, connect, init_journal
from .evolution import init_evolution, router as evolution_router
from .observer import init_observer
from .runtime import JournalRuntime
from .telemetry import init_telemetry, router as telemetry_router


@asynccontextmanager
async def lifespan(application):
    if not os.getenv("THOUGHT_DB") or "DEEPSEEK_API_KEY" not in os.environ:
        load_dotenv(ROOT / ".env")
    with connect() as db:
        db.execute("PRAGMA journal_mode = WAL")
        init_config(db)
        init_journal(db)
        init_evolution(db)
        init_observer(db)
        init_telemetry(db)
    runtime = application.state.runtime = JournalRuntime()
    runtime.start_observer()
    try:
        yield
    finally:
        await runtime.close()


app = FastAPI(title="想法簿", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])
for router in (config_router, evolution_router, telemetry_router, sessions.router, summaries.router, backups.router):
    app.include_router(router)


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, error: RequestValidationError):
    errors = [{field: entry[field] for field in ("loc", "msg", "type")} for entry in error.errors()]
    return JSONResponse({"detail": errors}, status_code=422)


@app.middleware("http")
async def local_requests(request: Request, call_next):
    origin = request.headers.get("origin")
    if request.method in {"POST", "PATCH", "DELETE"} and origin:
        expected = f"{request.url.scheme}://{request.headers.get('host')}"
        if origin != expected:
            return JSONResponse({"detail": "请从本地应用页面操作。"}, status_code=403)
    if not request.url.path.startswith("/api/"):
        return await call_next(request)

    runtime = request.app.state.runtime
    if runtime.restoring:
        return JSONResponse({"detail": "正在恢复备份，请稍后再试。"}, status_code=409)
    runtime.api_requests += 1
    try:
        return await call_next(request)
    finally:
        runtime.api_requests -= 1


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(ROOT / "static" / "index.html")


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

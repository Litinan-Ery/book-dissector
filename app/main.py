"""图书拆解器 —— 本地 Web 服务入口。

启动：uvicorn app.main:app（或 python -m app.main）
访问：http://127.0.0.1:8000
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from . import config
from .api import books, export, prune, settings, tasks


@asynccontextmanager
async def lifespan(_: FastAPI):
    """启动后在 5 秒目标内恢复 SQLite 中断任务并重新入队。"""
    tasks.recover_and_schedule()
    yield

app = FastAPI(
    title="图书拆解器",
    description="导入书籍 → 删减无关内容 → 压缩提炼 → 导出精华 MD",
    version="0.2.0",
    lifespan=lifespan,
)


class NoCacheStatic(BaseHTTPMiddleware):
    """静态资源禁用缓存，避免前端更新后浏览器仍用旧文件。"""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response


app.add_middleware(NoCacheStatic)

config.ensure_dirs()

app.include_router(books.router)
app.include_router(export.router)
app.include_router(prune.router)
app.include_router(settings.router)
app.include_router(tasks.router)

app.mount("/static", StaticFiles(directory=config.PROJECT_ROOT / "app" / "static"), name="static")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "图书拆解器", "version": app.version}


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(config.PROJECT_ROOT / "app" / "static" / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT)

"""
FastAPI 应用入口：组装所有路由 + 启动初始化 + 托管前端页面。

数据流位置：整个后端的总装车间。

"生产模式托管前端"：开发时前端跑在 Vite 的 5173 端口；日常使用时
`./start.sh` 会先 npm run build 把前端编译成静态文件（frontend/dist/），
本文件把它们挂载到根路径——于是一个 uvicorn 进程同时服务页面和 API，
单进程即整个"平台"。
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import chat, documents
from app.config import settings
from app.db import init_db


# lifespan 是 FastAPI 官方当前推荐的启动/关闭钩子写法
# （旧的 @app.on_event 已弃用）。yield 之前=启动时执行，之后=关闭时执行
@asynccontextmanager
async def lifespan(_: FastAPI):
    # 启动：建表（无则建）+ 从 SQLite 重建 BM25 关键词索引
    init_db()
    # 延迟导入避免循环依赖；refresh() 会全量重建内存索引
    from app.storage import bm25

    bm25.refresh()
    yield
    # 关闭：暂无需要释放的资源（SQLite/Chroma 由各自库自动落盘）


app = FastAPI(title="RAG Platform", version="0.1.0", lifespan=lifespan)

# CORS：开发模式下前端(5173)和后端(8000)是两个源，
# 浏览器默认禁止跨源请求，这里显式放行（生产单进程模式用不到）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册两组 API 路由
app.include_router(documents.router)
app.include_router(chat.router)


@app.get("/api/health")
def health() -> dict:
    """健康检查 + 配置摘要（前端"关于"信息与联调排障用）。"""
    return {
        "status": "ok",
        "chat_model": settings.chat_model,
        "embed_model": settings.embed_model,
        "api_key_set": bool(settings.zhipuai_api_key),
    }


# ---------- 生产模式：托管编译好的前端 ----------

_dist = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
if _dist.is_dir():
    # 静态资源（js/css/图片）直接挂载
    app.mount("/assets", StaticFiles(directory=_dist / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str) -> FileResponse:
        """SPA 兜底路由：浏览器刷新 /chat 这类前端路由时，返回 index.html
        让前端路由接管（否则 FastAPI 会 404）。真实存在的文件直接返回。"""
        file = _dist / full_path
        if full_path and file.is_file():
            return FileResponse(file)
        return FileResponse(_dist / "index.html")

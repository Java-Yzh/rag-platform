"""
文档管理 API：上传 / 列表 / 删除 / chunk 预览 / 重新入库。

数据流位置：前端"文档管理"页的后端入口，负责接收文件、登记数据库、
把重活儿（转换切分入库）交给 ingestion 管线的后台线程。

FastAPI 知识点：
- 路由函数参数里写 `session: Session = Depends(get_session)` 叫"依赖注入"，
  框架自动帮你创建/回收数据库会话，不用手写连接管理；
- UploadFile 是文件上传的标准类型，.file 是底层文件对象，流式读取省内存。
"""

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlmodel import Session, func, select

from app.config import settings
from app.db import get_session
from app.models import Chunk, Document
from app.services import ingestion

router = APIRouter(prefix="/api/documents", tags=["documents"])


@router.post("")
async def upload_documents(
    files: list[UploadFile],
    session: Session = Depends(get_session),
) -> list[dict]:
    """批量上传：每个文件存盘 + 建档(pending) + 提交后台入库。返回建档结果列表。"""
    created: list[dict] = []
    for f in files:
        ext = Path(f.filename or "").suffix.lower()
        if ext not in settings.allowed_extensions:
            raise HTTPException(400, f"不支持的格式 {ext}，仅支持: {settings.allowed_extensions}")

        # 存盘时用 uuid 改名：避免不同用户传同名文件互相覆盖
        doc_id = uuid.uuid4().hex
        stored = settings.upload_dir / f"{doc_id}{ext}"
        # 分块写入而不是一次 read() 全进内存——100MB 的文件也不怕
        size = 0
        with open(stored, "wb") as out:
            while block := await f.read(1 << 20):  # 每次 1MB
                size += len(block)
                out.write(block)
        if size > settings.max_upload_mb * 1024 * 1024:
            stored.unlink(missing_ok=True)
            raise HTTPException(400, f"{f.filename} 超过 {settings.max_upload_mb}MB 上限")

        doc = Document(
            id=doc_id,
            filename=f.filename or stored.name,
            stored_path=str(stored),
            ext=ext,
            size_bytes=size,
            md5=ingestion.md5_of_file(stored),
        )
        session.add(doc)
        session.commit()
        session.refresh(doc)
        created.append(_to_dict(doc))

        # 丢给后台线程池，接口立即返回（前端轮询列表看状态变化）
        ingestion.submit_ingest(doc.id)
    return created


def _to_dict(d: Document) -> dict:
    """把 ORM 对象转成可 JSON 序列化的 dict（前端友好格式）。"""
    return {
        "id": d.id,
        "filename": d.filename,
        "ext": d.ext,
        "size_bytes": d.size_bytes,
        "status": d.status,
        "error": d.error,
        "chunk_count": d.chunk_count,
        "md5": d.md5,
        "created_at": d.created_at.isoformat(),
    }


@router.get("")
def list_documents(session: Session = Depends(get_session)) -> list[dict]:
    """全部文档列表（新的在前）。前端每 2 秒轮询一次刷新状态。"""
    docs = session.exec(select(Document).order_by(Document.created_at.desc())).all()
    return [_to_dict(d) for d in docs]


@router.delete("/{doc_id}")
def remove_document(doc_id: str, session: Session = Depends(get_session)) -> dict:
    """级联删除：数据库 + 向量 + BM25 索引 + 原文件，一处不留。"""
    if not ingestion.delete_document(doc_id):
        raise HTTPException(404, "文档不存在")
    return {"ok": True}


@router.post("/{doc_id}/reingest")
def reingest_document(doc_id: str, session: Session = Depends(get_session)) -> dict:
    """重新入库：改了切分参数(.env)后对已有文档重跑管线。

    实现上只是把状态拨回 pending 再提交管线——管线内部本来就会
    "先清旧 chunk 再写新的"，所以重复执行是安全的（幂等）。
    """
    doc = session.get(Document, doc_id)
    if doc is None:
        raise HTTPException(404, "文档不存在")
    if not Path(doc.stored_path).exists():
        raise HTTPException(400, "原文件已不存在，无法重新入库")
    doc.status = "pending"
    doc.error = None
    session.add(doc)
    session.commit()
    ingestion.submit_ingest(doc.id)
    return {"ok": True}


@router.get("/{doc_id}/chunks")
def list_chunks(
    doc_id: str,
    page: int = 1,   # 第几页（从 1 开始）
    size: int = 10,  # 每页几条
    session: Session = Depends(get_session),
) -> dict:
    """chunk 预览：分页展示某文档切出来的所有块（调优切分参数的"眼睛"）。"""
    if session.get(Document, doc_id) is None:
        raise HTTPException(404, "文档不存在")
    total = session.exec(
        select(func.count()).select_from(Chunk).where(Chunk.doc_id == doc_id)
    ).one()
    rows = session.exec(
        select(Chunk)
        .where(Chunk.doc_id == doc_id)
        .order_by(Chunk.seq)
        .offset((page - 1) * size)
        .limit(size)
    ).all()
    return {
        "total": total,
        "page": page,
        "size": size,
        "chunks": [
            {
                "seq": r.seq,
                "heading_path": r.heading_path,
                "page_no": r.page_no,
                "content": r.content,
            }
            for r in rows
        ],
    }

"""
会话与问答 API：会话的增删查 + SSE 流式问答。

数据流位置：前端"对话"页的后端入口。

SSE（Server-Sent Events）知识点：
- 一种服务器向浏览器"持续推消息"的 HTTP 长连接协议，格式是
  一行 `data: {...}\n\n`；
- 比 WebSocket 简单得多，而且是单向的（服务器→浏览器）——
  正好匹配"流式回答"这种只需要服务器往外推的场景；
- 前端用 fetch + ReadableStream 逐行解析（见 frontend/src/api.ts）。
"""

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from app.db import get_session
from app.models import ChatMessage, ChatSession
from app.services import chat as chat_service

router = APIRouter(prefix="/api/chat", tags=["chat"])


# ---------- 请求体定义（pydantic 自动做校验和文档） ----------

class AskRequest(BaseModel):
    question: str


class RenameRequest(BaseModel):
    title: str


# ---------- 会话 CRUD ----------

@router.get("/sessions")
def list_sessions(session: Session = Depends(get_session)) -> list[dict]:
    """会话列表（新的在前），渲染在聊天页左侧栏。"""
    rows = session.exec(
        select(ChatSession).order_by(ChatSession.updated_at.desc())
    ).all()
    return [
        {"id": s.id, "title": s.title, "updated_at": s.updated_at.isoformat()}
        for s in rows
    ]


@router.post("/sessions")
def create_session(session: Session = Depends(get_session)) -> dict:
    """新建一个空会话。"""
    s = ChatSession(id=uuid.uuid4().hex)
    session.add(s)
    session.commit()
    return {"id": s.id, "title": s.title, "updated_at": s.updated_at.isoformat()}


@router.patch("/sessions/{session_id}")
def rename_session(
    session_id: str, req: RenameRequest, session: Session = Depends(get_session)
) -> dict:
    """重命名会话（侧栏 ✎ 按钮调用）。"""
    s = session.get(ChatSession, session_id)
    if s is None:
        raise HTTPException(404, "会话不存在")
    title = req.title.strip()
    if not title:
        raise HTTPException(400, "标题不能为空")
    s.title = title[:50]  # 防御性截断，避免侧栏被超长标题撑爆
    session.add(s)
    session.commit()
    return {"ok": True, "id": s.id, "title": s.title}


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str, session: Session = Depends(get_session)) -> dict:
    """删会话（连同消息一起删）。"""
    s = session.get(ChatSession, session_id)
    if s is None:
        raise HTTPException(404, "会话不存在")
    for m in session.exec(select(ChatMessage).where(ChatMessage.session_id == session_id)):
        session.delete(m)
    session.delete(s)
    session.commit()
    return {"ok": True}


@router.get("/sessions/{session_id}/messages")
def list_messages(session_id: str, session: Session = Depends(get_session)) -> list[dict]:
    """历史消息（打开旧会话时回填聊天窗口）。"""
    if session.get(ChatSession, session_id) is None:
        raise HTTPException(404, "会话不存在")
    rows = session.exec(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.id)
    ).all()
    return [
        {
            "role": m.role,
            "content": m.content,
            "sources": json.loads(m.sources) if m.sources else [],
            # trace（链路数据）只存在于助手消息上；旧消息可能没有
            "trace": json.loads(m.trace) if m.trace else None,
        }
        for m in rows
    ]


# ---------- 流式问答 ----------

@router.post("/sessions/{session_id}/ask")
def ask(session_id: str, req: AskRequest, session: Session = Depends(get_session)) -> StreamingResponse:
    """提问并流式返回回答（SSE）。

    注意：不用 async def——管线里有 CPU 密集的计算和阻塞 IO，
    放在线程池里跑（FastAPI 对同步路由函数的默认处理）反而更稳。
    """
    s = session.get(ChatSession, session_id)
    if s is None:
        raise HTTPException(404, "会话不存在")
    if not req.question.strip():
        raise HTTPException(400, "问题不能为空")

    # 第一个问题时自动把会话标题改成问题前缀（像主流聊天产品那样）
    if s.title == "新对话":
        s.title = req.question.strip()[:20]
        session.add(s)
        session.commit()

    def event_stream():
        """把 chat_service 的生成器逐条转成 SSE 格式的字节流。"""
        try:
            for event in chat_service.stream_answer(session_id, req.question):
                # SSE 标准格式：每条消息以 "data: " 开头、空行结尾
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as exc:  # noqa: BLE001
            yield f'data: {json.dumps({"type": "error", "message": str(exc)[:500]})}\n\n'

    return StreamingResponse(event_stream(), media_type="text/event-stream")

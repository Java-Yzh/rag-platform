"""
问答服务：把"检索到的 chunks + 用户问题"组装成提示词，流式生成带引用的回答。

数据流位置：RAG 的最后一棒——
    问题 + 历史 ──> retrieval.py(检索) ──> 本文件(组装提示词) ──> GLM 流式回答

回答要求模型用 [1] [2] 标注引用，前端据此渲染来源卡片。
"""

import json
from collections.abc import Generator

from langchain_core.prompts import ChatPromptTemplate
from sqlmodel import Session, select

from app.db import engine
from app.models import ChatMessage, Document
from app.services import retrieval

# ---------- 提示词 ----------

ANSWER_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "你是文档问答助手。只依据下面提供的【参考材料】回答问题，"
            "并在使用某条材料时用 [编号] 标注引用（如 [1]）。"
            "即使你本身知道答案，材料里没有的内容也必须回答不知道，"
            "禁止使用参考材料之外的知识。\n\n"
            "【参考材料】\n{context}",
        ),
        ("placeholder", "{chat_history}"),
        ("human", "{question}"),
    ]
)

# 知识库未命中时的固定回复。
# 为什么不交给大模型说这句话：实测模型对"空材料"的服从性不稳定，
# 有时会转头用自己的世界知识回答（比如背菜谱）——固定话术从根上杜绝
NO_HIT_ANSWER = (
    "🤷 知识库中没有找到与这个问题相关的内容。\n\n"
    "可以换个问法试试，或先把相关文档上传到「文档管理」页再来提问。"
)


def _format_context(docs: list) -> tuple[str, list[dict]]:
    """把检索到的 chunks 拼成提示词里的参考材料，同时收集来源信息。

    返回：(拼好的文本, 来源列表)。来源列表会存库并回传前端渲染引用卡片。
    """
    parts: list[str] = []
    sources: list[dict] = []
    for i, d in enumerate(docs, start=1):
        meta = d.metadata or {}
        parts.append(f"[{i}] {d.page_content}")
        sources.append(
            {
                "index": i,
                "doc": meta.get("doc_name", "未知文档"),
                "heading": meta.get("heading_path", ""),
                "page": meta.get("page_no"),
                "snippet": d.page_content[:150],  # 卡片上只展示开头 150 字
            }
        )
    return "\n\n".join(parts), sources


def _attach_doc_names(docs: list) -> list:
    """给每个检索结果的 metadata 补上文档名（向量库里只存了 doc_id）。"""
    with Session(engine) as session:
        ids = {d.metadata.get("doc_id") for d in docs if d.metadata}
        rows = session.exec(select(Document).where(Document.id.in_(ids))).all()
        names = {d.id: d.filename for d in rows}
    for d in docs:
        if d.metadata:
            d.metadata["doc_name"] = names.get(d.metadata.get("doc_id"), "未知文档")
    return docs


def _history_messages(session_id: str, limit: int = 8) -> list:
    """取最近几轮对话转成 LangChain 消息格式（供改写和回答使用）。

    只带最近 limit 条：历史太长既慢又稀释重点。
    """
    from langchain_core.messages import AIMessage, HumanMessage

    with Session(engine) as session:
        rows = session.exec(
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.id.desc())  # type: ignore[arg-type]  # id 可能为 None，运行时恒非空
            .limit(limit)
        ).all()
    # 数据库取出来是"新在前"，翻转变"旧在前"符合对话顺序
    rows.reverse()
    return [
        HumanMessage(content=r.content) if r.role == "user" else AIMessage(content=r.content)
        for r in rows
    ]


def stream_answer(
    session_id: str, question: str
) -> Generator[dict, None, None]:
    """流式问答主流程（生成器，逐条 yield 事件给 API 层转发）。

    事件协议（SSE 推给前端）：
      {"type": "sources", ...}   检索完成，先推来源列表
      {"type": "token", ...}     每个 token 推一次
      {"type": "done", ...}      回答结束，带上消息 id
      {"type": "error", ...}     出错
    """
    try:
        # ① 带着历史做检索（内部会先改写问题再混合检索）
        # 注意键名是 input 而不是 question——langchain 1.x 的约定
        retriever = retrieval.build_history_aware_retriever()
        history = _history_messages(session_id)
        docs = retriever.invoke({"input": question, "chat_history": history})
        docs = _attach_doc_names(docs)

        # ② 组装来源信息并推给前端
        context, sources = _format_context(docs)
        yield {"type": "sources", "sources": sources}

        # ③ 知识库未命中：固定话术直接返回，不调大模型
        #    （防止模型无视空材料、用自己的世界知识作答）
        if not docs:
            _save_turn(session_id, question, NO_HIT_ANSWER, sources)
            yield {"type": "token", "content": NO_HIT_ANSWER}
            yield {"type": "done", "answer": NO_HIT_ANSWER}
            return

        # ④ 组装提示词
        prompt_value = ANSWER_PROMPT.invoke(
            {"context": context, "question": question, "chat_history": history}
        )

        # ⑤ 流式生成。stream() 每次吐出一小段文本（chunk.content）
        answer_parts: list[str] = []
        for chunk in retrieval.chat_llm.stream(prompt_value.to_messages()):
            if chunk.content:
                answer_parts.append(chunk.content)
                yield {"type": "token", "content": chunk.content}

        answer = "".join(answer_parts)

        # ⑥ 把这一问一答存进会话历史
        _save_turn(session_id, question, answer, sources)

        yield {"type": "done", "answer": answer}
    except Exception as exc:  # noqa: BLE001
        yield {"type": "error", "message": str(exc)[:500]}


def _save_turn(session_id: str, question: str, answer: str, sources: list[dict]) -> None:
    """把一轮问答（用户问题 + 助手回答）写入会话历史。"""
    with Session(engine) as session:
        session.add(ChatMessage(session_id=session_id, role="user", content=question))
        session.add(
            ChatMessage(
                session_id=session_id,
                role="assistant",
                content=answer,
                sources=json.dumps(sources, ensure_ascii=False) if sources else None,
            )
        )
        session.commit()

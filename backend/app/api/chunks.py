"""
Chunk 管理 API：跨文档检索浏览 + 手工修正 chunk 文本（带向量同步）。

数据流位置：Chunk 管理页的后端入口。
与文档页的"重新入库"（整条管线重跑）互补——这里是精细化的单块手术。

RAG 知识点——为什么改完 chunk 必须"三处同步"：
chunk 的文本存在三个地方：SQLite（事实源）、Chroma（语义向量）、
BM25（关键词索引）。只改 SQLite 的话，向量还是旧文本的语义、
关键词还是旧文本的分词，检索结果就和展示内容错位了。
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, func, select

from app.db import get_session
from app.models import Chunk, Document
from app.storage import bm25, chroma

router = APIRouter(prefix="/api/chunks", tags=["chunks"])


class EditChunkRequest(BaseModel):
    content: str


class OptimizeRequest(BaseModel):
    """AI 优化请求：传当前编辑器里的文本（可能已含手工修改），而非库里的旧值"""

    content: str
    heading_path: str = ""  # 所属章节，给模型提供改写语境


# 优化提示词：目标是"检索友好"——结构工整（向量化时语义更集中）、
# 表述准确（关键词命中更稳），同时严禁增删事实（chunk 是知识库的事实载体）
OPTIMIZE_PROMPT = """你是 RAG 系统的文本清洗专家。请优化下面的文本块（chunk），使其更适合检索：

要求：
1. 保持原意不变，不得新增原文没有的信息；可修正错别字、修复乱码和断裂的句子
2. 结构更工整：适当分段，条目化内容用清晰的 Markdown 列表表达
3. 原样保留专有名词、型号编号、数字、代码——这些是关键词检索的锚点，禁止翻译或改写
4. 表述更准确、简洁

{heading_hint}
【待优化文本】
{content}

只输出优化后的文本本身，不要任何解释，不要用代码围栏包裹。"""


def _strip_code_fences(text: str) -> str:
    """防御性清理：模型偶尔不听话用 ``` 围栏包裹输出，剥掉首尾围栏行。"""
    t = text.strip()
    if t.startswith("```"):
        lines = t.split("\n")
        # 去掉第一行（```xxx）和最后一行（```）
        if len(lines) >= 2 and lines[-1].strip().startswith("```"):
            t = "\n".join(lines[1:-1]).strip()
    return t


@router.post("/optimize")
def optimize_chunk(req: OptimizeRequest) -> dict:
    """AI 优化 chunk 文本——无状态接口：只返回优化结果，不落库不嵌向量。

    保存与否完全由用户在编辑器里决定（点保存才走 PATCH 的三处同步），
    这样优化后还能继续手工微调。
    """
    if not req.content.strip():
        raise HTTPException(400, "内容不能为空")
    heading_hint = f"（该块所属章节：{req.heading_path}，优化时保持语境一致）" if req.heading_path else ""
    prompt = OPTIMIZE_PROMPT.format(heading_hint=heading_hint, content=req.content)

    from app.services.providers import make_chat_llm

    # 低温度 + 独立分配的模型（设置页可配"AI 优化"专用模型）
    result = make_chat_llm(role="optimize", temperature=0.2).invoke(prompt).content
    return {"content": _strip_code_fences(result)}


@router.get("")
def list_chunks(
    doc_id: str | None = None,  # 按文档过滤（文件名筛选在前端转成 doc_id）
    q: str = "",                # 内容关键词（LIKE 模糊匹配）
    page: int = 1,
    size: int = 10,
    session: Session = Depends(get_session),
) -> dict:
    """跨文档 chunk 列表：doc_id 过滤 + 内容搜索 + 分页，返回所属文件名。

    join Document 表是为了在列表里直接显示"这块属于哪个文件"，
    并按文件名排序让浏览顺序稳定。
    """
    base = select(Chunk, Document).join(Document, Chunk.doc_id == Document.id)  # type: ignore[arg-type]
    if doc_id:
        base = base.where(Chunk.doc_id == doc_id)
    if q.strip():
        # .contains() 生成 SQL 的 LIKE '%q%'（SQLite 对中文 LIKE 默认不区分大小写，
        # 对纯中文内容无影响）
        base = base.where(Chunk.content.contains(q.strip()))

    total = session.exec(select(func.count()).select_from(base.subquery())).one()
    rows = session.exec(
        base.order_by(Document.filename, Chunk.seq).offset((page - 1) * size).limit(size)
    ).all()

    return {
        "total": total,
        "page": page,
        "size": size,
        "chunks": [
            {
                "id": c.id,
                "doc_id": c.doc_id,
                "doc_name": d.filename,
                "seq": c.seq,
                "heading_path": c.heading_path,
                "page_no": c.page_no,
                "content": c.content,
                "chars": len(c.content),
            }
            for c, d in rows
        ],
    }


@router.patch("/{chunk_id}")
def edit_chunk(
    chunk_id: int, req: EditChunkRequest, session: Session = Depends(get_session)
) -> dict:
    """手工修正一个 chunk 的正文，并完成三处同步。

    ① SQLite 更新（事实源）→ ② Chroma 删旧向量、新文本重嵌入
    → ③ BM25 全量重建（分词变了）。元数据（章节/页码）不允许改：
    它们是切分管线的产物，手改也会被下次"重新入库"覆盖。
    """
    chunk = session.get(Chunk, chunk_id)
    if chunk is None:
        raise HTTPException(404, "chunk 不存在")
    content = req.content.strip()
    if not content:
        raise HTTPException(400, "内容不能为空")

    # ① 更新事实源
    chunk.content = content
    session.add(chunk)
    session.commit()

    # ② 向量重嵌入（先删后加，原位替换）
    chroma.replace_chunk_vector(
        chunk.doc_id,
        {"seq": chunk.seq, "heading_path": chunk.heading_path, "page_no": chunk.page_no, "content": content},
    )

    # ③ BM25 重建（毫秒级，见 bm25.refresh 注释）
    bm25.refresh()

    return {"ok": True, "id": chunk.id}

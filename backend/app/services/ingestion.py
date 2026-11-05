"""
入库管线：把一份上传的原始文档变成"可检索的知识"。

数据流位置：整个 RAG 系统的上半场，四步流水线——

    原始文件 ──①转换──> 结构化文档 ──②切分──> chunks ──③向量化──> Chroma
                                                      └──④登记──> SQLite + BM25

RAG 知识点——为什么要"切分"（chunking）：
- LLM 的上下文窗口有限，不可能把整份 50 页 PDF 塞给模型；
- 检索粒度太粗（整章）会引入噪声，太细（单句）会丢失上下文；
- 所以切成几百字的小块，检索时只把"最相关的几块"给模型。

结构感知切分（HybridChunker）的好处：它不是每 800 字硬剁一刀，
而是顺着文档自身的结构切——标题边界、段落、表格整块保留，
所以表格不会被拦腰斩断，每个 chunk 还自带"所在章节路径"。
"""

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling_core.transforms.chunker import HybridChunker
from docling_core.transforms.chunker.tokenizer.base import BaseTokenizer
from docling_core.types.doc import DocItemLabel
from sqlmodel import Session, delete, select

from app.config import settings
from app.db import engine
from app.models import Chunk, Document
from app.storage import bm25, chroma

# ---------- Docling 转换器（全局单例，重复创建很浪费） ----------

def _make_converter() -> DocumentConverter:
    """创建 Docling 转换器。唯一可调项是 OCR 开关（默认关，见 config.py 注释）。"""
    pipeline = PdfPipelineOptions()
    pipeline.do_ocr = settings.ocr_enabled
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline)}
    )


_converter = _make_converter()

# 入库线程池：max_workers=1 表示一次只转换一份文档。
# Docling 解析很吃 CPU，串行处理避免多任务同时跑把内存打爆；
# 任务排队交给线程池自动完成，无需自己写队列
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ingest")


def _convert_file(path: Path, ext: str):
    """第①步：原始文件 -> Docling 结构化文档。

    PDF/DOCX/PPTX/XLSX/HTML/MD/CSV 走 Docling；
    .txt 是 Docling 不支持的格式，这里手工包一层结构（纯文本本来就没有结构）。
    """
    if ext == ".txt":
        from docling_core.types.doc import DoclingDocument

        text = path.read_text(encoding="utf-8", errors="ignore")
        doc = DoclingDocument(name=path.stem)
        # 按空行分段（普通文本最自然的结构单位）
        for para in re.split(r"\n\s*\n", text):
            if para.strip():
                doc.add_text(label=DocItemLabel.PARAGRAPH, text=para.strip())
        return doc
    # convert() 返回 ConversionResult，真正的文档在 .document 属性上
    return _converter.convert(str(path)).document


class CharTokenizer(BaseTokenizer):
    """按字符数计数的极简 tokenizer（离线可用，零依赖）。

    为什么不用官方的 HuggingFaceTokenizer：它要从 HF 下载分词器模型，
    网络不通就卡死。而我们的目的只是"限制每块别太长"——对中文来说，
    字符数和 token 数本来就很接近（1 个汉字 ≈ 1~1.5 token），
    用字符计数做近似完全够用。
    """

    _max: int  # pydantic 模型允许的私有字段声明

    def __init__(self, max_tokens: int, **kwargs):
        super().__init__(**kwargs)
        self._max = max_tokens

    def count_tokens(self, text: str) -> int:
        return len(text)

    def get_max_tokens(self) -> int:
        return self._max

    def get_tokenizer(self) -> object:
        return self


def _chunk_document(doc) -> list[dict]:
    """第②步：结构化文档 -> chunk 列表（HybridChunker 结构感知切分）。"""
    chunker = HybridChunker(
        tokenizer=CharTokenizer(max_tokens=settings.chunk_max_tokens),
        merge_peers=settings.chunk_merge_peers,
    )
    chunks: list[dict] = []
    for seq, chunk in enumerate(chunker.chunk(doc)):
        # meta.headings：这个 chunk 所属的标题层级，如 ["第二章 计费", "2.1 单价"]
        headings = list(getattr(chunk.meta, "headings", None) or [])
        heading_path = " > ".join(h if isinstance(h, str) else getattr(h, "text", "") for h in headings)
        # 页码：从 chunk 关联的原文条目里取第一个来源的页码（MD/HTML 没有页码，取 None）
        page_no = None
        for item in getattr(chunk.meta, "doc_items", None) or []:
            if getattr(item, "prov", None):
                page_no = item.prov[0].page_no
                break
        chunks.append(
            {
                "seq": seq,
                "heading_path": heading_path,
                "page_no": page_no,
                # chunk.text 已经把表格转成 Markdown 形式拼进正文
                "content": chunk.text,
            }
        )
    return chunks


def _ingest_one(doc_id: str) -> None:
    """执行一份文档的完整管线（在后台线程里跑）。

    任何一步抛异常都会被捕获并记到 Document.error 上——
    用户在前端能看到失败原因，而不是永远卡在"转换中"。
    """
    with Session(engine) as session:
        doc = session.get(Document, doc_id)
        if doc is None:
            return
        try:
            doc.status = "processing"
            doc.error = None
            session.add(doc)
            session.commit()

            # ① 转换 ② 切分
            dl_doc = _convert_file(Path(doc.stored_path), doc.ext)
            chunks = _chunk_document(dl_doc)
            if not chunks:
                raise RuntimeError("转换后没有解析出任何文本（可能是空文件或纯图片 PDF）")

            # ③ 写向量库（带 doc_id 元数据，删除时按它清理）
            chroma.add_chunks(doc.id, chunks)

            # ④ 登记进 SQLite（事实源）：先清旧 chunk 再写新的，重入库也走这条路径
            # delete() 是 SQLAlchemy 的批量删除语句，比逐条查出来再删快
            session.exec(delete(Chunk).where(Chunk.doc_id == doc.id))
            for c in chunks:
                session.add(Chunk(doc_id=doc.id, **c))
            doc.chunk_count = len(chunks)
            doc.status = "done"
            session.add(doc)
            session.commit()

            # BM25 索引刷新（放在事务提交后，保证索引和数据库一致）
            bm25.refresh()
        except Exception as exc:  # noqa: BLE001 —— 管线里任何错误都要落到状态上
            doc.status = "failed"
            doc.error = str(exc)[:2000]
            session.add(doc)
            session.commit()


def submit_ingest(doc_id: str) -> None:
    """把文档丢进后台线程池。立即返回，前端轮询状态看进度。"""
    _executor.submit(_ingest_one, doc_id)


def md5_of_file(path: Path, bufsize: int = 1 << 20) -> str:
    """计算文件 MD5 指纹（读 1MB 分块，避免大文件一次性读进内存）。"""
    h = hashlib.md5()  # noqa: S324 —— 只做重复检测指纹，不用于安全场景
    with open(path, "rb") as f:
        while block := f.read(bufsize):
            h.update(block)
    return h.hexdigest()


def delete_document(doc_id: str) -> bool:
    """级联删除一份文档的所有痕迹：SQLite 行 + 向量 + BM25 索引 + 原文件。"""
    with Session(engine) as session:
        doc = session.get(Document, doc_id)
        if doc is None:
            return False
        # SQLite：先删子表（chunks）再删主表（document），顺序不能反
        for chunk in session.exec(select(Chunk).where(Chunk.doc_id == doc_id)):
            session.delete(chunk)
        session.delete(doc)
        session.commit()
    # 向量库清数据；BM25 直接全量重建（见 bm25.refresh 注释）
    chroma.delete_doc(doc_id)
    bm25.refresh()
    # 原文件（文件不存在也不报错，missing_ok=True）
    Path(doc.stored_path).unlink(missing_ok=True)
    return True

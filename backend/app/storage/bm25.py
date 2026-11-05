"""
BM25 关键词索引：混合检索的另一半。

数据流位置：启动时从 SQLite 的 chunks 表重建（内存索引）；
之后每次入库/删除/重入库文档后刷新。

RAG 知识点——为什么要 BM25 和向量检索"混着用"：
- 向量检索擅长"意思相近"：问"退货政策"能找到"不支持退款"；
- BM25 擅长"字面精确"：问"ZG-2024-887 号合同"这种型号/编号/人名，
  向量经常输给关键词匹配。
- 两者互补，所以混合检索几乎是中文 RAG 的标配。

实现说明：这里自写 JiebaBM25Retriever（约 50 行）而不是用
langchain-community 的 BM25Retriever——后者所在的包已进入停止维护状态，
官方迁移指引是"未维护的集成就 fork/自建"。算法本身用经典的
rank_bm25 库（我们本来就依赖它），中文分词用 jieba。

BM25 知识点：它是经典的"关键词打分"算法（可以理解为更聪明的 Ctrl+F，
按词频和稀有度给文档打分）。
"""

import threading

import jieba
from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document as LCDocument
from langchain_core.retrievers import BaseRetriever
from rank_bm25 import BM25Okapi
from sqlmodel import Session, select

from app.config import settings
from app.db import engine
from app.models import Chunk


def _tokenize(text: str) -> list[str]:
    """把中文文本切成词列表，BM25 按词打分的前提。
    lcut_for_search 会对长词再细切，对检索场景更友好。"""
    return jieba.lcut_for_search(text)


class JiebaBM25Retriever(BaseRetriever):
    """自实现的 BM25 检索器：实现 LangChain 的 BaseRetriever 接口。

    继承 BaseRetriever 后就能接入 LangChain 的检索编排组件；
    核心是 search_with_scores()（带分数），_get_relevant_documents 是
    接口要求的入口，直接委托给前者。
    """

    docs: list[LCDocument] = []       # 全量 chunk（保留原文和元数据）
    corpus_tokens: list[list[str]] = []  # 与 docs 一一对应的分词结果
    k: int = 6                         # 返回 top-k

    def search_with_scores(self, query: str) -> list[tuple[LCDocument, float]]:
        """打分检索：返回 [(Document, 分数)]，分数>0，按分数降序。

        BM25 的原始分数没有绝对意义（取值范围随语料变化），所以
        混合检索那边用的是"相对阈值"——和本次查询的最高分比。
        """
        if not self.docs:
            return []
        engine_bm25 = BM25Okapi(self.corpus_tokens)
        scores = engine_bm25.get_scores(_tokenize(query))
        # 按分数从高到低取下标；score=0 表示一个词都没命中，直接不要
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        return [
            (self.docs[i], float(scores[i]))
            for i in ranked[: self.k]
            if scores[i] > 0
        ]

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[LCDocument]:
        """LangChain 检索器接口入口：只要文档不要分数。"""
        return [d for d, _ in self.search_with_scores(query)]


class _EmptyRetriever(BaseRetriever):
    """空库时的哑检索器：库里还没有任何 chunk 时顶替 BM25，永远返回空结果。

    为什么需要它：rank-bm25 对空语料会除零崩溃；而检索链要求
    每一个成员都是"检索器"对象。空结果对融合检索是合法输入。
    """

    def search_with_scores(self, query: str) -> list[tuple[LCDocument, float]]:
        return []

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[LCDocument]:
        return []


# ---------- 索引管理 ----------
# 全局索引 + 一把锁。锁的作用：防止两个入库任务同时重建索引把数据写坏
# （并发编程常识：多个线程改同一份数据前要先拿锁）
_index: JiebaBM25Retriever | _EmptyRetriever | None = None
_lock = threading.Lock()


def _build_index() -> JiebaBM25Retriever | _EmptyRetriever:
    """从 SQLite 的 chunks 表全量构建 BM25 索引。"""
    with Session(engine) as session:
        rows = session.exec(select(Chunk)).all()

    if not rows:
        return _EmptyRetriever()  # 空库：返回哑检索器而不是崩溃

    # LangChain 检索器操作的对象叫 Document（page_content + metadata）
    docs = [
        LCDocument(
            page_content=r.content,
            metadata={
                "doc_id": r.doc_id,
                "seq": r.seq,
                "heading_path": r.heading_path,
                "page_no": r.page_no,
            },
        )
        for r in rows
    ]
    return JiebaBM25Retriever(
        docs=docs,
        corpus_tokens=[_tokenize(d.page_content) for d in docs],
        k=settings.top_k,
    )


def refresh() -> None:
    """重建整个 BM25 索引。文档量是几百份级别，全量重建毫秒级，足够快。"""
    global _index
    with _lock:
        _index = _build_index()


def get_retriever() -> JiebaBM25Retriever | _EmptyRetriever:
    """拿当前 BM25 检索器；还没建过就现场建一次（懒加载）。"""
    global _index
    if _index is None:
        refresh()
    assert _index is not None
    return _index

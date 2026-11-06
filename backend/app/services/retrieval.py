"""
检索层：给定一个问题（+对话历史），找出"真正相关"的几个 chunk——并把全过程记录成 trace。

数据流位置：RAG 的下半场入口——
    用户提问 ──改写──> 混合检索 ──把关──> 相关 chunks ──> 交给 chat.py 组装提示词
         └────────────── 每一步的输入输出都记进 trace（调试/调优的"黑匣子"）──────┘

三个核心机制：

1) 历史感知改写（手写版）
   多轮对话里，用户常问"那第二条呢？"这种指代前文的短问题，
   直接拿去检索会查偏。所以先用 LLM 结合历史把问题改写成独立完整的问题。
   （首轮没有历史时原样返回，省一次调用）

2) 混合检索 + LLM 相关性把关（CRAG 思想）
   向量检索（语义）+ BM25（关键词）各取 top-k，RRF 融合后得到候选；
   再让一次廉价的 LLM 调用判断"这些候选能否回答问题"，全不相关
   就清空结果。为什么不用分数阈值：实测 embedding-3 对相关/无关文本
   的相似度区间几乎重叠（0.40~0.50），阈值无法区分；LLM 判断更稳健。

3) RRF 融合（Reciprocal Rank Fusion，手写约 10 行）
   两种分数量纲不同没法直接比，所以只比较"排名"：
   每个 chunk 在每个榜单里的排名 r 贡献 1/(k+r) 分（k=60 是通用常数），
   在两个榜单都靠前的 chunk 总分最高。简单且有效。
"""

import time

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document as LCDocument
from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.retrievers import BaseRetriever
from langchain_openai import ChatOpenAI

from app.config import settings

# 问题改写提示词：要求输出一个"不依赖上下文也能看懂"的问题
CONDENSE_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "根据对话历史，把用户的最新问题改写成一个独立、完整、"
            "不使用代词（如'它/第二条/上面提到的'）的检索查询。"
            "只需输出改写后的问题本身，不要任何解释。",
        ),
        ("placeholder", "{chat_history}"),
        ("human", "{question}"),
    ]
)


def _make_chat_llm(temperature: float) -> ChatOpenAI:
    """创建一个指向智谱的对话模型客户端。

    问答用 temperature=0.1（要稳），改写/把关用 0（确定性任务）。
    """
    return ChatOpenAI(
        model=settings.chat_model,
        api_key=settings.zhipuai_api_key,
        base_url=settings.zhipu_base_url,
        temperature=temperature,
        # streaming 用默认配置：.stream() 调用时自动逐 token 返回
    )


# 对话模型全局单例（流式生成回答用它）
chat_llm: BaseChatModel = _make_chat_llm(temperature=0.1)


def rewrite_query(question: str, history: list) -> str:
    """历史感知改写（手写版，替代 langchain 的 create_history_aware_retriever）。

    为什么要手写：需要把"改写后的问题"采集进 trace 给前端展示，
    而官方封装把这一步藏在内部拿不出来。首轮无历史直接返回，省一次调用。
    """
    if not history:
        return question
    # temperature=0：改写是确定性任务，不需要创造性
    llm = _make_chat_llm(temperature=0)
    messages = CONDENSE_PROMPT.invoke({"question": question, "chat_history": history})
    return llm.invoke(messages).content.strip()


def _rrf_fuse(ranked_lists: list[list[LCDocument]], k: int) -> list[LCDocument]:
    """RRF 融合：多份排名榜单 → 一份去重后的总排名。

    scores 字典累计每个 chunk 的 1/(60+排名) 总分；
    chunk 的身份用 "doc_id:seq" 标识（同一块在两边榜单只算一次）。
    """
    scores: dict[str, float] = {}
    docs: dict[str, LCDocument] = {}
    for ranked in ranked_lists:
        for rank, d in enumerate(ranked, start=1):
            key = f"{d.metadata.get('doc_id')}:{d.metadata.get('seq')}"
            docs[key] = d
            scores[key] = scores.get(key, 0.0) + 1.0 / (60 + rank)
    # 按总分从高到低，取前 k 个
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [docs[key] for key, _ in ordered[:k]]


def _hit(d: LCDocument, score: float) -> dict:
    """把一条检索命中整理成 trace 里的精简格式（带节选，便于人看）。"""
    return {
        "doc_id": d.metadata.get("doc_id", ""),
        "seq": d.metadata.get("seq"),
        "heading": d.metadata.get("heading_path", ""),
        "score": round(float(score), 3),
        "excerpt": d.page_content[:60].replace("\n", " "),
    }


def _grade_relevance(query: str, docs: list[LCDocument]) -> tuple[bool, str]:
    """用一次廉价的 LLM 调用判断候选材料与问题是否相关。

    返回 (是否相关, 模型原始回答)。glm-4-flash 免费且响应快，
    这个额外调用换来的是"无关问题零来源"。
    """
    excerpts = "\n".join(
        f"[{i}] {(d.metadata.get('heading_path') or '')} {d.page_content[:80]}"
        for i, d in enumerate(docs, start=1)
    )
    prompt = (
        f"用户问题：{query}\n\n候选参考材料：\n{excerpts}\n\n"
        '这些材料能否为回答该问题提供有用信息？只回答"是"或"否"。'
    )
    raw = _make_chat_llm(temperature=0).invoke(prompt).content.strip()
    return raw.startswith("是"), raw


class HybridRetriever(BaseRetriever):
    """混合检索器：两路打分 → RRF 融合 → LLM 相关性把关，全程留痕。

    retrieve_with_trace() 是核心入口；_get_relevant_documents 是
    LangChain 检索器接口要求的入口，直接委托前者。
    """

    top_k: int = 6

    def retrieve_with_trace(self, query: str) -> tuple[list[LCDocument], dict]:
        """检索并返回 (最终通过的文档列表, 全过程 trace)。"""
        from app.storage import bm25, chroma

        timings: dict[str, float] = {}

        # ① 向量侧 top-k（相似度分数只做参考展示，不做阈值——见文件头注释）
        t0 = time.perf_counter()
        vec_hits = chroma.search_with_scores(query, k=self.top_k)
        timings["vector_ms"] = round((time.perf_counter() - t0) * 1000)

        # ② BM25 侧 top-k（search_with_scores 已过滤掉零命中的块）
        t0 = time.perf_counter()
        bm_hits = bm25.get_retriever().search_with_scores(query)
        timings["bm25_ms"] = round((time.perf_counter() - t0) * 1000)

        # ③ RRF 融合两份排名
        vec_docs = [d for d, _ in vec_hits]
        bm_docs = [d for d, _ in bm_hits]
        fused = _rrf_fuse([bm_docs, vec_docs], k=self.top_k)

        # ④ LLM 相关性把关：全都不相关就清空（前端来源卡片随之消失）
        gate = {"enabled": settings.relevance_gate, "relevant": None, "raw": ""}
        if fused and settings.relevance_gate:
            t0 = time.perf_counter()
            relevant, raw = _grade_relevance(query, fused)
            timings["gate_ms"] = round((time.perf_counter() - t0) * 1000)
            gate["relevant"], gate["raw"] = relevant, raw
            if not relevant:
                fused = []

        trace = {
            "vector": [_hit(d, s) for d, s in vec_hits],
            "bm25": [_hit(d, s) for d, s in bm_hits],
            # 融合后、把关前的候选顺序（doc_id:seq 形式）
            "fused": [f"{d.metadata.get('doc_id')}:{d.metadata.get('seq')}" for d in fused],
            "gate": gate,
            "timings_ms": timings,
        }
        return fused, trace

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[LCDocument]:
        """LangChain 检索器接口入口：只要文档不要 trace。"""
        return self.retrieve_with_trace(query)[0]

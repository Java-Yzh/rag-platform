"""
检索层：给定一个问题（+对话历史），找出"真正相关"的几个 chunk。

数据流位置：RAG 的下半场入口——
    用户提问 ──(可能先改写)──> 混合检索 ──> 过阈值的相关 chunks ──> 交给 chat.py 组装提示词

三个核心机制：

1) 混合检索 + LLM 相关性把关（CRAG 思想）
   向量检索（语义）+ BM25（关键词）各取 top-k，RRF 融合后得到候选；
   再让一次廉价的 LLM 调用判断"这些候选能否回答问题"，全不相关
   就清空结果——问知识库外的问题时来源列表为空，模型也明确回答
   "知识库中没有相关内容"。
   为什么不用分数阈值：实测 embedding-3 对相关/无关文本的相似度
   区间几乎重叠（0.40~0.50），阈值无法区分；LLM 判断更稳健。

2) RRF 融合（Reciprocal Rank Fusion，手写约 10 行）
   两种分数量纲不同没法直接比，所以只比较"排名"：
   每个 chunk 在每个榜单里的排名 r 贡献 1/(k+r) 分（k=60 是通用常数），
   在两个榜单都靠前的 chunk 总分最高。简单且有效。

3) 历史感知检索（history-aware）
   多轮对话里，用户常问"那第二条呢？"这种指代前文的短问题，
   直接拿去检索会查偏。所以先用 LLM 结合历史把问题"改写"成
   独立完整的问题，再去检索。
"""

from langchain_classic.chains.history_aware_retriever import (
    create_history_aware_retriever,
)
from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document as LCDocument
from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.retrievers import BaseRetriever
from langchain_openai import ChatOpenAI

from app.config import settings


def _make_chat_llm(temperature: float) -> ChatOpenAI:
    """创建一个指向智谱的对话模型客户端。

    问答用 temperature=0.1（要稳），问题改写用 0（确定性任务）。
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


class HybridRetriever(BaseRetriever):
    """混合检索器：两路打分 → RRF 融合 → LLM 相关性把关。

    把关逻辑可由 .env 的 RELEVANCE_GATE 开关控制。
    """

    top_k: int = 6

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[LCDocument]:
        # 延迟导入：storage 层引用了本文件的 chat_llm 所在模块链
        from app.storage import bm25, chroma

        # ① 向量侧 top-k（相似度分数只做参考，不做阈值——见文件头注释）
        vec_docs = [d for d, _ in chroma.search_with_scores(query, k=self.top_k)]

        # ② BM25 侧 top-k（search_with_scores 已过滤掉零命中的块）
        bm_docs = [d for d, _ in bm25.get_retriever().search_with_scores(query)]

        # ③ RRF 融合两份排名
        fused = _rrf_fuse([bm_docs, vec_docs], k=self.top_k)
        if not fused or not settings.relevance_gate:
            return fused  # 空结果或把关功能关闭，原样返回

        # ④ LLM 相关性把关：全都不相关就返回空（来源卡片随之消失）
        return fused if _grade_relevance(query, fused) else fused[:0]


def _grade_relevance(query: str, docs: list[LCDocument]) -> bool:
    """用一次廉价的 LLM 调用判断候选材料与问题是否相关。

    返回 True=至少有材料能提供有用信息，False=全部无关。
    glm-4-flash 免费且响应快，这个额外调用换来的是"无关问题零来源"。
    """
    excerpts = "\n".join(
        f"[{i}] {(d.metadata.get('heading_path') or '')} {d.page_content[:80]}"
        for i, d in enumerate(docs, start=1)
    )
    prompt = (
        f"用户问题：{query}\n\n候选参考材料：\n{excerpts}\n\n"
        '这些材料能否为回答该问题提供有用信息？只回答"是"或"否"。'
    )
    resp = _make_chat_llm(temperature=0).invoke(prompt)
    return resp.content.strip().startswith("是")


def build_history_aware_retriever() -> BaseRetriever:
    """包一层"问题改写"逻辑，返回的检索器能接收 chat_history。

    用法：retriever.invoke({"input": "那第二条呢", "chat_history": [...]})
    它会先让 LLM 把问题改写完整，再送去混合检索。
    """
    # 改写提示词：要求输出一个"不依赖上下文也能看懂"的问题
    # 注意两个 langchain 1.x 的变化：
    #   1. 参数名从 condense_question_prompt 改成了 prompt
    #   2. 输入变量必须叫 input（不能再用 question）
    condense_prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "根据对话历史，把用户的最新问题改写成一个独立、完整、"
                "不使用代词（如'它/第二条/上面提到的'）的检索查询。"
                "只需输出改写后的问题本身，不要任何解释。",
            ),
            ("placeholder", "{chat_history}"),
            ("human", "{input}"),
        ]
    )
    # temperature=0：改写是确定性任务，不需要创造性
    rewrite_llm = _make_chat_llm(temperature=0)
    return create_history_aware_retriever(
        rewrite_llm,
        HybridRetriever(top_k=settings.top_k),
        prompt=condense_prompt,
    )

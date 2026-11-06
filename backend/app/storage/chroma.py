"""
向量存储层：Chroma 向量数据库的封装。

数据流位置：入库管线的终点之一（chunk 向量写进来）+ 检索的起点之一（相似度查询）。

RAG 知识点——为什么需要向量库：
- 把文本通过 Embedding 模型压缩成一串数字（向量），语义相近的文本向量距离也近；
- 用户提问时把问题也变成向量，在库里找"距离最近"的 chunk，就完成了语义检索。
- Chroma 是"嵌入式"数据库：不需要单独安装服务，数据落在本地目录 data/chroma/ 里。

模型接入说明：用 langchain-openai（一级维护包）指向智谱的 OpenAI 兼容端点。
这是智谱官方文档推荐的方式；langchain-community 里旧的 ZhipuAI 专用类
已随该包进入停止维护状态，不建议新项目使用。
"""

from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings

from app.config import settings

# 全局单例：Embedding 客户端（负责 文本->向量 的转换）
_embeddings = OpenAIEmbeddings(
    model=settings.embed_model,
    api_key=settings.zhipuai_api_key,
    base_url=settings.zhipu_base_url,
)

# 全局单例：Chroma 向量库（persist_directory 参数让数据落盘，重启不丢）
# hnsw:space=cosine 表示用余弦相似度度量距离（语义检索的常规选择）
_vectorstore = Chroma(
    collection_name="docs",
    embedding_function=_embeddings,
    persist_directory=str(settings.chroma_dir),
    collection_metadata={"hnsw:space": "cosine"},
)

# 统一的检索器接口：向量检索 top-k。
# as_retriever() 把向量库包装成 LangChain 的"检索器"对象，
# 这样后面就能和 BM25 检索器用同一套接口混合使用
retriever = _vectorstore.as_retriever(search_kwargs={"k": settings.top_k})


def search_with_scores(query: str, k: int) -> list[tuple]:
    """带相关性分数的向量检索：返回 [(Document, 分数)]。

    分数就是余弦相似度（0~1，越大越相关）——混合检索用它做
    "及格线"过滤：无关问题能拿到分数分布，低于阈值的块直接丢弃。
    """
    return _vectorstore.similarity_search_with_relevance_scores(query, k=k)


def add_chunks(doc_id: str, chunks: list[dict]) -> None:
    """把一份文档的所有 chunk 写入向量库。

    每个chunk dict 需要带 content/heading_path/page_no/seq。
    metadata 里带上 doc_id 是关键：删除文档时才能按它精准清掉对应向量。
    """
    if not chunks:
        return
    texts = [c["content"] for c in chunks]
    metadatas = [
        {
            "doc_id": doc_id,
            "seq": c["seq"],
            "heading_path": c["heading_path"],
            "page_no": c["page_no"] if c["page_no"] is not None else -1,
        }
        for c in chunks
    ]
    # 每条向量一个确定性 id（doc_id + 序号），重复入库时旧数据会被覆盖而不是翻倍
    ids = [f"{doc_id}:{c['seq']}" for c in chunks]
    _vectorstore.add_texts(texts=texts, metadatas=metadatas, ids=ids)


def delete_doc(doc_id: str) -> None:
    """删除某份文档在向量库里的全部向量。

    用公开 API 两步走：先按 metadata 过滤查出 id 列表，再按 id 删除。
    （绕开对私有属性 _collection 的依赖，升级 langchain-chroma 不容易踩坏）
    """
    hits = _vectorstore.get(where={"doc_id": doc_id}, include=[])
    if hits["ids"]:
        _vectorstore.delete(ids=hits["ids"])


def replace_chunk_vector(doc_id: str, chunk: dict) -> None:
    """重嵌入单个 chunk：删旧向量 → 用新文本重新 embedding 写入。

    使用场景：手工修正了某个 chunk 的文本。此时旧向量还是旧文本的
    语义，必须换掉，否则检索会"文不对向量"。id 仍是确定性的
    doc_id:seq，所以先删后加正好完成原位替换。
    """
    _vectorstore.delete(ids=[f"{doc_id}:{chunk['seq']}"])
    add_chunks(doc_id, [chunk])


def count() -> int:
    """当前向量总数（诊断/验证用，比如删除后确认归零）。"""
    return _vectorstore._collection.count()


def embed_query(text: str) -> list[float]:
    """把一句文本变成向量（给 BM25 之外的地方备用，如调试脚本）。"""
    return _embeddings.embed_query(text)

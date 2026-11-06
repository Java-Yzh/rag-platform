"""
向量存储层：Chroma 向量数据库的封装。

数据流位置：入库管线的终点之一（chunk 向量写进来）+ 检索的起点之一（相似度查询）。

RAG 知识点——为什么需要向量库：
- 把文本通过 Embedding 模型压缩成一串数字（向量），语义相近的文本向量距离也近；
- 用户提问时把问题也变成向量，在库里找"距离最近"的 chunk，就完成了语义检索。
- Chroma 是"嵌入式"数据库：不需要单独安装服务，数据落在本地目录 data/chroma/ 里。

多供应商改造后的两个要点：
1. embedding 客户端不再是模块级单例——向量模型可在设置页随时更换，
   每次操作都通过 providers.make_embeddings() 现场取当前配置；
2. 换向量模型必须 reset_collection()：不同模型的向量维度和语义空间都不同，
   旧向量留着只会错乱（Chroma 遇到维度不符会直接报错）。
"""

import chromadb
from langchain_chroma import Chroma

from app.config import settings
from app.services import providers

COLLECTION = "docs"

# chromadb 原生客户端（全局单例：文件句柄和缓存较重，值得复用）
# 注：PersistentClient 是工厂函数不是类，类型标注要用字符串形式（否则运行时报错）
_client: "chromadb.PersistentClient | None" = None


def _get_client() -> "chromadb.PersistentClient":
    global _client
    if _client is None:
        _client = chromadb.PersistentClient(path=str(settings.chroma_dir))
    return _client


def get_vectorstore() -> Chroma:
    """构造向量库包装。构造很轻（毫秒级），每次现造，embedding 配置变更即刻生效。"""
    return Chroma(
        collection_name=COLLECTION,
        embedding_function=providers.make_embeddings(),
        persist_directory=str(settings.chroma_dir),
        collection_metadata={"hnsw:space": "cosine"},  # 余弦相似度
    )


def reset_collection() -> None:
    """删除整个 collection（下次使用时自动重建，维度归零）。

    换向量模型时调用——见文件头注释第 2 点。删掉的向量需要通过
    "全部重新入库"重建。
    """
    try:
        _get_client().delete_collection(COLLECTION)
    except Exception:  # noqa: BLE001 —— collection 不存在时 delete 会抛错，忽略
        pass


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
    get_vectorstore().add_texts(texts=texts, metadatas=metadatas, ids=ids)


def delete_doc(doc_id: str) -> None:
    """删除某份文档在向量库里的全部向量。

    用公开 API 两步走：先按 metadata 过滤查出 id 列表，再按 id 删除。
    （绕开对私有属性的依赖，升级 langchain-chroma 不容易踩坏）
    """
    hits = get_vectorstore().get(where={"doc_id": doc_id}, include=[])
    if hits["ids"]:
        get_vectorstore().delete(ids=hits["ids"])


def replace_chunk_vector(doc_id: str, chunk: dict) -> None:
    """重嵌入单个 chunk：删旧向量 → 用新文本重新 embedding 写入。

    使用场景：手工修正了某个 chunk 的文本。此时旧向量还是旧文本的
    语义，必须换掉，否则检索会"文不对向量"。id 仍是确定性的
    doc_id:seq，所以先删后加正好完成原位替换。
    """
    vs = get_vectorstore()
    vs.delete(ids=[f"{doc_id}:{chunk['seq']}"])
    ids = [f"{doc_id}:{chunk['seq']}"]
    vs.add_texts(
        texts=[chunk["content"]],
        metadatas=[{
            "doc_id": doc_id,
            "seq": chunk["seq"],
            "heading_path": chunk["heading_path"],
            "page_no": chunk["page_no"] if chunk["page_no"] is not None else -1,
        }],
        ids=ids,
    )


def search_with_scores(query: str, k: int) -> list[tuple]:
    """带相关性分数的向量检索：返回 [(Document, 分数)]。

    分数就是余弦相似度（0~1，越大越相关）。
    """
    return get_vectorstore().similarity_search_with_relevance_scores(query, k=k)


def count() -> int:
    """当前向量总数（诊断/验证用）。不依赖 embedding 配置，任何时候可用。"""
    return _get_client().get_or_create_collection(COLLECTION).count()

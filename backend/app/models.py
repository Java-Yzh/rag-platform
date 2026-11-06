"""
数据模型（ORM）：定义 SQLite 里有哪些表、每张表有哪些字段。

数据流位置：整条链路的"账本"——
  documents 表记录每份上传文档及其入库状态；
  chunks 表存切分后的文本块（chunk 预览、BM25 索引重建都靠它）；
  chat_sessions / chat_messages 表存多轮会话历史。

设计说明：
- 所有表都预留了 user 字段（当前恒为 "local"）。现在用不上，
  以后要加多用户时不需要改表结构。
- chunks 以 SQLite 为"事实源"（最权威的存档），Chroma 向量库和
  BM25 内存索引都是从它派生的，删了随时能重建——这样系统更不容易出
  "两边数据对不上"的问题。
"""

from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    """统一的"当前时间"函数（带时区的 UTC）。写个小函数是避免各处写法不一致。"""
    return datetime.now(timezone.utc)


class Document(SQLModel, table=True):
    """一份上传的原始文档 + 它在入库管线中的进度。

    status 状态机：pending(刚上传) → processing(转换中) → done(已入库) / failed(失败)
    前端文档列表页就是轮询这张表来展示进度的。
    """

    id: str = Field(primary_key=True)          # uuid，同时用作向量库/BM25 里的关联键
    user: str = Field(default="local", index=True)  # 预留：多用户隔离
    filename: str                               # 用户看到的原始文件名
    stored_path: str                            # 服务器上保存的路径（重命名过，避免同名冲突）
    ext: str                                    # 扩展名，如 ".pdf"
    size_bytes: int = 0
    status: str = Field(default="pending", index=True)
    error: str | None = None                    # 失败原因，展示给前端
    chunk_count: int = 0                        # 切出了多少块，入库完成后更新
    md5: str = ""                               # 文件指纹，方便排查重复上传
    created_at: datetime = Field(default_factory=utcnow)


class Chunk(SQLModel, table=True):
    """切分后的一个文本块。

    heading_path 记录这块内容在原文档中的"章节路径"（如 "第二章 > 2.1 定价"），
    page_no 记录页码——引用来源卡片上展示的就是这些信息。
    """

    id: int | None = Field(default=None, primary_key=True)  # 自增主键
    doc_id: str = Field(index=True, foreign_key="document.id")
    seq: int = 0                                # 这块在文档中的顺序（第几块）
    heading_path: str = ""                      # 所在章节路径
    page_no: int | None = None                  # 所在页码（Markdown/HTML 等无页码概念时为空）
    content: str = ""                           # 块的正文


class ChatSession(SQLModel, table=True):
    """一次"对话"（左侧栏里的一个会话条目）。"""

    id: str = Field(primary_key=True)
    user: str = Field(default="local", index=True)  # 预留：多用户隔离
    title: str = "新对话"                        # 取第一句提问的前缀自动生成
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ChatMessage(SQLModel, table=True):
    """对话里的一条消息（用户提问或助手回答）。

    sources 字段存 JSON 字符串：助手回答引用了哪些 chunk（文档名/章节/页码/片段），
    前端把它渲染成回答下方的"来源卡片"。
    """

    id: int | None = Field(default=None, primary_key=True)
    session_id: str = Field(index=True, foreign_key="chatsession.id")
    role: str                                    # "user" 或 "assistant"
    content: str = ""
    sources: str | None = None                   # JSON：[{"doc", "heading", "page", "snippet"}]
    # JSON：本次回答的完整链路（改写问题/检索分数/把关/提示词/耗时），调优用的"黑匣子"
    trace: str | None = None
    created_at: datetime = Field(default_factory=utcnow)

"""
配置中心：整个项目所有"可调参数"都集中在这一个文件。

数据流位置：所有模块的起点——先有配置，才有数据库、模型客户端、管线参数。

为什么这样做：
- 把参数从代码里抽出来放到 .env 文件，改配置不用改代码；
- pydantic-settings 会做类型检查和默认值兜底，配错了启动时就会报错，
  而不是等到运行到一半才崩。
"""

from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/ 目录（本文件在 backend/app/config.py，往上两级就是 backend/）
BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """所有配置项。同名环境变量（.env 文件里）会自动覆盖这里的默认值。"""

    # --- 模型相关（智谱全家桶，一个 key 搞定对话 + 向量） ---
    zhipuai_api_key: str = ""                 # 智谱 API Key，必填，放在 .env 里
    # 智谱的 OpenAI 兼容端点：官方推荐的接入方式是 langchain-openai
    # 指向这个地址（而不是 langchain-community 里已停止维护的智谱类）
    zhipu_base_url: str = "https://open.bigmodel.cn/api/paas/v4/"
    chat_model: str = "glm-4-flash"           # 对话模型，flash 档免费且够用
    # chunk AI 优化用的模型：独立配置，可以和对话模型不同（仍走智谱端点，
    # 改写质量要求高时可以换成 glm-4-plus 等更强档位）
    optimize_model: str = "glm-4-flash"
    embed_model: str = "embedding-3"          # 向量（Embedding）模型
    embed_dim: int = 2048                     # embedding-3 输出的向量维度

    # --- 检索相关 ---
    top_k: int = 6                            # 向量检索和 BM25 各自取前几条
    # 相关性把关：检索后让 LLM 判断候选材料是否真能回答问题（CRAG 思想）。
    # 背景：实测 embedding-3 对相关/无关文本的相似度区间几乎重叠（都在
    # 0.40~0.50），分数阈值无法区分；LLM 判断对任何语料都稳健。
    relevance_gate: bool = True

    # --- 切分相关 ---
    chunk_max_tokens: int = 800               # 单个 chunk 的最大长度（近似字符数）
    chunk_merge_peers: bool = True            # 合并相邻的小块，避免碎片

    # --- 文档转换（Docling） ---
    ocr_enabled: bool = False                 # 扫描件开关：默认关（电子版文档用不上，开了会慢很多）

    # --- 路径与限制 ---
    data_dir: Path = BACKEND_DIR / "data"
    upload_dir: Path = BACKEND_DIR / "data" / "uploads"
    chroma_dir: Path = BACKEND_DIR / "data" / "chroma"
    db_path: Path = BACKEND_DIR / "data" / "app.db"
    max_upload_mb: int = 100                  # 单文件上传上限（MB）
    # 允许上传的扩展名白名单。Docling 原生支持这些格式的解析
    allowed_extensions: list[str] = [
        ".pdf", ".docx", ".pptx", ".xlsx", ".html", ".md", ".csv", ".txt",
    ]

    # 告诉 pydantic-settings：从 backend/.env 读环境变量
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def _check_api_key(self) -> "Settings":
        """启动时就把"没配 Key"拦下来，给一句能看懂的人话。

        否则这个错误会延迟到 langchain 初始化模型时才爆出来，
        堆栈又深又难定位——那正是很多人第一次运行被劝退的地方。
        """
        if not self.zhipuai_api_key:
            raise ValueError(
                "\n============================================\n"
                "  缺少智谱 API Key，服务无法启动。\n"
                "  两步解决：\n"
                "  1. cp backend/.env.example backend/.env\n"
                "  2. 编辑 backend/.env，在 ZHIPUAI_API_KEY= 后面填入你的 Key\n"
                "     （智谱开放平台 https://open.bigmodel.cn/ 申请）\n"
                "  然后重新启动。\n"
                "============================================"
            )
        return self


# 全局单例：其他模块直接 `from app.config import settings` 使用。
# 单例模式保证整个进程读的是同一份配置，改 .env 重启即生效
settings = Settings()


def ensure_dirs() -> None:
    """启动时确保数据目录存在（不存在则创建，已存在不报错）。"""
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    settings.chroma_dir.mkdir(parents=True, exist_ok=True)
    settings.data_dir.mkdir(parents=True, exist_ok=True)

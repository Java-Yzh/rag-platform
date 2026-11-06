"""
配置中心：整个项目所有"可调参数"都集中在这一个文件。

数据流位置：所有模块的起点——先有配置，才有数据库、模型客户端、管线参数。

为什么这样做：
- 把参数从代码里抽出来放到 .env 文件，改配置不用改代码；
- pydantic-settings 会做类型检查和默认值兜底，配错了启动时就会报错，
  而不是等到运行到一半才崩。
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/ 目录（本文件在 backend/app/config.py，往上两级就是 backend/）
BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """所有配置项。同名环境变量（.env 文件里）会自动覆盖这里的默认值。

    模型相关配置（供应商/key/模型选择）不在这里——它们在设置页管理，
    存 SQLite（见 services/providers.py）。.env 只保留"不需要频繁改"的参数。
    """

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


# 全局单例：其他模块直接 `from app.config import settings` 使用。
# 单例模式保证整个进程读的是同一份配置，改 .env 重启即生效
settings = Settings()


def ensure_dirs() -> None:
    """启动时确保数据目录存在（不存在则创建，已存在不报错）。"""
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    settings.chroma_dir.mkdir(parents=True, exist_ok=True)
    settings.data_dir.mkdir(parents=True, exist_ok=True)

"""
数据库连接与会话管理（SQLite + SQLModel）。

数据流位置：所有要读写表（文档/chunk/会话）的地方都经过这里拿数据库会话。

知识点：
- "会话(session)"是 SQLAlchemy 的术语，可以理解为一次短对话：
  改了对象后需要 commit（提交）才真正写进库。
- get_session() 是一个生成器依赖：FastAPI 请求处理完会自动关闭会话，
  避免手写 try/finally 的样板代码。
"""

from collections.abc import Generator

from sqlmodel import Session, SQLModel, create_engine

from app.config import ensure_dirs, settings

# 建库三件套：先保证 data/ 目录存在，再创建引擎。
# check_same_thread=False：SQLite 默认禁止跨线程，但 FastAPI 的线程池
# 后台任务需要访问数据库，这里显式放开（配合每个线程独立 session 使用是安全的）
ensure_dirs()
engine = create_engine(
    f"sqlite:///{settings.db_path}",
    connect_args={"check_same_thread": False},
)


def init_db() -> None:
    """应用启动时调用一次：按 models.py 里的定义建表（表已存在则跳过）。"""
    # 导入 models 是为了让 SQLModel 知道有哪些表定义
    import app.models  # noqa: F401

    SQLModel.metadata.create_all(engine)
    _migrate()


def _migrate() -> None:
    """轻量迁移：给已存在的旧表补新列。

    背景：SQLModel 的 create_all 只建新表，不会给旧表加列。
    升级过代码的用户库里 chatmessage 表没有 trace 列，这里手工补上。
    （列已存在时 ALTER 会报错，捕获后忽略即可——"幂等"迁移）
    """
    from sqlalchemy import text

    with engine.begin() as conn:
        try:
            conn.execute(text("ALTER TABLE chatmessage ADD COLUMN trace TEXT"))
        except Exception:  # noqa: BLE001 —— 列已存在
            pass


def get_session() -> Generator[Session, None, None]:
    """FastAPI 依赖：请求进来时开一个会话，请求结束自动关闭。"""
    with Session(engine) as session:
        yield session

# 后端目录结构与文件说明

> 本文是后端的"导览地图"：先看分层架构，再逐文件说明职责，最后附"常见改动去哪个文件"速查表。
> 配合项目根目录 README 的数据流图一起食用更佳。

## 整体架构：三层 + 地基

```
backend/
├── app/                    ← 应用主体
│   ├── __init__.py           【入口哨兵】
│   ├── config.py             【配置中心】
│   ├── db.py                 【数据库接线】
│   ├── models.py             【表结构定义】
│   ├── api/                ← 第①层：HTTP 接口（只管"收发"）
│   │   ├── documents.py
│   │   └── chat.py
│   ├── services/           ← 第②层：业务逻辑（真正干活的地方）
│   │   ├── ingestion.py
│   │   ├── retrieval.py
│   │   └── chat.py
│   └── storage/            ← 第③层：存储封装（跟具体数据库打交道）
│       ├── chroma.py
│       └── bm25.py
├── scripts/
│   └── smoke_pipeline.py     离线冒烟测试
├── data/                     运行数据（uploads 原文件 / chroma 向量 / app.db）
├── pyproject.toml            依赖清单（uv 管理的"requirements.txt"）
├── .env / .env.example       你的密钥和调参 / 给别人看的模板
└── uv.lock                   锁定的精确版本（自动生成，别手改）
```

**调用方向永远是单向的**：`api → services → storage → db/models`，`config` 被所有层引用。
上层只说"做什么"，下层负责"怎么做"——所以改提示词不用碰路由，换向量库不用碰业务。

## 逐个文件说

### 地基（app/ 根目录的四个文件）

| 文件 | 职责 | 一句话记忆 |
|------|------|-----------|
| `__init__.py` | 在所有 import 之前设置 `HF_ENDPOINT`（模型下载源） | "开机第一件事" |
| `config.py` | 定义 `Settings` 类，从 `.env` 读所有可调参数（模型名、阈值、路径…），启动时缺 key 直接报人话 | "所有旋钮的家" |
| `db.py` | 创建 SQLite 引擎、启动建表、旧库补列迁移、给 FastAPI 提供会话 | "数据库的总闸" |
| `models.py` | 四张表的 ORM 定义：`Document`（文档+入库状态）、`Chunk`（切分块，事实源）、`ChatSession`、`ChatMessage`（含 sources 和 trace 两个 JSON 字段） | "表的图纸" |

### 第①层 api/ —— HTTP 的"前台接待"

只做三件事：解析请求参数、调用 services、把结果变成 JSON 返回。**不含业务逻辑**。

- `documents.py`：上传（校验格式/大小→存盘→建档→扔给后台）、文档列表（前端轮询状态用）、删除（级联清理）、重新入库、**chunk 分页预览**
- `chat.py`：会话增删查改、**SSE 流式问答接口**（把 services 的生成器逐条转成 `data: {...}` 事件流）

### 第②层 services/ —— RAG 的"三大车间"

- `ingestion.py`：**入库管线**（后台线程跑）。四步流水线：Docling 转换 → HybridChunker 结构感知切分（含自写的 `CharTokenizer`）→ 写向量库 → 登记 SQLite。任何一步失败都记到 `Document.error` 上
- `retrieval.py`：**检索编排**，问答质量的灵魂。问题改写（多轮指代消解）→ 向量+BM25 两路打分 → 手写 RRF 融合 → LLM 相关性把关（CRAG）→ 全程录 trace
- `chat.py`：**问答组装**。拼参考材料、组装提示词、调 GLM 流式生成、收集引用来源、把整轮问答+trace 存库

### 第③层 storage/ —— 存储的"方言翻译"

- `chroma.py`：向量库的增删查封装（语义检索）
- `bm25.py`：自写的 `JiebaBM25Retriever`（关键词检索），启动/变更时从 SQLite 全量重建内存索引

### 其他

- `scripts/smoke_pipeline.py`：不调 API、不花钱，单独测"转换+切分"两步——出问题时用来切割定位
- `data/`：三个位置对应三份拷贝（原文件/向量/元数据），删掉即恢复出厂

## 常见改动去哪个文件（实用速查）

| 想改什么 | 去哪 |
|---------|------|
| 提示词（回答风格/引用格式） | `services/chat.py` 顶部的 `ANSWER_PROMPT` |
| 切分大小、模型、阈值 | `.env`（不用改代码） |
| 检索逻辑（如以后加 rerank） | `services/retrieval.py` |
| 加新接口 | `api/` 里加文件，`main.py` 注册 |
| 表结构 | `models.py` + `db.py` 的 `_migrate()` |

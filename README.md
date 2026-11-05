# 知识库问答平台（LangChain + Docling RAG）

个人自用的通用 RAG 平台：上传文档 → Docling 转换 → 结构感知切分 → 混合检索 → 流式多轮问答，带完整的 Web 界面。

## 快速开始

```bash
# 1. 配置 API Key（智谱开放平台 https://open.bigmodel.cn/ 申请，glm-4-flash 免费）
cp backend/.env backend/.env
#    编辑 backend/.env，填入 ZHIPUAI_API_KEY=...

# 2. 一键启动（首次会自动安装依赖；PDF 首次转换需从镜像下载模型，耐心等待）
./start.sh

# 3. 浏览器打开 http://localhost:8000
```

开发模式（改代码热重载）：`./dev.sh`，前端访问 http://localhost:5173

## 数据流导览

```
┌──────────┐   ┌─────────────┐   ┌──────────────┐   ┌──────────────────┐
│ 上传文档  │──▶│ Docling 转换 │──▶│ HybridChunker│──▶│ 三处登记          │
│ (前端拖拽)│   │ PDF/Word/…  │   │ 结构感知切分  │   │ SQLite(事实源)    │
└──────────┘   │ → 结构化文档 │   │ 按标题/表格切 │   │ Chroma(向量)      │
               └─────────────┘   └──────────────┘   │ BM25(关键词,内存) │
                                                     └──────────────────┘
┌──────────┐   ┌──────────────────┐   ┌───────────────────────────────┐
│ 用户提问  │──▶│ 历史感知改写      │──▶│ 混合检索: 向量(语义) + BM25(精确)│
│ (多轮对话)│   │ "那第二条呢?"     │   │ RRF 融合排名 → 取 top-6 chunk  │
└──────────┘   │ → 完整独立的问题  │   └───────────────────────────────┘
               └──────────────────┘                │
                                                     ▼
                                          ┌──────────────────────┐
                                          │ GLM 流式生成 + [n]引用 │
                                          │ SSE → 前端打字机效果   │
                                          └──────────────────────┘
```

各环节一句话说明：

| 环节 | 文件 | 做什么 |
|------|------|--------|
| 配置 | `backend/app/config.py` | 所有可调参数（模型/切分/检索），`.env` 一处改 |
| 入库管线 | `backend/app/services/ingestion.py` | 转换→切分→写三处存储，后台线程异步执行 |
| 切分 | 同上 `CharTokenizer` + HybridChunker | 按文档真实结构切，表格不碎、标题路径保留 |
| 向量库 | `backend/app/storage/chroma.py` | 语义检索（"意思相近"的查询靠它） |
| 关键词索引 | `backend/app/storage/bm25.py` | 精确匹配（型号/人名/编号靠它），启动时从 SQLite 重建 |
| 检索编排 | `backend/app/services/retrieval.py` | 混合检索 RRF 融合 + 多轮问题改写 |
| 问答 | `backend/app/services/chat.py` | 组装提示词、流式生成、收集引用来源 |
| 文档 API | `backend/app/api/documents.py` | 上传/列表/删除/重入库/chunk 预览 |
| 会话 API | `backend/app/api/chat.py` | 会话 CRUD + SSE 流式接口 |
| 前端 | `frontend/src/` | 对话页（流式+引用卡片）、文档管理页（上传+进度+chunk 抽屉） |

## 常用操作

- **调优切分参数**：改 `.env` 里的 `CHUNK_MAX_TOKENS` → 重启 → 文档管理页点"重新入库"→ 点"查看 chunk"看效果
- **接入扫描件**：`.env` 里 `OCR_ENABLED=true` 后重启（转换会明显变慢）
- **验证删除是否干净**：`cd backend && uv run python -c "from app.storage import chroma; print(chroma.count())"`
- **单独测转换/切分**（不调 API、不花钱）：`cd backend && uv run python -m scripts.smoke_pipeline /path/to/file.pdf`

## 技术选型速记

| 选择 | 理由 |
|------|------|
| 智谱 GLM-4-Flash + embedding-3 | 一个 Key 搞定对话+向量，flash 档免费 |
| Docling HybridChunker | 结构感知切分是选 Docling 的核心理由；自带字符 tokenizer 避免下载 HF 模型 |
| 向量 + BM25 混合检索 | 中文专有名词/编号靠关键词兜底，语义问题靠向量，RRF 融合 |
| Chroma 嵌入式向量库 | 几百份文档量级零运维；数据就是 data/ 目录下的文件 |
| SQLite 存 chunk 事实源 | chunk 预览、BM25 重建、会话历史一个文件全搞定 |
| FastAPI 托管前端 dist | 生产模式单进程即整个平台 |

## 目录约定

- 原始上传文件：`backend/data/uploads/`
- 向量库：`backend/data/chroma/`
- 数据库（文档表/chunk表/会话表）：`backend/data/app.db`
- 全部删掉这三个位置 = 平台恢复出厂（上传的文档需要重新传）

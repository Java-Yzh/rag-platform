"""
模型供应商服务：平台与具体 LLM 厂商解耦的核心。

数据流位置：设置页（供应商管理 + 模型配置）的后端支撑；
同时是所有需要模型的地方（问答/改写/把关/优化/向量化）的唯一取用处——
想用模型？来这儿领，别自己 new 客户端。

设计核心：所有主流厂商（智谱/DeepSeek/Moonshot/百炼/硅基流动/MiniMax）
都提供 OpenAI 兼容端点，所以一个供应商 = base_url + api_key，
全部用 langchain-openai 的 ChatOpenAI / OpenAIEmbeddings 访问，
换厂商只是换这两个字符串。

配置存 SQLite（Provider 表 + AppConfig 表），保存即生效、不用重启——
每次创建模型客户端都现场查库，SQLite 毫秒级查询，无需缓存。
"""

import json
import re
import uuid

import httpx
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from sqlmodel import Session, select

from app.db import engine
from app.models import AppConfig, Provider

# ---------- 国产厂商模板（添加供应商时免填 base_url） ----------
# name: 显示名 / base_url: OpenAI 兼容端点 / key_url: 申请 key 的入口
# models_api: 该厂商是否支持 GET /models 拉取模型列表（个别厂商不支持，
# 不支持的会回退到"手动填模型名"）

PROVIDER_TEMPLATES: list[dict] = [
    {
        "id": "zhipu",
        "name": "智谱 BigModel",
        "base_url": "https://open.bigmodel.cn/api/paas/v4/",
        "key_url": "https://open.bigmodel.cn/usercenter/apikeys",
        "note": "flash 档免费；注意 /models 只列当前世代对话模型，embedding-3 等向量模型需点「手动添加模型名」补上",
        "models_api": True,
    },
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "key_url": "https://platform.deepseek.com/api_keys",
        "note": "chat 强且便宜；无 embedding API（向量选别家）",
        "models_api": True,
    },
    {
        "id": "moonshot",
        "name": "月之暗面 Moonshot (Kimi)",
        "base_url": "https://api.moonshot.cn/v1",
        "key_url": "https://platform.moonshot.cn/console/api-keys",
        "note": "长上下文见长",
        "models_api": True,
    },
    {
        "id": "dashscope",
        "name": "阿里云百炼 DashScope",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "key_url": "https://bailian.console.aliyun.com/",
        "note": "qwen 系列 chat + text-embedding-v3/v4",
        "models_api": True,
    },
    {
        "id": "siliconflow",
        "name": "硅基流动 SiliconCloud",
        "base_url": "https://api.siliconflow.cn/v1",
        "key_url": "https://cloud.siliconflow.cn/account/ak",
        "note": "开源模型聚合，有免费 embedding（bge-m3），适合做向量兜底",
        "models_api": True,
    },
    {
        "id": "minimax",
        "name": "MiniMax",
        "base_url": "https://api.minimax.chat/v1",
        "key_url": "https://platform.minimaxi.com/user-center/basic-information/interface-key",
        "note": "chat；/models 支持不稳定，取不到就手动填模型名",
        "models_api": False,
    },
]


# ---------- 模型分类与工具 ----------

def classify_model(model_id: str) -> str:
    """按模型名启发式判断用途：含 embedding/bge 的算向量模型，其余算对话模型。

    各厂商 /models 接口不返回用途字段，名称启发式是通行做法；
    分错的可以在设置页勾选时手动切换归类。
    """
    return "embed" if re.search(r"embedding|bge", model_id, re.IGNORECASE) else "chat"


def _unconfigured_error(role: str) -> RuntimeError:
    """未配置某角色模型时抛的友好错误（前端原样展示给用户）。"""
    role_name = {"chat": "对话", "embed": "向量", "optimize": "AI 优化"}[role]
    return RuntimeError(f"尚未配置{role_name}模型：请到「设置」页完成供应商和模型配置")


# ---------- 供应商存取 ----------

def list_providers() -> list[Provider]:
    with Session(engine) as session:
        return session.exec(select(Provider).order_by(Provider.created_at)).all()


def get_provider(provider_id: str) -> Provider | None:
    with Session(engine) as session:
        return session.get(Provider, provider_id)


def parse_models(p: Provider) -> list[dict]:
    """Provider.models 是 JSON 字符串，解开成 [{id, kind}]。"""
    try:
        return json.loads(p.models)
    except json.JSONDecodeError:
        return []


# ---------- 模型分配（三个角色用哪家哪个模型） ----------

ASSIGN_KEY = "model_assignments"


def get_assignments() -> dict:
    """读三组分配：{"chat": {provider_id, model}|None, "embed": ..., "optimize": ...}"""
    with Session(engine) as session:
        row = session.get(AppConfig, ASSIGN_KEY)
        if row is None:
            return {"chat": None, "embed": None, "optimize": None}
        data = json.loads(row.value)
        # 兜底：保证三个键都存在
        return {role: data.get(role) for role in ("chat", "embed", "optimize")}


def set_assignments(assignments: dict) -> None:
    with Session(engine) as session:
        row = session.get(AppConfig, ASSIGN_KEY)
        if row is None:
            row = AppConfig(key=ASSIGN_KEY, value="{}")
        row.value = json.dumps(assignments, ensure_ascii=False)
        session.add(row)
        session.commit()


def _resolve(role: str) -> tuple[Provider, str]:
    """把角色分配解析成 (Provider, 模型名)。没配置就抛友好错误。"""
    assign = get_assignments().get(role)
    if not assign:
        raise _unconfigured_error(role)
    provider = get_provider(assign["provider_id"])
    if provider is None:
        raise RuntimeError(f"{role} 角色指向的供应商已被删除，请到「设置」页重新配置")
    return provider, assign["model"]


# ---------- 模型工厂（所有取模型的地方统一走这里） ----------

def make_chat_llm(role: str = "chat", temperature: float = 0.1) -> ChatOpenAI:
    """按角色分配创建对话模型客户端。

    role: "chat"（问答/改写/把关）或 "optimize"（chunk AI 优化）
    """
    provider, model = _resolve(role)
    return ChatOpenAI(
        model=model,
        api_key=provider.api_key,
        base_url=provider.base_url,
        temperature=temperature,
    )


def make_embeddings() -> OpenAIEmbeddings:
    """按向量模型分配创建 embedding 客户端。"""
    provider, model = _resolve("embed")
    return OpenAIEmbeddings(
        model=model, api_key=provider.api_key, base_url=provider.base_url
    )


# ---------- 在线能力：拉取模型列表 / 测试连接 ----------

def fetch_remote_models(base_url: str, api_key: str) -> list[dict]:
    """调厂商的 GET /models 拉取该 key 可用的全部模型（不落库）。

    兼容两种返回格式：{"data": [{"id": ...}]}（OpenAI 风格）或裸列表。
    顺便完成了 key 校验——key 错会收到 401，原样抛给前端展示。
    """
    url = base_url.rstrip("/") + "/models"
    resp = httpx.get(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=15)
    resp.raise_for_status()  # 401/403 等直接转成带状态码的异常
    data = resp.json()
    items = data.get("data", data) if isinstance(data, dict) else data
    ids = sorted({item["id"] for item in items if isinstance(item, dict) and item.get("id")})
    # 返回时顺带给每个模型标好启发式分类，前端直接展示勾选
    return [{"id": mid, "kind": classify_model(mid)} for mid in ids]


def test_provider(p: Provider) -> dict:
    """测试已保存供应商的连通性：chat 一个来回 + embed 一次（各取其第一个模型）。

    返回明细而不是抛异常：哪怕 chat 通了 embed 不通，也要都告诉你。
    """
    models = parse_models(p)
    result: dict = {"chat": {"ok": False, "error": "未勾选对话模型"}, "embed": None}

    chat_model = next((m["id"] for m in models if m["kind"] == "chat"), None)
    if chat_model:
        try:
            ChatOpenAI(
                model=chat_model, api_key=p.api_key, base_url=p.base_url, temperature=0
            ).invoke("ping")
            result["chat"] = {"ok": True, "model": chat_model, "error": None}
        except Exception as exc:  # noqa: BLE001
            result["chat"] = {"ok": False, "model": chat_model, "error": str(exc)[:300]}

    embed_model = next((m["id"] for m in models if m["kind"] == "embed"), None)
    if embed_model:
        try:
            OpenAIEmbeddings(
                model=embed_model, api_key=p.api_key, base_url=p.base_url
            ).embed_query("ping")
            result["embed"] = {"ok": True, "model": embed_model, "error": None}
        except Exception as exc:  # noqa: BLE001
            result["embed"] = {"ok": False, "model": embed_model, "error": str(exc)[:300]}
    return result


# ---------- 供应商增删改 ----------

def create_provider(name: str, base_url: str, api_key: str, models: list[dict]) -> Provider:
    with Session(engine) as session:
        p = Provider(
            id=uuid.uuid4().hex,
            name=name.strip(),
            base_url=base_url.strip(),
            api_key=api_key.strip(),
            models=json.dumps(models, ensure_ascii=False),
        )
        session.add(p)
        session.commit()
        session.refresh(p)
        return p


def update_provider(
    provider_id: str,
    name: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,  # 传空/None = 保留旧 key（前端编辑时不回显明文）
    models: list[dict] | None = None,
) -> Provider | None:
    with Session(engine) as session:
        p = session.get(Provider, provider_id)
        if p is None:
            return None
        if name is not None:
            p.name = name.strip()
        if base_url is not None:
            p.base_url = base_url.strip()
        if api_key:  # 空字符串视为"不改"
            p.api_key = api_key.strip()
        if models is not None:
            p.models = json.dumps(models, ensure_ascii=False)
        session.add(p)
        session.commit()
        session.refresh(p)
        return p


def delete_provider(provider_id: str) -> bool:
    """删除供应商，并清掉引用它的角色分配（避免悬空指针）。"""
    with Session(engine) as session:
        p = session.get(Provider, provider_id)
        if p is None:
            return False
        session.delete(p)
        session.commit()
    # 清理分配：删掉的供应商不再被任何角色引用
    assignments = get_assignments()
    changed = False
    for role, assign in assignments.items():
        if assign and assign.get("provider_id") == provider_id:
            assignments[role] = None
            changed = True
    if changed:
        set_assignments(assignments)
    return True

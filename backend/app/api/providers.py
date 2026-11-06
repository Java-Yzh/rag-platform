"""
供应商与模型配置 API：设置页两大部分的后端入口。

Part 1 供应商管理：模板列表 / CRUD / 测试连接 / 拉取可用模型
Part 2 模型配置：三个角色（对话/向量/AI优化）各分配"供应商 + 模型"

最重的一段业务逻辑在 PUT /api/settings/models 里：**向量模型变更保护**——
换向量模型必须清空整个向量库（维度和语义空间都变了，物理上不兼容），
返回 vector_wiped=true 让前端提示用户去"全部重新入库"。
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app.db import get_session
from app.models import Document
from app.services import ingestion, providers
from app.storage import chroma

router = APIRouter(prefix="/api", tags=["providers"])


# ---------- 请求体 ----------

class ProviderIn(BaseModel):
    name: str
    base_url: str
    api_key: str = ""
    models: list[dict] = []  # [{"id": "...", "kind": "chat"|"embed"}]


class ProviderPatch(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None  # None/空 = 保留旧 key
    models: list[dict] | None = None


class FetchModelsIn(BaseModel):
    base_url: str
    api_key: str


class RoleAssignIn(BaseModel):
    provider_id: str
    model: str


class ModelSettingsIn(BaseModel):
    chat: RoleAssignIn | None = None
    embed: RoleAssignIn | None = None
    optimize: RoleAssignIn | None = None


# ---------- Part 1: 供应商管理 ----------

def _masked(p) -> dict:
    """供应商转 JSON：key 脱敏（只露头尾），模型解开成列表。"""
    key = p.api_key
    masked = f"{key[:6]}…{key[-4:]}" if len(key) > 12 else ("***" if key else "")
    return {
        "id": p.id,
        "name": p.name,
        "base_url": p.base_url,
        "api_key_masked": masked,
        "has_key": bool(key),
        "models": providers.parse_models(p),
        "created_at": p.created_at.isoformat(),
    }


@router.get("/providers/templates")
def list_templates() -> list[dict]:
    """六家国产厂商模板 + 自定义占位（前端渲染添加入口用）。"""
    return providers.PROVIDER_TEMPLATES


@router.get("/providers")
def list_providers() -> list[dict]:
    return [_masked(p) for p in providers.list_providers()]


@router.post("/providers")
def create_provider(req: ProviderIn) -> dict:
    if not req.name.strip() or not req.base_url.strip():
        raise HTTPException(400, "名称和 base_url 不能为空")
    p = providers.create_provider(req.name, req.base_url, req.api_key, req.models)
    return _masked(p)


@router.patch("/providers/{provider_id}")
def patch_provider(provider_id: str, req: ProviderPatch) -> dict:
    p = providers.update_provider(
        provider_id, req.name, req.base_url, req.api_key, req.models
    )
    if p is None:
        raise HTTPException(404, "供应商不存在")
    return _masked(p)


@router.delete("/providers/{provider_id}")
def remove_provider(provider_id: str) -> dict:
    """删除供应商。若某角色正分配着它，对应分配会被一并清空（返回提示）。"""
    assignments = providers.get_assignments()
    affected = [r for r, a in assignments.items() if a and a["provider_id"] == provider_id]
    if not providers.delete_provider(provider_id):
        raise HTTPException(404, "供应商不存在")
    return {"ok": True, "cleared_roles": affected}


@router.post("/providers/fetch-models")
def fetch_models(req: FetchModelsIn) -> list[dict]:
    """拉取某 key 可用的全部模型（不落库）。同时天然完成 key 校验（错则 401 抛出）。"""
    if not req.base_url.strip() or not req.api_key.strip():
        raise HTTPException(400, "base_url 和 api_key 不能为空")
    try:
        return providers.fetch_remote_models(req.base_url, req.api_key)
    except Exception as exc:  # noqa: BLE001 —— 把厂商返回的错误转成友好提示
        raise HTTPException(400, f"获取模型列表失败：{str(exc)[:200]}") from exc


@router.post("/providers/{provider_id}/test")
def test_provider(provider_id: str) -> dict:
    """测试已保存供应商的连通性：chat 一个来回 + embed 一次。"""
    p = providers.get_provider(provider_id)
    if p is None:
        raise HTTPException(404, "供应商不存在")
    return providers.test_provider(p)


# ---------- Part 2: 模型配置 ----------

@router.get("/settings/models")
def get_model_settings() -> dict:
    return providers.get_assignments()


@router.put("/settings/models")
def put_model_settings(req: ModelSettingsIn) -> dict:
    """保存三组模型分配。

    关键保护：向量模型从"已配置的 A"换成"不同的 B"时，清空整个向量库
    （维度和语义空间都变了，旧向量物理上不可用），并提示需要全部重新入库。
    首次配置（null → A）不清——比如升级后把原来 .env 的智谱重新配回去，
    旧向量本来就是同一个模型嵌的，继续可用。
    """
    # 校验：分配指向的供应商必须存在，模型必须在该供应商勾选列表里
    for role, assign in (("chat", req.chat), ("embed", req.embed), ("optimize", req.optimize)):
        if assign is None:
            continue
        p = providers.get_provider(assign.provider_id)
        if p is None:
            raise HTTPException(400, f"{role} 角色指向的供应商不存在")
        model_ids = [m["id"] for m in providers.parse_models(p)]
        if assign.model not in model_ids:
            raise HTTPException(400, f"模型 {assign.model} 不在该供应商的勾选列表里，请先在供应商管理中勾选")

    old = providers.get_assignments()
    vector_wiped = bool(
        old.get("embed")  # 旧的向量模型已配置
        and req.embed  # 新的也要配置
        and (
            old["embed"]["provider_id"] != req.embed.provider_id
            or old["embed"]["model"] != req.embed.model
        )
    )
    if vector_wiped:
        chroma.reset_collection()

    providers.set_assignments(
        {
            "chat": req.chat.model_dump() if req.chat else None,
            "embed": req.embed.model_dump() if req.embed else None,
            "optimize": req.optimize.model_dump() if req.optimize else None,
        }
    )
    return {
        "ok": True,
        "vector_wiped": vector_wiped,
        "message": "向量模型已变更，向量库已清空——请到文档管理页执行「全部重新入库」" if vector_wiped else None,
    }


@router.post("/documents/reingest-all")
def reingest_all(session: Session = Depends(get_session)) -> dict:
    """全部重新入库：把所有文档重置为 pending 并逐个提交管线（串行队列自动排队）。

    典型场景：换了向量模型后重建全部向量。原文件丢失的文档跳过并计数。
    """
    docs = session.exec(select(Document)).all()
    submitted, skipped = 0, 0
    from pathlib import Path

    for d in docs:
        if not Path(d.stored_path).exists():
            skipped += 1
            continue
        d.status = "pending"
        d.error = None
        session.add(d)
        submitted += 1
        ingestion.submit_ingest(d.id)
    session.commit()
    return {"ok": True, "submitted": submitted, "skipped": skipped}


def health_summary() -> dict:
    """健康检查摘要：供应商数量 + 三个角色是否已配置（main.py 复用）。"""
    assignments = providers.get_assignments()
    return {
        "providers": len(providers.list_providers()),
        "chat_configured": assignments["chat"] is not None,
        "embed_configured": assignments["embed"] is not None,
        "optimize_configured": assignments["optimize"] is not None,
    }

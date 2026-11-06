import { useEffect, useState } from "react";
import {
  settingsApi,
  type ModelEntry,
  type ModelSettings,
  type ProviderInfo,
  type ProviderTemplate,
  type RoleAssign,
} from "../api";

/**
 * 设置页：两大部分。
 *
 * Part 1 供应商管理——添加（六家国产模板 + 自定义）→ 填 key →
 *   「获取可用模型」拉列表勾选（这一步顺带验证了 key）→ 保存。
 *   已有供应商可测试连接 / 编辑 / 删除。
 * Part 2 模型配置——对话 / 向量 / AI优化 三个角色，
 *   各选"供应商 + 该供应商勾选过的模型"。
 *
 * 最需要注意的交互：换向量模型会清空向量库（维度和语义空间变了，
 * 物理上必须清），保存时弹确认，保存后提示去「全部重新入库」。
 */

type Role = "chat" | "embed" | "optimize";

const ROLE_LABELS: Record<Role, string> = {
  chat: "对话模型",
  embed: "向量模型",
  optimize: "AI 优化模型",
};

export default function SettingsPage() {
  // ---------- Part 1 状态 ----------
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [templates, setTemplates] = useState<ProviderTemplate[]>([]);
  // 添加/编辑表单：editingId=null 且 open=true 表示新增
  const [formOpen, setFormOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState({ name: "", base_url: "", api_key: "" });
  const [fetchedModels, setFetchedModels] = useState<ModelEntry[]>([]); // 拉到的可勾选模型
  const [fetching, setFetching] = useState(false);
  const [testingId, setTestingId] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<Record<string, string>>({}); // id → 结果摘要

  // ---------- Part 2 状态 ----------
  const [assignments, setAssignments] = useState<ModelSettings>({
    chat: null,
    embed: null,
    optimize: null,
  });
  const [saveMsg, setSaveMsg] = useState("");

  const refresh = async () => {
    setProviders(await settingsApi.listProviders());
    setAssignments(await settingsApi.getModelSettings());
  };

  useEffect(() => {
    settingsApi.listTemplates().then(setTemplates);
    refresh();
  }, []);

  // ---------- Part 1：供应商增删改 ----------

  const openAddForm = (tpl: ProviderTemplate | null) => {
    setEditingId(null);
    setFetchedModels([]);
    setForm(
      tpl
        ? { name: tpl.name, base_url: tpl.base_url, api_key: "" }
        : { name: "", base_url: "", api_key: "" } // 自定义：全手填
    );
    setFormOpen(true);
  };

  const openEditForm = (p: ProviderInfo) => {
    setEditingId(p.id);
    setForm({ name: p.name, base_url: p.base_url, api_key: "" }); // key 不回显，留空=不改
    setFetchedModels(p.models);
    setFormOpen(true);
  };

  /** 获取可用模型（顺带验证 key）：成功后全部默认勾选，可手动取消 */
  const fetchModels = async () => {
    if (fetching) return;
    setFetching(true);
    try {
      const models = await settingsApi.fetchModels(form.base_url, form.api_key);
      setFetchedModels(models);
      if (models.length === 0) alert("接口返回了空列表——可以手动添加模型名");
    } catch (e) {
      alert(`获取失败：${String(e)}\n可改用手动添加模型名`);
    } finally {
      setFetching(false);
    }
  };

  const toggleModel = (m: ModelEntry) => {
    setFetchedModels((prev) =>
      prev.some((x) => x.id === m.id) ? prev.filter((x) => x.id !== m.id) : [...prev, m]
    );
  };

  const switchKind = (m: ModelEntry, kind: "chat" | "embed") => {
    setFetchedModels((prev) => prev.map((x) => (x.id === m.id ? { ...x, kind } : x)));
  };

  /** 手动补一个模型名（个别厂商不支持 /models，或新模型还没进列表） */
  const addManualModel = () => {
    const id = prompt("输入模型名（如 glm-4-flash 或 BAAI/bge-m3）");
    if (!id) return;
    const kind: "chat" | "embed" = /embedding|bge/i.test(id) ? "embed" : "chat";
    setFetchedModels((prev) =>
      prev.some((x) => x.id === id) ? prev : [...prev, { id, kind }]
    );
  };

  const saveProvider = async () => {
    if (!form.name.trim() || !form.base_url.trim()) {
      alert("名称和 base_url 不能为空");
      return;
    }
    // 新增必须有 key；编辑时 key 留空 = 沿用旧值
    if (!editingId && !form.api_key.trim()) {
      alert("请填写 API Key");
      return;
    }
    try {
      if (editingId) {
        await settingsApi.updateProvider(editingId, { ...form, models: fetchedModels });
      } else {
        await settingsApi.createProvider({ ...form, models: fetchedModels });
      }
      setFormOpen(false);
      await refresh();
    } catch (e) {
      alert(String(e));
    }
  };

  const removeProvider = async (p: ProviderInfo) => {
    if (!confirm(`删除供应商「${p.name}」？`)) return;
    const res = await settingsApi.deleteProvider(p.id);
    if (res.cleared_roles.length > 0) {
      alert(
        `已删除。注意：${res.cleared_roles.map(roleName).join("、")}的模型分配已被清空，请重新配置`
      );
    }
    await refresh();
  };

  const runTest = async (p: ProviderInfo) => {
    setTestingId(p.id);
    setTestResult((prev) => ({ ...prev, [p.id]: "测试中…" }));
    try {
      const r = await settingsApi.testProvider(p.id);
      const chatPart = r.chat.ok ? `✓ chat(${r.chat.model})` : `✗ chat: ${r.chat.error ?? ""}`;
      const embedPart =
        r.embed === null ? "（未配向量模型）" : r.embed.ok ? `✓ embed(${r.embed.model})` : `✗ embed: ${r.embed.error ?? ""}`;
      setTestResult((prev) => ({ ...prev, [p.id]: `${chatPart} ${embedPart}` }));
    } catch (e) {
      setTestResult((prev) => ({ ...prev, [p.id]: String(e) }));
    } finally {
      setTestingId(null);
    }
  };

  // ---------- Part 2：模型分配 ----------

  const setRoleProvider = (role: Role, providerId: string) => {
    // 换供应商时清掉已选模型（模型列表跟着供应商走）
    setAssignments((prev) => ({
      ...prev,
      [role]: providerId ? { provider_id: providerId, model: "" } : null,
    }));
  };

  const setRoleModel = (role: Role, model: string) => {
    setAssignments((prev) => ({
      ...prev,
      [role]: prev[role] ? { ...prev[role]!, model } : null,
    }));
  };

  /** 某角色可选的模型列表：限定该供应商 + 用途匹配（chat 角色看 chat 模型） */
  const modelsForRole = (role: Role, assign: RoleAssign): ModelEntry[] => {
    if (!assign) return [];
    const p = providers.find((x) => x.id === assign.provider_id);
    if (!p) return [];
    const wantKind = role === "embed" ? "embed" : "chat"; // chat/optimize 都用对话类模型
    return p.models.filter((m) => m.kind === wantKind);
  };

  const saveAssignments = async () => {
    // 校验：三个角色要么不配，要么配全（供应商+模型都选了）
    for (const role of ["chat", "embed", "optimize"] as Role[]) {
      const a = assignments[role];
      if (a && !a.model) {
        alert(`${roleName(role)}还没选具体模型`);
        return;
      }
    }
    // 换向量模型的后果确认
    const old = await settingsApi.getModelSettings();
    const embedChanging =
      old.embed &&
      assignments.embed &&
      (old.embed.provider_id !== assignments.embed.provider_id ||
        old.embed.model !== assignments.embed.model);
    if (
      embedChanging &&
      !confirm(
        "检测到向量模型变更！\n\n保存将清空整个向量库（不同模型的向量维度和语义空间不兼容），之后需要在文档管理页「全部重新入库」。\n\n确定继续？"
      )
    ) {
      return;
    }
    try {
      const res = await settingsApi.putModelSettings(assignments);
      setSaveMsg(
        res.vector_wiped
          ? "⚠️ 已保存。向量模型已变更，向量库已清空——请点击下方「全部重新入库」重建"
          : "✓ 已保存，配置即刻生效"
      );
      await refresh();
    } catch (e) {
      alert(String(e));
    }
  };

  const reingestAll = async () => {
    if (!confirm("把所有文档重新跑一遍入库管线（转换→切分→嵌入）？文档多时需要较长时间")) return;
    const res = await settingsApi.reingestAll();
    setSaveMsg(`已提交 ${res.submitted} 份文档重新入库（跳过 ${res.skipped} 份原文件缺失），可到文档管理页查看进度`);
  };

  // ---------- 渲染 ----------

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto max-w-3xl space-y-8 pb-10">
        {/* ============ Part 1 供应商管理 ============ */}
        <section className="space-y-4">
          <h2 className="text-base font-bold">模型供应商</h2>

          {/* 已有供应商卡片 */}
          {providers.map((p) => (
            <div key={p.id} className="rounded-xl border border-gray-200 bg-white p-4">
              <div className="flex items-center justify-between">
                <div className="min-w-0">
                  <span className="font-medium">{p.name}</span>
                  <span className="ml-3 text-xs text-gray-400">
                    {p.models.filter((m) => m.kind === "chat").length} 个对话模型 ·{" "}
                    {p.models.filter((m) => m.kind === "embed").length} 个向量模型
                  </span>
                  <div className="mt-0.5 truncate text-xs text-gray-400">
                    {p.base_url} · key: {p.api_key_masked || "未填写"}
                  </div>
                </div>
                <div className="flex shrink-0 items-center gap-3 text-sm">
                  <button
                    onClick={() => runTest(p)}
                    disabled={testingId === p.id}
                    className="text-blue-600 hover:underline disabled:opacity-50"
                  >
                    测试连接
                  </button>
                  <button onClick={() => openEditForm(p)} className="text-gray-500 hover:underline">
                    编辑
                  </button>
                  <button onClick={() => removeProvider(p)} className="text-red-500 hover:underline">
                    删除
                  </button>
                </div>
              </div>
              {testResult[p.id] && (
                <div className="mt-2 break-all rounded-lg bg-gray-50 px-3 py-2 text-xs text-gray-500">
                  {testResult[p.id]}
                </div>
              )}
            </div>
          ))}

          {/* 添加入口：模板卡片（未展开表单时显示） */}
          {!formOpen && (
            <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
              {templates.map((t) => (
                <button
                  key={t.id}
                  onClick={() => openAddForm(t)}
                  title={t.note}
                  className="rounded-xl border border-dashed border-gray-300 bg-white p-3 text-left text-sm hover:border-blue-400 hover:bg-blue-50/40"
                >
                  <div className="font-medium">＋ {t.name}</div>
                  <div className="mt-1 line-clamp-2 text-xs text-gray-400">{t.note}</div>
                </button>
              ))}
              <button
                onClick={() => openAddForm(null)}
                className="rounded-xl border border-dashed border-gray-300 bg-white p-3 text-left text-sm hover:border-blue-400 hover:bg-blue-50/40"
              >
                <div className="font-medium">＋ 自定义</div>
                <div className="mt-1 text-xs text-gray-400">任意 OpenAI 兼容端点</div>
              </button>
            </div>
          )}

          {/* 添加/编辑表单 */}
          {formOpen && (
            <div className="space-y-3 rounded-xl border border-blue-200 bg-blue-50/30 p-4">
              <div className="grid gap-3 md:grid-cols-2">
                <label className="text-sm">
                  名称
                  <input
                    value={form.name}
                    onChange={(e) => setForm({ ...form, name: e.target.value })}
                    className="mt-1 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm"
                  />
                </label>
                <label className="text-sm">
                  Base URL（OpenAI 兼容端点）
                  <input
                    value={form.base_url}
                    onChange={(e) => setForm({ ...form, base_url: e.target.value })}
                    className="mt-1 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 font-mono text-xs"
                  />
                </label>
              </div>
              <label className="block text-sm">
                API Key{editingId && "（留空 = 沿用已保存的 key）"}
                <input
                  type="password"
                  value={form.api_key}
                  onChange={(e) => setForm({ ...form, api_key: e.target.value })}
                  className="mt-1 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 font-mono text-xs"
                />
              </label>

              {/* 获取模型 + 勾选列表 */}
              <div className="flex items-center gap-3">
                <button
                  onClick={fetchModels}
                  disabled={fetching || !form.base_url || !(form.api_key || editingId)}
                  className="rounded-lg border border-blue-300 bg-white px-3 py-1.5 text-sm text-blue-600 hover:bg-blue-50 disabled:opacity-50"
                >
                  {fetching ? "获取中…" : "⬇ 获取可用模型（顺带验证 Key）"}
                </button>
                <button
                  onClick={addManualModel}
                  className="text-xs text-gray-400 hover:text-gray-600"
                >
                  手动添加模型名
                </button>
              </div>

              {fetchedModels.length > 0 && (
                <div className="max-h-64 space-y-1 overflow-y-auto rounded-lg border border-gray-200 bg-white p-2">
                  {fetchedModels.map((m) => (
                    <div key={m.id} className="flex items-center gap-2 text-sm">
                      <input
                        type="checkbox"
                        checked
                        onChange={() => toggleModel(m)}
                        className="h-3.5 w-3.5"
                      />
                      <span className="min-w-0 flex-1 truncate font-mono text-xs">{m.id}</span>
                      {/* 用途切换：名称启发式分错了可以手动改 */}
                      <div className="flex overflow-hidden rounded border border-gray-200 text-xs">
                        {(["chat", "embed"] as const).map((k) => (
                          <button
                            key={k}
                            onClick={() => switchKind(m, k)}
                            className={`px-2 py-0.5 ${
                              m.kind === k ? "bg-blue-600 text-white" : "bg-white text-gray-400"
                            }`}
                          >
                            {k === "chat" ? "对话" : "向量"}
                          </button>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              )}

              <div className="flex justify-end gap-3 text-sm">
                <button
                  onClick={() => setFormOpen(false)}
                  className="rounded-lg px-3 py-1.5 text-gray-500 hover:bg-gray-100"
                >
                  取消
                </button>
                <button
                  onClick={saveProvider}
                  className="rounded-lg bg-blue-600 px-4 py-1.5 text-white hover:bg-blue-700"
                >
                  保存供应商
                </button>
              </div>
            </div>
          )}
        </section>

        {/* ============ Part 2 模型配置 ============ */}
        <section className="space-y-4">
          <h2 className="text-base font-bold">模型配置</h2>
          <p className="text-xs text-gray-400">
            三个角色独立配置。⚠️ 换向量模型会清空向量库并需要全部重新入库
          </p>

          <div className="space-y-3 rounded-xl border border-gray-200 bg-white p-4">
            {(["chat", "embed", "optimize"] as Role[]).map((role) => {
              const assign = assignments[role];
              return (
                <div key={role} className="flex flex-wrap items-center gap-3 text-sm">
                  <span className="w-24 shrink-0 font-medium">{ROLE_LABELS[role]}</span>
                  <select
                    value={assign?.provider_id ?? ""}
                    onChange={(e) => setRoleProvider(role, e.target.value)}
                    className="rounded-lg border border-gray-300 bg-white px-2 py-1.5"
                  >
                    <option value="">— 未配置 —</option>
                    {providers.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name}
                      </option>
                    ))}
                  </select>
                  <select
                    value={assign?.model ?? ""}
                    onChange={(e) => setRoleModel(role, e.target.value)}
                    disabled={!assign}
                    className="min-w-52 rounded-lg border border-gray-300 bg-white px-2 py-1.5 font-mono text-xs disabled:bg-gray-50"
                  >
                    <option value="">— 选择模型 —</option>
                    {modelsForRole(role, assign).map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.id}
                      </option>
                    ))}
                  </select>
                </div>
              );
            })}

            <div className="flex items-center justify-between border-t border-gray-100 pt-3">
              <span className="text-xs text-gray-500">{saveMsg}</span>
              <div className="flex gap-3 text-sm">
                <button
                  onClick={reingestAll}
                  className="rounded-lg border border-gray-300 px-3 py-1.5 text-gray-600 hover:bg-gray-50"
                >
                  全部重新入库
                </button>
                <button
                  onClick={saveAssignments}
                  className="rounded-lg bg-blue-600 px-4 py-1.5 text-white hover:bg-blue-700"
                >
                  保存模型配置
                </button>
              </div>
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}

function roleName(role: string): string {
  return { chat: "对话模型", embed: "向量模型", optimize: "AI优化模型" }[role] ?? role;
}

/**
 * 后端 API 客户端：所有网络请求集中在这一个文件。
 *
 * 知识点——SSE 流式读取：
 * fetch 本身不提供"边收边处理"的回调，但返回的 response.body
 * 是一个 ReadableStream。用 TextDecoder 逐块解码、按 SSE 协议
 * （消息以空行 \n\n 分隔，每行 "data: {...}"）手工切分，
 * 就能实现"服务器每吐一个字，页面就渲染一个字"的效果。
 */

// ---------- 类型定义（和后端返回的 JSON 字段一一对应） ----------

export interface DocInfo {
  id: string;
  filename: string;
  ext: string;
  size_bytes: number;
  status: "pending" | "processing" | "done" | "failed";
  error: string | null;
  chunk_count: number;
  md5: string;
  created_at: string;
}

export interface ChunkInfo {
  seq: number;
  heading_path: string;
  page_no: number | null;
  content: string;
}

/** Chunk 管理页的一行（比文档抽屉版多带 id/所属文件/字符数） */
export interface ChunkRowInfo extends ChunkInfo {
  id: number;
  doc_id: string;
  doc_name: string;
  chars: number;
}

export interface SessionInfo {
  id: string;
  title: string;
  updated_at: string;
}

export interface Source {
  index: number;
  doc: string;
  heading: string;
  page: number | null;
  snippet: string;
}

/** 链路跟踪：一次回答的完整流水线记录（后端 trace 的形状） */
export interface TraceHit {
  doc_id: string;
  seq: number | null;
  heading: string;
  score: number;
  excerpt: string;
}

export interface TraceInfo {
  question: string;
  rewritten_question: string;
  retrieval: {
    query_used?: string;
    vector: TraceHit[];
    bm25: TraceHit[];
    fused: string[];
    gate: { enabled: boolean; relevant: boolean | null; raw?: string };
    timings_ms: Record<string, number>;
  };
  final_prompt?: string | null;
  timings_ms?: Record<string, number | Record<string, number>>;
}

export interface ChatMessageInfo {
  role: "user" | "assistant";
  content: string;
  sources: Source[];
  trace?: TraceInfo | null;
}

// ---------- 普通请求 ----------

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    // 后端 FastAPI 的错误信息在 body.detail 里，取出来展示
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(body.detail ?? `请求失败 (${res.status})`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  // 文档管理
  listDocuments: () => fetch("/api/documents").then((r) => json<DocInfo[]>(r)),
  uploadDocuments: (files: File[]) => {
    const form = new FormData();
    files.forEach((f) => form.append("files", f));
    return fetch("/api/documents", { method: "POST", body: form }).then((r) =>
      json<DocInfo[]>(r)
    );
  },
  deleteDocument: (id: string) =>
    fetch(`/api/documents/${id}`, { method: "DELETE" }).then((r) => json<{ ok: boolean }>(r)),
  reingestDocument: (id: string) =>
    fetch(`/api/documents/${id}/reingest`, { method: "POST" }).then((r) =>
      json<{ ok: boolean }>(r)
    ),

  // Chunk 管理
  listChunks: (params: { docId?: string; q?: string; page: number; size: number }) => {
    const search = new URLSearchParams({
      page: String(params.page),
      size: String(params.size),
    });
    if (params.docId) search.set("doc_id", params.docId);
    if (params.q) search.set("q", params.q);
    return fetch(`/api/chunks?${search}`).then((r) =>
      json<{ total: number; page: number; size: number; chunks: ChunkRowInfo[] }>(r)
    );
  },
  editChunk: (id: number, content: string) =>
    fetch(`/api/chunks/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    }).then((r) => json<{ ok: boolean }>(r)),
  optimizeChunk: (content: string, headingPath: string) =>
    fetch("/api/chunks/optimize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content, heading_path: headingPath }),
    }).then((r) => json<{ content: string }>(r)),

  // 会话
  listSessions: () => fetch("/api/chat/sessions").then((r) => json<SessionInfo[]>(r)),
  createSession: () =>
    fetch("/api/chat/sessions", { method: "POST" }).then((r) => json<SessionInfo>(r)),
  deleteSession: (id: string) =>
    fetch(`/api/chat/sessions/${id}`, { method: "DELETE" }).then((r) =>
      json<{ ok: boolean }>(r)
    ),
  renameSession: (id: string, title: string) =>
    fetch(`/api/chat/sessions/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    }).then((r) => json<{ ok: boolean }>(r)),
  getMessages: (id: string) =>
    fetch(`/api/chat/sessions/${id}/messages`).then((r) => json<ChatMessageInfo[]>(r)),
};

// ---------- 设置：供应商与模型配置 ----------

export interface ProviderTemplate {
  id: string;
  name: string;
  base_url: string;
  key_url: string;
  note: string;
  models_api: boolean;
}

export interface ModelEntry {
  id: string; // 模型名，如 glm-4-flash
  kind: "chat" | "embed";
}

export interface ProviderInfo {
  id: string;
  name: string;
  base_url: string;
  api_key_masked: string;
  has_key: boolean;
  models: ModelEntry[];
  created_at: string;
}

export type RoleAssign = { provider_id: string; model: string } | null;

export interface ModelSettings {
  chat: RoleAssign;
  embed: RoleAssign;
  optimize: RoleAssign;
}

/** 设置页专用：供应商与模型配置的 API（独立成对象，和业务 api 分开） */
export const settingsApi = {
    listTemplates: () => fetch("/api/providers/templates").then((r) => json<ProviderTemplate[]>(r)),
    listProviders: () => fetch("/api/providers").then((r) => json<ProviderInfo[]>(r)),
    createProvider: (body: { name: string; base_url: string; api_key: string; models: ModelEntry[] }) =>
      fetch("/api/providers", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }).then((r) => json<ProviderInfo>(r)),
    updateProvider: (
      id: string,
      body: { name?: string; base_url?: string; api_key?: string; models?: ModelEntry[] }
    ) =>
      fetch(`/api/providers/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }).then((r) => json<ProviderInfo>(r)),
    deleteProvider: (id: string) =>
      fetch(`/api/providers/${id}`, { method: "DELETE" }).then((r) =>
        json<{ ok: boolean; cleared_roles: string[] }>(r)
      ),
    fetchModels: (baseUrl: string, apiKey: string) =>
      fetch("/api/providers/fetch-models", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ base_url: baseUrl, api_key: apiKey }),
      }).then((r) => json<ModelEntry[]>(r)),
    testProvider: (id: string) =>
      fetch(`/api/providers/${id}/test`, { method: "POST" }).then((r) =>
        json<{
          chat: { ok: boolean; model?: string; error?: string | null };
          embed: { ok: boolean; model?: string; error?: string | null } | null;
        }>(r)
      ),
    getModelSettings: () => fetch("/api/settings/models").then((r) => json<ModelSettings>(r)),
    putModelSettings: (body: ModelSettings) =>
      fetch("/api/settings/models", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }).then((r) => json<{ ok: boolean; vector_wiped: boolean; message: string | null }>(r)),
    reingestAll: () =>
      fetch("/api/documents/reingest-all", { method: "POST" }).then((r) =>
        json<{ ok: boolean; submitted: number; skipped: number }>(r)
      ),
};

// ---------- SSE 流式问答 ----------

/** 提问并流式处理事件。onEvent 收到 {type:"sources"|"token"|"done"|"error"} */
export async function askStream(
  sessionId: string,
  question: string,
  onEvent: (ev: any) => void
): Promise<void> {
  const res = await fetch(`/api/chat/sessions/${sessionId}/ask`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
  });
  if (!res.ok || !res.body) {
    const body = await res.json().catch(() => ({ detail: "请求失败" }));
    throw new Error(body.detail ?? `请求失败 (${res.status})`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = ""; // 跨网络数据块的不完整行会暂存在这里

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // SSE 消息以空行分隔；可能一次收到多条，逐条处理
    const messages = buffer.split("\n\n");
    buffer = messages.pop() ?? ""; // 最后一段可能不完整，留到下一轮
    for (const msg of messages) {
      const line = msg.trim();
      if (line.startsWith("data: ")) {
        onEvent(JSON.parse(line.slice(6)));
      }
    }
  }
}

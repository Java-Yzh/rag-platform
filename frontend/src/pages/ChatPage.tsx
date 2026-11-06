import { useEffect, useRef, useState } from "react";
import {
  api,
  askStream,
  type ChatMessageInfo,
  type SessionInfo,
  type Source,
  type TraceHit,
  type TraceInfo,
} from "../api";

/**
 * 对话页：左侧会话列表 + 右侧聊天窗口。
 *
 * React 知识点——流式渲染怎么实现：
 * 回答没结束前，把逐个收到的 token 拼进一个"正在生成"的临时消息里，
 * 每次 setState React 就会重渲染一次，肉眼看就是打字机效果。
 * 回答结束（done 事件）后才把整段话正式写入消息列表。
 */
export default function ChatPage() {
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessageInfo[]>([]);
  const [input, setInput] = useState("");
  // 正在生成的回答：null 表示当前没有在回答
  const [streaming, setStreaming] = useState<{ text: string; sources: Source[] } | null>(null);
  const [busy, setBusy] = useState(false);
  // 会话重命名：renamingId 记录"哪一行正在编辑"，renameText 是输入框内容
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameText, setRenameText] = useState("");
  // 工作中计时：每次提问从 0 起跳，每秒 +1，回答结束停止（驱动 WorkingStatus 的秒数）
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    if (!busy) return;
    setElapsed(0);
    const timer = setInterval(() => setElapsed((s) => s + 1), 1000);
    // 组件卸载或 busy 变化时清掉定时器（useEffect 返回清理函数是 React 的固定套路）
    return () => clearInterval(timer);
  }, [busy]);
  const bottomRef = useRef<HTMLDivElement>(null); // 用于自动滚到底部

  // ---------- 会话管理 ----------

  const refreshSessions = async () => setSessions(await api.listSessions());

  useEffect(() => {
    refreshSessions();
  }, []);

  // 切换会话时加载历史消息
  useEffect(() => {
    if (activeId) api.getMessages(activeId).then(setMessages);
    else setMessages([]);
  }, [activeId]);

  // 新消息/流式更新时滚到底部
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, streaming]);

  const newSession = async () => {
    const s = await api.createSession();
    await refreshSessions();
    setActiveId(s.id);
  };

  const removeSession = async (id: string) => {
    await api.deleteSession(id);
    if (activeId === id) setActiveId(null);
    refreshSessions();
  };

  // ---------- 会话重命名（行内编辑：✎ 变输入框，回车保存 / Esc 取消） ----------

  const startRename = (s: SessionInfo) => {
    setRenamingId(s.id);
    setRenameText(s.title);
  };

  const commitRename = async () => {
    const title = renameText.trim();
    if (renamingId && title) {
      await api.renameSession(renamingId, title);
      await refreshSessions();
    }
    setRenamingId(null); // 无论保存还是取消，都退出编辑态
  };

  // ---------- 发送与流式接收 ----------

  const send = async () => {
    const question = input.trim();
    if (!question || busy || !activeId) return;
    setInput("");
    setBusy(true);
    // 用户消息先上屏；助手占位为空
    setMessages((prev) => [...prev, { role: "user", content: question, sources: [] }]);
    setStreaming({ text: "", sources: [] });
    // 记住本次回答的来源列表：done 事件里要用它组装正式消息
    // （不存的话，流式临时气泡被替换成正式消息时来源就丢了）
    let latestSources: Source[] = [];

    try {
      await askStream(activeId, question, (ev) => {
        if (ev.type === "sources") {
          latestSources = ev.sources;
          setStreaming((s) => (s ? { ...s, sources: ev.sources } : s));
        } else if (ev.type === "token") {
          // 逐 token 拼接，React 自动重渲染 = 打字机效果
          setStreaming((s) => (s ? { ...s, text: s.text + ev.content } : s));
        } else if (ev.type === "done") {
          setMessages((prev) => [
            ...prev,
            // trace 和 sources 都要在这一刻带上（sources 来自闭包变量）
            { role: "assistant", content: ev.answer, sources: latestSources, trace: ev.trace },
          ]);
          setStreaming(null);
          refreshSessions(); // 标题可能被后端更新了
        } else if (ev.type === "error") {
          setMessages((prev) => [
            ...prev,
            { role: "assistant", content: `⚠️ 出错了：${ev.message}`, sources: [] },
          ]);
          setStreaming(null);
        }
      });
    } catch (e) {
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: `⚠️ ${String(e)}`, sources: [] },
      ]);
      setStreaming(null);
    } finally {
      setBusy(false);
    }
  };

  // ---------- 渲染 ----------

  return (
    <div className="flex h-full">
      {/* 会话列表 */}
      <div className="flex w-60 shrink-0 flex-col border-r border-gray-200 bg-white">
        <button
          onClick={newSession}
          className="m-3 rounded-lg bg-blue-600 py-2 text-sm font-medium text-white hover:bg-blue-700"
        >
          ＋ 新对话
        </button>
        <div className="flex-1 overflow-y-auto px-2 pb-3">
          {sessions.map((s) =>
            renamingId === s.id ? (
              // 编辑态：整行变成输入框，回车保存 / Esc 取消 / 失焦保存
              <input
                key={s.id}
                autoFocus
                value={renameText}
                onChange={(e) => setRenameText(e.target.value)}
                onBlur={commitRename}
                onKeyDown={(e) => {
                  if (e.key === "Enter") commitRename();
                  if (e.key === "Escape") setRenamingId(null);
                }}
                className="my-1 w-full rounded-lg border border-blue-300 px-2 py-1.5 text-sm focus:border-blue-500 focus:outline-none"
              />
            ) : (
              <div
                key={s.id}
                onClick={() => setActiveId(s.id)}
                className={`group flex cursor-pointer items-center justify-between rounded-lg px-3 py-2 text-sm ${
                  activeId === s.id ? "bg-blue-50 text-blue-700" : "text-gray-600 hover:bg-gray-50"
                }`}
              >
                <span className="truncate">{s.title}</span>
                <span className="ml-2 hidden shrink-0 gap-1 group-hover:flex">
                  <button
                    onClick={(e) => {
                      e.stopPropagation(); // 阻止冒泡：点重命名不要触发"选中会话"
                      startRename(s);
                    }}
                    className="text-gray-300 hover:text-blue-500"
                    title="重命名"
                  >
                    ✎
                  </button>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      removeSession(s.id);
                    }}
                    className="text-gray-300 hover:text-red-500"
                    title="删除会话"
                  >
                    ✕
                  </button>
                </span>
              </div>
            )
          )}
        </div>
      </div>

      {/* 聊天窗口 */}
      <div className="flex min-w-0 flex-1 flex-col">
        {!activeId ? (
          <div className="flex flex-1 items-center justify-center text-sm text-gray-400">
            点击「新对话」开始提问，答案基于你上传的文档
          </div>
        ) : (
          <>
            <div className="flex-1 overflow-y-auto px-6 py-4">
              <div className="mx-auto max-w-3xl space-y-4">
                {messages.map((m, i) => (
                  <MessageBubble
                    key={i}
                    role={m.role}
                    content={m.content}
                    sources={m.sources}
                    trace={m.trace}
                  />
                ))}
                {/* 正在生成的回答 */}
                {streaming && (
                  <MessageBubble
                    role="assistant"
                    content={streaming.text}
                    sources={streaming.sources}
                    streaming
                    elapsed={elapsed}
                  />
                )}
                <div ref={bottomRef} />
              </div>
            </div>
            <div className="border-t border-gray-200 bg-white px-6 py-4">
              <div className="mx-auto flex max-w-3xl gap-3">
                <input
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => {
                    // 中文输入法"选字确认"也会触发一次 Enter，此时
                    // isComposing 为 true（Safari 老版本用 keyCode 229 表达）。
                    // 必须放行给输入法，否则拼音没选完消息就发出去了
                    if (e.key === "Enter" && !e.nativeEvent.isComposing && e.keyCode !== 229) {
                      send();
                    }
                  }}
                  placeholder="基于文档提问…（回车发送）"
                  disabled={busy}
                  className="flex-1 rounded-lg border border-gray-300 px-4 py-2.5 text-sm focus:border-blue-500 focus:outline-none disabled:bg-gray-50"
                />
                <button
                  onClick={send}
                  disabled={busy || !input.trim()}
                  className="rounded-lg bg-blue-600 px-5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
                >
                  {busy ? "回答中…" : "发送"}
                </button>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

/** 工作中状态：旋转图标 + 微光文字 + 递增秒数（zcode 风格） */
function WorkingStatus({ seconds }: { seconds: number }) {
  return (
    <span className="inline-flex items-center gap-2 py-1" aria-label="正在思考">
      {/* animate-spin 是 Tailwind 内置的旋转动画 */}
      <svg
        className="h-3.5 w-3.5 animate-spin text-gray-400"
        viewBox="0 0 24 24"
        fill="none"
      >
        <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" className="opacity-25" />
        <path d="M22 12a10 10 0 0 0-10-10" stroke="currentColor" strokeWidth="4" strokeLinecap="round" />
      </svg>
      <span className="shimmer-text text-sm">正在检索与思考</span>
      {/* tabular-nums：等宽数字，跳动时宽度不变 */}
      <span className="text-xs tabular-nums text-gray-400">{seconds}s</span>
    </span>
  );
}

/** 复制按钮：悬停显示，点击后复制并短暂变成 ✓ 反馈（参考 zcode 交互） */
function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    // Clipboard API 是浏览器标准做法；localhost 属于安全上下文可直接用
    await navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500); // 1.5 秒后恢复图标
  };

  return (
    <button
      onClick={copy}
      title={copied ? "已复制" : "复制"}
      className={`shrink-0 rounded-md px-1.5 py-1 text-xs transition-opacity ${
        copied
          ? "text-green-500 opacity-100"
          : "text-gray-400 opacity-0 hover:text-gray-600 group-hover:opacity-100"
      }`}
    >
      {copied ? (
        "✓ 已复制"
      ) : (
        // 两个叠放圆角矩形 = 通用"复制"图标（SVG 手绘，免去引入图标库）
        <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
          <rect x="5" y="5" width="8" height="8" rx="1.5" />
          <path d="M11 5V3.5A1.5 1.5 0 0 0 9.5 2H4a1.5 1.5 0 0 0-1.5 1.5V9A1.5 1.5 0 0 0 4 10.5h1" />
        </svg>
      )}
    </button>
  );
}

/** 链路面板的一路检索命中列表 */
function TraceHits({ title, hits }: { title: string; hits: TraceHit[] }) {
  return (
    <div>
      <div className="font-medium text-gray-700">{title}</div>
      {hits.length === 0 ? (
        <div className="mt-1 text-gray-400">无命中</div>
      ) : (
        <div className="mt-1 space-y-0.5">
          {hits.map((h, i) => (
            <div key={i} className="flex gap-2 rounded bg-white px-2 py-1">
              <span className="w-12 shrink-0 text-right font-mono text-blue-600">{h.score}</span>
              <span className="min-w-0 truncate">
                {h.heading || "（无章节）"} · {h.excerpt}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** 链路跟踪面板：展示本次回答从提问到生成的完整流水线（调优的"黑匣子"） */
function TracePanel({ trace }: { trace: TraceInfo }) {
  const r = trace.retrieval;
  const t = (trace.timings_ms ?? {}) as Record<string, any>;
  const changed = trace.rewritten_question !== trace.question;

  return (
    <div className="mt-2 space-y-2.5 rounded-lg border border-gray-200 bg-gray-50 p-3 text-xs text-gray-600">
      <div>
        <div className="font-medium text-gray-700">① 问题改写{changed ? "" : "（无变化）"}</div>
        <div className="mt-1 rounded bg-white px-2 py-1 font-mono">{trace.rewritten_question}</div>
      </div>

      <TraceHits title={`② 向量检索（${r.vector.length} 条）`} hits={r.vector} />
      <TraceHits title={`③ BM25 检索（${r.bm25.length} 条）`} hits={r.bm25} />

      <div>
        <div className="font-medium text-gray-700">④ RRF 融合 + 相关性把关</div>
        <div className="mt-1">
          {r.gate.relevant === null
            ? "把关未启用"
            : r.gate.relevant
              ? `✅ 判定相关，放行 ${r.fused.length} 条`
              : `❌ 判定全部无关，来源清空（把关模型原话：${r.gate.raw}）`}
        </div>
      </div>

      {trace.final_prompt && (
        <details>
          <summary className="cursor-pointer font-medium text-gray-700">
            ⑤ 发给模型的完整提示词（{trace.final_prompt.length} 字，点开查看）
          </summary>
          <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap rounded bg-white p-2 font-mono text-[11px] leading-relaxed">
            {trace.final_prompt}
          </pre>
        </details>
      )}

      <div className="text-gray-400">
        耗时：改写 {String(t.rewrite_ms ?? "-")}ms · 向量 {String(r.timings_ms.vector_ms ?? "-")}ms ·
        BM25 {String(r.timings_ms.bm25_ms ?? "-")}ms · 把关 {String(r.timings_ms.gate_ms ?? "-")}ms ·
        生成 {String(t.generate_ms ?? "-")}ms
      </div>
    </div>
  );
}

/** 单条消息气泡：用户靠右、助手靠左；助手消息下方渲染引用来源卡片 */
function MessageBubble({
  role,
  content,
  sources,
  streaming,
  elapsed = 0,
  trace = null,
}: {
  role: string;
  content: string;
  sources: Source[];
  streaming?: boolean;
  elapsed?: number;
  trace?: TraceInfo | null;
}) {
  const [showSources, setShowSources] = useState(true);
  const [showTrace, setShowTrace] = useState(false);
  const isUser = role === "user";
  return (
    <div className={`group flex items-start gap-1.5 ${isUser ? "justify-end" : "justify-start"}`}>
      {/* 用户消息在右：复制按钮放气泡左侧 */}
      {isUser && !streaming && (
        <div className="mt-1.5">
          <CopyButton text={content} />
        </div>
      )}
      <div className={`max-w-[85%] ${isUser ? "" : "w-full"}`}>
        <div
          className={`whitespace-pre-wrap rounded-2xl px-4 py-2.5 text-sm leading-relaxed ${
            isUser
              ? "rounded-br-sm bg-blue-600 text-white"
              : "rounded-bl-sm border border-gray-200 bg-white"
          }`}
        >
          {/* 流式等待中且还没收到字：工作中状态条；来了字：正文 + 闪烁光标 */}
          {streaming && !content ? <WorkingStatus seconds={elapsed} /> : content}
          {streaming && !!content && <span className="ml-0.5 animate-pulse">▍</span>}
        </div>
        {/* 助手消息全宽：操作条（复制 + 链路）放气泡下方（生成中不显示） */}
        {!isUser && !streaming && (
          <div className="-mt-0.5 flex items-center gap-1">
            <CopyButton text={content} />
            {trace && (
              <button
                onClick={() => setShowTrace((v) => !v)}
                title="查看本次回答的完整链路"
                className={`rounded-md px-1.5 py-1 text-xs transition-opacity ${
                  showTrace
                    ? "font-medium text-blue-600 opacity-100"
                    : "text-gray-400 opacity-0 hover:text-gray-600 group-hover:opacity-100"
                }`}
              >
                🔍 链路
              </button>
            )}
          </div>
        )}
        {/* 链路跟踪面板（点击 🔍 展开） */}
        {showTrace && trace && <TracePanel trace={trace} />}
        {/* 引用来源卡片（仅助手消息且检索到了内容时显示） */}
        {!isUser && sources.length > 0 && (
          <div className="mt-2">
            <button
              onClick={() => setShowSources((v) => !v)}
              className="text-xs text-gray-400 hover:text-gray-600"
            >
              📎 来源 {sources.length} 条 {showSources ? "▲" : "▼"}
            </button>
            {showSources && (
              <div className="mt-1.5 grid gap-1.5">
                {sources.map((s) => (
                  <div
                    key={s.index}
                    className="rounded-lg border border-gray-100 bg-gray-50 px-3 py-2 text-xs text-gray-500"
                  >
                    <div className="font-medium text-gray-700">
                      [{s.index}] {s.doc}
                      {s.page != null && s.page > 0 && ` · 第${s.page}页`}
                    </div>
                    {s.heading && <div className="mt-0.5">{s.heading}</div>}
                    <div className="mt-1 line-clamp-2 text-gray-400">{s.snippet}…</div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

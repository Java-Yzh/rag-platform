import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, type ChunkRowInfo, type DocInfo } from "../api";

/**
 * Chunk 管理页：跨文档检索 + 手工修正 chunk 文本。
 *
 * 两个筛选维度可叠加：
 *   文件名（选择器，本质是按 doc_id 过滤）× 内容关键词（LIKE 搜索）。
 * 编辑保存后，后端会同步重嵌入向量并重建 BM25——这是本页存在的意义：
 * 改完的 chunk 立刻以正确的语义参与检索。
 */
export default function ChunkPage() {
  // ---------- 筛选状态 ----------
  const [docs, setDocs] = useState<DocInfo[]>([]);
  const [selectedDoc, setSelectedDoc] = useState<DocInfo | null>(null); // 选中的文档（null=全部）
  const [docInput, setDocInput] = useState(""); // 文件名过滤输入框的文字
  const [showDocList, setShowDocList] = useState(false);
  const [qInput, setQInput] = useState(""); // 内容搜索输入框
  const [q, setQ] = useState(""); // 已提交的内容搜索（回车/点按钮才生效，避免打字过程中频繁请求）

  // ---------- 列表与编辑状态 ----------
  const [page, setPage] = useState(1);
  const [data, setData] = useState<{
    total: number;
    size: number;
    chunks: ChunkRowInfo[];
  } | null>(null);
  const [editingId, setEditingId] = useState<number | null>(null); // 正在编辑的 chunk id
  const [editText, setEditText] = useState("");
  const [saving, setSaving] = useState(false);
  const [optimizing, setOptimizing] = useState(false); // AI 优化进行中

  // 文档列表加载一次（给文件名选择器用）
  useEffect(() => {
    api.listDocuments().then(setDocs);
  }, []);

  // URL 参数 ?doc=xxx 预选文档（从文档管理页"查看 chunk"跳转过来的场景）。
  // 关键：参数只消费一次，用完立刻从地址栏抹掉——否则它会一直留在 URL 里，
  // 用户点 ✕ 清筛选或换选别的文档时，这个 effect 重跑又会把筛选塞回去
  const [searchParams, setSearchParams] = useSearchParams();
  const appliedDocParam = useRef(false);
  useEffect(() => {
    if (appliedDocParam.current) return; // 已消费过，不再干预用户的选择
    const id = searchParams.get("doc");
    if (!id || !docs.length) return;
    const d = docs.find((x) => x.id === id);
    if (!d) return;
    appliedDocParam.current = true;
    setSelectedDoc(d);
    setDocInput(d.filename);
    setPage(1);
    // replace: true = 不留历史记录（浏览器后退不会回到带参数的地址）
    setSearchParams({}, { replace: true });
  }, [docs, searchParams, setSearchParams]);

  // 筛选条件变化时加载 chunk 列表
  useEffect(() => {
    api
      .listChunks({ docId: selectedDoc?.id, q, page, size: 10 })
      .then((d) => setData({ total: d.total, size: d.size, chunks: d.chunks }));
  }, [selectedDoc, q, page]);

  // ---------- 交互 ----------

  /** 按输入的文字过滤文档（文件名包含即命中，取前 20 条防止下拉过长） */
  const filteredDocs = docs
    .filter((d) => d.filename.toLowerCase().includes(docInput.toLowerCase()))
    .slice(0, 20);

  const pickDoc = (d: DocInfo) => {
    setSelectedDoc(d);
    setDocInput(d.filename);
    setShowDocList(false);
    setPage(1);
  };

  const clearDoc = () => {
    setSelectedDoc(null);
    setDocInput("");
    setPage(1);
  };

  const commitQ = () => {
    setQ(qInput.trim());
    setPage(1);
  };

  const startEdit = (c: ChunkRowInfo) => {
    setEditingId(c.id);
    setEditText(c.content);
  };

  const saveEdit = async () => {
    if (editingId == null || saving) return;
    setSaving(true);
    try {
      await api.editChunk(editingId, editText);
      setEditingId(null);
      // 重新拉当前页，让列表立刻反映新文本
      const d = await api.listChunks({ docId: selectedDoc?.id, q, page, size: 10 });
      setData({ total: d.total, size: d.size, chunks: d.chunks });
    } catch (e) {
      alert(String(e)); // 后端的校验错误（如内容为空）直接弹窗
    } finally {
      setSaving(false);
    }
  };

  /** AI 优化：结果覆写编辑框（不保存），可继续手工修改，满意了再点保存 */
  const optimizeEdit = async () => {
    if (optimizing || !editText.trim()) return;
    setOptimizing(true);
    try {
      const chunk = data?.chunks.find((c) => c.id === editingId);
      const res = await api.optimizeChunk(editText, chunk?.heading_path ?? "");
      setEditText(res.content);
    } catch (e) {
      alert(String(e));
    } finally {
      setOptimizing(false);
    }
  };

  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.size)) : 1;

  // ---------- 渲染 ----------

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto max-w-6xl space-y-4">
        {/* 工具栏：文件名选择器 + 内容搜索 */}
        <div className="flex flex-wrap items-center gap-3">
          {/* 文件名选择器：输入过滤 + 下拉点选 */}
          <div className="relative w-72">
            <input
              value={selectedDoc ? selectedDoc.filename : docInput}
              onChange={(e) => {
                // 已选中状态下再打字 = 重新开始筛选，取消选中
                if (selectedDoc) clearDoc();
                setDocInput(e.target.value);
                setShowDocList(true);
              }}
              onFocus={() => setShowDocList(true)}
              onBlur={() => setTimeout(() => setShowDocList(false), 150)} // 延迟收起，让点击事件来得及触发
              placeholder="🔍 按文件名筛选…"
              className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm focus:border-blue-500 focus:outline-none"
            />
            {selectedDoc && (
              <button
                onClick={clearDoc}
                title="清除，查看全部文档"
                className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-300 hover:text-gray-500"
              >
                ✕
              </button>
            )}
            {showDocList && filteredDocs.length > 0 && (
              <div className="absolute z-20 mt-1 max-h-64 w-full overflow-y-auto rounded-lg border border-gray-200 bg-white shadow-lg">
                {filteredDocs.map((d) => (
                  <div
                    key={d.id}
                    // onMouseDown 先于输入框的 onBlur 触发，保证能点中得到
                    onMouseDown={() => pickDoc(d)}
                    className="cursor-pointer truncate px-3 py-2 text-sm hover:bg-blue-50"
                  >
                    {d.filename}
                    <span className="ml-2 text-xs text-gray-400">{d.chunk_count} 块</span>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* 内容关键词搜索 */}
          <input
            value={qInput}
            onChange={(e) => setQInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.nativeEvent.isComposing && e.keyCode !== 229) commitQ();
            }}
            placeholder="🔍 在 chunk 内容中搜索…（回车生效）"
            className="w-72 rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm focus:border-blue-500 focus:outline-none"
          />
          <button
            onClick={commitQ}
            className="rounded-lg bg-blue-600 px-4 py-2 text-sm text-white hover:bg-blue-700"
          >
            搜索
          </button>
        </div>

        {/* 结果统计 */}
        <p className="text-xs text-gray-400">
          {data ? `共 ${data.total} 个 chunk` : "加载中…"}
          {selectedDoc && ` · 文件：${selectedDoc.filename}`}
          {q && ` · 内容含「${q}」`}
        </p>

        {/* chunk 表格 */}
        <table className="w-full overflow-hidden rounded-xl border border-gray-200 bg-white text-sm">
          <thead>
            <tr className="border-b border-gray-200 bg-gray-50 text-left text-xs text-gray-400">
              <th className="px-4 py-2.5">所属文件</th>
              <th className="w-14 px-2 py-2.5">序号</th>
              <th className="px-4 py-2.5">章节</th>
              <th className="w-16 px-2 py-2.5">页码</th>
              <th className="px-4 py-2.5">内容</th>
              <th className="w-16 px-2 py-2.5">字符数</th>
              <th className="w-20 px-4 py-2.5 text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            {(data?.chunks ?? []).map((c) =>
              editingId === c.id ? (
                // 编辑态：整行变成一个大编辑区（meta 行 + textarea + 按钮）
                <tr key={c.id} className="border-b border-gray-50 bg-blue-50/40">
                  <td colSpan={7} className="px-4 py-3">
                    <div className="mb-2 text-xs text-gray-400">
                      {c.doc_name} · #{c.seq} · {c.heading_path || "（无章节）"}
                      {c.page_no != null && ` · 第${c.page_no}页`} · 保存后将重新计算向量
                    </div>
                    <textarea
                      value={editText}
                      onChange={(e) => setEditText(e.target.value)}
                      rows={6}
                      className="w-full rounded-lg border border-blue-300 p-2 font-mono text-xs leading-relaxed focus:border-blue-500 focus:outline-none"
                    />
                    <div className="mt-2 flex justify-end gap-3 text-sm">
                      <button
                        onClick={() => setEditingId(null)}
                        className="rounded-lg px-3 py-1.5 text-gray-500 hover:bg-gray-100"
                      >
                        取消
                      </button>
                      <button
                        onClick={optimizeEdit}
                        disabled={optimizing || saving || !editText.trim()}
                        title="AI 优化文本结构使其更利于检索（不保存，可继续手改）"
                        className="rounded-lg border border-blue-300 px-3 py-1.5 text-blue-600 hover:bg-blue-50 disabled:opacity-50"
                      >
                        {optimizing ? "AI 优化中…" : "✨ AI 优化"}
                      </button>
                      <button
                        onClick={saveEdit}
                        disabled={saving || !editText.trim()}
                        className="rounded-lg bg-blue-600 px-4 py-1.5 text-white hover:bg-blue-700 disabled:opacity-50"
                      >
                        {saving ? "保存中…" : "保存（重嵌入向量）"}
                      </button>
                    </div>
                  </td>
                </tr>
              ) : (
                <tr key={c.id} className="border-b border-gray-50 last:border-0 hover:bg-gray-50/60">
                  <td className="max-w-[160px] truncate px-4 py-2.5" title={c.doc_name}>
                    {c.doc_name}
                  </td>
                  <td className="px-2 py-2.5 font-mono text-xs text-gray-500">#{c.seq}</td>
                  <td className="max-w-[140px] truncate px-4 py-2.5 text-xs text-gray-500" title={c.heading_path}>
                    {c.heading_path || "—"}
                  </td>
                  <td className="px-2 py-2.5 text-xs text-gray-500">
                    {c.page_no != null ? c.page_no : "—"}
                  </td>
                  <td className="max-w-[420px] px-4 py-2.5">
                    <p className="line-clamp-2 text-xs leading-relaxed text-gray-700">{c.content}</p>
                  </td>
                  <td className="px-2 py-2.5 text-xs tabular-nums text-gray-400">{c.chars}</td>
                  <td className="px-4 py-2.5 text-right">
                    <button
                      onClick={() => startEdit(c)}
                      className="text-blue-600 hover:underline"
                    >
                      编辑
                    </button>
                  </td>
                </tr>
              )
            )}
            {data && data.chunks.length === 0 && (
              <tr>
                <td colSpan={7} className="px-4 py-10 text-center text-gray-400">
                  没有匹配的 chunk——换个文件名或关键词试试
                </td>
              </tr>
            )}
          </tbody>
        </table>

        {/* 分页 */}
        <div className="flex items-center justify-between text-sm">
          <button
            disabled={page <= 1}
            onClick={() => setPage((p) => p - 1)}
            className="text-gray-500 disabled:text-gray-300"
          >
            ← 上一页
          </button>
          <span className="text-xs text-gray-400">
            {page} / {totalPages}
          </span>
          <button
            disabled={page >= totalPages}
            onClick={() => setPage((p) => p + 1)}
            className="text-gray-500 disabled:text-gray-300"
          >
            下一页 →
          </button>
        </div>
      </div>
    </div>
  );
}

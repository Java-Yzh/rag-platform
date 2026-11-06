import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, type DocInfo } from "../api";

/**
 * 文档管理页：拖拽/点击上传 + 文档表格。
 *
 * 状态轮询知识点：Docling 转换一份文档要几十秒，后端是异步处理的，
 * 前端每 2 秒拉一次列表刷新状态徽章；全部处理完后停止轮询省资源。
 *
 * "查看 chunk"不再用抽屉（已并入 Chunk 管理页），改为跳转并带上
 * 该文档的筛选参数——单一入口，少维护一套 UI。
 */
export default function DocumentsPage() {
  const [docs, setDocs] = useState<DocInfo[]>([]);
  const [uploading, setUploading] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const navigate = useNavigate();

  const refresh = useCallback(async () => {
    setDocs(await api.listDocuments());
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // 存在进行中的任务时每 2 秒轮询；全部结束则停止
  useEffect(() => {
    const busy = docs.some((d) => d.status === "pending" || d.status === "processing");
    if (!busy) return;
    const t = setInterval(refresh, 2000);
    return () => clearInterval(t);
  }, [docs, refresh]);

  // ---------- 上传 ----------

  const upload = async (files: FileList | File[] | null) => {
    if (!files || uploading) return;
    setUploading(true);
    try {
      await api.uploadDocuments(Array.from(files));
      await refresh(); // 立刻刷一次，让新文档以 pending 状态出现
    } catch (e) {
      alert(String(e)); // 后端返回的错误（如格式不支持）直接弹窗
    } finally {
      setUploading(false);
    }
  };

  // ---------- 操作 ----------

  const del = async (d: DocInfo) => {
    if (!confirm(`删除「${d.filename}」？向量与索引会一并清除`)) return;
    await api.deleteDocument(d.id);
    refresh();
  };

  const reingest = async (d: DocInfo) => {
    await api.reingestDocument(d.id);
    refresh();
  };

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto max-w-5xl space-y-5">
        {/* 上传区 */}
        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDragOver(true);
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragOver(false);
            upload(e.dataTransfer.files);
          }}
          onClick={() => fileInput.current?.click()}
          className={`flex cursor-pointer flex-col items-center justify-center rounded-xl border-2 border-dashed py-10 text-sm transition-colors ${
            dragOver ? "border-blue-500 bg-blue-50" : "border-gray-300 bg-white hover:bg-gray-50"
          }`}
        >
          <div className="text-3xl">📥</div>
          <p className="mt-2 text-gray-600">
            {uploading ? "上传中…" : "拖拽文件到此处，或点击选择（支持批量）"}
          </p>
          <p className="mt-1 text-xs text-gray-400">
            支持 PDF / Word / PPT / Excel / HTML / Markdown / CSV / TXT，单文件 ≤ 100MB
          </p>
          <input
            ref={fileInput}
            type="file"
            multiple
            hidden
            accept=".pdf,.docx,.pptx,.xlsx,.html,.md,.csv,.txt"
            onChange={(e) => {
              upload(e.target.files);
              e.target.value = ""; // 允许连续选同一个文件
            }}
          />
        </div>

        {/* 文档表格 */}
        <table className="w-full overflow-hidden rounded-xl border border-gray-200 bg-white text-sm">
          <thead>
            <tr className="border-b border-gray-200 bg-gray-50 text-left text-xs text-gray-400">
              <th className="px-4 py-2.5">文件名</th>
              <th className="px-4 py-2.5">大小</th>
              <th className="px-4 py-2.5">状态</th>
              <th className="px-4 py-2.5">切块</th>
              <th className="px-4 py-2.5 text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            {docs.length === 0 && (
              <tr>
                <td colSpan={5} className="px-4 py-10 text-center text-gray-400">
                  还没有文档，先上传一份试试
                </td>
              </tr>
            )}
            {docs.map((d) => (
              <tr key={d.id} className="border-b border-gray-50 last:border-0 hover:bg-gray-50/60">
                <td className="max-w-[280px] truncate px-4 py-2.5" title={d.filename}>
                  {d.filename}
                </td>
                <td className="px-4 py-2.5 text-gray-500">{fmtSize(d.size_bytes)}</td>
                <td className="px-4 py-2.5">
                  <StatusBadge status={d.status} error={d.error} />
                </td>
                <td className="px-4 py-2.5 text-gray-500">
                  {d.status === "done" ? `${d.chunk_count} 块` : "—"}
                </td>
                <td className="whitespace-nowrap px-4 py-2.5 text-right">
                  <button
                    onClick={() => navigate(`/chunks?doc=${d.id}`)}
                    disabled={d.status !== "done"}
                    className="text-blue-600 hover:underline disabled:text-gray-300"
                  >
                    查看 chunk
                  </button>
                  <button
                    onClick={() => reingest(d)}
                    disabled={d.status === "pending" || d.status === "processing"}
                    className="ml-3 text-gray-500 hover:underline disabled:text-gray-300"
                  >
                    重新入库
                  </button>
                  <button onClick={() => del(d)} className="ml-3 text-red-500 hover:underline">
                    删除
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** 状态徽章 + 失败原因提示 */
function StatusBadge({ status, error }: { status: DocInfo["status"]; error: string | null }) {
  const map: Record<string, { label: string; cls: string }> = {
    pending: { label: "排队中", cls: "bg-gray-100 text-gray-500" },
    processing: { label: "转换中…", cls: "bg-amber-50 text-amber-600" },
    done: { label: "已入库", cls: "bg-green-50 text-green-600" },
    failed: { label: "失败", cls: "bg-red-50 text-red-500" },
  };
  const s = map[status];
  return (
    <span
      title={error ?? ""}
      className={`inline-block rounded-full px-2 py-0.5 text-xs ${s.cls} ${
        status === "failed" ? "cursor-help" : ""
      }`}
    >
      {status === "processing" && "⏳ "}
      {s.label}
    </span>
  );
}

function fmtSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

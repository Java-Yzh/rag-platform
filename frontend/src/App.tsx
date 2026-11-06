import { NavLink, Route, Routes } from "react-router-dom";
import ChatPage from "./pages/ChatPage";
import ChunkPage from "./pages/ChunkPage";
import DocumentsPage from "./pages/DocumentsPage";

/**
 * 应用骨架：左侧固定导航栏 + 右侧内容区（两个页面切换）。
 * NavLink 自带激活态样式（isActive），比 Link 更适合做导航。
 */
export default function App() {
  return (
    <div className="flex h-full">
      <aside className="flex w-56 shrink-0 flex-col border-r border-gray-200 bg-white">
        <div className="border-b border-gray-100 px-5 py-4">
          <h1 className="text-base font-bold">📚 知识库问答</h1>
          <p className="mt-0.5 text-xs text-gray-400">Docling × LangChain RAG</p>
        </div>
        <nav className="space-y-1 p-3">
          {[
            { to: "/chat", label: "💬 对话" },
            { to: "/docs", label: "📄 文档管理" },
            { to: "/chunks", label: "🧩 Chunk 管理" },
          ].map(({ to, label }) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                `block rounded-lg px-3 py-2 text-sm ${
                  isActive
                    ? "bg-blue-50 font-medium text-blue-700"
                    : "text-gray-600 hover:bg-gray-50"
                }`
              }
            >
              {label}
            </NavLink>
          ))}
        </nav>
      </aside>
      <main className="min-w-0 flex-1">
        <Routes>
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/docs" element={<DocumentsPage />} />
          <Route path="/chunks" element={<ChunkPage />} />
          <Route path="*" element={<ChatPage />} />
        </Routes>
      </main>
    </div>
  );
}

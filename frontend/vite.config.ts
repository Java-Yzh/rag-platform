import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Vite 配置。
// proxy 的作用：开发模式下前端跑在 5173 端口，把 /api 开头的请求
// 转发到后端 8000 端口——前端代码里只用写相对路径 /api/...，
// 开发(跨端口)和生产(同进程)行为一致，不用改代码
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
});

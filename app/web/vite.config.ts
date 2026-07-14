import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// dev では /api を FastAPI(:8000) へ proxy する。
// これによりフロントは同一オリジンの /api/... を叩くだけでよく、CORS 設定が不要になる。
//   例: フロントの fetch("/api/recommend") → http://localhost:8000/recommend
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        "/api": {
          target: env.VITE_API_TARGET || "http://localhost:8000",
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/api/, ""),
        },
      },
    },
  };
});

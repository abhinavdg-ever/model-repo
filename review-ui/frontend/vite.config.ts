import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  // Dev-server ports from ../.env.development (or the environment): DEV_FRONTEND_PORT for
  // this server, DEV_BACKEND_PORT for the uvicorn it proxies /api to. The client UI repo
  // sets 5175 / 8003 there so it runs beside ours.
  const env = loadEnv(mode, "..", "DEV_");
  return {
    plugins: [react()],
    server: {
      port: Number(env.DEV_FRONTEND_PORT || 5174),
      proxy: {
        "/api": {
          target: `http://127.0.0.1:${env.DEV_BACKEND_PORT || 8002}`,
          changeOrigin: true,
        },
      },
    },
    build: {
      outDir: "dist",
      emptyOutDir: true,
    },
  };
});

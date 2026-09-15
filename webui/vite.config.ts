import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  const apiTarget = env.VITE_API_PROXY_TARGET || "http://127.0.0.1:18380";
  return {
    plugins: [react()],
    server: {
      strictPort: true,
      proxy: {
        "/api": {
          target: apiTarget,
          changeOrigin: true,
          configure: proxy => {
            proxy.on("proxyReq", request => request.setHeader("origin", apiTarget));
          },
        },
      },
    },
  };
});

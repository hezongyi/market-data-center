import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  const apiTarget = env.VITE_API_PROXY_TARGET || "http://127.0.0.1:18380";
  return {
    plugins: [
      {
        name: "reject-unknown-static-assets",
        configureServer(server) {
          server.middlewares.use((request, response, next) => {
            if (new URL(request.originalUrl ?? "/", "http://vite.local").pathname.startsWith("/assets/")) {
              response.statusCode = 404;
              response.end("Not found");
              return;
            }
            next();
          });
        },
      },
      react(),
      tailwindcss(),
    ],
    build: {
      rollupOptions: {
        input: { main: "index.html", legacy: "legacy.html" },
      },
    },
    server: {
      strictPort: true,
      proxy: {
        "/api": {
          target: apiTarget,
          changeOrigin: true,
          configure: (proxy) => {
            proxy.on("proxyReq", (request) =>
              request.setHeader("origin", apiTarget),
            );
          },
        },
      },
    },
  };
});

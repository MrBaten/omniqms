import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const backend = "http://localhost:8000";

export default defineConfig({
  base: "/app/",
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": backend,
      "/oauth": backend,
      "/webhooks": backend,
    },
  },
  build: { outDir: "dist", sourcemap: false },
});

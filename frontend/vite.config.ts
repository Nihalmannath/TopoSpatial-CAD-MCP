import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";

export default defineConfig({
  plugins: [react()],
  base: "/static/editor/",
  build: {
    outDir: resolve(__dirname, "../src/web/static/editor"),
    emptyOutDir: true,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes("cytoscape")) return "topology-graph";
          if (id.includes("konva")) return "plan-canvas";
          if (id.includes("node_modules")) return "vendor";
        },
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
  },
});

import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";

export default defineConfig({
  plugins: [react()],
  define: {
    "process.env.NODE_ENV": JSON.stringify("production"),
    "process.env": "{}"
  },
  build: {
    outDir: resolve(import.meta.dirname, "../../static/vendor/knowledge-editor"),
    emptyOutDir: true,
    lib: {
      entry: resolve(import.meta.dirname, "src/main.jsx"),
      name: "CaddieKnowledgeEditorBundle",
      formats: ["iife"],
      fileName: () => "caddie-knowledge-editor.js"
    },
    cssCodeSplit: false,
    rollupOptions: {
      output: { inlineDynamicImports: true }
    }
  }
});

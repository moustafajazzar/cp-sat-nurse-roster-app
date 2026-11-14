import path from "node:path"

import tailwindcss from "@tailwindcss/vite"
import { tanstackRouter } from "@tanstack/router-plugin/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vitest/config"

export default defineConfig({
  plugins: [tanstackRouter({ target: "react", autoCodeSplitting: true }), react(), tailwindcss()],
  resolve: {
    alias: [
      { find: "@", replacement: path.resolve(import.meta.dirname, "./src") },
      // Every `import … from "zod"` gets zod with `jitless` already set (src/lib/zod.ts).
      { find: /^zod$/, replacement: path.resolve(import.meta.dirname, "./src/lib/zod.ts") },
    ],
  },
  server: {
    // In development the browser talks to Vite, and Vite passes the API on to Django.
    // PROTOTYPE: against an e2e stack, the Origin header is rewritten to the stack's own, so its CSRF check passes.
    proxy: { "/api": { target: process.env.VITE_API_PROXY ?? "http://localhost:8000", changeOrigin: true,
      headers: process.env.VITE_API_PROXY ? { Origin: process.env.VITE_API_PROXY } : {} } },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: "./src/test-setup.ts",
  },
})

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
    proxy: { "/api": process.env.VITE_API_PROXY ?? "http://localhost:8000" },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: "./src/test-setup.ts",
  },
})

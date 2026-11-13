import { defineConfig } from "orval"

// openapi.json is written by `manage.py export_openapi_schema` (see the README); CI regenerates
// both it and src/api/generated, and fails when either differs from what is committed.
export default defineConfig({
  api: {
    input: "./openapi.json",
    output: {
      target: "./src/api/generated/endpoints.ts",
      schemas: "./src/api/generated/model",
      client: "react-query",
      httpClient: "fetch",
      mode: "single",
      clean: true,
      override: {
        mutator: { path: "./src/api/fetcher.ts", name: "apiFetch" },
        fetch: { includeHttpResponseReturnType: false },
      },
    },
  },
  zod: {
    input: "./openapi.json",
    output: {
      target: "./src/api/generated/zod.ts",
      client: "zod",
      mode: "single",
    },
  },
})

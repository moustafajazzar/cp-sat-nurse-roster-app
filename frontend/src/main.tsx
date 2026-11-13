import { QueryClientProvider } from "@tanstack/react-query"
import { RouterProvider } from "@tanstack/react-router"
import { StrictMode } from "react"
import { createRoot } from "react-dom/client"

import "./index.css"
import { registerServiceWorker } from "./lib/service-worker"
import { reloadOnStaleBuild } from "./lib/stale-build"
import { createAppRouter, createQueryClient } from "./router"

const queryClient = createQueryClient()
const router = createAppRouter(queryClient)

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
)

registerServiceWorker()
reloadOnStaleBuild()

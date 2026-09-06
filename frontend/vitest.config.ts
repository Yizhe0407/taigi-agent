import path from "node:path"
import vue from "@vitejs/plugin-vue"
import { defineConfig } from "vitest/config"

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  test: {
    environment: "happy-dom",
    restoreMocks: true,
    // Leak tests need real reachability, not just bookkeeping counters.
    pool: "forks",
    execArgv: ["--expose-gc"],
  },
})

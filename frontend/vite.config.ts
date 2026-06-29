import path from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import vue from '@vitejs/plugin-vue'
import { defineConfig, loadEnv } from 'vite'

const isIgnorableThirdPartyWarning = (warning: {
  code?: string
  id?: string
}) =>
  warning.code === 'INVALID_ANNOTATION' &&
  warning.id?.includes('@vueuse/core/dist/index.js')

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "")

  return {
    plugins: [vue(), tailwindcss()],
    build: {
      // MapLibre is intentionally lazy-loaded by the route planner tab, but its
      // minified vendor chunk is still larger than Vite's generic 500 kB limit.
      chunkSizeWarningLimit: 1100,
      rolldownOptions: {
        onwarn(warning, defaultHandler) {
          if (isIgnorableThirdPartyWarning(warning)) return
          defaultHandler(warning)
        },
        output: {
          codeSplitting: {
            minSize: 20_000,
            maxSize: 480_000,
            groups: [
              {
                name: 'vendor-map',
                test: /node_modules[\\/]maplibre-gl/,
                priority: 30,
              },
              {
                name: 'vendor-ui',
                test: /node_modules[\\/](reka-ui|@vueuse|@floating-ui|@internationalized|@tanstack)/,
                priority: 20,
              },
              {
                name: 'vendor-core',
                test: /node_modules[\\/](vue|@lucide|clsx|tailwind-merge|class-variance-authority)/,
                priority: 10,
              },
            ],
          },
        },
      },
    },
    resolve: {
      alias: {
        '@': path.resolve(__dirname, './src'),
      },
    },
    server: {
      proxy: {
        "/api": {
          target: env.VITE_API_PROXY_TARGET || "http://127.0.0.1:8000",
          changeOrigin: true,
        },
      },
    },
  }
})

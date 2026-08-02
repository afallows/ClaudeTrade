/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// ClaudeTrade's React SPA (ADR-0008 Decision 2). Built assets are committed
// straight into the Python package's static/ directory so end users never
// need Node -- see frontend/DESIGN.md for the full rationale.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    // Committed into the Python package; served by claudetrade.webapi.app.
    outDir: '../src/claudetrade/webapi/static',
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    proxy: {
      // `npm run dev` talks to a locally running `python -m claudetrade.webapi`
      // for API calls, so the dev server never needs its own mock backend.
      '/api': {
        // Matches AppConfig.ui.port's default -- `python -m claudetrade.webapi`
        // with no --port override.
        target: 'http://127.0.0.1:8501',
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    css: true,
  },
})

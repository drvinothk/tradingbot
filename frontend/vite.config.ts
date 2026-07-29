import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // Same-origin proxy so the backend's HttpOnly/SameSite=Lax session
      // cookie just works — no CORS middleware needed for local dev.
      '/api': {
        target: 'http://127.0.0.1:5000',
        changeOrigin: true,
      },
      // Shoonya's OAuth routes live outside /api/v1 on the backend (see
      // api/v1/shoonya.py's own comment: SHOONYA_REDIRECT_URL is a fixed
      // URL registered on Shoonya's own API key form, so it can't be
      // prefixed) — needs its own proxy rule, found missing when
      // "Connect Shoonya" silently 404'd against Vite's own dev server
      // instead of reaching the backend at all.
      '/shoonya': {
        target: 'http://127.0.0.1:5000',
        changeOrigin: true,
      },
    },
  },
})

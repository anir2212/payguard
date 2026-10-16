import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api/payments': {
        target: 'http://127.0.0.1:8086',
        rewrite: path => path.replace(/^\/api\/payments/, ''),
      },
      '/api/cases': {
        target: 'http://127.0.0.1:18087',
        rewrite: path => path.replace(/^\/api\/cases/, ''),
      },
    },
  },
})

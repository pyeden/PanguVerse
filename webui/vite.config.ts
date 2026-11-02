import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  base: './',
  plugins: [
    react(),
    {
      name: 'panguverse-static-paths',
      apply: 'build',
      transformIndexHtml(html: string) {
        return html
          .replace('src="./app.js"', 'src="static/app.js"')
          .replace('href="./style.css"', 'href="static/style.css"')
      },
    },
  ],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:7788',
    },
  },
  build: {
    outDir: '../panguverse/web/static',
    emptyOutDir: true,
    cssCodeSplit: false,
    sourcemap: false,
    rollupOptions: {
      output: {
        codeSplitting: false,
        entryFileNames: 'app.js',
        chunkFileNames: 'assets/[name]-[hash].js',
        assetFileNames: (assetInfo) => {
          if (assetInfo.names?.some((name) => name.endsWith('.css'))) return 'style.css'
          return 'assets/[name]-[hash][extname]'
        },
      },
    },
  },
})

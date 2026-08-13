import path from "path"
import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"

// webview (React) 构建配置
export default defineConfig({
  plugins: [react()],
  root: path.resolve(__dirname, "webview"),
  base: "./",
  build: {
    outDir: path.resolve(__dirname, "out/webview"),
    emptyOutDir: true,
    sourcemap: true,
  },
  server: {
    port: 5173,
    strictPort: true,
  },
})

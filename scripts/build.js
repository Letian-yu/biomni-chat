// 构建脚本：先用 esbuild 打包 extension (cjs)，再用 vite 打包 webview (React)
const esbuild = require("esbuild")
const { execSync } = require("child_process")
const path = require("path")

const isWatch = process.argv.includes("--watch")

async function buildExtension() {
  const options = {
    entryPoints: ["src/extension.ts"],
    bundle: true,
    outfile: "out/extension.js",
    external: ["vscode"],
    format: "cjs",
    platform: "node",
    target: "node18",
    sourcemap: true,
    logLevel: "info",
  }
  if (isWatch) {
    const ctx = await esbuild.context(options)
    await ctx.watch()
    console.log("[biomni-chat] watching extension...")
  } else {
    await esbuild.build(options)
    console.log("[biomni-chat] extension built -> out/extension.js")
  }
}

function buildWebview() {
  execSync("npx vite build", { stdio: "inherit", cwd: path.resolve(__dirname, "..") })
  console.log("[biomni-chat] webview built -> out/webview/")
}

async function main() {
  await buildExtension()
  if (!isWatch) {
    buildWebview()
    console.log("[biomni-chat] BUILD COMPLETE")
  }
}

main().catch((e) => {
  console.error(e)
  process.exit(1)
})

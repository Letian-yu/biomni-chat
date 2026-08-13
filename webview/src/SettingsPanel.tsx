import { useEffect, useRef, useState } from "react"

interface Props {
  vscode: { postMessage: (msg: unknown) => void }
}

interface EnvStatus {
  python: boolean
  biomni: boolean
  tools: string[]
  dataLake: boolean
  dataLakeSize: string
  envFile: boolean
  remoteOk: boolean
  remoteMode: string
}

const PROVIDERS = [
  { key: "deepseek", label: "DeepSeek", base: "https://api.deepseek.com/v1", model: "deepseek-chat" },
  { key: "openai", label: "OpenAI", base: "https://api.openai.com/v1", model: "gpt-4o" },
  { key: "anthropic", label: "Anthropic", base: "https://api.anthropic.com/v1", model: "claude-3-5-sonnet" },
  { key: "custom", label: "自定义", base: "", model: "" },
]

const MIRROR_PRESETS = [
  { label: "清华 (Tsinghua)", conda: "https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud/conda-forge", pip: "https://pypi.tuna.tsinghua.edu.cn/simple", cran: "https://mirrors.tuna.tsinghua.edu.cn/CRAN/" },
  { label: "官方 (Official)", conda: "https://conda.anaconda.org/conda-forge", pip: "https://pypi.org/simple", cran: "https://cloud.r-project.org" },
]

export function SettingsPanel({ vscode }: Props) {
  const [provider, setProvider] = useState("custom")
  const [baseUrl, setBaseUrl] = useState("")
  const [apiKey, setApiKey] = useState("")
  const [model, setModel] = useState("")
  const [mirrors, setMirrors] = useState({ conda: "", pip: "", cran: "" })
  const [env, setEnv] = useState<EnvStatus | null>(null)
  const [checking, setChecking] = useState(false)
  const [testResult, setTestResult] = useState("")
  const [deploying, setDeploying] = useState(false)
  const [deployLines, setDeployLines] = useState<string[]>([])
  const [deployResult, setDeployResult] = useState("")
  const [deployingL2, setDeployingL2] = useState(false)
  const [l2Lines, setL2Lines] = useState<string[]>([])
  const [l2Result, setL2Result] = useState("")
  const deployOutRef = useRef<HTMLPreElement>(null)
  const l2OutRef = useRef<HTMLPreElement>(null)

  useEffect(() => {
    vscode.postMessage({ type: "settings_load" })
    vscode.postMessage({ type: "env_check" })
    const h = (e: MessageEvent) => {
      const msg = e.data
      if (!msg || typeof msg.type !== "string") return
      if (msg.type === "settings_data") {
        if (msg.byok) {
          setProvider(msg.byok.provider || "custom")
          setBaseUrl(msg.byok.baseUrl || "")
          setModel(msg.byok.model || "")
        }
        if (msg.mirrors) setMirrors(msg.mirrors)
        if (msg.env) {
          setEnv(msg.env)
          setChecking(false)
        }
      } else if (msg.type === "settings_test_result") {
        setTestResult(`${msg.ok ? "✅ 连接成功" : "❌ 连接失败"}\n${msg.message || ""}`)
      } else if (msg.type === "deploy_progress") {
        // 部署进度：按来源分发（deploy=L0L1, l2=数据湖）
        const line = String(msg.line ?? "")
        if (msg.source === "l2") {
          setDeployingL2(true)
          setL2Lines((prev) => [...prev, line])
        } else {
          setDeploying(true)
          setDeployLines((prev) => [...prev, line])
        }
      } else if (msg.type === "deploy_result") {
        const text = `${msg.ok ? "✅ 完成" : "❌ 失败"}\n${msg.message || ""}`
        if (msg.source === "l2") {
          setDeployingL2(false)
          setL2Result(text)
        } else {
          setDeploying(false)
          setDeployResult(text)
        }
      }
    }
    window.addEventListener("message", h)
    return () => window.removeEventListener("message", h)
  }, [vscode])

  const selectProvider = (key: string) => {
    setProvider(key)
    const p = PROVIDERS.find((x) => x.key === key)
    if (p && key !== "custom") {
      setBaseUrl(p.base)
      setModel(p.model)
    }
  }

  const applyMirrorPreset = (i: number) => {
    setMirrors({ conda: MIRROR_PRESETS[i].conda, pip: MIRROR_PRESETS[i].pip, cran: MIRROR_PRESETS[i].cran })
  }

  const runEnvCheck = () => {
    setChecking(true)
    setEnv(null)
    vscode.postMessage({ type: "env_check" })
  }

  const runDeploy = () => {
    setDeploying(true)
    setDeployLines([])
    setDeployResult("")
    vscode.postMessage({ type: "deploy" })
  }

  const runDeployL2 = () => {
    setDeployingL2(true)
    setL2Lines([])
    setL2Result("")
    vscode.postMessage({ type: "deploy_l2" })
  }

  // 部署输出自动滚动到底部
  useEffect(() => {
    const el = deployOutRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [deployLines])

  useEffect(() => {
    const el = l2OutRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [l2Lines])

  return (
    <div className="settings">
      <section className="settings-section">
        <h3>模型配置（BYOK）</h3>
        <label>提供方</label>
        <select value={provider} onChange={(e) => selectProvider(e.target.value)}>
          {PROVIDERS.map((p) => (
            <option key={p.key} value={p.key}>
              {p.label}
            </option>
          ))}
        </select>
        <label>Base URL</label>
        <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://api.deepseek.com/v1" />
        <label>API Key（已保存的 key 不会回显，留空则保留）</label>
        <input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="sk-..." />
        <label>模型名</label>
        <input value={model} onChange={(e) => setModel(e.target.value)} placeholder="deepseek-chat" />
        <div className="settings-buttons">
          <button className="plan-btn primary" onClick={() => vscode.postMessage({ type: "settings_test" })}>
            测试连接
          </button>
          <button
            className="plan-btn primary"
            onClick={() => vscode.postMessage({ type: "settings_save", byok: { provider, baseUrl, model, hasKey: false, apiKey } })}
          >
            保存
          </button>
        </div>
        {testResult && <pre className="settings-output">{testResult}</pre>}
      </section>

      <section className="settings-section">
        <h3>镜像源</h3>
        <div className="settings-buttons">
          {MIRROR_PRESETS.map((m, i) => (
            <button key={m.label} className="plan-btn" onClick={() => applyMirrorPreset(i)}>
              {m.label}
            </button>
          ))}
        </div>
        <label>conda 源</label>
        <input value={mirrors.conda} onChange={(e) => setMirrors({ ...mirrors, conda: e.target.value })} />
        <label>pip 源</label>
        <input value={mirrors.pip} onChange={(e) => setMirrors({ ...mirrors, pip: e.target.value })} />
        <label>CRAN 源</label>
        <input value={mirrors.cran} onChange={(e) => setMirrors({ ...mirrors, cran: e.target.value })} />
        <button
          className="plan-btn primary"
          onClick={() => vscode.postMessage({ type: "mirror_save", mirrors })}
        >
          保存镜像
        </button>
      </section>

      <section className="settings-section">
        <h3>环境状态 / 部署向导</h3>
        {env && !env.remoteOk && (
          <div className="remote-warning">
            ⚠ 当前不是 Linux 服务器（{env.remoteMode}）。本插件需在 Remote-SSH 连接 Linux 服务器后使用，部署已禁用。
          </div>
        )}
        {checking ? (
          <div className="settings-hint checking">检测中，请稍候...</div>
        ) : env ? (
          <div className="env-status">
            <div className={env.remoteOk ? "ok" : "bad"}>运行环境: {env.remoteMode}</div>
            <div className={env.python ? "ok" : "bad"}>
              Python (biomni_e1): {env.python ? "✅ 已就绪" : "❌ 未找到"}
            </div>
            <div className={env.biomni ? "ok" : "bad"}>biomni 包: {env.biomni ? "✅ 已安装" : "❌ 缺失"}</div>
            <div>生信工具: {env.tools.length > 0 ? `✅ ${env.tools.join(" / ")}` : "（未检测到）"}</div>
            <div className={env.dataLake ? "ok" : "bad"}>
              数据湖: {env.dataLake ? `✅ ${env.dataLakeSize}` : "（未下载，运行时按需自动获取）"}
            </div>
            <div className={env.envFile ? "ok" : "bad"}>
              .env 配置: {env.envFile ? "✅ 存在" : "❌ 缺失"}
            </div>
          </div>
        ) : (
          <div className="settings-hint">尚未检测</div>
        )}
        <div className="settings-buttons">
          <button className="plan-btn" onClick={runEnvCheck} disabled={checking}>
            {checking ? "检测中..." : "重新检测"}
          </button>
          <button className="plan-btn primary" onClick={runDeploy} disabled={deploying || (env ? !env.remoteOk : false)}>
            {deploying ? "部署中..." : env && !env.remoteOk ? "部署不可用（非 Linux 主机）" : "一键部署（L0+L1）"}
          </button>
          <button
            className="plan-btn"
            onClick={runDeployL2}
            disabled={deployingL2 || (env ? !env.remoteOk : false) || (env ? !env.python : false)}
          >
            {deployingL2 ? "下载中..." : "下载数据湖（L2）"}
          </button>
        </div>
        {(deploying || deployLines.length > 0) && (
          <pre className={`settings-output deploy-output ${deploying ? "live" : ""}`} ref={deployOutRef}>
            {deployLines.join("\n")}
          </pre>
        )}
        {deployResult && <pre className="settings-output">{deployResult}</pre>}
        {(deployingL2 || l2Lines.length > 0) && (
          <pre className={`settings-output deploy-output ${deployingL2 ? "live" : ""}`} ref={l2OutRef}>
            {l2Lines.join("\n")}
          </pre>
        )}
        {l2Result && <pre className="settings-output">{l2Result}</pre>}
      </section>
    </div>
  )
}

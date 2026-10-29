// lrdmem for OpenCode v2 (Promise plugin API: default export { id, setup }); copy to ~/.config/opencode/plugins/.
// Each hook runs `lrdmem hook <phase> --agent opencode` (killed after 5 s) with one JSON object on stdin,
// {"session_id", "cwd", "tool"?, "input"?} (tool and input only for pre-tool/post-tool), and reads plain text back.
// Injection: session-start text joins `system` on every request of its session (session "context" hook);
// pre-tool and post-tool text is appended to that call's result content; prompt text, and tool text of a
// failed call, join `system` on the next request only. stop runs when a session execution ends; it cannot block.
import { spawn } from "node:child_process"

const run = (phase, payload) => new Promise((resolve) => {
  try {
    const child = spawn(process.env.LRDMEM_BIN || "lrdmem", ["hook", phase, "--agent", "opencode"],
      { stdio: ["pipe", "pipe", "ignore"] })
    let out = ""
    const timer = setTimeout(() => { try { child.kill("SIGKILL") } catch {} resolve("") }, 5000)
    const done = (text) => { clearTimeout(timer); resolve(text) }
    child.on("error", () => done("")).on("close", () => done(out.trim()))
    child.stdout.on("data", (chunk) => { out += chunk })
    child.stdin.on("error", () => {})
    child.stdin.end(JSON.stringify(payload))
  } catch { resolve("") }
})
const join = (...texts) => texts.filter(Boolean).join("\n\n")

export default {
  id: "lrdmem",
  setup: async (ctx) => {
    const regs = [], started = new Map(), next = new Map(), pre = new Map(), stop = new AbortController()
    try {
      const base = (id) => ({ session_id: id || "", cwd: ctx.location.directory })
      const later = (id, text) => { if (text) next.set(id, join(next.get(id), text)) }
      const hook = async (domain, name, fn) => regs.push(await ctx[domain].hook(name, async (ev) => {
        try { await fn(ev) } catch {}
      }))
      await hook("session", "context", async (ev) => {
        if (!started.has(ev.sessionID)) started.set(ev.sessionID, run("session-start", base(ev.sessionID)))
        const texts = [await started.get(ev.sessionID), next.get(ev.sessionID)]
        next.delete(ev.sessionID)
        for (const text of texts) if (text) ev.system.push({ type: "text", text })
      })
      await hook("session", "prompt", async (ev) => later(ev.sessionID, await run("prompt", base(ev.sessionID))))
      const tool = (ev) => ({ ...base(ev.sessionID), tool: ev.tool, input: ev.input })
      await hook("tool", "execute.before", async (ev) => pre.set(ev.id, await run("pre-tool", tool(ev))))
      await hook("tool", "execute.after", async (ev) => {
        const text = join(pre.get(ev.id), await run("post-tool", tool(ev))), content = ev.result?.content
        pre.delete(ev.id)
        if (!text) return
        if (typeof content === "string") ev.result.content = join(content, text)
        else if (Array.isArray(content)) ev.result.content = [...content, { type: "text", text }]
        else later(ev.sessionID, text)
      })
      ;(async () => {
        for await (const e of ctx.event.subscribe({ signal: stop.signal })) {
          if (/^session\.execution\.(succeeded|failed|interrupted)$/.test(e?.type)) await run("stop", base(e.data?.sessionID))
        }
      })().catch(() => {})
    } catch {}
    return async () => { stop.abort(); for (const r of regs) try { await r?.dispose?.() } catch {} }
  },
}

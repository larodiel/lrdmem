// lrdmem for OpenCode v2 (Promise plugin API: default export { id, setup }); copy to ~/.config/opencode/plugins/.
// Each hook runs `lrdmem hook <phase> --agent opencode` (killed after 5 s) with one JSON object on stdin,
// {"session_id", "cwd", "tool"?, "input"?} (tool and input only for pre-tool/post-tool), and reads plain text back.
// Injection: session-start text joins `system` on every request (session "context" hook); pre/post-tool text is
// appended to the call's result content; prompt and failed-call text join `system` once. stop runs (never blocks)
// when a session execution in this directory ends, which also drops that session's unused pre-tool text.
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
    child.stdin.on("error", () => {}).end(JSON.stringify(payload))
  } catch { resolve("") }
})
const join = (...texts) => texts.filter(Boolean).join("\n\n")

export default {
  id: "lrdmem",
  setup: async (ctx) => {
    const regs = [], started = new Map(), next = new Map(), pre = new Map(), stop = new AbortController()
    try {
      const cwd = ctx.location.directory, base = (id) => ({ session_id: id || "", cwd })
      const later = (id, text) => { if (text) next.set(id, join(next.get(id), text)) }
      const hook = async (domain, name, fn) =>
        regs.push(await ctx[domain].hook(name, async (ev) => { try { await fn(ev) } catch {} }))
      await hook("session", "context", async ({ sessionID: id, system }) => {
        if (!started.has(id)) started.set(id, run("session-start", base(id)))
        for (const text of [await started.get(id), next.get(id)]) if (text) system.push({ type: "text", text })
        next.delete(id)
      })
      await hook("session", "prompt", async (ev) => later(ev.sessionID, await run("prompt", base(ev.sessionID))))
      const tool = (ev) => ({ ...base(ev.sessionID), tool: ev.tool, input: ev.input })
      await hook("tool", "execute.before", async (e) => pre.set(e.id, [e.sessionID, await run("pre-tool", tool(e))]))
      await hook("tool", "execute.after", async (ev) => {
        const text = join(pre.get(ev.id)?.[1], await run("post-tool", tool(ev))), content = ev.result?.content
        pre.delete(ev.id)
        if (!text) return
        if (typeof content === "string") ev.result.content = join(content, text)
        else if (Array.isArray(content)) ev.result.content = [...content, { type: "text", text }]
        else later(ev.sessionID, text)
      })
      ;(async () => {
        for await (const e of ctx.event.subscribe({ signal: stop.signal })) {
          const dir = e?.location?.directory, sid = e?.data?.sessionID
          if (!/^session\.execution\.(succeeded|failed|interrupted)$/.test(e?.type) || (dir && dir !== cwd)) continue
          for (const [id, [owner]] of pre) if (owner === sid) pre.delete(id)
          await run("stop", base(sid))
        }
      })().catch(() => {})
    } catch {}
    return async () => { stop.abort(); for (const r of regs) try { await r?.dispose?.() } catch {} }
  },
}

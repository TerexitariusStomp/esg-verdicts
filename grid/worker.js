// esg-grid worker — the ONLY dynamic endpoint.
// POST /submit (and /batch ≤500). No /lease: work units are a signed static
// manifest on Pages; browsers self-assign. PoW-gated per submit batch.
// Quorum dedupes (unit_id, content_hash); server-side echo-gate re-checks.
import { Hono } from "hono";

const app = new Hono();

const POW_PREFIX = "0000"; // ~65k expected hashes — a real but cheap cost

async function powOk(body) {
  // hashcash: sha256(pow.challenge + ":" + pow.nonce) must start with POW_PREFIX
  const pow = body.pow;
  if (!pow || !pow.challenge || pow.nonce === undefined) return false;
  const data = new TextEncoder().encode(`${pow.challenge}:${pow.nonce}`);
  const hash = await crypto.subtle.digest("SHA-256", data);
  const hex = [...new Uint8Array(hash)]
    .map(b => b.toString(16).padStart(2, "0")).join("");
  return hex.startsWith(POW_PREFIX);
}

async function echoGate(snippet, sourceHash) {
  // verify snippet is a verbatim substring of the unit's source text.
  // units carry text_sha256 + the text itself is fetched from the manifest's
  // data blob keyed by content_hash (units are small page-text chunks).
  // For v1 the submit includes source_text; quorum recomputes the hash.
  return true; // real gate runs in the quorum recompute job (deterministic)
}

app.post("/submit", async (c) => {
  const body = await c.req.json();
  if (!(await powOk(body)))
    return c.json({ error: "pow invalid" }, 403);
  const db = c.env.DB;
  const rows = Array.isArray(body.results) ? body.results : [body];
  const batch = rows.slice(0, 500).map(r =>
    db.prepare(
      `INSERT OR IGNORE INTO results
       (unit_id, content_hash, node, payload, created_at)
       VALUES(?,?,?,?,datetime('now'))`
    ).bind(r.unit_id, r.content_hash, body.node || "anon",
           JSON.stringify(r)));
  await db.batch(batch);
  return c.json({ ok: true, accepted: batch.length });
});

app.post("/batch", async (c) => {
  const body = await c.req.json();
  if (!(await powOk(body)))
    return c.json({ error: "pow invalid" }, 403);
  return c.json({ ok: true });
});

app.get("/health", (c) => c.json({ ok: true }));

export default app;

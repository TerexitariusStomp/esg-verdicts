// Browser grid worker — opt-in "contribute compute".
// ALL inference is client-side: WebGPU → WebLLM; WASM → transformers.js.
// Self-assigns from the signed static manifest on Pages (no /lease).
// Only verified results leave the device, batched + PoW-gated.
import { solve } from "./pow.js";

const MANIFEST = "/grid-manifest.json";
const SUBMIT = "https://esg-grid.YOUR-ACCT.workers.dev/submit";
const NODE = crypto.randomUUID();
const hasGPU = !!navigator.gpu;
const Tier = hasGPU ? "t1" : "t0";

const METRICS = "scope1_mtco2e, scope2_loc_mtco2e, scope2_mkt_mtco2e, " +
  "scope3_mtco2e, energy_mwh, renewable_pct, water_m3, waste_t, " +
  "recycled_pct, emissions_intensity, netzero_target_year";

const X_PROMPT = t => `Extract environmental metrics from this ESG report page.
For EACH metric return a JSON list item:
{"metric": one of ${METRICS}, "value": number, "unit": "string",
 "year": int, "scope": "1/2/3/null", "target_year": int|null,
 "snippet": "EXACT substring copied verbatim from the page text",
 "confidence": 0.0-1.0}
Only metrics literally stated. snippet MUST be verbatim. [] if none.
JSON list only.

---PAGE---
${t}`;

// --- deterministic regex fallback (always available, no model) ---
const RX = [
  [/scope\s*1[^\d]{0,60}([\d,]+(?:\.\d+)?)\s*(?:million\s+|thousand\s+)?(?:metric\s+)?(?:tons?|mt|kt)\s*(?:of\s+)?co2e?/gi, "scope1_mtco2e"],
  [/scope\s*3[^\d]{0,60}([\d,]+(?:\.\d+)?)\s*(?:million\s+|thousand\s+)?(?:metric\s+)?(?:tons?|mt|kt)\s*(?:of\s+)?co2e?/gi, "scope3_mtco2e"],
  [/([\d,]+(?:\.\d+)?)\s*%\s*(?:of\s+(?:total\s+)?(?:energy|electricity)\s+)(?:from\s+)?renewable/gi, "renewable_pct"],
  [/net[\s-]?zero[^\d]{0,40}by\s*(20[0-2]\d)/gi, "netzero_target_year"],
];

function regexExtract(text) {
  const out = [];
  for (const [rx, key] of RX) {
    let m;
    while ((m = rx.exec(text)) !== null) {
      const value = parseFloat(m[1].replace(/,/g, ""));
      const snippet = text.slice(Math.max(0, m.index - 40), m.index + m[0].length + 160).trim();
      out.push({ metric: key, value, unit: key === "netzero_target_year" ? "year" : key.includes("pct") ? "%" : "tco2e",
                 snippet, confidence: 0.9 });
    }
  }
  return out;
}

// echo-gate: drop any result whose snippet isn't verbatim in the page text
const norm = s => s.replace(/\s+/g, " ").trim();
const gate = (items, text) =>
  items.filter(it => it.snippet && norm(text).includes(norm(it.snippet)));

async function* workLoop() {
  const mf = await (await fetch(MANIFEST)).json();
  // self-assign: pick units at random; quorum dedupes collisions server-side
  const units = mf.units || [];
  while (true) {
    const u = units[Math.floor(Math.random() * units.length)];
    if (!u) { await new Promise(r => setTimeout(r, 60000)); continue; }
    const page = await (await fetch(u.text_url)).text();
    let items = regexExtract(page);
    let tier = "regex";
    if (Tier === "t1" && items.length === 0) {
      // heavier path: WebLLM prompt (loaded lazily — see contribute.html)
      items = await llmExtract(page);
      tier = "llm";
    }
    const good = gate(items, page);
    if (good.length) {
      const pow = await solve(u.unit_id + ":" + NODE.slice(0, 8));
      await fetch(SUBMIT, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          node: NODE, tier,
          pow,
          results: good.map(it => ({
            unit_id: u.unit_id,
            content_hash: u.content_hash,
            ...it })) })
      });
    }
    yield good.length;
  }
}

let llm = null;
async function llmExtract(text) {
  // lazy-load WebLLM only for t1 nodes; returns gated items or []
  if (!llm && navigator.gpu) {
    const { CreateMLCEngine } = await import(
      "https://esm.run/@mlc-ai/web-llm");
    llm = await CreateMLCEngine("Llama-3.2-3B-Instruct-q4f16_1-MLC");
  }
  if (!llm) return [];
  try {
    const r = await llm.chat.completions.create({
      messages: [{ role: "user", content: X_PROMPT(text.slice(0, 12000)) }],
      temperature: 0 });
    const m = r.choices[0].message.content.match(/\[[\s\S]*\]/);
    return m ? JSON.parse(m[0]) : [];
  } catch { return []; }
}

export { workLoop };

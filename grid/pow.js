// pow.js — hashcash proof-of-work for grid submits (Artizen pow-challenge port).
// Client solves: find nonce where sha256(challenge + ":" + nonce) starts with
// the required prefix. ~65k hashes ≈ tens of ms per submit batch — real cost
// to a Sybil, invisible to a human.
export async function solve(challenge, prefix = "0000", max = 1e7) {
  const enc = new TextEncoder();
  for (let nonce = 0; nonce < max; nonce++) {
    const data = enc.encode(`${challenge}:${nonce}`);
    const hash = await crypto.subtle.digest("SHA-256", data);
    const hex = [...new Uint8Array(hash)]
      .map(b => b.toString(16).padStart(2, "0")).join("");
    if (hex.startsWith(prefix))
      return { challenge, nonce };
  }
  throw new Error("pow exhausted");
}

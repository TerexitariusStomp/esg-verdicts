-- grid/schema.sql — D1 results buffer + quorum dedupe.
-- Small durable buffer only; analytical queries stay client-side on parquet.
CREATE TABLE IF NOT EXISTS results(
  id INTEGER PRIMARY KEY,
  unit_id TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  node TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(unit_id, content_hash));
CREATE INDEX IF NOT EXISTS ix_results_unit ON results(unit_id);
CREATE TABLE IF NOT EXISTS pow_receipts(
  challenge TEXT PRIMARY KEY,
  issued_at TEXT NOT NULL);

-- pro tier: key-gated read API over the public export layer.
-- tools/sync_public.py mirrors publishable rows here after each export.
CREATE TABLE IF NOT EXISTS api_keys(
  key TEXT PRIMARY KEY,
  tier TEXT NOT NULL DEFAULT 'analyst',   -- analyst | license | admin
  email TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pro_flags(
  id INTEGER PRIMARY KEY,
  company TEXT NOT NULL,
  flag TEXT NOT NULL,
  severity TEXT NOT NULL,
  posture TEXT NOT NULL,
  basis TEXT NOT NULL,
  pin TEXT,
  year TEXT);
CREATE TABLE IF NOT EXISTS pro_companies(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  ticker TEXT,
  country TEXT,
  sector TEXT,
  verdict TEXT,
  reports INTEGER DEFAULT 0,
  extractions INTEGER DEFAULT 0,
  facilities INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS pro_stats(
  k TEXT PRIMARY KEY,
  v INTEGER);

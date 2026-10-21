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

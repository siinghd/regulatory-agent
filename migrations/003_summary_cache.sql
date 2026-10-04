-- Cited summaries keyed by the exact document versions they were written from.
CREATE TABLE IF NOT EXISTS summaries (
    key             text PRIMARY KEY,                -- sha256(prompt version + sorted doc sha256s)
    summary         text,
    claims          jsonb NOT NULL,                  -- [{claim, doc_external_id, page, quote, char_start, char_end}]
    created_at      timestamptz NOT NULL DEFAULT now()
);

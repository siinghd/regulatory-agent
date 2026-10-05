-- Citations on documents the viewer can't render as pages (Word files): the sentence before and
-- after the quote, taken from pages.text when the citation is made. The viewer role (agent_web)
-- reads citations but not pages, so the passage's context travels with the citation.
ALTER TABLE citations ADD COLUMN IF NOT EXISTS context_before text;
ALTER TABLE citations ADD COLUMN IF NOT EXISTS context_after text;

-- What a cached summary is based on ({used, total, unreadable, unread, kinds}), so a cache hit
-- can still say "based on N of M documents".
ALTER TABLE summaries ADD COLUMN IF NOT EXISTS basis jsonb;

-- The rate-limit key of the request's normalised sender (agent.limits.sender_key: an HMAC of the
-- address lowercased, without +tag, Gmail dots removed, A-label domain), so the per-sender
-- in-flight cap can count a sender's requests however they spell the address.
ALTER TABLE requests ADD COLUMN IF NOT EXISTS sender_h text;
CREATE INDEX IF NOT EXISTS requests_inflight_idx ON requests (sender_h)
    WHERE state IN ('fetching', 'packaging');

-- A third kind of email: the one notice a request gets when it has to wait for a daily limit
-- (a regulator's portal-visit budget) to reset.
ALTER TABLE outbound DROP CONSTRAINT IF EXISTS outbound_kind_check;
ALTER TABLE outbound ADD CONSTRAINT outbound_kind_check CHECK (kind IN ('ack', 'reply', 'notice'));

-- The portal's own document type (OEB "Decision and Order", FERC "Order/Opinion: ..."), which
-- ranks documents for the summary: kept with the document so listings served from the cache
-- keep it too.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_type text;

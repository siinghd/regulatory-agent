-- Documents are identified per matter: UARB exhibit numbers (H-1, H-2, ...) repeat in every
-- matter, so (provider, external_id) let one matter's request reuse another matter's file.
-- Existing rows already satisfy the narrower key and are kept as they are.
CREATE UNIQUE INDEX IF NOT EXISTS documents_provider_matter_external_key
    ON documents (provider, matter, external_id);
ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_provider_external_id_key;

CREATE INDEX IF NOT EXISTS documents_sha256_idx ON documents (sha256);
CREATE INDEX IF NOT EXISTS citations_request_idx ON citations (request_id);
CREATE INDEX IF NOT EXISTS citations_document_idx ON citations (document_id);

-- The delivery made for a request (drop id, expiry; link and delete token sealed), so a retried
-- reply reuses the upload instead of making another one.
ALTER TABLE requests ADD COLUMN IF NOT EXISTS delivery jsonb;
-- Set once the original message has been removed from the agent's mailbox.
ALTER TABLE requests ADD COLUMN IF NOT EXISTS imap_expunged_at timestamptz;
CREATE INDEX IF NOT EXISTS requests_expunge_idx ON requests (updated_at)
    WHERE imap_uid IS NOT NULL AND imap_expunged_at IS NULL;

-- Outbox: every email is rendered and stored in the same transaction as the state change that
-- decides to send it, then delivered (and retried) independently of the request's job.
CREATE TABLE IF NOT EXISTS outbound (
    id              bigserial PRIMARY KEY,
    request_id      uuid NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    kind            text NOT NULL CHECK (kind IN ('ack', 'reply')),
    message_id      text NOT NULL UNIQUE,            -- deterministic: <kind.request-id@domain>
    body            bytea NOT NULL,                  -- the rendered RFC 5322 message, sealed (AES-GCM)
    has_attachment  boolean NOT NULL DEFAULT false,
    final_state     text,                            -- the request's state once this is sent
    status          text NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'sent', 'undeliverable', 'superseded')),
    attempts        int NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    last_error      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    sent_at         timestamptz
);
CREATE INDEX IF NOT EXISTS outbound_due_idx ON outbound (next_attempt_at) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS outbound_request_idx ON outbound (request_id);

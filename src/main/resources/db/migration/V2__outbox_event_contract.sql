-- Early local smoke rows contained only an operation result and cannot be decoded
-- by the v1 feature consumer. Preserve them for audit, but do not publish them.
ALTER TABLE outbox ADD COLUMN discarded_at TIMESTAMPTZ;
ALTER TABLE outbox ADD COLUMN discard_reason TEXT;
UPDATE outbox SET discarded_at=now(),
  discard_reason='legacy result-only payload predates authorization event contract'
WHERE topic='authorization' AND published_at IS NULL;

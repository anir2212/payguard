-- The publisher polls for unpublished, undiscarded rows in creation order every
-- 100 ms. Without this index each poll scans the whole outbox, so its cost grows
-- with every event ever written. The partial index holds only pending rows.
CREATE INDEX outbox_pending_idx ON outbox (created_at, id)
  WHERE published_at IS NULL AND discarded_at IS NULL;

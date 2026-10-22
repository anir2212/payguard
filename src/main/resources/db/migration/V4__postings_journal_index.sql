-- The deferred journal-balance trigger sums a journal's postings at every commit.
-- Without an index on journal_id each check scans the whole postings table, so
-- issuing a card or posting a capture slows down as the ledger grows.
CREATE INDEX postings_journal_idx ON postings (journal_id);

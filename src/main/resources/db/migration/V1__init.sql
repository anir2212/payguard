CREATE TABLE companies (
  id TEXT PRIMARY KEY
);

CREATE TABLE cards (
  id TEXT PRIMARY KEY CHECK (id LIKE 'card_%'),
  company_id TEXT NOT NULL REFERENCES companies(id),
  holder TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('CREATED','ACTIVE','FROZEN','LOST','STOLEN','CANCELLED')),
  currency CHAR(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
  limit_minor BIGINT NOT NULL CHECK (limit_minor > 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE controls (
  card_id TEXT PRIMARY KEY REFERENCES cards(id),
  policy JSONB NOT NULL
);

CREATE TABLE accounts (
  id TEXT PRIMARY KEY,
  currency CHAR(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$')
);

CREATE TABLE journal_entries (
  id UUID PRIMARY KEY,
  kind TEXT NOT NULL,
  reference TEXT NOT NULL,
  currency CHAR(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE postings (
  id BIGSERIAL PRIMARY KEY,
  journal_id UUID NOT NULL REFERENCES journal_entries(id),
  account_id TEXT NOT NULL REFERENCES accounts(id),
  amount_minor BIGINT NOT NULL CHECK (amount_minor <> 0)
);
CREATE INDEX postings_account_idx ON postings(account_id);

CREATE OR REPLACE FUNCTION check_journal_balance() RETURNS TRIGGER AS $$
DECLARE
  entry_id UUID;
  post_count INTEGER;
  post_sum NUMERIC;
  currency_mismatches INTEGER;
BEGIN
  IF TG_TABLE_NAME = 'journal_entries' THEN
    entry_id := NEW.id;
  ELSIF TG_OP = 'DELETE' THEN
    entry_id := OLD.journal_id;
  ELSE
    entry_id := NEW.journal_id;
  END IF;
  SELECT count(p.id), coalesce(sum(p.amount_minor), 0),
         count(*) FILTER (WHERE a.currency <> j.currency)
    INTO post_count, post_sum, currency_mismatches
    FROM journal_entries j
    LEFT JOIN postings p ON p.journal_id = j.id
    LEFT JOIN accounts a ON a.id = p.account_id
    WHERE j.id = entry_id;
  IF post_count < 2 OR post_sum <> 0 OR currency_mismatches <> 0 THEN
    RAISE EXCEPTION 'unbalanced or mixed-currency journal %', entry_id;
  END IF;
  RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER journal_balance_on_entry
  AFTER INSERT OR UPDATE ON journal_entries
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_journal_balance();
CREATE CONSTRAINT TRIGGER journal_balance_on_posting
  AFTER INSERT OR UPDATE OR DELETE ON postings
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_journal_balance();

CREATE OR REPLACE FUNCTION forbid_ledger_mutation() RETURNS TRIGGER AS $$
BEGIN
  RAISE EXCEPTION 'journal and posting rows are append-only';
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER postings_append_only BEFORE UPDATE OR DELETE ON postings
  FOR EACH ROW EXECUTE FUNCTION forbid_ledger_mutation();
CREATE TRIGGER journals_append_only BEFORE UPDATE OR DELETE ON journal_entries
  FOR EACH ROW EXECUTE FUNCTION forbid_ledger_mutation();
CREATE TRIGGER accounts_append_only BEFORE UPDATE OR DELETE ON accounts
  FOR EACH ROW EXECUTE FUNCTION forbid_ledger_mutation();

CREATE TABLE authorizations (
  id TEXT PRIMARY KEY,
  card_id TEXT NOT NULL REFERENCES cards(id),
  merchant_id TEXT NOT NULL,
  mcc TEXT NOT NULL,
  country CHAR(2) NOT NULL,
  currency CHAR(3) NOT NULL,
  amount_minor BIGINT NOT NULL CHECK (amount_minor > 0),
  billing_minor BIGINT NOT NULL CHECK (billing_minor > 0),
  fx_numerator BIGINT NOT NULL CHECK (fx_numerator > 0),
  fx_denominator BIGINT NOT NULL CHECK (fx_denominator > 0),
  captured_amount_minor BIGINT NOT NULL DEFAULT 0,
  captured_billing_minor BIGINT NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK (status IN ('OPEN','CLOSED','REVERSED')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX auth_card_time_idx ON authorizations(card_id,created_at);

CREATE TABLE captures (
  id TEXT PRIMARY KEY,
  auth_id TEXT NOT NULL REFERENCES authorizations(id),
  amount_minor BIGINT NOT NULL CHECK (amount_minor > 0),
  billing_minor BIGINT NOT NULL CHECK (billing_minor > 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE refunds (
  id TEXT PRIMARY KEY,
  capture_id TEXT NOT NULL REFERENCES captures(id),
  amount_minor BIGINT NOT NULL CHECK (amount_minor > 0),
  billing_minor BIGINT NOT NULL CHECK (billing_minor > 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE auth_attempts (
  id BIGSERIAL PRIMARY KEY,
  card_id TEXT NOT NULL REFERENCES cards(id),
  merchant_id TEXT NOT NULL,
  amount_billing_minor BIGINT NOT NULL CHECK (amount_billing_minor > 0),
  decision TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX attempt_card_time_idx ON auth_attempts(card_id,created_at);

CREATE TABLE fx_rates (
  source_currency CHAR(3) NOT NULL,
  target_currency CHAR(3) NOT NULL,
  numerator BIGINT NOT NULL CHECK (numerator > 0),
  denominator BIGINT NOT NULL CHECK (denominator > 0),
  PRIMARY KEY (source_currency,target_currency)
);
INSERT INTO fx_rates VALUES ('USD','SGD',135,100), ('EUR','SGD',148,100), ('SGD','USD',100,135);

CREATE TABLE idempotency (
  request_key TEXT PRIMARY KEY,
  operation TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  response JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE outbox (
  id UUID PRIMARY KEY,
  topic TEXT NOT NULL,
  event_key TEXT NOT NULL,
  payload JSONB NOT NULL,
  published_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

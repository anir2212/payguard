-- Creates the case store's own role and database on the stack's PostgreSQL.
-- Safe to run on every start: each step checks before it creates.
-- LOCAL DEMO ONLY: the password matches the stack's other local-demo-only
-- credentials and is not a real secret.
--
-- The case services connect as payguard_cases, which owns only this database
-- and cannot connect to the ledger database. CaseStore creates the tables.
SELECT 'CREATE ROLE payguard_cases LOGIN PASSWORD ''local-demo-only'''
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'payguard_cases') \gexec
SELECT 'CREATE DATABASE payguard_cases OWNER payguard_cases'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'payguard_cases') \gexec
REVOKE CONNECT ON DATABASE payguard_cases FROM PUBLIC;
REVOKE CONNECT ON DATABASE payguard FROM PUBLIC;

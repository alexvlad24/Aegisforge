-- SPEC §5.1 cluster-level setup (Fix 3). Runs once, on first volume init.
-- Passwords come from the container environment (set via .env) using psql \getenv,
-- so no secret is written into this file.

\getenv harness_pw HARNESS_PW
\getenv agent_pw AGENT_RW_PW
\getenv intake_pw INTAKE_READER_PW
\getenv test_pw TEST_RUNNER_PW

CREATE ROLE harness        LOGIN PASSWORD :'harness_pw' NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE agent_rw       LOGIN PASSWORD :'agent_pw'   NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE intake_reader  LOGIN PASSWORD :'intake_pw'  NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE test_runner    LOGIN PASSWORD :'test_pw'    NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE auditor        NOLOGIN;

ALTER ROLE agent_rw    SET statement_timeout = '5s';
ALTER ROLE agent_rw    SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE test_runner SET statement_timeout = '10s';

CREATE DATABASE aegis_golden;
CREATE DATABASE harness_db OWNER harness;

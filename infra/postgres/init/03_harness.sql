-- SPEC §4: harness_db. Created in 01_roles.sql (cluster level, owned by `harness`).
-- Tables (events, egress_outbox) arrive with their weeks (Week 2 events, Week 3 outbox).
\connect harness_db

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT ALL ON SCHEMA public TO harness;

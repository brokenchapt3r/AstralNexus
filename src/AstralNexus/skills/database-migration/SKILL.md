---
name: database-migration
description: Plan safe database schema and dialect migrations with compatibility, rollback, idempotency, indexes, transactions, and real validation.
platforms: [windows, linux, macos]
metadata:
  astralnexus:
    source: https://www.skills.sh/charleswiltgen/axiom/axiom-database-migration
    install_command: npx skills add https://github.com/charleswiltgen/axiom --skill axiom-database-migration
    adapter: curated
---

# Database Migration

Inspect existing schema, data volume and consumers before changing types or
constraints. Prefer additive, idempotent migrations with explicit backfill,
transaction and rollback plans. Check dialect-specific SQL, pagination,
indexes, drivers and isolation levels in the target database; a compatibility
claim is invalid without an actual migration or integration test.

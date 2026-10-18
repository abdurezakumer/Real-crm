# Test and disposable database instructions

Run the offline unit suite with:

```powershell
python -m pip install -e ".[dev]"
python -m pytest
```

The unit tests use temporary XLSX/CSV files and mocked database or Telegram interfaces. They do not connect to a database and do not prove PostgreSQL locking behavior.

For a manual integration check, use a disposable local PostgreSQL instance or a separate Supabase project. Never use production credentials or production data.

1. Create an empty disposable database and apply migrations `202610160001_initial_schema.sql` through `202610160005_telegram_outbox.sql` in order.
2. Review `supabase/performance/seed_50000_contacts.sql` before running it; use that fixture only in this disposable database.
3. Use the application connection string with SSL where Supabase requires it.
4. Exercise import, campaign linking, queue selection, outcome recording, follow-up rescheduling, and reminder delivery using a test Telegram bot and the authorized test user ID.
5. Inspect the resulting `contacts`, `call_logs`, `session_contacts`, `follow_ups`, `reminder_deliveries`, and `audit_events` rows.
6. Run the statements in `supabase/performance/explain_critical_queries.sql` with `EXPLAIN (ANALYZE, BUFFERS)` and retain the plan output with the test notes.
7. Drop the disposable database or project when finished.

No automated integration test consumes `TEST_SUPABASE_DATABASE_URL` in this repository. The environment variable in the README refers to a possible future integration suite; it is not currently read by pytest.

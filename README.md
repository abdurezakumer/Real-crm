# Real Estate Calling CRM Telegram Bot

A multi-user, real-estate sales CRM operated from Telegram. It imports Excel/CSV lead lists, organizes contacts into campaigns, supports team ownership and lead distribution, records manually selected call outcomes, schedules follow-ups, and tracks the sales pipeline and performance.

The bot never detects whether a cellular call ended. The user opens the phone dialer and records the result in Telegram.

## Architecture

- **Telegram:** webhook updates are parsed with `python-telegram-bot`. Configured bootstrap administrators are authorized first; invited members are checked against their active CRM account and server-side permissions.
- **Vercel:** Python 3.12 function handlers live in `api/`. Webhooks stay short; imports, reminders, and exports run from a protected scheduled endpoint.
- **Supabase PostgreSQL:** durable lead, campaign, call, follow-up, import, export, session, and reminder state. Database constraints enforce uniqueness and idempotency.
- **Supabase Storage:** private `crm-imports` bucket for staged uploads and private `crm-backups` bucket for dated daily snapshots.
- **Imports:** openpyxl read-only mode and streamed CSV parsing. The worker processes a configurable row batch, saves its cursor, and resumes on the next scheduled request.
- **Calling queue:** bounded PostgreSQL query. DNC, invalid, paused, converted, disqualified, not-interested, over-attempt-limit, and future-follow-up contacts are excluded.

Telegram inline URL buttons accept HTTP or `tg://` links. The Call Now button therefore sends an authorized, normalized `tel:` link in a Telegram Rich Message using the current Bot API `sendRichMessage` method. Telegram documents `tel:` links as phone links. The actual Android handset interaction still needs a device check during setup. The user must press the phone app’s call button; this bot does not place calls. [Telegram Bot API: inline buttons and Rich Messages](https://core.telegram.org/bots/api#inlinekeyboardbutton)

Vercel’s Python runtime supports Python 3.12 and handler classes based on `BaseHTTPRequestHandler`. The configured function duration is 60 seconds. [Vercel Python runtime](https://vercel.com/docs/functions/runtimes/python)

## Project layout

```text
api/
  webhook.py                 Telegram webhook function
  worker.py                  authenticated Supabase Cron function
  health.py                  public health response only
realestate_crm/
  config.py                  validated environment settings
  handlers.py                Telegram workflow and callback routing
  phase4_handlers.py         team, assignment, handover, targets, and audit workflows
  runtime.py                 webhook dispatch and Telegram output
  telegram_ui.py             inline keyboards and message rendering
  services/
    crm.py                   database-backed CRM operations
    intelligence.py          buyer matching, qualification, scoring, SQL reports
    ai.py                    optional provider adapter and strict output validation
    ai_prompts.py            bounded AI operation prompts
    importing.py             column mapping and bounded import batches
    text_import.py           pasted Telegram lead parsing and phone detection
    storage.py               private Supabase Storage operations
    backups.py               streaming multi-sheet Excel database snapshots
  worker.py                  imports, queued exports, reminders, retention cleanup
supabase/
  migrations/                version-controlled PostgreSQL schema
  cron.sql                   protected scheduled worker setup
  seed.sql                   empty development campaign
  performance/               disposable 50,000-contact benchmark scripts
scripts/
  register_webhook.py         idempotent Telegram webhook registration
tests/                        unit tests and disposable DB instructions
```

## Requirements

- Python 3.12
- A Telegram bot token from BotFather
- A Supabase project with PostgreSQL, Storage, Vault, `pg_cron`, and `pg_net`
- A Vercel project connected to this repository
- A public HTTPS Vercel deployment URL

Python 3.12 is declared in `.python-version` and `pyproject.toml`. The runtime dependencies are installed from that project file.

## Local setup

PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Fill in `.env` using the instructions below. Never commit `.env` or real credentials.

To run checks:

```powershell
python -m pytest
python -m ruff check realestate_crm api scripts
python -m ruff format --check realestate_crm api scripts
```

The webhook requires a public HTTPS URL for Telegram delivery. For local webhook work, deploy a preview or use a trusted HTTPS tunnel and set `TELEGRAM_WEBHOOK_URL` to that exact endpoint.

## Supabase setup

1. Create a new Supabase project. Use a dedicated project for this CRM.
2. Apply `supabase/migrations/202610160001_initial_schema.sql` in the SQL Editor or with the Supabase CLI.
3. Apply `supabase/migrations/202610160002_phase2_sales_intelligence.sql` after the initial schema. It is additive: it extends buyer preferences and adds inventory, recommendations, qualification, scoring, and optional AI accounting tables. It preserves existing contact, call, campaign, and follow-up IDs; do not reset the database.
4. Apply `supabase/migrations/202610160003_phase3_sales_conversion.sql` after Phase 2. This adds opportunities, visit and negotiation histories, reservations, sale verification, commissions, notification delivery records, and reactivation suggestions. It also extends durable AI jobs for optional opportunity assistance. The migration is additive and preserves existing IDs; never reset production data to deploy a phase.
5. Apply `supabase/migrations/202610160004_phase4_team_management.sql` after Phase 3. It adds CRM roles and memberships, hashed invitations, lead ownership and assignment history, bounded assignment jobs, team settings and targets, campaign team fields, handover requests, and commission governance history. Review the SQL against your deployed schema before applying. The migration is additive and preserves existing IDs; never reset production data to deploy a phase.
   Take a verified database backup first. This migration adds durable ownership, audit, and approval history, so a rollback should use a reviewed forward-fix or a restore plan that preserves those records; do not drop the new tables or columns after the team begins using them.
6. Apply `supabase/migrations/202610160005_telegram_outbox.sql` after Phase 4 and before deploying code that uses durable Telegram actions. It adds a private retry queue for user-facing bot responses. It is additive; use a backup and forward-fix/restore plan instead of dropping delivery history.
7. Apply `supabase/migrations/202610180001_daily_excel_backup.sql` after migration 005. It adds private scheduler state and retry tracking.
8. Apply `supabase/migrations/202610180002_customer_profile_backup.sql` after the daily backup migration and before deploying this backup worker version. It tracks the separate customer profile workbook.
9. Apply `supabase/migrations/202610180003_customer_backup_worker_stages.sql` after migration 002 and before deploying this backup worker version. It adds a durable stage marker so the two workbooks are generated in separate worker runs.
10. Apply `supabase/migrations/202610180004_midnight_backup_schedule.sql` after migration 003 and before deploying this backup worker version. Daily backups are scheduled by the `APP_TIMEZONE` local date, at midnight.
11. Apply `supabase/migrations/202610180005_text_import_outbox_action.sql` after the outbox migration and before deploying code that stages pasted Telegram leads.
12. Apply `supabase/seed.sql` only if you want the empty development campaign.
13. Copy the Shared Pooler transaction-mode URI from Supabase **Connect** into `SUPABASE_DATABASE_URL` for Vercel. Use the exact host, username, and port `6543` shown by Supabase, and require SSL. The application uses short-lived transactions and bounded queries.
14. Copy the project URL and backend service-role key into `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`. The key is used only by the server to create, upload, download, and delete files in private Storage buckets. It is never included in Telegram messages or client-side code.
15. Keep the database schema private. The migrations enable RLS on CRM tables and define no anon/authenticated policies. The backend’s server-side database credential bypasses RLS, so application authorization remains mandatory on every webhook and worker action.
16. After deployment, follow the **Supabase Cron setup** below.

Apply migrations `001` through `005`, the four daily-backup migrations, and the pasted-text outbox migration on a restored staging copy first. Verify that the expected schema exists, the backend can connect, and `/api/ready` returns `200` before switching traffic. Apply schema changes before deploying code that depends on them. Release code and migrations in order. If a migration fails, stop the deployment and preserve the database for inspection.

Do not run `supabase/performance/seed_50000_contacts.sql` or `EXPLAIN ANALYZE` against production data. Those scripts are for a disposable test project only.

## Environment variables

`.env.example` contains placeholders for every required variable:

| Variable | Purpose |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot API credential |
| `TELEGRAM_WEBHOOK_SECRET` | Telegram webhook header verification secret; 32–256 varied URL-safe characters |
| `TELEGRAM_WEBHOOK_URL` | Exact public URL, normally `https://<project>.vercel.app/api/webhook` |
| `AUTHORIZED_TELEGRAM_USER_ID` | One or more comma-separated numeric Telegram IDs bootstrapped as super administrators; additional accounts join through one-time invitations |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Backend-only key for private import and backup buckets |
| `SUPABASE_DATABASE_URL` | PostgreSQL connection string; remote databases must use `sslmode=require` or stronger |
| `APP_TIMEZONE` | Initial display timezone; defaults to `Africa/Addis_Ababa` |
| `MAX_IMPORT_FILE_MB` | Maximum upload size, capped at Telegram’s hosted Bot API file limit |
| `MAX_IMPORT_ROWS` | Maximum rows the worker will process per file |
| `IMPORT_BATCH_SIZE` | Rows committed per import transaction |
| `IMPORT_RETENTION_HOURS` | Time to retain unconfirmed or failed staged files; default 24 hours |
| `MAX_CALL_ATTEMPTS` | Initial per-contact attempt limit |
| `DEFAULT_DAILY_CALL_TARGET` | Initial dashboard target |
| `FOLLOW_UP_CRON_SECRET` | Separate bearer secret of at least 32 characters for the scheduled worker endpoint |
| `MAX_SEARCH_RESULTS` | Bounded search page size |
| `EXPORT_MAX_ROWS` | Maximum rows per CSV/XLSX export |
| `AI_ENABLED` | Optional AI assistant toggle; defaults to `false` |
| `AI_PROVIDER` | Currently `openai_compatible`; provider and model are explicitly configured |
| `AI_BASE_URL` | HTTPS base URL for the configured Chat Completions compatible endpoint |
| `AI_API_KEY` | Provider secret; keep only in deployment secrets, never Telegram or logs |
| `AI_MODEL` | Exact administrator-selected model; the app does not substitute another model |
| `AI_REQUEST_TIMEOUT_SECONDS` | Per-request timeout; 3–60 seconds |
| `AI_MAX_INPUT_CHARS` / `AI_MAX_OUTPUT_CHARS` | Bounds on each provider request and response |
| `AI_DAILY_REQUEST_LIMIT` | Maximum reserved provider calls per user in a rolling 24-hour window |
| `MATCH_ALLOW_NEAR_BUDGET` | Explicit opt-in to partial-score suggestions up to 10% over maximum budget |
| `MATCH_*_WEIGHT` | Property match weights; must total 100 |
| `LEAD_*_WEIGHT` | Explainable lead-score weights; must total 100 |
| `QUEUE_PRIORITY_ORDER` | Comma-separated order of all seven supported queue groups |

`APP_TIMEZONE`, daily target, attempt limit, and currency seed the user settings on first use. The authorized user can update timezone, daily target, and currency from Settings. Budget values are numeric and each property-interest record stores its currency code.

### Phase 2 buyer and property intelligence

The contact screen opens a progressively editable buyer profile. Budget and other unknown facts stay unknown; profile completion is an explicit user action. Property Inventory supports adding, editing, searching, price updates, and availability changes through Telegram. Inventory spreadsheet import is not included yet; properties are entered through the form. Only properties marked `AVAILABLE` are considered for new matches and recommendations.

Matching is deterministic and explains its score. The default 100 points are budget 35, location 25, property type 15, bedrooms 10, verified payment compatibility 10, and purchase timeline 5. Missing information earns no points. Type, accepted location, currency/budget ceiling, and bedroom conflicts are hard rejections by default. Price below a stated minimum remains a confirmation prompt. Near-budget matches add half the budget points only when explicitly enabled and the price is at most 10% above the buyer’s maximum. Match and lead weights are validated to sum to 100. Lead priority is an explained workflow score, not a predicted conversion probability.

Buyer qualification stores a separate, manually confirmed status and checklist. A scored lead or an unsuccessful call does not overwrite recorded qualification. Manually disqualified buyers are excluded from matching and the calling queue. The default queue order is overdue high-priority follow-ups, today’s follow-ups, high-priority qualified buyers, other qualified buyers, interested prospects, new leads, then retry-eligible leads. Filters are stored in `user_settings`; campaign selection remains the Phase 1 campaign filter. Contacts with a future callback remain excluded unless the existing user setting allows calling during future follow-ups.

Sales Intelligence calculates call, follow-up, qualification, campaign, and property metrics from database rows using the user’s timezone and half-open date windows. Reports include today, yesterday, last 7 days, last 30 days, current month, and custom dates. AI can summarize the already calculated aggregates when configured.

### Optional AI provider

AI stays disabled by default and core CRM functions do not call the provider. To enable it, configure `AI_ENABLED=true`, `AI_PROVIDER=openai_compatible`, an HTTPS `AI_BASE_URL`, `AI_API_KEY`, and the exact `AI_MODEL` supported by that provider. If optional AI configuration is incomplete or invalid, the app logs the reason and disables AI while keeping the CRM available. The adapter posts to the configured Chat Completions compatible endpoint, uses JSON output mode, and validates each operation’s input and output schema. It does not automatically switch providers or models. Review the provider’s current documentation for its endpoint, JSON mode, model availability, and pricing before enabling it.

AI work is queued and runs in the scheduled worker, not in the Telegram webhook. Input and output lengths, timeout, and a rolling 24-hour request cap are configurable. Request attempts are reserved atomically per Telegram user; provider timeouts and rate limits receive at most one retry. The selected contact’s phone field is excluded and its number is redacted if it appears in a note; the complete contact database is never sent. Structured call notes are shown for explicit confirmation and the original text is saved alongside the confirmed fields. Discarded or confirmed structured-note jobs clear their raw input and result payloads; other terminal AI jobs are removed after 90 days. Ambiguous follow-up timing is recorded for review but never scheduled automatically. Draft messages are not sent through WhatsApp; the agent must explicitly mark a draft as sent. AI output is untrusted and never executes SQL or changes lead status directly.

### Phase 3 sales conversion and command center

The opportunity pipeline is separate from the contact record and keeps a timestamped event history. It supports qualification, property matching and information shared, follow-up, scheduled and completed visits, negotiation, reservation, booked, won, lost, and on-hold stages. Stage changes into visits, negotiation, reservation, and booked require the corresponding recorded evidence. Visit scheduling asks the agent to review meeting details and confirm before saving; an unavailable property requires an explicit seller-availability confirmation. Visit outcomes, buyer feedback, and next actions remain manually recorded.

Negotiation records keep buyer offers, seller counteroffers, current discussed price, currency-aware price differences, payment discussions, offer expiry, and next actions. Acceptance, reservation status, payments, sale verification, commission status, and commission rule changes use explicit confirmation. A sale is recorded as pending verification; it becomes a verified conversion only after the agent confirms an authorized source. Commission rules calculate an estimated amount for a sale and keep that amount separate from later confirmation and payment. Manual commission amounts are supported and audited.

The command center includes current opportunity stages, due actions, visits, negotiation, reservations, expected closings, and commission records. Pipeline reports show stage counts, aging, qualification, visit, follow-up, and lost-reason activity for the selected period; monetary amounts remain separated by currency. Lost opportunities can be reopened with their previous history retained. The reactivation queue creates rule-based suggestions from recorded buyer timing, callback, and property availability facts. Calls remain manual: a dialer link opens the handset and the agent starts the call.

### Phase 4 teams, ownership, and accountability

The configured Telegram ID is bootstrapped as the first super administrator and is not promoted by normal invitation acceptance. Administrators invite role-scoped members with hashed, expiring, single-use tokens. Team membership scopes manager access; it does not grant administrator permissions. Suspensions require an explicit choice to retain current ownership or transfer eligible active leads.

Contacts have one authoritative assigned salesperson, and active opportunities follow that contact assignment. Manual and bounded bulk assignments keep an immutable assignment history, audit event, and idempotency key. Round-robin state is persisted. Salespeople can request handover with a structured reason; managers review the current stage and active task counts before selecting a new owner. Existing follow-ups and pipeline history remain attached to the contact. Team settings control assignment capacity, unassigned visibility, reassignment requests, and override policy.

Campaigns reuse the Phase 1 campaign and campaign-contact records. Team performance and target progress are calculated from recorded CRM activity. Commission policy snapshots preserve the rule used for a sale; payment status requires a recorded amount and payment reference, supports partial payments, and cannot exceed the approved balance. Audit views are limited by role and team/contact scope. User-visible Telegram replies are committed to an outbox with their business update and retried with bounded backoff; terminal delivery records are pruned after 30 days. A timeout after Telegram accepted a send can still produce a duplicate on retry because the Bot API has no send idempotency key.

Phase 3 reminders are durable, deduplicated delivery jobs. The worker rechecks the source record before sending so a completed or rescheduled action does not generate a stale reminder. Follow-up reminders include a do-not-contact warning when the contact is currently restricted. They are internal agent reminders; the bot never sends a message to a buyer or places a call. Opportunity AI is optional, off by default, bounded to one opportunity and a small set of recorded facts, and only returns a reviewable summary or draft.

## Telegram bot setup

1. Create the bot in BotFather and copy the token.
2. Find the account’s numeric Telegram ID using a trusted ID lookup bot or the bot’s first authorized update in local development.
3. Set `AUTHORIZED_TELEGRAM_USER_ID` to one or more numeric IDs, comma-separated if there are multiple administrators. Usernames are not used for authorization.
4. Generate a long random webhook secret and a separate long random `FOLLOW_UP_CRON_SECRET`.
5. Set the required values in Vercel Project Settings → Environment Variables, including Production and Preview only if you intend to run the bot in Preview.
6. Deploy the project.
7. Set `TELEGRAM_WEBHOOK_URL` to the deployed `/api/webhook` URL, then register it:

```powershell
python scripts/register_webhook.py
```

The script reads `.env` locally, calls `setWebhook`, preserves pending Telegram updates, and confirms the registered URL. Run it again after changing the deployment URL or webhook secret.

The webhook rejects a missing or incorrect `X-Telegram-Bot-Api-Secret-Token`, ignores numeric user IDs without an active CRM account or valid invitation and messages outside a private chat, records update IDs transactionally, and limits each active account to 60 webhook updates per minute.

## Vercel deployment

Install the Vercel CLI, then from the project directory:

```powershell
npm install --global vercel
vercel link
vercel env add TELEGRAM_BOT_TOKEN production
vercel env add TELEGRAM_WEBHOOK_SECRET production
vercel env add TELEGRAM_WEBHOOK_URL production
vercel env add AUTHORIZED_TELEGRAM_USER_ID production
vercel env add SUPABASE_URL production
vercel env add SUPABASE_SERVICE_ROLE_KEY production
vercel env add SUPABASE_DATABASE_URL production
vercel env add APP_TIMEZONE production
vercel env add FOLLOW_UP_CRON_SECRET production
vercel --prod
```

Add the remaining limit/target variables from `.env.example` if you want values other than their defaults. Set `TELEGRAM_WEBHOOK_URL` to the final production domain before running `register_webhook.py`. `/api/health` is a liveness check; `/api/ready` verifies configuration, database connectivity, and required migration tables without returning internal error details. Readiness returns `503` until the required schema is present.

## Supabase Cron setup

After deployment, open `supabase/cron.sql`, replace the two placeholders, and run it in the Supabase SQL Editor. It stores the worker URL and bearer secret in Vault, then calls `/api/worker` every minute through `pg_cron` and `pg_net`. The worker is not usable without the correct bearer secret.

Each worker invocation prepares at most one newly uploaded preview, processes up to ten import batches, processes at most one queued export and one AI request, prepares and retries due follow-up and Phase 3 sales reminders, removes expired staged objects, clears expired input state, and prunes processed update IDs older than 30 days. Imports commit each batch and resume from the persisted row cursor. Exports are saved as durable jobs and generated outside webhook requests; a failed export is retried up to three times. A 50,000-row file will normally need multiple scheduled invocations; check Telegram for the final result and the Vercel/Supabase logs for operational failures.

The worker starts one daily backup pair at 00:00 in `APP_TIMEZONE` (default `Africa/Addis_Ababa`). It creates two multi-sheet `.xlsx` workbooks in separate scheduled runs so each run fits the serverless time limit: a full snapshot of every PostgreSQL `public` base table and a separate customer workbook with every `contacts` field plus linked call, follow-up, property, sales, assignment, and audit records. It stores timestamped copies in the private `crm-backups` Storage bucket and sends both private download links, valid for seven days, to every configured super administrator in Telegram. Timestamped backup objects remain in private Storage until manually deleted. Apply `202610180001_daily_excel_backup.sql`, `202610180002_customer_profile_backup.sql`, `202610180003_customer_backup_worker_stages.sql`, and `202610180004_midnight_backup_schedule.sql` before deploying this worker version.

Reminder attempts are recorded and retried up to five times. Concurrent workers claim reminder records with row locks. Telegram does not provide an idempotency key for sending messages, so a process crash after Telegram accepts a reminder but before the database records success can still cause one retry duplicate.

## Imports and calling workflow

- Upload `.xlsx` or `.csv` in Telegram. The worker stages the upload and displays up to 20 sample rows.
- Choose **Paste Leads** or send `/import`, then paste tab-separated/CSV rows or one lead per line. A `NO, Name, Phone` header preserves the source number. Ethiopian phone formats with spaces are detected, and a preview requires confirmation before saving.
- Common headers such as `No.`, `Name`, `Phone No.`, `Mobile No.`, and `Phone Number` are detected. A lone `No.`/`Number` column is treated as phone when no explicit phone header exists; when a separate phone column exists, it remains the serial number. Spaces and punctuation in Ethiopian mobile numbers are handled; `+2519XXXXXXXX`, `09XXXXXXXX`, and `9XXXXXXXX` are normalized to E.164. If name or phone columns are missing or ambiguous, the bot asks the user to choose columns.
- Choose one of three policies: add existing contacts to the new campaign, skip existing contacts, or update contact details while preserving call history.
- The database’s partial unique index on normalized phone number protects against duplicate contacts across campaigns. Excel row numbers are never used as primary keys.
- Invalid rows and duplicate phone numbers are counted. Completion messages include representative row errors; invalid rows are not silently imported.
- Start Calling selects a campaign or all campaigns. The current session and selected/skip/call state live in PostgreSQL and can resume after a serverless restart.
- A phone dialer link only opens the dialer. It never initiates a call or creates a call record. The user selects a categorized outcome and explicitly confirms missed/failed attempts.
- Do Not Contact, invalid, paused, converted, disqualified, and not-interested leads do not enter the automatic queue. Restoring a contact or reopening a lead is an explicit action.

The initial phone parser uses Ethiopia (`ET`) as the default region and stores valid values in E.164. Invalid numbers are reported during import; a number can also be marked invalid from a call result.

## Performance metric definitions

- A call attempt is one `call_logs` row. Answered calls are rows whose connection result is `ANSWERED`, including answered sales outcomes.
- Interested prospects count distinct contacts with a `SALES_INTEREST` outcome; qualified prospects count distinct contacts with an explicit `QUALIFIED` outcome in the selected period.
- Meetings, property visits, and conversions count distinct contacts with their corresponding outcome codes.
- Follow-ups scheduled counts rows created during the period. Follow-ups due counts rows scheduled in the period up to the current time, excluding cancelled and rescheduled rows. Completed counts completed follow-ups from that due cohort; the completion rate is completed divided by due.
- Overdue follow-ups is the current count of pending rows scheduled before now. Rates return 0 when the denominator is zero.
- Opportunity stage counts and aging use open opportunity rows and their current stage. Won conversions are counted when a sale is verified; sale value totals use the recorded sale date and verified status. Visit attendance uses resolved attendance outcomes; negotiation, reservation, and sale counts use their recorded status. These are workflow measures, not forecasts.
- Sales and commission totals are grouped by currency. Sales with `PENDING_VERIFICATION` or `DISPUTED` status are not reported as verified conversions. An expected closing date is an agent-entered estimate.

## Tests and performance checks

Unit tests use temporary workbooks and mocked database/Telegram interfaces. CI runs formatting, lint, compilation, the test suite, and a wheel build on Python 3.12. Run tests locally with `python -m pytest`.

For a manual database integration check, create a separate Supabase project or disposable local PostgreSQL database, apply migrations `001` through `005` in order, and follow `tests/README.md`. The current pytest suite is offline and does not read `TEST_SUPABASE_DATABASE_URL`. Never point integration checks at production.

For representative query plans in a disposable test database:

1. Apply the schema migration.
2. Run `supabase/performance/seed_50000_contacts.sql`.
3. Replace the sample session UUIDs in `supabase/performance/explain_critical_queries.sql` if you add session rows.
4. Run the EXPLAIN script and record execution time/buffer usage before making index changes.

The repository includes the 50,000-contact fixture and EXPLAIN queries. They are not claimed to have been run because no Supabase test database is configured in this workspace.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Bot does not answer | `TELEGRAM_WEBHOOK_URL`, Telegram `getWebhookInfo`, the exact webhook secret, and Vercel function logs |
| “Unauthorized” or silent bot | Confirm the account’s numeric ID is included in `AUTHORIZED_TELEGRAM_USER_ID` |
| Database connection fails | Use the Supabase dashboard connection string, SSL, and a connection mode reachable from Vercel |
| Import stays queued | Confirm `supabase/cron.sql` ran, Vault secrets are correct, and `/api/worker` returns success when invoked with the bearer secret |
| Import file cannot be staged | Confirm the service-role key is correct and the private Storage bucket can be created |
| Header selection does not finish | Reopen the most recent preview; select both the name and phone columns before confirming |
| Dialer link fails | Update Telegram on Android and verify `sendRichMessage` phone links on the handset; the call remains manual |
| Follow-up or sales notification not delivered | Check `notification_deliveries.status`, attempts/last error, Cron job runs, and Telegram bot permissions for the authorized user |

## Security checklist

- Use only the authorized numeric Telegram user ID in that user's private chat; usernames and group membership are not access controls.
- Keep all tokens and database credentials in Vercel environment variables or Supabase Vault.
- Do not put the service-role key in messages, frontend code, logs, or Git.
- Keep `crm-imports` private and allow access only through backend Storage requests.
- RLS is enabled with no anon/authenticated policies. The backend database credential is privileged and must remain server-side.
- Webhook and worker endpoints verify independent secrets.
- Updates and call records use database uniqueness for retry protection.
- Do not log phone numbers, notes, bot tokens, SQL parameters, or uploaded file content.
- Do not record call audio or infer call completion automatically.
- Treat export files as sensitive and send them only to the authorized private Telegram account.

## Cost and operating notes

- Phase 1 has no AI API, Redis, message broker, or paid CRM dependency.
- Dashboard values use SQL counts; queues, searches, and exports are bounded.
- Import work is scheduled and batched. Adjust `IMPORT_BATCH_SIZE` after measuring against your Supabase plan and Vercel duration.
- The initial export cap is 50,000 rows. CSV/XLSX export files are generated in temporary files by the scheduled worker and deleted after sending.
- Vercel runtime behavior and Supabase Cron availability depend on the selected account plans. Review current plan limits before deploying.

## Implementation limits

- No external services were configured from this workspace, so the bot was not deployed, the Supabase migration was not applied, and webhook delivery was not verified.
- The Phase 2 migration has only been reviewed and tested offline in this workspace; apply it to the intended Supabase database after the existing migration and before using Phase 2.
- The Phase 3 migration has only been reviewed and tested offline in this workspace; apply it after the initial and Phase 2 migrations before using the sales pipeline.
- The Phase 4 and Telegram outbox migrations have only been syntax-checked offline; apply them to a restored staging database and verify database behavior before production deployment. The outbox is independent of the 30-day webhook deduplication retention so a delayed retry is not deleted with its processed update record.
- No AI credentials were configured. AI provider behavior was exercised with mocked responses; no request was sent to a real provider. Property inventory is entered through Telegram forms; bulk property spreadsheet import is not implemented.
- The Android dialer flow is implemented using Telegram’s documented `tel:` Rich Message link but must be checked on the target Android/Telegram version.
- A full integration run, EXPLAIN ANALYZE run, and 50,000-row import require a disposable Supabase test database and Telegram credentials.

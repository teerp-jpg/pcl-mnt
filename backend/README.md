# MTN backend — T1/T2/T3/T4/T5/T6

Scope: approved Spec/Plan revision 1, T1 (scaffold/readiness) and T2 (data model
and initial schema), T3 (local authentication, users and admin bootstrap), and T4
(audit, garages, categories and PM settings), and T5 (vehicle registry and CORE
CSV export), and T6 (two-step CORE CSV import). Maintenance workflows, business
sample seed and frontend are later tasks.

## Configuration

Required environment variables: `DATABASE_URL` (`postgresql+psycopg`), `APP_ENV`,
`SANDBOX_SEED` (`true` or `false`), `JWT_SECRET`, `SEED_ADMIN_EMAIL`,
`SEED_ADMIN_PASSWORD`, `UPLOAD_ROOT`, `CORS_ORIGINS` (JSON array of HTTP origins).
Secrets come from the controller. Do not put them in source, command arguments,
reports or logs.

## Registered checks and runtime

The controller installs `requirements.txt` in its isolated Python 3.12 environment
with `--no-deps --only-binary=:all:` and verifies consistency. The worker does not
download dependencies or start Docker. From `backend/` in that environment:

```text
python -m pip check
python -m ruff check .
python -m ruff format --check .
python -m pytest
python -m alembic upgrade head
```

PostgreSQL integration tests require the controller's separate test database in
`DATABASE_URL` and `SANDBOX_SEED=false`. Without that URL they are explicitly
skipped, not counted as evidence of database readiness. Unit tests override the
session; they do not connect to external endpoints. Revision `0001_mtn_schema`
creates the 16 approved tables, a record-number sequence and its formatter.
Alembic supports online/offline migrations. Run migrations only through the
registered controller against its isolated test/runtime databases.

T2 schema tests create a random `mtn_t2_*` schema in the controller test database,
run upgrade/downgrade there on an injected connection, and roll back the entire
schema and synthetic rows. They never downgrade the existing public schema.
Tests compare the migrated database with model metadata and check uniqueness,
foreign keys, allowed states, numeric storage and sequence-generated record numbers.

Statuses use text plus CHECK constraints; workflow transitions and optimistic
version comparisons remain the responsibility of later domain services. Only
maintenance lines have the specified cascading foreign key; other history links
prevent deleting a referenced parent. Nullable actor columns support initial admin
bootstrap; service writes must attribute actors and audits in later tasks.

Record numbers use one database sequence (no monthly reset), the Bangkok month
and at least five digits. Sequence gaps on rollback are expected; numbers beyond
99999 expand instead of truncating and colliding. Draft service dates can be NULL;
T7 must enforce the required fields at start/completion. No approval state is added.

The registered runtime launches `uvicorn app.main:app --host 0.0.0.0 --port 8000`.
Application startup validates configuration and never runs migrations. The public
`GET /health/ready` executes `SELECT 1` through SQLAlchemy and returns
`200 {"status":"ready"}` or `503 {"status":"not_ready"}` without driver details.
An HTTP 200 here proves only this database probe, not login or business workflows.

## Local sandbox identity (T3)

After controller-managed migrations, startup runs administrator bootstrap only when
`SANDBOX_SEED=true` and `APP_ENV=sandbox`. `python -m app.seed` invokes the same
idempotent bootstrap. False performs no seed queries. Existing accounts (including
password, role and active state) are never overwritten. Only a synthetic admin is
created here; the full business sample dataset is T12. The controller owns credential
handoff; no password or token belongs in reports or logs. This is not company SSO.

`POST /api/v1/auth/login` accepts email/password and returns `{user}` while setting
an eight-hour HS256 `mtn_session` httpOnly, SameSite=Lax cookie. Secure is disabled
only in sandbox. `GET /api/v1/auth/me` returns `{user}` and authenticated
`POST /api/v1/auth/logout` clears the browser cookie. Bearer JWTs are accepted for
tests. Logout clears this cookie; it does not revoke a separately copied Bearer JWT.
All routes registered on the `/api/v1` router require authentication except login.
Role headers have no effect. Browser writes with an Origin must match CORS_ORIGINS.

Admin-only endpoints: `GET /api/v1/users?offset=0&limit=100` (maximum 200),
`POST /api/v1/users` (email/display_name/role/password),
`PATCH /api/v1/users/{id}` (version plus display_name/role/is_active), and
`POST /api/v1/users/{id}/reset-password` (new_password). Creation/reset require
12–1024 characters; PBKDF2-SHA256 uses 600,000 iterations and a random 16-byte salt.
Every user update/reset increments version and invalidates that user's older JWTs;
users must log in again after profile/role changes. PATCH with a stale version gives
409 VERSION_CONFLICT. Duplicate email gives 409 USER_EMAIL_EXISTS.

User writes and seed take one PostgreSQL transaction advisory lock before rechecking
the actor and last active admin, preventing concurrent removal of all admins. No
user deletion is implemented. User changes and safe before/after snapshots are
audited in the same transaction, excluding passwords and hashes. The user-specific
wrapper now delegates to the general audit service from T4.

T3 tests use real JWTs and synthetic users in rollback-only PostgreSQL transactions,
covering cookie flags, auth failure/expiry/tampering, role checks, session invalidation,
last-admin protection, lock exclusion, audit redaction and idempotent bootstrap.
Only password primitive tests run without application/database dependencies.

## Master data and settings (T4)

All roles can read `GET /api/v1/garages` and `/maintenance-categories`, including
`/{id}`. Lists accept optional `is_active`, `offset` and `limit` (1–200). Inactive
rows remain visible by default for historical references. Manager/admin may POST,
PATCH and DELETE these resources. DELETE only changes `is_active=false`; it never
removes a row or its references. Garages can be reactivated with PATCH.

Garage fields are `name`, optional `tax_id`, `branch_name`, `address`, `phone`,
`contact_name`, and `is_active`. Garage PATCH requires `version`; DELETE requires
the current `?version=` query parameter. Both return 409 VERSION_CONFLICT on a
stale version. Names compare case-insensitively with all whitespace removed.
Tax identifiers, when supplied, must contain exactly 13 ASCII digits. Nullable
contact fields and tax identifiers can be cleared with explicit null. Uniqueness
also applies to inactive rows (409 GARAGE_NAME_EXISTS/GARAGE_TAX_ID_EXISTS).

Category fields are `code`, `name_th`, nullable `parent_id`, and `is_active`.
Code is unique as stored (trimmed; case-sensitive). Parent null detaches a category;
missing parents return 404 NOT_FOUND, self/ancestor cycles 422 CATEGORY_CYCLE.
Category writes use a transaction lock for the hierarchy; the approved schema has
no category version column, so no category optimistic-version contract is added.
Disabling a parent does not delete or disable its children.

`require_active_garage` is the shared guard for new maintenance records: it rejects
inactive garages with 422 GARAGE_INACTIVE and holds a row lock until transaction
completion. T7 must integrate it with record creation; historical reads retain the
garage reference and name after deactivation. No maintenance endpoint exists yet.

Admin-only GET/PUT `/api/v1/settings` expose only `pm_due_soon_pct` (finite 0–100,
fractional percentages allowed), `pm_due_soon_km`, and `pm_due_soon_days` (whole
non-negative counts). Missing database values default to 10/500/14 without writes.
PUT requires all three fields, persists them together, and preserves unrelated
setting rows. Secrets/environment configuration cannot be read or written here.

Master/settings writes and explicit before/after audit snapshots share one
transaction, including soft deactivation. Collection-specific advisory locks
serialize uniqueness/hierarchy/settings changes; garage row locks plus version
checks reject stale updates. Savepoints roll back rejected or unauditable writes.
The common audit writer never commits and accepts only explicit public snapshots.
It copies JSON-compatible fields (Decimal/UUID/dates as strings), recursively
removes credential keys, and rejects arbitrary objects rather than stringifying
them. Callers must never place secrets in free text or unlabeled snapshot values.
The user/seed audit wrapper uses the same writer without changing T3 behavior.

T4 adds pure schema tests plus PostgreSQL tests with real JWTs for roles,
uniqueness, versions, parent cycles, deactivation/history, settings, advisory lock
exclusion and audit rollback. Business endpoint enforcement of inactive garages
remains T7; these tests do not claim the maintenance workflow is implemented.

## Vehicle registry and CORE export (T5)

All roles can read `/api/v1/vehicles` (q/status/sync_status/page/page_size),
`/vehicles/{id}` including plate history, `/vehicles/{id}/summary`, and
`/vehicles/{id}/maintenance-records` (type/from/to/status/offset/limit).
Search removes spaces and hyphens and includes historical plates. Literal `%`
and `_` are escaped. Page size is limited to 200; results have items/total/page/page_size.
Summary totals include only COMPLETED records and serialize Decimal as strings.
It exposes active PM plan baselines; `pm_status` is null until T8 integrates its
due engine, and must not be displayed as OK. The latest record includes its status.

Manager/admin may POST or PATCH vehicles. PATCH requires the current version.
Local creation assigns origin MTN and PENDING_CREATE; clients cannot assign
CORE IDs, origin, sync status or actor fields. Optional fields can be cleared
with null. Years above 2400 are converted from Buddhist to Gregorian once on
input. Identity uniqueness follows BR-13, including INACTIVE vehicles. A disposed
vehicle requires disposed_at and cannot return to ACTIVE/INACTIVE. Plate changes
close the current history interval and add a new one without changing old
maintenance snapshots. No-op PATCH does not create a new version/change/audit.

Manager/admin `GET /vehicles/core-export` returns a UTF-8 BOM CSV with the
approved import fields plus `mnt_vehicle_id`, `change_type`, and `changed_fields`.
The historical `mnt_vehicle_id` column name is retained for CSV compatibility;
the system is named MTN. Only pending vehicles are included. Changed fields are
the union of local edits not yet assigned an export batch, in template order.
All current field values are always exported. Missing CORE ID means CREATE,
otherwise UPDATE. Spreadsheet formulas (including those behind leading whitespace)
are prefixed with an apostrophe in CSV only; stored values are not altered.
An empty export returns headers and records an empty batch. No files are written
and CORE is never contacted. This GET changes bookkeeping as required by the
approved API: the response is no-store and foreign Origin/cross-site browser
requests are rejected. Export responses include an X-Export-Batch-ID header.

Export links vehicle_change rows to the batch, changes sync status to EXPORTED,
increments vehicle versions and writes audits atomically. Subsequent edits return
to PENDING_CREATE when no CORE ID exists, or PENDING_UPDATE otherwise, and require
another export before confirmation. POST `/vehicles/{id}/confirm-core-sync` takes
core_asset_id and an optional observed version; it requires EXPORTED and returns
SYNCED. Existing CORE identities cannot be replaced; duplicates are rejected.
Clients should send version to guard acknowledgements against a newer export.
Confirmation is the operator's recorded assertion, not proof CORE received a file.

DELETE `/vehicles/{id}?version=` is limited to PENDING_CREATE vehicles without
maintenance records (BR-14). Any remaining FK reference also prevents deletion.
Deletion removes the unsynced registry draft/history/change rows but preserves
the audit snapshots. Other vehicles must be disabled or disposed instead.

Registry writes/export use transaction advisory lock 6835046301127, then row
locks. T6 import apply must reuse `lock_vehicle_writes` before matching/writing
identities, and preserve the same actor/audit/history semantics. Savepoints keep
rejected writes and audit/CSV failures atomic. T5 adds PostgreSQL tests for JWT
roles, identity uniqueness, statuses, history, immutable maintenance snapshots,
versions, delete protection, export/sync lifecycle, locks and failure rollback,
plus pure schema and CSV-injection tests. T5 does not implement CSV importing,
maintenance writes, due calculations or a frontend.

## Two-step CORE CSV import (T6)

Manager/admin only: `POST /api/v1/vehicle-imports` accepts multipart field `file`.
CSV is UTF-8 with optional BOM, comma-delimited, and requires core_asset_id,
plate_no and province headers. Content-Type and filename are not trusted.
The reader consumes at most 5 MiB + 1 byte and rejects oversized files; CSV is
limited to 10,000 nonblank logical rows. Blank rows are ignored, and row_no starts
at 1 for the first data row (multiline quoted cells remain one logical row).
No XML, spreadsheets, archives, OCR, network calls or arbitrary file paths are used.
Only canonical vehicle columns are retained; unrelated CORE columns are ignored.
Multipart handling may spool uploads in the controller's writable temporary area
before endpoint validation; no raw upload is persisted by the application.

Optional `CORE_CSV_COLUMN_MAP` is a JSON object mapping canonical names to actual
CSV headers, for example `{"core_asset_id":"asset_code","plate_no":"registration"}`.
Unspecified mappings retain canonical names; duplicate/unknown mappings fail startup
without displaying their values. Each preview stores canonical input, so a later
configuration change does not reinterpret an already validated batch.
Optional cells or absent optional columns mean null; absent/blank status defaults
to ACTIVE. Years must have four ASCII digits; values above 2400 subtract 543 once.
disposed_at uses YYYY-MM-DD and is required for DISPOSED. NULs, invalid UTF-8,
malformed CSV/header/limits give 422 IMPORT_FILE_INVALID; field errors are per-row
ERROR results. Registry identity collisions also produce per-row errors.

Preview returns id/status/row_count/summary/warnings/rows. Row outcomes are NEW,
UPDATE, UNCHANGED, CONFLICT or ERROR. Preview never changes vehicles. Summary
includes counts and IDs not found in this file, without changing those vehicles.
`GET /vehicle-imports/{id}` retrieves the preview and its decisions.
`POST /vehicle-imports/{id}/resolutions` takes:

```json
{"resolutions":[{"row_no":1,"resolution":"USE_CORE"}]}
```

Matching uses CORE ID first. A unique normalized plate/province match to an MTN
vehicle without CORE ID requires LINK. LINK explicitly adopts the incoming CORE
identity and values, retains origin=MTN, and marks SYNCED. A differing CORE snapshot
against unsynced local edits requires USE_CORE or KEEP_MNT. USE_CORE accepts CORE
values and marks SYNCED; KEEP_MNT preserves values and pending/exported sync state.
Decisions cannot specify a different vehicle or edit raw input. Duplicate row
decisions and resolutions inappropriate to the conflict type are rejected.
Duplicate CORE IDs/live plates/chassis within one file are errors for every
affected row, avoiding order-dependent overwrites. Duplicate targets are rejected.

`POST /vehicle-imports/{id}/apply` refuses unresolved conflicts with
409 IMPORT_UNRESOLVED_CONFLICT. It applies valid NEW/UPDATE/resolved rows, skips
ERROR and KEEP_MNT rows, and marks APPLIED in one transaction including audit.
Repeated apply returns 409 IMPORT_ALREADY_APPLIED. An equal incoming snapshot may
acknowledge pending/exported data as SYNCED without changing registry values.
`POST /vehicle-imports/{id}/discard` marks DISCARDED and preserves all evidence.
Applied/discarded batches cannot be edited or revalidated.

All import writes reuse the T5 registry advisory lock. Apply rechecks every target,
version and outcome before its first vehicle write. Changed previews return
409 IMPORT_STALE, with no partial apply. To review the original bytes again,
`POST /vehicle-imports/{id}/revalidate` refreshes the original VALIDATED batch,
clears all earlier decisions and requires review/resolution again. This technical
endpoint avoids requiring a duplicate upload as a way to recover from staleness.

SHA-256 repeat detection covers all previous batches (including VALIDATED and
DISCARDED), as required by AC-02. Repeated uploads return all UNCHANGED plus
IMPORT_FILE_SEEN_BEFORE and never modify vehicles on apply. Revalidate cannot turn
a repeated-file batch into a write. To recover a stale preview, revalidate its
original batch rather than uploading identical bytes again.

Accepted CORE changes retain old maintenance plate snapshots and add CORE_IMPORT
plate history when the plate/province changes. CORE acceptance sets last_imported_at;
export excludes superseded local change rows at or before that timestamp, preserving
their history. New local changes use wall-clock timestamps so this cutoff also works
when a transaction began before acquiring the shared registry lock. No schema change
or migration is added. CSV confirmation remains local bookkeeping, not proof of a
live CORE integration.

T6 includes pure parser/config tests and synthetic PostgreSQL integration tests for
all outcomes, resolutions/LINK, SHA repeats, roles, stale previews/revalidation,
history, audit and rollback after a midway failure. These new integration tests must
be executed by prepare_backend for the exact T6 snapshot; earlier T5 results cannot
certify this source. The synthetic template is tests/fixtures/core-assets-synthetic.csv.

## T7 maintenance records (Spec/Plan revision 2)

The existing schema is retained. `POST /api/v1/maintenance-records` creates a
DRAFT bound to a vehicle, with immutable plate/province snapshots. GET returns
header, lines, attachments, garage name and warnings. PATCH requires `version`;
`/start` and `/complete` accept `{version}`. Manager/admin alone can call `/cancel`
with `{version,reason}` or `/corrections` with `{version,reason,changes}`.
Clerk/manager/admin can create/edit/start/complete and manage attachments; all
authenticated roles can read. COMPLETED permits corrections only; CANCELLED is
terminal. All writes share the registry transaction lock and savepoint, including
audit, odometer projection and linked PM baseline updates. Transitions use one
table in `app/domain/maintenance_state.py`. Optional in-process handlers in
`session.info["record_status_changed_handlers"]` execute transactionally and must
only perform database work, never external effects.

Send qty/unit_price/VAT as decimal **strings** (exact integers also work).
Fractional JSON numbers are rejected to avoid passing through binary float.
Input scale is at most two decimal places, ignoring trailing zeros, and must fit
the existing NUMERIC precision. Invalid scale, NaN, infinity and calculated
overflow return 422 VALIDATION_FAILED. The server ignores client-supplied amount,
subtotal and total_amount; it rounds each line HALF_UP, then sums rounded amounts.
`1.50 * 33.33` produces `50.00`; all money and quantities serialize with two
decimal places. Header-only edits and corrections recalculate persisted operands.

Completion requires start/end dates, garage, at least one line, and odometer or
its missing reason. CM requires failure_description. Regression against the latest
prior dated reading needs a reason. The service projects completed readings and
recomputes linked plan baselines after correction. PM due calculations/API remain
T8. Duplicate invoice and inactive vehicle return warnings without blocking.

Multipart field `file` at `/{id}/attachments` accepts PDF/JPEG/PNG signatures up
to 10 MiB. Filename and declared MIME are not trusted. Immutable UUID blobs are
copied in 64 KiB chunks under UPLOAD_ROOT, with SHA-256 metadata. Downloads require
auth, use attachment disposition and nosniff, and never expose storage keys.
Attachments can be changed only in DRAFT/IN_PROGRESS; changes increment record
version. Deletion removes access/metadata transactionally while retaining the blob.
Failed DB transactions may also leave unreferenced blobs. There is intentionally
no automatic filesystem deletion; preserve the upload volume for separately
authorized retention/recovery. Magic-byte checks are not antivirus scanning;
ClamAV and OCR remain outside this approved Sandbox scope.

T7 adds pure money/storage tests plus PostgreSQL workflow/role/audit/attachment
tests. Run registered `prepare_backend` against the new source before independent
review; the earlier 278-test result certifies the previous T1–T6 snapshot only.
No new dependency or migration is required. This milestone does not start an
application or establish UAT readiness.

## T8: PM plans and due observations

All authenticated roles can read `/api/v1/pm-plans` (optional vehicle_id,
is_active, offset/limit), plan detail, and `/api/v1/pm-due`. Manager/admin can
POST/PATCH plans; PATCH requires version, DELETE requires `?version=` and
deactivates without deleting the plan or historical references. Vehicle identity
is immutable. Engine-hour intervals remain phase 2 and cannot be written here.
Each plan needs a positive km/day interval. Baselines can be supplied for existing
history; T7 updates them from linked completed work. Writes and audit use the same
registry transaction lock as T7; rejected writes roll back together.

`/pm-due` returns `{items,total,offset,limit}` for active plans, with optional
vehicle_id and comma-separated status=OK,DUE_SOON,OVERDUE filters. It reports the
next due date/km, remaining date/km, observed reading/time, Bangkok data_as_of,
and reason. An unperformed plan is OVERDUE with reason NEVER_DONE. At/beyond a
due boundary is OVERDUE; DUE_SOON means remaining < configured percentage of the
interval OR <= configured absolute km/days. The highest risk from available
clocks wins. The latest dated observation is used, including justified mileage
regressions; future readings are excluded. Missing mileage falls back to days.
When no clock can be evaluated, status is null with INSUFFICIENT_DATA, never OK.
Settings are read once and readings ranked in one query, avoiding per-plan reads.
Filtering evaluates the small fleet's active plans before pagination.

Vehicle summary pm_status uses this same engine: OVERDUE then DUE_SOON; OK only
when all active plans are known OK, otherwise null (including no active plans).
Date arithmetic outside the representable calendar is unevaluable rather than
crashing the fleet list. Preserve readings and baselines to recover missing data.

Pure boundary/calculation tests and PostgreSQL CRUD, role, stale-version, settings,
completion, summary and rollback tests cover T8. The earlier 351-test evidence
belongs to Source before T8; run registered prepare_backend for this snapshot
before independent review. No schema or dependency changes are introduced.

## T9: cost summary reports

Every authenticated role can GET `/api/v1/reports/cost-summary` with
`group_by=vehicle|month|type|garage` (default vehicle), optional inclusive ISO dates
`from`/`to`, and `format=json|csv` (default json). Invalid dates, reversed ranges,
unknown groupings/formats return 422 VALIDATION_FAILED. No branch restriction or
external system connection is introduced.

Only COMPLETED headers with a recorded service_end_date contribute; the date
range and calendar month use that completion date, not start/creation date.
SQL aggregates headers directly, so multiple lines never multiply totals.
JSON returns `{group_by,date_from,date_to,items,totals}`. Each item has group_key,
group_label, record_count, subtotal, vat_amount and total_amount. Money is always
an exact two-decimal string; totals can exceed a single record's storage limit.
The grand totals derive from the same query result, avoiding inconsistent reads
during a concurrent correction. No writes, audit mutations or commits occur.

Vehicle/garage groups use stable IDs with current master labels (vehicle includes
province); past plate snapshots do not split one vehicle into multiple groups.
Inactive masters remain included. A missing garage has null key and the label
`ไม่ระบุอู่`. Months are YYYY-MM. Groups sort by key, null last. An empty result has
no items and zero totals; missing calendar months are not filled with synthetic
rows. The report reflects current corrected records, not an audit reconstruction.

CSV uses the same items, UTF-8 BOM and the columns group_by, group_key, group_label,
record_count, subtotal, vat_amount, total_amount. It is a download with no-store
and nosniff. Spreadsheet formula prefixes are escaped using the shared CSV guard;
stored data and JSON labels remain unchanged. No extra total row is emitted, so
summing CSV rows equals JSON totals without double counting. Empty CSV has headers.
Aggregation happens in PostgreSQL; only the grouped results and CSV are buffered,
appropriate for the small fleet recorded in company_context rev 29.

T9 tests independently reconcile raw database headers with all four groupings,
JSON/CSV parity, status/date/year boundaries, inactive/missing garages, multiline
headers, exact money, role authentication, filters and a single read query.
Run registered prepare_backend for the changed Source before independent review.
No migration or dependency changes are required; this is not runtime/UAT evidence.

## Recovery

No production action or real record modification is authorized by these tasks.
If configuration/dependencies fail, retain the source snapshot and correct the
registered sandbox environment before retrying the checks. For a code regression,
keep the previous reviewed snapshot available and have the controller select it;
do not delete source or reset Git history. Preserve database/upload volumes.
T2 downgrade drops its domain tables and must only be exercised in the disposable
test schema. For recovery of a populated sandbox, preserve a database snapshot
and use a reviewed forward repair; do not run downgrade against existing data.

# Employee Attendance & Analytics API

FastAPI + MongoDB service for punch-in/punch-out, manual corrections with an audit trail, and attendance analytics.
All application code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # or export MONGO_URI / MONGO_DB; real environment variables take priority
python sample_seed.py             # optional: the nine sample documents
uvicorn app.main:app --port 8000
```
Once it is running, open http://127.0.0.1:8000/docs to try the endpoints and http://127.0.0.1:8000/health to check it is ready. The root URL `/` returns 404 on purpose: the contract defines no home page.
Requires MongoDB 6.0+ (developed for 7.0). Indexes are created at startup, idempotently; no manual steps.
`MONGO_URI` is required; `MONGO_DB` defaults to `attendance_db`.

## Check it
```bash
pip install httpx2   # only needed for the self-check, not for the app
MONGO_URI=mongodb://localhost:27017 python tests/selfcheck.py         # correctness, races, explain plans
MONGO_URI=mongodb://localhost:27017 python tests/selfcheck.py --big   # + ~140k records and timings
```
`tests/selfcheck.py` uses its own database (`attendance_selfcheck`, dropped and re-seeded on every run). It seeds the
awkward cases (no-log employees, mid-month and future joiners, overnight shifts, weekend records, legacy documents,
ties at the leaderboard cutoff, logs of unknown employees), compares every analytics endpoint with an independent
Python implementation of R1-R10, re-runs the concurrency tests, and checks that every explain plan uses `IXSCAN` and
no `COLLSCAN`.

## Notes
* Instants are stored as BSON dates and exchanged as epoch milliseconds, truncated to whole seconds before any rule.
* Rounding is half-up everywhere (R8): `Decimal` in Python; in pipelines a `Decimal128` floor(x * 10^n + 0.5)
  expression, because MongoDB's `$round` rounds half to even.
* Analytics are aggregation pipelines; `/admin/explain/{endpoint}` explains the exact pipeline the endpoint runs (the
  same builder function is used by both).
* Interpretation choices where the contract is silent: the trend range limit is 92 days inclusive of both ends; a
  `PATCH` with no `status`/`punch_in`/`punch_out` is 422; `null` for those fields in a `PATCH` is 422 (omit to keep).
* Department summary and trend count logs of the department's employees who joined on or before the end of the
  period (the same set as the headcount).

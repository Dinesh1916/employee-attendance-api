# DECISIONS.md

## 1. Indexes

I created five indexes, all at startup via `create_indexes()`:

- **`employees.emp_code` (unique)** — the primary uniqueness constraint. Because the unique index is enforced by MongoDB, concurrent `insert_one` calls for the same code race at the database level; only one wins and the loser gets `DuplicateKeyError`. This is the only correct way to guarantee uniqueness without a distributed lock.
- **`employees.department`** — speeds up the department filter in `GET /employees` and analytics lookups that match employees by department.
- **`employees.joined_on`** — used in `$match` stages that filter employees hired on or before the end of a period (R9 headcount).
- **`attendance_logs (emp_code, date)` (unique)** — the natural compound key for every per-employee-per-day lookup (punch-in, punch-out, regularize). The unique constraint enforces the one-record-per-day business rule at the database level, replacing the unsafe check-then-insert pattern in the starter.
- **`attendance_logs (date DESC, emp_code ASC)`** — matches the sort order of `GET /attendance` exactly, so MongoDB can serve the query with an index scan rather than an in-memory sort.
- **`attendance_logs (date ASC, status ASC)`** — supports analytics aggregations that filter on a date range plus optionally a status.

I considered a separate index on `attendance_logs.emp_code` alone but rejected it: the compound `(emp_code, date)` unique index already satisfies all emp_code prefix queries, so a single-field index would be redundant.

## 2. Punch-in race condition

Two identical punch-in requests arrive simultaneously for the same employee and date. Both read the employee document (a read, so no conflict there). Both compute the same attendance date and build an identical document. Both call `db.attendance_logs.insert_one(doc)`. MongoDB's unique index on `(emp_code, date)` ensures only one insert succeeds; the other receives a `DuplicateKeyError`, which my code catches and converts to HTTP 409. The winner returns 201. No check-then-insert, no application-level lock needed.

## 3. Ties in the late leaderboard

If two employees are tied on `total_late_minutes` at the cutoff rank, both are included in the response. I implement standard competition ranking (1, 2, 2, 4): I sort by `total_late_minutes` DESC then `emp_code` ASC in MongoDB, then assign ranks in Python by grouping consecutive rows with equal totals. When the tie straddles `limit` (e.g., limit=2 and ranks 2 and 2 both qualify), both tied employees are returned because the spec says "return every employee whose rank ≤ limit."

## 4. Headcount with zero logs

The `department_summary` aggregation starts from the `employees` collection and `$lookup`s attendance logs as an optional side. I use `$unwind` with `preserveNullAndEmpty: true` so that employees with no logs in the month still produce a row (contributing to headcount). Summing the log-derived fields for those rows produces zeros, which is correct.

## 5. One thing I would change for 100× the data

I would move the working-days calculation and the 7-day moving average in `department_trend` entirely into the MongoDB aggregation pipeline (replacing the Python loops), and add a time-series-style compound index on `(emp_code, date)` partitioned by month, so date-range scans on ~10 M log rows stay sub-millisecond. I would also consider sharding `attendance_logs` on `emp_code` to distribute write load across nodes.

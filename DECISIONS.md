# DECISIONS.md

## 1. Indexes

I have created five indexes and they were all created at the moment of startup, using `create_indexes()`.

The first one is on `employees.emp_code` (unique) – it is the primary uniqueness constraint that makes concurrent `insert_one`s for the same code race at the DB level, with only one winning and the others raising `DuplicateKeyError`. This is the only correct way of doing things that require uniqueness, in this case, without requiring a distributed lock.

Next, I have created an index on `employees.department` that will speed up the filtering in `GET /employees` and analytics lookups that match by department.

Then, I have created an index (ascending) on `employees.joined_on` – used in `$match` stages that require selecting employees based on joined-on-or-before-end-of-period (R9 headcount).

On the `attendance_logs (emp_code, date)` compound field – a natural compound primary key for every per-employee-per-day lookup (punch-in, punch-out, regularize). The unique constraint here enforces the one-record-per-day business rule at the DB level (replacing the unsafe check-then-insert pattern in the starter).

The next is an index (descending, then ascending) on the `attendance_logs (date DESC, emp_code ASC)` – it will match the sort order of `GET /attendance` exactly, meaning the DB can serve the query with an index scan, instead of a sort in memory.

Finally, on `attendance_logs (date ASC, status ASC)` – which will help with analytics aggregations that require filtering on a date range plus optionally a status.

I have considered making a separate index on `attendance_logs.emp_code`, but rejected the idea – the compound `(emp_code, date)` index includes all of the queries' emp_code prefix conditions, and therefore a single-field index would be redundant.

## 2. Punch-in race condition

When two identical punch-in requests come in at the same time for the same employee and date, they both read the employee document (which is a read, and therefore there is no conflict), and they compute the same attendance date and build the same document; when they call `db.attendance_logs.insert_one(doc)`, MongoDB's unique index on `(emp_code, date)` ensures that only one insert will succeed, raising `DuplicateKeyError` on the other, which my code will catch and return HTTP 409. The winner will get 201. There is no need for a check-then-insert or any other application-level lock.

## 3. Ties in the late leaderboard

If two employees are tied on `total_late_minutes`, at the cutoff rank, and should both be included in the response, I use standard competition ranking (1, 2, 2, 4), etc. I sort by `total_late_minutes` descending, then `emp_code` ascending in MongoDB, and assign ranks in Python by grouping consecutive rows with the same `total_late_minutes`. When the tie spans the `limit` (e.g. limit=2, and there are two 2s, then all of them are included in the response, as per the spec).

## 4. Headcount with zero logs

The `department_summary` aggregation starts from the `employees` collection, and `$lookup`s attendance logs as an optional side. I use `$unwind`s with `preserveNullAndEmpty: true`, which means that even employees with no logs in the month will produce a row, contributing to the headcount. Summing the log-derived fields for these rows will produce zeros, which is correct.

## 5. One thing I would change for 100× the data

I would have moved the calculation of the working-days and the 7-day moving average in `department_trend` into the MongoDB aggregation pipeline (instead of the Python loops), and added a time-series-style compound index on `(emp_code, date)` partitioned by month, to make date-range scans on ~10 M log rows be sub-millisecond. Additionally, I would consider sharding the `attendance_logs` on `emp_code` to spread the write load across nodes.

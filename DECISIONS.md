# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.**
   - `employees.emp_code` (unique): Guarantees global employee uniqueness and accelerates direct lookups.
   - `attendance_logs(emp_code, date)` (compound unique): Enforces at most one attendance record per employee per calendar day and eliminates punch-in race conditions.
   - `attendance_logs(date: -1, emp_code: 1)`: Backs the paginated `GET /attendance` query with matching index sort to eliminate in-memory sorting.
   - `employees(department: 1, joined_on: 1)`: Supports department filtering, summary aggregations, and headcount range filters.
   - `attendance_logs(date: 1, late_minutes: 1, emp_code: 1)`: Backs the late leaderboard and monthly aggregation pipelines.
   - *Rejected index:* An index on `(punch_in)` was considered but rejected because records are addressed by `(emp_code, date)` rather than raw timestamps, and superfluous indexes unnecessarily degrade write throughput during high-frequency punch operations.

2. **Punch-in race.**
   Both requests compute the attendance date independently and attempt an atomic `insert_one`. Because of the unique compound index on `(emp_code, date)`, MongoDB accepts the first insert and rejects the second with a `DuplicateKeyError`. The winner returns HTTP 201 with the created record; the loser catches the `DuplicateKeyError` and returns HTTP 409 ("already punched in for this date"), completely avoiding check-then-insert race conditions.

3. **Ties.**
   The leaderboard uses MongoDB's `$rank` window function in `$setWindowFields` ordered by `total_late_minutes` descending. Following standard competition ranking (1, 2, 2, 4), tied employees share the same rank. The query then filters by `rank <= limit`. If multiple employees tie at the cutoff rank, all of them are returned (which may exceed `limit` total rows), strictly adhering to the contract. Tied entries are ordered by `emp_code` ascending.

4. **Headcount.**
   The department summary aggregation starts from the `employees` collection filtered by `joined_on <= last_day_of_month` rather than from `attendance_logs`. Each matching employee is grouped by department to calculate `headcount` via `$sum: 1`. Attendance records are joined via a `$lookup`. Employees with zero logs yield an empty log array while still being counted toward their department's headcount.

5. **One thing you would change for 100x the data.**
   For 100x data, pre-aggregate daily and monthly summaries into dedicated rollup collections (via MongoDB Change Streams or a scheduled pipeline) instead of computing ad-hoc cross-collection `$lookup` joins and `$densify` aggregations over millions of raw documents on every analytics request.

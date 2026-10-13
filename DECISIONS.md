# DECISIONS.md

1. **Indexes.**
   - `employees`: Unique index on `emp_code` enforces business identifier uniqueness and concurrency safety; index on `department` and compound `(department, joined_on)` optimizes department filtering and headcount queries.
   - `attendance_logs`: Unique compound index on `(emp_code, date)` guarantees at most one record per employee per day and eliminates race conditions. Compound index on `(date, emp_code)` satisfies list query sorting and date filtering without in-memory sorting. Compound `(date, late_minutes)` accelerates leaderboard aggregations.
   - *Rejected*: An index on the `history` array was rejected because corrections are only retrieved as part of single-document fetches, never filtered across documents.

2. **Punch-in race.**
   When two identical punch-in requests arrive simultaneously, both compute the IST attendance date and execute `insert_one`. The database unique index on `(emp_code, date)` ensures MongoDB atomically commits exactly one insert. The second insert raises a `DuplicateKeyError`, which the API catches and translates to HTTP 409 Conflict.

3. **Ties.**
   The leaderboard calculates standard competition ranking (e.g., 1, 2, 2, 4). When two employees tie at the cutoff rank, both are returned because `limit` filters by `rank <= limit` after ranking, rather than slicing array positions. Tied employees are ordered by `emp_code` ascending.

4. **Headcount.**
   The aggregation begins on the `employees` collection matching `joined_on <= last_day_of_month` and groups by department before performing a `$lookup` against `attendance_logs`. Every eligible employee is counted in headcount even if their logs array is empty.

5. **One thing you would change for 100x data.**
   I would implement an incremental summary collection (or bucket pattern) updated during punch-out and regularizations, serving pre-computed monthly and daily department aggregates instead of scanning and joining millions of raw attendance logs per request.

# DECISIONS.md

1. **Indexes.**
   On `employees`, unique `emp_code` guarantees identity and fast lookups; `(department, emp_code)` and `(joined_on, department)` accelerate filtering and headcount aggregation. On `attendance_logs`, unique `(emp_code, date)` guarantees exactly one record per day and race-safety; `(date, -1, emp_code, 1)` powers sorting without in-memory reordering; `(date, 1, late_minutes, 1)` backs monthly leaderboards. We rejected a standalone index on `status` due to low cardinality (5 values), which degrades write throughput without improving query selectivity.

2. **Punch-in race.**
   Both concurrent requests pass validation and attempt `insert_one()`. The database engine resolves the collision on the unique index `(emp_code, date)`: the first write succeeds and returns 201, while the second raises `pymongo.errors.DuplicateKeyError`, which our code intercepts to immediately return 409.

3. **Ties.**
   Using MongoDB’s `$rank` window function implements standard competition ranking (1, 2, 2, 4). The `limit` filter (`rank <= limit`) applies after ranking. If two employees tie at the cutoff, both share the same rank and are included (the response may exceed `limit` rows), ordered by `total_late_minutes` descending, then `emp_code` ascending.

4. **Headcount.**
   The aggregation pipeline starts from `employees` filtered by `joined_on <= end_of_month`. It joins logs using `$lookup` and `$unwind` with `preserveNullAndEmptyArrays: true`. Headcount is computed via `$addToSet: "$emp_code"`, ensuring employees with zero logs are retained.

5. **One thing you would change.**
   At 100x scale, pre-computing daily departmental summaries into a separate materialized collection via incremental pipelines would avoid heavy real-time cross-collection `$lookup` joins on millions of records.

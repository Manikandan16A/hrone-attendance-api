# DECISIONS.md

### 1. Indexes

I created four indexes at startup (idempotently via `create_index`):

| Index | Fields | Purpose |
|---|---|---|
| `idx_emp_code` | `employees.emp_code` (unique) | Enforces uniqueness of the business key at the DB level, making duplicate employee creation a single atomic operation rather than a TOCTOU check. Used by every employee lookup. |
| `idx_log_empcode_date` | `attendance_logs.(emp_code, date)` (unique) | Natural key for the record; enforces at-most-one record per (employee, day). Supports punch-in, regularize, and analytics lookups. Also makes the punch-in race safe without application-level locking. |
| `idx_log_empcode_punchin` | `attendance_logs.(emp_code, punch_in)` | Used by `punch-out` to find the most recent open record for an employee via a range query on `punch_in`, sorted descending. Without it this would be a collection scan. |
| `idx_log_date_desc_emp` | `attendance_logs.(date DESC, emp_code ASC)` | Matches the required sort order for `GET /attendance`. MongoDB can satisfy both the filter and the sort from this index, avoiding an in-memory sort on large collections. |
| `idx_log_date_status` | `attendance_logs.(date, status)` | Supports analytics aggregations that filter by a date range and status in the same stage, allowing IXSCAN instead of COLLSCAN on large datasets. |

**Index I considered and rejected:** A single-field index on `attendance_logs.emp_code`. The compound index `(emp_code, date)` already covers all single-`emp_code` lookups as a prefix, so a separate single-field index would be redundant and waste write throughput.

---

### 2. Punch-in race

Two identical punch-in requests for the same employee arrive simultaneously.

Both requests query the employee (succeeds for both) and build a document with the same `(emp_code, date)` key. They then both call `insert_one`. MongoDB evaluates the unique index atomically: the first insert acquires a write lock on that key slot and succeeds with HTTP 201. The second insert finds the unique key already taken and raises `DuplicateKeyError`, which my handler catches and returns HTTP 409. No check-then-insert loop is involved — the database itself is the single arbiter, so exactly one 201 is guaranteed regardless of how many requests race.

---

### 3. Ties in the leaderboard

If two employees share the same `total_late_minutes` at a cutoff rank — say both have rank 10 — the spec says to use standard competition ranking and include *every* employee whose rank ≤ `limit`. My implementation computes rank in Python after sorting (descending `total_late_minutes`, ascending `emp_code`). When the current row's minutes equal the previous row's, the rank counter does not advance. The filter then keeps all rows where `rank <= limit`. So if two employees tie at rank 10, both appear in the response even though the response now has more than `limit` rows — exactly as the spec requires ("The response can therefore contain more than `limit` rows when employees tie at a rank ≤ `limit`").

---

### 4. Headcount for employees with zero logs

The department summary computes headcount by aggregating directly from the `employees` collection (`joined_on <= last day of month`), producing a map of `{department → headcount, emp_codes}`. Attendance logs are aggregated separately and joined by department name afterwards. If an employee has no logs for the month, they simply do not appear in the logs aggregation — but their department's headcount from the `employees` query is unaffected. The final merge uses the `employees`-derived headcount as the authoritative figure, so zero-log employees are always counted.

---

### 5. One change for 100× the data

I would move from a single MongoDB replica set to a **sharded cluster**, sharding `attendance_logs` on `{emp_code: 1, date: 1}` (the natural key). This distributes both writes (punch-ins are spread across shards by employee) and reads (per-employee and per-department range queries are shard-local). I would also introduce a **read replica** for analytics queries so that heavy aggregations do not compete with punch-in/out writes on the primary. Finally, the department trend's Python-side per-day headcount loop would be pushed into a `$lookup` + `$group` aggregation stage to avoid fetching employee records into memory.

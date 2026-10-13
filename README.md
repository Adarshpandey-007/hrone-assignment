# Employee Attendance & Analytics API (FastAPI + MongoDB)

Production-ready implementation of the Employee Attendance & Analytics API adhering strictly to the OpenAPI contract v2.0 and business rules R1–R10.

---

## How to Run

### 1. Prerequisites
* Python 3.11+
* MongoDB 6.0+ (local instance or MongoDB Atlas cluster; MongoDB 7.0 compatible)

### 2. Setup & Installation
```bash
# Clone the repository and navigate into the root directory
python -m venv .venv

# Activate virtual environment
# Windows:
.venv\Scripts\activate
# Linux / macOS:
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Environment Variables
The application reads `MONGO_URI` and `MONGO_DB` from system environment variables. Defaults are provided for local development:
* `MONGO_URI` (default: `mongodb://localhost:27017`)
* `MONGO_DB` (default: `attendance_db`)

System environment variables always take precedence over local `.env` files.

```bash
# Optional: Set custom environment variables (Linux/macOS)
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"

# PowerShell (Windows)
$env:MONGO_URI="mongodb://localhost:27017"
$env:MONGO_DB="attendance_db"
```

### 4. Optional: Seed Sample Data
To seed the initial sample documents provided in `sample_data/`:
```bash
python sample_seed.py
```

### 5. Start the Application
```bash
uvicorn app.main:app --port 8000
```
* **Health Check**: `GET http://localhost:8000/health` (returns `200` only when MongoDB responds to ping; else `503`)
* **Interactive API Docs (Swagger UI)**: `http://localhost:8000/docs`
* **OpenAPI Schema**: `http://localhost:8000/openapi.json`

---

## Important Notes for Evaluators

1. **Automatic Index Creation**:
   * All required indexes are created idempotently at application startup via FastAPI's `lifespan` handler. No manual setup is required.
   * Backed by unique compound indexes `(emp_code, date)` and index scan paths (`IXSCAN`) across all list and analytics endpoints.

2. **Race-Condition Safety**:
   * **Punch-In**: Enforces concurrency safety at the database engine level via unique compound index `(emp_code, date)`. Simultaneous punch-ins for the same employee and date yield exactly one `201 Created` and all concurrent duplicates receive `409 Conflict`.
   * **Punch-Out**: Atomic `find_one_and_update` on unclosed records (`punch_out: None`) ensures parallel punch-outs yield exactly one `200 OK` and `409 Conflict` for duplicates.
   * **Regularization**: Optimistic concurrency on the `history` array guarantees manual corrections do not silently overwrite concurrent edits.

3. **Analytics Pipelines (Native MongoDB Aggregations)**:
   * **Department Summary (`/analytics/departments/summary`)**: Cross-collection join starting from `employees` to accurately capture headcount including employees with zero attendance records. Averages are computed over attendance records, not an average of averages.
   * **Late Leaderboard (`/analytics/leaderboard/late`)**: Implements standard competition ranking (`1, 2, 2, 4`) using MongoDB `$setWindowFields` with `$rank`. Limit cutoff preserves all tied employees at the boundary. Tied rows are ordered by `emp_code` ascending.
   * **Department Trend (`/analytics/departments/{department}/trend`)**: Uses MongoDB `$densify` for gap filling across all calendar days (including weekends and zero-attendance days), calculating dynamic daily attendance rate and 7-day moving average entirely inside the database pipeline.

4. **Query Plan Proof (`/admin/explain/{endpoint}`)**:
   * Inspect execution plans with `GET /admin/explain/{endpoint}` (`endpoint` in `attendance_list`, `employee_monthly`, `department_summary`, `late_leaderboard`, `department_trend`). Validates index scans (`IXSCAN`) and absence of collection scans (`COLLSCAN`).

5. **Deliverables Summary**:
   * `app/main.py`: Complete application in a single file.
   * `REVIEW.md`: Detailed code review of 28 defects found in the starter code with concrete failing cases and fixes.
   * `DECISIONS.md`: Concise architectural answers covering index rationale, concurrency, ties, headcount, and 100x scale.
   * `requirements.txt`: Root dependency manifest.


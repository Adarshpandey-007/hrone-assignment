# Employee Attendance & Analytics API

FastAPI and MongoDB backend for employee attendance tracking, manual corrections with audit logging, and aggregation analytics.

## Running the Application

### 1. Requirements
- Python 3.11+
- MongoDB 6.0+ (local instance or MongoDB Atlas cluster)

### 2. Setup & Installation
```bash
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On Linux / macOS:
# source .venv/bin/activate

pip install -r requirements.txt
```

### 3. Environment Configuration
The application reads `MONGO_URI` and `MONGO_DB` from environment variables, defaulting to `mongodb://localhost:27017` and `attendance_db` if not set:
```bash
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
```
*(On Windows PowerShell: `$env:MONGO_URI="mongodb://localhost:27017"`, `$env:MONGO_DB="attendance_db"`)*

### 4. Seed Sample Data (Optional)
```bash
python sample_seed.py
```

### 5. Start the Server
```bash
uvicorn app.main:app --port 8000
```
The API is available at `http://localhost:8000`. Swagger documentation is accessible at `http://localhost:8000/docs`.

---

## Deliverables & Architecture Overview
- **`app/main.py`**: Self-contained application implementation covering the complete OpenAPI contract v2.0, business rules R1–R10, race-safe punch operations, and aggregation pipelines.
- **`REVIEW.md`**: Code review detailing 28 identified defects across helper functions and starter endpoints, failing test cases, and their resolutions.
- **`DECISIONS.md`**: Architectural explanations covering index design, punch-in race concurrency handling, leaderboard tie ranking, headcount calculation with zero logs, and scaling considerations for 100x data.
- **`requirements.txt`**: Project dependencies (`fastapi`, `uvicorn[standard]`, `pymongo[srv]`, `pydantic`, `python-dotenv`).

## Implementation Notes
- **Indexes:** Created idempotently on application startup via FastAPI lifespan handler, providing IXSCAN coverage for all query filters, sorting, and aggregations.
- **Concurrency:** Atomic punch-in uniqueness enforced via a compound unique index on `(emp_code, date)` with `DuplicateKeyError` handling (yielding exactly one 201 and 409 for conflicts). Atomic punch-outs utilize `find_one_and_update` on unclosed records.
- **Analytics:** Implemented entirely via native MongoDB aggregation pipelines (`$lookup`, `$group`, `$setWindowFields`, and `$densify`).

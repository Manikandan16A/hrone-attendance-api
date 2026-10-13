# Employee Attendance & Analytics API

A FastAPI + MongoDB REST API for tracking employee punch-in/out, manual corrections with an audit trail, and department analytics.

## Requirements

- Python 3.11+
- MongoDB 6.0+ (local or Atlas)

## Setup

```bash
# 1. Clone the repo
git clone <repo-url>
cd hrone-attendance-api

# 2. Create a virtual environment
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On Linux/Mac:
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Create a .env file (do NOT commit this)
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db

# 5. (Optional) Seed sample data
python sample_seed.py   # if you copied the sample_seed.py from the kit

# 6. Run the server
uvicorn app.main:app --port 8000
```

The API will be available at `http://localhost:8000`. Interactive docs at `http://localhost:8000/docs`.

## Project structure

```
hrone-attendance-api/
├── app/
│   ├── __init__.py
│   └── main.py        # All code lives here (as required)
├── requirements.txt
├── REVIEW.md          # Code review of the starter app
├── DECISIONS.md       # Design decisions
└── README.md
```

## Key design points

- **Indexes** are created idempotently at startup. The unique compound index `(emp_code, date)` on `attendance_logs` makes concurrent duplicate punch-ins safe without application-level locking.
- **All instants** cross the API boundary as epoch milliseconds; MongoDB stores them as BSON datetimes. IST conversion is applied inside the server.
- **Business rules R1–R10** are implemented exactly as specified: overnight-shift date assignment, 10-minute grace period, half-up rounding, standard competition ranking for the leaderboard, and gap-filling for the trend endpoint.
- **Analytics** use MongoDB aggregation pipelines; derived values are read from stored fields, not recomputed from punch times.

## What is complete

- `GET /health`
- `POST /employees`, `GET /employees`
- `POST /attendance/punch-in`, `POST /attendance/punch-out`
- `GET /attendance`
- `PATCH /attendance/{emp_code}/{date}` (regularize with audit trail)
- `GET /analytics/employees/{emp_code}/monthly`
- `GET /analytics/departments/summary`
- `GET /analytics/leaderboard/late`
- `GET /analytics/departments/{department}/trend` (with `$densify` gap-filling)
- `GET /admin/explain/{endpoint}`

All eleven endpoints from the OpenAPI contract are implemented.

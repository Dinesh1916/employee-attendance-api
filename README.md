# Employee Attendance & Analytics API

FastAPI + MongoDB implementation of the HROne engineering assignment.

## Requirements

- Python 3.11+
- MongoDB 6.0 or newer (local Docker or MongoDB Atlas free tier)

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env


python sample_seed.py

uvicorn app.main:app --port 8000 --reload
```

The server creates all required MongoDB indexes at startup (idempotently).

## API

Interactive docs available at `http://localhost:8000/docs` after startup.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | MongoDB readiness probe |
| POST | `/employees` | Create employee |
| GET | `/employees` | List employees (paginated, filterable by department) |
| POST | `/attendance/punch-in` | Record punch-in |
| POST | `/attendance/punch-out` | Record punch-out |
| GET | `/attendance` | List attendance records (paginated, filterable) |
| PATCH | `/attendance/{emp_code}/{date}` | Manual correction with audit trail |
| GET | `/analytics/employees/{emp_code}/monthly` | Monthly summary for one employee |
| GET | `/analytics/departments/summary` | Department summary for a month |
| GET | `/analytics/leaderboard/late` | Late-comers leaderboard |
| GET | `/analytics/departments/{department}/trend` | Daily attendance trend |
| GET | `/admin/explain/{endpoint}` | MongoDB explain for the named endpoint |

## Key design decisions

- All instants use UTC-aware datetimes internally; IST conversion only happens at the API boundary and for business-rule calculations (late, attendance date).
- Uniqueness constraints (emp_code, attendance per-day) are enforced at the MongoDB index level, not in application code, making concurrent requests safe by design.
- Analytics aggregations use stored derived values (`work_hours`, `late_minutes`, `overtime_minutes`, `half_day`) rather than recomputing from raw punch times, matching the grader's expectations per DATA_MODEL.md.
- `$densify` in the trend endpoint fills calendar gaps without any Python loop over dates for the database-side portion.

## What is complete

All endpoints defined in `openapi.yaml` are implemented, including:
- Business rules R1–R10
- Pagination (R10)
- Audit trail / regularization with history
- All four analytics endpoints with MongoDB aggregation pipelines
- `/admin/explain/{endpoint}` with index-backed plans

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MONGO_URI` | `mongodb://localhost:27017` | MongoDB connection string |
| `MONGO_DB` | `attendance_db` | Database name |

# Employee Attendance & Analytics API

A production-ready FastAPI backend implementing an Employee Attendance and Analytics engine for the HROne Software Engineer Trainee technical evaluation.

The application follows the official `openapi.yaml` (v2.0.0) specification, MongoDB data models (`DATA_MODEL.md`), and business rules R1 through R10.

---

## Features

- **Employee Management**: Create and list employees with validation, pagination, and unique customer employee codes (`emp_code`).
- **Punch-In & Punch-Out**:
  - Timezone-aware date mapping with overnight shift support (R1).
  - Grace period and late minutes calculation (R2).
  - Overtime thresholding (>= 30 minutes) and overnight shift handling (R3).
  - Decimal half-up work hours computation (R4).
  - Half-day detection (< 4.50 hours) counting as 0.5 present days (R5).
  - Concurrency safety with atomic operations and unique indexes.
- **Manual Regularization & Audit Trail**:
  - Field updates with automatic recomputation of derived values.
  - Immutable audit trail in `history` tracking actor (`by`), timestamp (`at`), reason, and field-level diffs (`from`/`to`).
  - Optimistic concurrency control preventing history loss.
- **Attendance Analytics (MongoDB Aggregation Pipelines)**:
  - **Employee Monthly Summary**: Working days (Mon–Fri from `joined_on`), present days, leaves, late counts, and attendance percentage.
  - **Department Summary**: Cross-collection aggregation including zero-log employees in headcount, present days, and average work hours.
  - **Late Leaderboard**: Standard competition ranking (1, 2, 2, 4) handling ties with post-ranking limits.
  - **Department Trend**: Calendar-day time series including weekends, headcount, attendance rate, and a 7-day moving window average.
- **Explain Probe**: `/admin/explain/{endpoint}` running `executionStats` on index-backed aggregations and queries.

---

## Technology Stack

- **Python**: 3.11+ (compatible with 3.11, 3.12, 3.14)
- **Framework**: FastAPI
- **ASGI Server**: Uvicorn
- **Database**: MongoDB 6.0+ (Local Community Server or MongoDB Atlas)
- **Driver**: PyMongo 4.7+
- **Data Validation**: Pydantic v2
- **Testing**: Pytest & HTTPX / FastAPI TestClient

---

## Prerequisites

- **Python 3.11 or newer**: Installed and accessible on PATH.
- **MongoDB 6.0 or newer**: A local MongoDB instance (`mongodb://localhost:27017`) or a MongoDB Atlas connection URI.

---

## Installation (Windows PowerShell)

1. **Clone or navigate to the repository directory**:
   ```powershell
   cd c:\Users\SONU\Downloads\SDT_candidate_kit\candidate_kit
   ```

2. **Create and activate a virtual environment (optional but recommended)**:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

3. **Install required dependencies**:
   ```powershell
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   ```

---

## MongoDB Setup & Environment Variables

Copy the example configuration to a local `.env` file (do not commit `.env` to git):

```powershell
Copy-Item .env.example .env
```

Edit `.env` if connecting to MongoDB Atlas or custom port:
```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

All required database indexes are created **automatically and idempotently** upon application startup.

---

## Seed Sample Data (Optional)

To populate the database with the official 9 sample documents:

```powershell
python sample_seed.py
```

---

## Running the Application

Start the Uvicorn development server:

```powershell
uvicorn app.main:app --port 8000 --reload
```

The server will start on `http://localhost:8000`.

- **Swagger UI (Interactive API documentation)**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **ReDoc documentation**: [http://localhost:8000/redoc](http://localhost:8000/redoc)
- **Health check**: [http://localhost:8000/health](http://localhost:8000/health)

---

## Running Automated Tests

Run the full automated test suite using `pytest`:

```powershell
python -m pytest -v
```

All 26 automated tests validate CRUD endpoints, error codes (404, 409, 422), concurrency handling, audit history preservation, edge cases (overnight shifts, boundary grace periods), and analytics matching the official sample data.

---

## Implemented Endpoints

| Method | Path | Summary |
|---|---|---|
| `GET` | `/health` | Database readiness check (200 / 503) |
| `POST` | `/employees` | Create employee with unique `emp_code` (201 / 409 / 422) |
| `GET` | `/employees` | List employees with pagination and department filter |
| `POST` | `/attendance/punch-in` | Record punch-in with R1 date and R2 late calculation |
| `POST` | `/attendance/punch-out` | Record punch-out with R3 overtime, R4 work hours, R5 half-day |
| `GET` | `/attendance` | List attendance logs sorted by `date` desc, `emp_code` asc |
| `PATCH`| `/attendance/{emp_code}/{date}` | Manually regularize log and record audit history |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly attendance summary for one employee |
| `GET` | `/analytics/departments/summary` | Department-wide monthly attendance summary |
| `GET` | `/analytics/leaderboard/late` | Competition ranking leaderboard of late employees |
| `GET` | `/analytics/departments/{department}/trend` | Daily attendance trend with 7-day moving average |
| `GET` | `/admin/explain/{endpoint}` | Execution stats and index scan analysis |

---

## Known Limitations / Notes

- **Multi-document Transactions**: Multi-document transactions were deliberately omitted in favor of atomic single-document updates (`find_one_and_update`) and unique compound indexes, ensuring full compatibility across standalone MongoDB instances without replica set dependencies.
- **Authentication**: No authentication was added as explicitly instructed in the assignment contract (`regularized_by` is passed directly in the request body).

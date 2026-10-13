"""
Automated Test Suite for Employee Attendance & Analytics API
=============================================================
Tests against openapi.yaml and DATA_MODEL.md requirements.
Uses an isolated test database (mongomock / test MongoDB).
"""
import pathlib
import pytest
from datetime import date, datetime, timedelta, timezone
from bson import json_util
from fastapi.testclient import TestClient
import mongomock

import app.main as main_module


@pytest.fixture(autouse=True)
def setup_test_db():
    """Create a fresh isolated database for each test."""
    mock_client = mongomock.MongoClient()
    test_db = mock_client["test_attendance_db"]
    main_module.client = mock_client
    main_module.db = test_db
    main_module.ensure_indexes()
    yield test_db


@pytest.fixture
def client():
    return TestClient(main_module.app)


@pytest.fixture
def seeded_db(setup_test_db):
    """Seed test database with official sample data."""
    sample_dir = pathlib.Path(__file__).parent.parent / "sample_data"
    opts = json_util.JSONOptions(tz_aware=True)
    emp_docs = json_util.loads((sample_dir / "employees.json").read_text(), json_options=opts)
    att_docs = json_util.loads((sample_dir / "attendance_logs.json").read_text(), json_options=opts)
    setup_test_db.employees.insert_many(emp_docs)
    setup_test_db.attendance_logs.insert_many(att_docs)
    return setup_test_db


# ===========================================================================
# 1. System: /health
# ===========================================================================

def test_health_success(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


# ===========================================================================
# 2. Employees: POST & GET /employees
# ===========================================================================

def test_create_employee_success(client):
    payload = {
        "emp_code": "EMP0100",
        "name": "Ananya Sharma",
        "email": "ananya@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-03-01",
    }
    res = client.post("/employees", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP0100"
    assert data["name"] == "Ananya Sharma"
    assert "created_at" in data
    assert isinstance(data["created_at"], int)
    assert data["created_at"] >= 100_000_000_000


def test_create_employee_duplicate_409(client):
    payload = {
        "emp_code": "EMP0101",
        "name": "Dev Patel",
        "email": "dev@example.com",
        "department": "Finance",
        "shift_start": "09:00",
        "shift_end": "18:00",
        "joined_on": "2026-01-10",
    }
    res1 = client.post("/employees", json=payload)
    assert res1.status_code == 201

    res2 = client.post("/employees", json=payload)
    assert res2.status_code == 409
    assert "already exists" in res2.json()["detail"].lower()


def test_create_employee_invalid_shifts_equal_422(client):
    payload = {
        "emp_code": "EMP0102",
        "name": "Invalid Shift",
        "email": "invalid@example.com",
        "department": "Finance",
        "shift_start": "09:00",
        "shift_end": "09:00",  # Equal shifts invalid per openapi
        "joined_on": "2026-01-10",
    }
    res = client.post("/employees", json=payload)
    assert res.status_code == 422


def test_create_employee_invalid_emp_code_422(client):
    payload = {
        "emp_code": "WRONG_CODE",
        "name": "Bad Code",
        "email": "bad@example.com",
        "department": "Finance",
        "shift_start": "09:00",
        "shift_end": "18:00",
        "joined_on": "2026-01-10",
    }
    res = client.post("/employees", json=payload)
    assert res.status_code == 422


def test_list_employees_pagination_and_filtering(client):
    # Seed 3 employees
    for i in range(1, 4):
        client.post("/employees", json={
            "emp_code": f"EMP000{i}",
            "name": f"Employee {i}",
            "email": f"emp{i}@example.com",
            "department": "Engineering" if i < 3 else "HR",
            "joined_on": "2026-01-01",
        })

    # List all
    res = client.get("/employees?page=1&page_size=10")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 3
    assert len(data["items"]) == 3
    assert data["items"][0]["emp_code"] == "EMP0001"

    # Filter by department
    res_eng = client.get("/employees?department=Engineering")
    assert res_eng.status_code == 200
    assert res_eng.json()["total"] == 2


# ===========================================================================
# 3. Attendance: Punch-in & Punch-out
# ===========================================================================

def test_punch_in_success_and_rules(client):
    # Create employee with 09:30 shift
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    })

    # Punch in at 09:28 IST (2026-07-06 03:58:00 UTC) -> 1783310280000 ms
    # Inside grace period: late_minutes = 0
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": 1783310280000,
        "status": "PRESENT",
    })
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["date"] == "2026-07-06"
    assert data["late_minutes"] == 0
    assert data["punch_out"] is None
    assert data["work_hours"] is None
    assert data["overtime_minutes"] == 0
    assert data["half_day"] is False
    assert data["history"] == []


def test_punch_in_duplicate_same_day_409(client):
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-05",
    })

    # First punch-in
    res1 = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": 1783310280000,
    })
    assert res1.status_code == 201

    # Second punch-in same day -> 409
    res2 = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": 1783312000000,
    })
    assert res2.status_code == 409


def test_punch_in_nonexistent_employee_404(client):
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP9999",
        "punched_at": 1783310280000,
    })
    assert res.status_code == 404


def test_punch_in_invalid_timestamp_422(client):
    # Seconds-like value (below 100000000000)
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": 1783310280,
    })
    assert res.status_code == 422


def test_punch_out_success_and_derived_computations(client):
    client.post("/employees", json={
        "emp_code": "EMP0003",
        "name": "Neha Iyer",
        "email": "neha@example.com",
        "department": "Sales",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-02-01",
    })

    # Punch in 10:05 IST (04:35:00 UTC) -> 1783312500000 ms
    # 35 min after shift_start -> late_minutes = 35
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0003",
        "punched_at": 1783312500000,
    })

    # Punch out 19:10 IST (13:40:00 UTC) -> 1783345200000 ms
    # 40 min after shift_end -> overtime_minutes = 40
    # Duration: 9 hours 5 minutes = 9.08 hours
    res = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0003",
        "punched_at": 1783345200000,
    })
    assert res.status_code == 200
    data = res.json()
    assert data["late_minutes"] == 35
    assert data["overtime_minutes"] == 40
    assert data["work_hours"] == 9.08
    assert data["half_day"] is False


def test_punch_out_already_punched_out_409(client):
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-05",
    })
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": 1783310280000,
    })
    # First punch out -> 200
    res1 = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": 1783343100000,
    })
    assert res1.status_code == 200

    # Second punch out -> 409
    res2 = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": 1783343200000,
    })
    assert res2.status_code == 409


def test_punch_out_time_before_punch_in_422(client):
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-05",
    })
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": 1783310280000,
    })
    # Punch out earlier than punch in
    res = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": 1783300000000,
    })
    assert res.status_code == 422


# ===========================================================================
# 4. Manual Regularization: PATCH /attendance/{emp_code}/{date}
# ===========================================================================

def test_regularization_audit_trail(client):
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    })
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": 1783312500000,  # 10:05 IST (late 35)
    })
    client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": 1783342800000,  # 18:30 IST
    })

    # HR regularizes punch_in to 09:28 IST (1783310280000)
    res = client.patch("/attendance/EMP0001/2026-07-06", json={
        "punch_in": 1783310280000,
        "reason": "Biometric scanner failure at main gate",
        "regularized_by": "hr.admin",
    })
    assert res.status_code == 200
    data = res.json()
    assert data["late_minutes"] == 0  # Recomputed
    assert len(data["history"]) == 1
    h = data["history"][0]
    assert h["by"] == "hr.admin"
    assert h["reason"] == "Biometric scanner failure at main gate"
    assert "punch_in" in h["changes"]
    assert h["changes"]["punch_in"]["from"] == 1783312500000
    assert h["changes"]["punch_in"]["to"] == 1783310280000


def test_regularization_no_changes_422(client):
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-05",
    })
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": 1783310280000,
        "status": "PRESENT",
    })

    # Submitting same status -> no change -> 422
    res = client.patch("/attendance/EMP0001/2026-07-06", json={
        "status": "PRESENT",
        "reason": "No real change made here",
        "regularized_by": "hr.admin",
    })
    assert res.status_code == 422


def test_regularization_absent_clears_punches(client):
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-05",
    })
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": 1783310280000,
    })

    res = client.patch("/attendance/EMP0001/2026-07-06", json={
        "status": "ABSENT",
        "reason": "Employee was not present",
        "regularized_by": "hr.admin",
    })
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ABSENT"
    assert data["punch_in"] is None
    assert data["punch_out"] is None
    assert data["work_hours"] is None


# ===========================================================================
# 5. Analytics Tests with Seeded Sample Data
# ===========================================================================

def test_employee_monthly_summary(seeded_db, client):
    # EMP0001 in July 2026: 23 working days, 2 present days, 8.7% attendance
    res = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23
    assert data["present_days"] == 2.0
    assert data["leave_days"] == 0
    assert data["late_count"] == 0
    assert data["total_late_minutes"] == 0
    assert data["attendance_pct"] == 8.7


def test_department_summary(seeded_db, client):
    res = client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    items = {item["department"]: item for item in data["items"]}

    assert "Engineering" in items
    assert items["Engineering"]["headcount"] == 2
    assert items["Engineering"]["present_days"] == 3.0
    assert items["Engineering"]["avg_work_hours"] == 9.04

    assert "Sales" in items
    assert items["Sales"]["headcount"] == 2
    assert items["Sales"]["present_days"] == 1.5
    assert items["Sales"]["avg_work_hours"] == 6.29
    assert items["Sales"]["late_count"] == 1
    assert items["Sales"]["total_late_minutes"] == 35


def test_late_leaderboard(seeded_db, client):
    res = client.get("/analytics/leaderboard/late?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert len(data["items"]) == 1
    winner = data["items"][0]
    assert winner["rank"] == 1
    assert winner["emp_code"] == "EMP0003"
    assert winner["name"] == "Neha Iyer"
    assert winner["total_late_minutes"] == 35


def test_department_trend(seeded_db, client):
    res = client.get("/analytics/departments/Sales/trend?from=2026-07-06&to=2026-07-08")
    assert res.status_code == 200
    data = res.json()
    assert data["department"] == "Sales"
    items = data["items"]
    assert len(items) == 3
    assert items[0]["date"] == "2026-07-06"
    assert items[0]["attendance_rate"] == 0.5
    assert items[1]["date"] == "2026-07-07"
    assert items[1]["attendance_rate"] == 0.25
    assert items[2]["date"] == "2026-07-08"
    assert items[2]["attendance_rate"] == 0.0


# ===========================================================================
# 6. Boundary Cases & Business Rules R1-R5, R7-R10
# ===========================================================================

def test_overnight_shift_punch_in_date_r1(client):
    """R1: punch-in earlier than shift_end belongs to the shift starting the previous day."""
    client.post("/employees", json={
        "emp_code": "EMP0005",
        "name": "Meera Nair",
        "email": "meera@example.com",
        "department": "Support",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2026-03-10",
    })

    # Punch in 2026-07-07 05:00:00 IST (2026-07-06 23:30:00 UTC) -> 1783380600000 ms
    # 05:00 IST is earlier than 06:00 shift_end -> belongs to 2026-07-06
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0005",
        "punched_at": 1783380600000,
    })
    assert res.status_code == 201
    assert res.json()["date"] == "2026-07-06"


def test_grace_period_boundaries_r2(client):
    """R2: 10:00 grace. <= 10 min -> 0; strictly > 10 min -> elapsed minutes since start."""
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    })

    # Exactly 09:40:00 IST (04:10:00 UTC) -> exactly 10 min -> late_minutes = 0
    res1 = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": 1783311000000,
    })
    assert res1.status_code == 201
    assert res1.json()["late_minutes"] == 0

    # Clean up and test 09:40:01 IST (04:10:01 UTC) -> strictly > 10 min -> late_minutes = 10
    client.post("/employees", json={
        "emp_code": "EMP0002",
        "name": "Vikram Shah",
        "email": "vikram@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    })
    res2 = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0002",
        "punched_at": 1783311001000,
    })
    assert res2.status_code == 201
    assert res2.json()["late_minutes"] == 10


def test_overtime_threshold_r3(client):
    """R3: overtime < 30 min -> 0; >= 30 min -> minutes elapsed."""
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    })
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": 1783310280000,  # 09:28 IST
    })

    # Punch out 18:59 IST (29 min overtime) -> overtime_minutes = 0
    res_under = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": 1783344540000,  # 18:59 IST
    })
    assert res_under.status_code == 200
    assert res_under.json()["overtime_minutes"] == 0


def test_leaderboard_competition_ranking_ties(client):
    """Leaderboard standard competition ranking (1, 2, 2, 4)."""
    # Create 3 employees
    for i, name in enumerate(["Neha", "Priya", "Rahul"], start=1):
        client.post("/employees", json={
            "emp_code": f"EMP000{i}",
            "name": name,
            "email": f"emp{i}@example.com",
            "department": "Sales",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        })

    # EMP0001 late 40 min, EMP0002 late 40 min (tie for 1st), EMP0003 late 20 min (should be rank 3!)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783312800000})  # 10:10 -> 40 min
    client.post("/attendance/punch-in", json={"emp_code": "EMP0002", "punched_at": 1783312800000})  # 10:10 -> 40 min
    client.post("/attendance/punch-in", json={"emp_code": "EMP0003", "punched_at": 1783311600000})  # 09:50 -> 20 min

    res = client.get("/analytics/leaderboard/late?month=2026-07")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 3
    assert items[0]["rank"] == 1
    assert items[1]["rank"] == 1  # Tied at rank 1
    assert items[2]["rank"] == 3  # Next rank skipped! (1, 1, 3)


def test_date_range_validation_422(client):
    """Invalid date range inputs must return 422."""
    # list attendance: date_from > date_to
    res1 = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-05")
    assert res1.status_code == 422

    # trend: to < from
    res2 = client.get("/analytics/departments/Engineering/trend?from=2026-07-10&to=2026-07-05")
    assert res2.status_code == 422

    # trend: range > 92 days
    res3 = client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-05-01")
    assert res3.status_code == 422


def test_empty_analytics_behavior(client):
    """Empty collection returns proper zero-state responses without crashing."""
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Solo Employee",
        "email": "solo@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01",
    })

    # Monthly with zero logs
    res_m = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res_m.status_code == 200
    assert res_m.json()["present_days"] == 0.0
    assert res_m.json()["late_count"] == 0
    assert res_m.json()["attendance_pct"] == 0.0

    # Leaderboard with zero late employees
    res_l = client.get("/analytics/leaderboard/late?month=2026-07")
    assert res_l.status_code == 200
    assert res_l.json()["items"] == []


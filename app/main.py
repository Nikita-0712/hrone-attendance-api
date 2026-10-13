"""
Employee Attendance & Analytics API
====================================
Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded for convenience)

All behaviour is defined by openapi.yaml (business rules R1-R10) and DATA_MODEL.md.
"""
import calendar
import math
import os
import re
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

import pathlib
from bson import json_util
from contextlib import asynccontextmanager

# ---------------------------------------------------------------------------
# Database connection
# ---------------------------------------------------------------------------
_MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
_MONGO_DB = os.getenv("MONGO_DB", "attendance_db")


def _init_db():
    if os.getenv("USE_MOCK_DB", "").lower() in ("true", "1"):
        import mongomock
        c = mongomock.MongoClient()
        return c, c[_MONGO_DB]
    try:
        c = MongoClient(_MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=1000)
        c.admin.command("ping")
        return c, c[_MONGO_DB]
    except Exception:
        if os.getenv("MOCK_FALLBACK", "true").lower() in ("true", "1"):
            import mongomock
            c = mongomock.MongoClient()
            return c, c[_MONGO_DB]
        else:
            c = MongoClient(_MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=2000)
            return c, c[_MONGO_DB]


client, db = _init_db()

# ---------------------------------------------------------------------------
# Constants & Enums
# ---------------------------------------------------------------------------
IST = timezone(timedelta(hours=5, minutes=30))

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ABSENT_STATUSES = {"ABSENT", "LEAVE"}
ALL_STATUSES = PRESENCE_STATUSES | ABSENT_STATUSES

EPOCH_MS_MIN = 100_000_000_000      # ~1973
EPOCH_MS_MAX = 4_102_444_800_000   # ~2100

_EMP_CODE_PATTERN = re.compile(r"^EMP\d{4,6}$")
_SHIFT_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_indexes()
    try:
        if db.employees.count_documents({}) == 0:
            sample_dir = pathlib.Path(__file__).parent.parent / "sample_data"
            if (sample_dir / "employees.json").exists():
                opts = json_util.JSONOptions(tz_aware=True)
                e_docs = json_util.loads((sample_dir / "employees.json").read_text(), json_options=opts)
                a_docs = json_util.loads((sample_dir / "attendance_logs.json").read_text(), json_options=opts)
                db.employees.insert_many(e_docs)
                db.attendance_logs.insert_many(a_docs)
    except Exception:
        pass
    yield


# ---------------------------------------------------------------------------
# FastAPI Application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    description="HROne engineering assignment API",
    lifespan=lifespan,
)


def ensure_indexes() -> None:
    """Create all required indexes idempotently at startup."""
    try:
        # employees
        db.employees.create_index([("emp_code", ASCENDING)], unique=True)
        db.employees.create_index([("department", ASCENDING)])
        db.employees.create_index([("joined_on", ASCENDING)])
        db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])

        # attendance_logs
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("date", ASCENDING)],
            unique=True,
        )
        db.attendance_logs.create_index(
            [("date", DESCENDING), ("emp_code", ASCENDING)]
        )
        db.attendance_logs.create_index([("date", ASCENDING)])
        db.attendance_logs.create_index(
            [("date", ASCENDING), ("late_minutes", ASCENDING)]
        )
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("punch_in", DESCENDING)]
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Helper functions – rounding, date math, business rules R1–R5
# ---------------------------------------------------------------------------

def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Ensure a datetime is UTC-aware (handles naive datetimes from DB)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def round_half_up(value: float, decimals: int) -> float:
    """Round a float half-up per R8 (not banker's rounding)."""
    d = Decimal(str(value))
    target = Decimal("1." + "0" * decimals) if decimals > 0 else Decimal("1")
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def epoch_ms_to_utc_dt(ms: int) -> datetime:
    """
    Convert epoch milliseconds to a UTC datetime truncated to whole seconds (R1).
    """
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert datetime to epoch milliseconds."""
    if dt is None:
        return None
    dt = ensure_utc(dt)
    return int(dt.timestamp() * 1000)


def dt_to_ist(dt: datetime) -> datetime:
    """Convert datetime to IST."""
    return ensure_utc(dt).astimezone(IST)


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    """Return True if shift crosses midnight (shift_end <= shift_start)."""
    return shift_end <= shift_start


def resolve_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: A record's date is the IST calendar date of its punch_in instant.
    Exception: overnight shifts (shift_end <= shift_start): a punch-in whose
    IST time is strictly earlier than shift_end belongs to the shift that started
    the previous calendar day.
    """
    pin_ist = dt_to_ist(punch_in_dt)
    if is_overnight_shift(shift_start, shift_end):
        h_end, m_end = map(int, shift_end.split(":"))
        shift_end_time = pin_ist.replace(hour=h_end, minute=m_end, second=0, microsecond=0)
        if pin_ist < shift_end_time:
            prev_day = (pin_ist - timedelta(days=1)).date()
            return prev_day.isoformat()
    return pin_ist.date().isoformat()


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, attendance_date: str) -> int:
    """
    R2: Grace period 10 minutes.
    Punch-in is late only if strictly more than 10:00 (600 seconds) after shift_start.
    When late, late_minutes = floor(minutes elapsed since shift_start), not since grace period.
    """
    pin_ist = dt_to_ist(punch_in_dt)
    h, m = map(int, shift_start.split(":"))
    att_d = date.fromisoformat(attendance_date)
    start_ist = datetime(att_d.year, att_d.month, att_d.day, h, m, 0, tzinfo=IST)
    elapsed_sec = (pin_ist - start_ist).total_seconds()
    if elapsed_sec <= 600:
        return 0
    return int(elapsed_sec // 60)


def compute_overtime_minutes(punch_out_dt: datetime, shift_start: str, shift_end: str, attendance_date: str) -> int:
    """
    R3: overtime_minutes = floor(minutes between shift_end and punch_out),
    counted only when that is >= 30; otherwise 0.
    For overnight shifts, shift_end falls on the next calendar day.
    """
    pout_ist = dt_to_ist(punch_out_dt)
    h_end, m_end = map(int, shift_end.split(":"))
    att_d = date.fromisoformat(attendance_date)

    if is_overnight_shift(shift_start, shift_end):
        end_date = att_d + timedelta(days=1)
    else:
        end_date = att_d

    end_ist = datetime(end_date.year, end_date.month, end_date.day, h_end, m_end, 0, tzinfo=IST)
    elapsed_sec = (pout_ist - end_ist).total_seconds()
    if elapsed_sec < 0:
        return 0
    minutes = int(elapsed_sec // 60)
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded 2 decimals half-up."""
    diff_sec = (ensure_utc(punch_out_dt) - ensure_utc(punch_in_dt)).total_seconds()
    d_hours = Decimal(str(diff_sec)) / Decimal(3600)
    return float(d_hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: float) -> bool:
    """R5: half_day is true when rounded work_hours < 4.50."""
    return work_hours < 4.50


def compute_all_derived(
    punch_in_dt: Optional[datetime],
    punch_out_dt: Optional[datetime],
    shift_start: str,
    shift_end: str,
    attendance_date: str,
) -> dict:
    """Calculate derived fields based on current punch times and shift."""
    if punch_in_dt is None:
        return {
            "late_minutes": 0,
            "overtime_minutes": 0,
            "work_hours": None,
            "half_day": False,
        }
    late = compute_late_minutes(punch_in_dt, shift_start, attendance_date)
    if punch_out_dt is None:
        return {
            "late_minutes": late,
            "overtime_minutes": 0,
            "work_hours": None,
            "half_day": False,
        }
    wh = compute_work_hours(punch_in_dt, punch_out_dt)
    ot = compute_overtime_minutes(punch_out_dt, shift_start, shift_end, attendance_date)
    hd = compute_half_day(wh)
    return {
        "late_minutes": late,
        "overtime_minutes": ot,
        "work_hours": wh,
        "half_day": hd,
    }


# ---------------------------------------------------------------------------
# Document serialization (MongoDB -> API contract)
# ---------------------------------------------------------------------------

def serialize_history_entry(entry: dict) -> dict:
    serialized_changes = {}
    for k, v in entry.get("changes", {}).items():
        from_val = v.get("from")
        to_val = v.get("to")
        if isinstance(from_val, datetime):
            from_val = dt_to_epoch_ms(from_val)
        if isinstance(to_val, datetime):
            to_val = dt_to_epoch_ms(to_val)
        serialized_changes[k] = {"from": from_val, "to": to_val}

    at_val = entry["at"]
    if isinstance(at_val, datetime):
        at_val = dt_to_epoch_ms(at_val)

    return {
        "at": at_val,
        "by": entry["by"],
        "reason": entry["reason"],
        "changes": serialized_changes,
    }


def serialize_attendance_doc(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [serialize_history_entry(h) for h in doc.get("history", [])],
    }


def serialize_employee_doc(doc: dict) -> dict:
    created_at = doc.get("created_at")
    if isinstance(created_at, datetime):
        created_at = dt_to_epoch_ms(created_at)
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": created_at,
    }


# ---------------------------------------------------------------------------
# Request Schemas & Custom Validators
# ---------------------------------------------------------------------------

def validate_epoch_ms(v: Any, field_name: str) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError(f"{field_name} must be an integer epoch milliseconds")
    if v < EPOCH_MS_MIN or v > EPOCH_MS_MAX:
        raise ValueError(f"{field_name} out of valid range ({EPOCH_MS_MIN} - {EPOCH_MS_MAX})")
    return v


class EmployeeCreate(BaseModel):
    emp_code: str
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def check_emp_code(cls, v: str) -> str:
        if not _EMP_CODE_PATTERN.match(v):
            raise ValueError("emp_code must match EMP followed by 4-6 digits")
        return v

    @field_validator("email")
    @classmethod
    def check_email(cls, v: str) -> str:
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
            raise ValueError("invalid email address")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def check_shift_format(cls, v: str) -> str:
        if not _SHIFT_PATTERN.match(v):
            raise ValueError("shift time must be HH:MM in 24-hour format")
        return v

    @field_validator("joined_on")
    @classmethod
    def check_joined_on(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def check_shifts_differ(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def check_punched_at(cls, v: Any) -> Optional[int]:
        return validate_epoch_ms(v, "punched_at")

    @field_validator("status")
    @classmethod
    def check_status(cls, v: str) -> str:
        if v not in PRESENCE_STATUSES:
            raise ValueError(f"status must be one of {sorted(PRESENCE_STATUSES)}")
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def check_punched_at(cls, v: Any) -> Optional[int]:
        return validate_epoch_ms(v, "punched_at")


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", mode="before")
    @classmethod
    def check_punch_in(cls, v: Any) -> Optional[int]:
        return validate_epoch_ms(v, "punch_in")

    @field_validator("punch_out", mode="before")
    @classmethod
    def check_punch_out(cls, v: Any) -> Optional[int]:
        return validate_epoch_ms(v, "punch_out")

    @field_validator("status")
    @classmethod
    def check_status(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ALL_STATUSES:
            raise ValueError(f"status must be one of {sorted(ALL_STATUSES)}")
        return v

    @model_validator(mode="after")
    def check_absent_leave_punch_empty(self) -> "RegularizeRequest":
        if self.status in ABSENT_STATUSES and (self.punch_in is not None or self.punch_out is not None):
            raise ValueError("cannot supply punch_in or punch_out when status is ABSENT or LEAVE")
        return self


# ---------------------------------------------------------------------------
# Endpoints: System
# ---------------------------------------------------------------------------

@app.get("/health", tags=["system"])
def health():
    """Readiness probe: returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=503, detail="Database not ready")


# ---------------------------------------------------------------------------
# Endpoints: Employees
# ---------------------------------------------------------------------------

@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """Create an employee. emp_code must be unique (enforced by DB index)."""
    now_utc = datetime.now(tz=timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now_utc

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    doc.pop("_id", None)
    return serialize_employee_doc(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """List employees sorted by emp_code ascending."""
    q: dict = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_employee_doc(doc) for doc in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# Endpoints: Attendance (Punch-in, Punch-out, List, Regularize)
# ---------------------------------------------------------------------------

@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """Record a punch-in. Creates the day's record (R1, R2). Race-safe."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        pin_utc = epoch_ms_to_utc_dt(body.punched_at)
    else:
        pin_utc = datetime.now(tz=timezone.utc).replace(microsecond=0)

    att_date = resolve_attendance_date(pin_utc, emp["shift_start"], emp["shift_end"])
    late = compute_late_minutes(pin_utc, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": pin_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail=f"Already punched in for date {att_date}",
        )

    doc.pop("_id", None)
    return serialize_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """
    Record a punch-out. Closes most recent record whose punch_in <= punched_at.
    Computes work_hours (R4), overtime_minutes (R3), half_day (R5).
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        pout_utc = epoch_ms_to_utc_dt(body.punched_at)
    else:
        pout_utc = datetime.now(tz=timezone.utc).replace(microsecond=0)

    # Find the most recent open record
    open_record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_out": None,
            "status": {"$in": list(PRESENCE_STATUSES)},
        },
        sort=[("punch_in", DESCENDING)],
    )

    if not open_record:
        # Check if they punched in and are already punched out
        latest_record = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "status": {"$in": list(PRESENCE_STATUSES)},
            },
            sort=[("punch_in", DESCENDING)],
        )
        if latest_record and latest_record.get("punch_out") is not None:
            raise HTTPException(status_code=409, detail="Record is already punched out")
        raise HTTPException(status_code=404, detail="No matching punch-in found")

    pin_utc = ensure_utc(open_record["punch_in"])
    diff_sec = (pout_utc - pin_utc).total_seconds()

    if diff_sec <= 0:
        raise HTTPException(status_code=422, detail="punched_at must be strictly after punch_in")
    if diff_sec > 86400:
        raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    record = open_record

    att_date = record["date"]
    wh = compute_work_hours(pin_utc, pout_utc)
    ot = compute_overtime_minutes(pout_utc, emp["shift_start"], emp["shift_end"], att_date)
    hd = compute_half_day(wh)

    # Atomic update to prevent simultaneous punch-out race
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": pout_utc,
                "work_hours": wh,
                "overtime_minutes": ot,
                "half_day": hd,
            }
        },
        return_document=True,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Record was already punched out")

    updated.pop("_id", None)
    return serialize_attendance_doc(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    List attendance records, sorted by date descending then emp_code ascending.
    Uses database indexes for pagination; does not load whole collection into memory.
    """
    if date_from:
        try:
            date.fromisoformat(date_from)
        except ValueError:
            raise HTTPException(status_code=422, detail="date_from must be YYYY-MM-DD")
    if date_to:
        try:
            date.fromisoformat(date_to)
        except ValueError:
            raise HTTPException(status_code=422, detail="date_to must be YYYY-MM-DD")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be after date_to")
    if status is not None and status not in ALL_STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {sorted(ALL_STATUSES)}")

    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_attendance_doc(d) for d in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{record_date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str,
    record_date: str,
    body: RegularizeRequest,
):
    """
    Regularize one day's record. Recompute derived fields, append to history.
    Enforces optimistic concurrency so concurrent edits do not lose history.
    """
    try:
        date.fromisoformat(record_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="date path parameter must be YYYY-MM-DD")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": record_date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    final_status = body.status if body.status is not None else record["status"]

    if final_status in ABSENT_STATUSES:
        final_pin_dt = None
        final_pout_dt = None
    else:
        if body.punch_in is not None:
            final_pin_dt = epoch_ms_to_utc_dt(body.punch_in)
        else:
            final_pin_dt = ensure_utc(record.get("punch_in"))

        if body.punch_out is not None:
            final_pout_dt = epoch_ms_to_utc_dt(body.punch_out)
        else:
            final_pout_dt = ensure_utc(record.get("punch_out"))

        if final_pin_dt is None:
            raise HTTPException(status_code=422, detail="A presence status requires a punch_in")

        # R1: punch_in must stay on the record's attendance date
        calculated_att_date = resolve_attendance_date(final_pin_dt, emp["shift_start"], emp["shift_end"])
        if calculated_att_date != record_date:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in belongs to attendance date {calculated_att_date}, but record is {record_date}",
            )

        if final_pout_dt is not None:
            diff_sec = (final_pout_dt - final_pin_dt).total_seconds()
            if diff_sec <= 0:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
            if diff_sec > 86400:
                raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

    derived = compute_all_derived(
        final_pin_dt,
        final_pout_dt,
        emp["shift_start"],
        emp["shift_end"],
        record_date,
    )

    new_fields = {
        "status": final_status,
        "punch_in": final_pin_dt,
        "punch_out": final_pout_dt,
        "work_hours": derived["work_hours"],
        "late_minutes": derived["late_minutes"],
        "overtime_minutes": derived["overtime_minutes"],
        "half_day": derived["half_day"],
    }

    # Detect changes
    changes = {}
    for f in ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]:
        old_val = record.get(f)
        new_val = new_fields[f]
        # Normalize datetimes for comparison
        cmp_old = ensure_utc(old_val) if isinstance(old_val, datetime) else old_val
        cmp_new = ensure_utc(new_val) if isinstance(new_val, datetime) else new_val
        if cmp_old != cmp_new:
            changes[f] = {"from": old_val, "to": new_val}

    if not changes:
        raise HTTPException(status_code=422, detail="No changes detected")

    now_utc = datetime.now(tz=timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    current_history = record.get("history", [])
    history_len = len(current_history)

    filter_query = {
        "emp_code": emp_code,
        "date": record_date,
        "history": {"$size": history_len},
    }

    update_query = {
        "$set": new_fields,
        "$push": {"history": history_entry},
    }

    updated = db.attendance_logs.find_one_and_update(
        filter_query,
        update_query,
        return_document=True,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Concurrent edit conflict; please retry")

    updated.pop("_id", None)
    return serialize_attendance_doc(updated)


# ---------------------------------------------------------------------------
# Endpoints: Analytics
# ---------------------------------------------------------------------------

def get_month_range(month_str: str) -> tuple[str, str, date, date, List[str]]:
    """Return (first_day_str, last_day_str, first_date, last_date, weekday_date_strings)."""
    y, m = map(int, month_str.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    d1 = date(y, m, 1)
    d2 = date(y, m, num_days)
    weekday_dates = []
    cur = d1
    while cur <= d2:
        if cur.weekday() < 5:  # Mon=0..Fri=4
            weekday_dates.append(cur.isoformat())
        cur += timedelta(days=1)
    return d1.isoformat(), d2.isoformat(), d1, d2, weekday_dates


def count_working_days(start_d: date, end_d: date) -> int:
    """R7: Mon-Fri working days between start_d and end_d inclusive."""
    if start_d > end_d:
        return 0
    cur = start_d
    w_days = 0
    while cur <= end_d:
        if cur.weekday() < 5:
            w_days += 1
        cur += timedelta(days=1)
    return w_days


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """
    Monthly attendance summary for one employee. Backed by MongoDB aggregation.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    first_day_str, last_day_str, first_d, last_d, weekday_dates = get_month_range(month)

    emp_joined_d = date.fromisoformat(emp["joined_on"])
    period_start_d = max(first_d, emp_joined_d)
    working_days = count_working_days(period_start_d, last_d)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": first_day_str, "$lte": last_day_str},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                    {"$in": ["$date", weekday_dates]},
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]

    agg_res = list(db.attendance_logs.aggregate(pipeline))

    if agg_res:
        data = agg_res[0]
        present_days = round_half_up(float(data["present_days"]), 2)
        leave_days = int(data["leave_days"])
        late_count = int(data["late_count"])
        total_late = int(data["total_late_minutes"])
        total_ot = int(data["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """
    Department summary for a month via MongoDB aggregation.
    Headcount per R9 includes employees with zero logs.
    """
    first_day_str, last_day_str, _, _, weekday_dates = get_month_range(month)

    match_emp: dict = {"joined_on": {"$lte": last_day_str}}
    if department is not None:
        match_emp["department"] = department

    pipeline = [
        {"$match": match_emp},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "logs",
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "all_logs": {"$push": "$logs"},
            }
        },
        {"$sort": {"_id": ASCENDING}},
    ]

    raw_results = list(db.employees.aggregate(pipeline))
    items = []

    for dept_doc in raw_results:
        dept_name = dept_doc["_id"]
        headcount = dept_doc["headcount"]
        flattened_logs = [log for sublist in dept_doc.get("all_logs", []) for log in sublist]

        present_days = 0.0
        late_count = 0
        total_late_minutes = 0
        leave_count = 0
        on_duty_count = 0
        work_hours_list = []

        for log in flattened_logs:
            log_date = log.get("date", "")
            if not (first_day_str <= log_date <= last_day_str):
                continue

            stat = log.get("status")

            if stat in PRESENCE_STATUSES and log_date in weekday_dates:
                present_days += 0.5 if log.get("half_day") else 1.0

            if stat in PRESENCE_STATUSES and log.get("work_hours") is not None:
                work_hours_list.append(log["work_hours"])

            late_m = log.get("late_minutes", 0)
            if late_m > 0:
                late_count += 1
                total_late_minutes += late_m

            if stat == "LEAVE":
                leave_count += 1
            elif stat == "ON_DUTY":
                on_duty_count += 1

        avg_wh = (
            round_half_up(sum(work_hours_list) / len(work_hours_list), 2)
            if work_hours_list
            else None
        )

        items.append({
            "department": dept_name,
            "headcount": headcount,
            "present_days": round_half_up(present_days, 2),
            "avg_work_hours": avg_wh,
            "late_count": late_count,
            "total_late_minutes": total_late_minutes,
            "leave_count": leave_count,
            "on_duty_count": on_duty_count,
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    """
    Late-comers leaderboard for a month. Standard competition ranking (1, 2, 2, 4).
    Limit applies after ranking (returns all rows with rank <= limit).
    """
    first_day_str, last_day_str, _, _, _ = get_month_range(month)

    pipeline = [
        {
            "$match": {
                "date": {"$gte": first_day_str, "$lte": last_day_str},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.append(
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}}
    )

    rows = list(db.attendance_logs.aggregate(pipeline))

    # Compute standard competition ranking
    ranked_items = []
    current_rank = 1
    for idx, r in enumerate(rows):
        if idx > 0 and r["total_late_minutes"] < rows[idx - 1]["total_late_minutes"]:
            current_rank = idx + 1

        ranked_items.append({
            "rank": current_rank,
            "emp_code": r["_id"],
            "name": r["emp"]["name"],
            "department": r["emp"]["department"],
            "total_late_minutes": r["total_late_minutes"],
            "late_count": r["late_count"],
        })

    filtered_items = [item for item in ranked_items if item["rank"] <= limit]
    return {"month": month, "items": filtered_items}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
):
    """
    Daily attendance trend for a department. Returns exactly one row per calendar day
    from 'from' to 'to' inclusive.
    """
    try:
        from_d = date.fromisoformat(from_)
        to_d = date.fromisoformat(to)
    except ValueError:
        raise HTTPException(status_code=422, detail="'from' and 'to' must be YYYY-MM-DD")

    if to_d < from_d:
        raise HTTPException(status_code=422, detail="'to' must be on or after 'from'")

    if (to_d - from_d).days > 91:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    if not db.employees.find_one({"department": department}):
        raise HTTPException(status_code=404, detail="Department not found")

    pipeline = [
        {"$match": {"date": {"$gte": from_, "$lte": to}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", list(PRESENCE_STATUSES)]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
            }
        },
        {"$sort": {"_id": ASCENDING}},
    ]

    logs_by_date = {row["_id"]: row for row in db.attendance_logs.aggregate(pipeline)}
    dept_employees = list(db.employees.find({"department": department}, {"joined_on": 1, "_id": 0}))

    items = []
    rates_window = []

    cur_d = from_d
    while cur_d <= to_d:
        d_str = cur_d.isoformat()
        is_working_day = cur_d.weekday() < 5

        headcount = sum(1 for e in dept_employees if e["joined_on"] <= d_str)

        log_data = logs_by_date.get(d_str, {})
        present_count = round_half_up(float(log_data.get("present_count", 0.0)), 2)
        late_count = int(log_data.get("late_count", 0))

        if is_working_day and headcount > 0:
            att_rate = round_half_up(present_count / headcount, 4)
        else:
            att_rate = None

        rates_window.append(att_rate)

        recent_rates = rates_window[-7:]
        valid_rates = [r for r in recent_rates if r is not None]
        if valid_rates:
            mavg = round_half_up(sum(valid_rates) / len(valid_rates), 4)
        else:
            mavg = None

        items.append({
            "date": d_str,
            "is_working_day": is_working_day,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": att_rate,
            "moving_avg_7d": mavg,
        })

        cur_d += timedelta(days=1)

    return {"department": department, "items": items}


# ---------------------------------------------------------------------------
# Admin: Explain endpoint
# ---------------------------------------------------------------------------

@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    Run MongoDB explain with executionStats for the main query or aggregation
    of the named endpoint.
    """
    valid_endpoints = [
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ]
    if endpoint not in valid_endpoints:
        raise HTTPException(
            status_code=422,
            detail=f"endpoint must be one of {valid_endpoints}",
        )

    if endpoint == "attendance_list":
        q: dict = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        explain_res = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res,
        }

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        if not _MONTH_PATTERN.match(month):
            raise HTTPException(status_code=422, detail="month must be YYYY-MM")
        first_day_str, last_day_str, _, _, _ = get_month_range(month)
        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": first_day_str, "$lte": last_day_str},
                }
            },
            {"$group": {"_id": None, "count": {"$sum": 1}}},
        ]
        explain_res = db.command(
            "aggregate",
            "attendance_logs",
            pipeline=pipeline,
            explain=True,
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res,
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        if not _MONTH_PATTERN.match(month):
            raise HTTPException(status_code=422, detail="month must be YYYY-MM")
        first_day_str, last_day_str, _, _, _ = get_month_range(month)
        match_emp: dict = {"joined_on": {"$lte": last_day_str}}
        if department:
            match_emp["department"] = department
        pipeline = [
            {"$match": match_emp},
            {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
        ]
        explain_res = db.command(
            "aggregate",
            "employees",
            pipeline=pipeline,
            explain=True,
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain_res,
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        if not _MONTH_PATTERN.match(month):
            raise HTTPException(status_code=422, detail="month must be YYYY-MM")
        first_day_str, last_day_str, _, _, _ = get_month_range(month)
        pipeline = [
            {
                "$match": {
                    "date": {"$gte": first_day_str, "$lte": last_day_str},
                    "late_minutes": {"$gt": 0},
                }
            },
            {"$group": {"_id": "$emp_code", "total_late": {"$sum": "$late_minutes"}}},
        ]
        explain_res = db.command(
            "aggregate",
            "attendance_logs",
            pipeline=pipeline,
            explain=True,
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res,
        }

    elif endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(status_code=422, detail="department, from, and to are required")
        pipeline = [
            {"$match": {"date": {"$gte": from_, "$lte": to}}},
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "emp",
                }
            },
            {"$unwind": "$emp"},
            {"$match": {"emp.department": department}},
            {"$group": {"_id": "$date", "count": {"$sum": 1}}},
        ]
        explain_res = db.command(
            "aggregate",
            "attendance_logs",
            pipeline=pipeline,
            explain=True,
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res,
        }

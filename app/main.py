"""
Employee Attendance & Analytics API
Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""

import os
import math
from datetime import datetime, timedelta, timezone, date
from typing import Optional, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, errors as pymongo_errors
from pymongo.collection import Collection

load_dotenv()

# --------------------------------------------------------------------------- #
# DB Setup
# --------------------------------------------------------------------------- #
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB  = os.getenv("MONGO_DB", "attendance_db")

IST = timezone(timedelta(hours=5, minutes=30))

_client: MongoClient | None = None
_db = None


def get_db():
    global _client, _db
    if _client is None:
        _client = MongoClient(MONGO_URI, tz_aware=True)
        _db = _client[MONGO_DB]
    return _db


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes():
    """Create all required indexes idempotently at startup."""
    db = get_db()

    # employees: unique on emp_code (enforces dedup at DB level for concurrent creates)
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="idx_emp_code_unique")
    db.employees.create_index([("department", ASCENDING)], name="idx_emp_dept")
    db.employees.create_index([("joined_on", ASCENDING)], name="idx_emp_joined_on")

    # attendance_logs: unique compound key is our main access pattern
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="idx_log_emp_date_unique",
    )
    # Needed for sorting/filtering by date (list endpoint, trend)
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="idx_log_date_emp",
    )
    # Analytics aggregations filter by date range + status
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("status", ASCENDING)],
        name="idx_log_date_status",
    )


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES      = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
EPOCH_MIN = 100_000_000_000   # milliseconds: ~1973 (guards against seconds)
EPOCH_MAX = 4_102_444_800_000


# --------------------------------------------------------------------------- #
# Business Rule Helpers (R1-R5)
# --------------------------------------------------------------------------- #
def millis_to_dt(ms: int) -> datetime:
    """Convert epoch-milliseconds to a timezone-aware UTC datetime, truncated to whole seconds."""
    dt = datetime.fromtimestamp(ms // 1000, tz=timezone.utc)
    return dt.replace(microsecond=0)


def dt_to_millis(dt: datetime | None) -> int | None:
    """Convert a datetime to epoch milliseconds (int), or None."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def to_ist(dt: datetime) -> datetime:
    return dt.astimezone(IST)


def attendance_date(punch_in_utc: datetime, shift_end: str) -> str:
    """
    R1: Determine the attendance date (IST calendar date) for a punch-in.
    For overnight shifts (shift_end <= shift_start by definition), a punch-in
    whose IST time is strictly earlier than shift_end belongs to the PREVIOUS day's shift.
    """
    punch_ist = to_ist(punch_in_utc)
    # Determine if this is an overnight shift possibility:
    # We compare the IST time of punch_in to shift_end time on the same IST day.
    # If shift_end <= shift_start (the shift spans midnight), and punch_ist.time < shift_end,
    # then the attendance date is the previous IST calendar day.
    eh, em = map(int, shift_end.split(":"))
    shift_end_time = punch_ist.replace(hour=eh, minute=em, second=0, microsecond=0)
    # We don't have shift_start here; that logic is handled before calling this.
    # The caller must pass a sentinel to indicate overnight.
    return punch_ist.date().isoformat()


def attendance_date_full(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Full overnight handling. Determines the correct IST calendar date.
    Overnight shift: shift_end <= shift_start (e.g., 22:00-06:00).
    A punch-in whose IST time is *earlier than* shift_end belongs to the previous day.
    """
    punch_ist = to_ist(punch_in_utc)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))

    is_overnight = (eh * 60 + em) <= (sh * 60 + sm)

    if is_overnight:
        punch_time_mins = punch_ist.hour * 60 + punch_ist.minute
        shift_end_mins  = eh * 60 + em
        if punch_time_mins < shift_end_mins:
            # This punch belongs to the shift that started yesterday
            return (punch_ist.date() - timedelta(days=1)).isoformat()

    return punch_ist.date().isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, att_date: str) -> int:
    """
    R2: Grace is 10 min. Late only if strictly > 10 min after shift_start.
    late_minutes = floor(minutes since shift_start), not since grace period end.
    """
    punch_ist = to_ist(punch_in)
    h, m = map(int, shift_start.split(":"))
    # Use att_date (the attendance date) to build the shift start on the correct IST day
    att = date.fromisoformat(att_date)
    start_ist = datetime(att.year, att.month, att.day, h, m, 0, tzinfo=IST)
    elapsed_secs = (punch_ist - start_ist).total_seconds()
    elapsed_mins = elapsed_secs / 60
    if elapsed_mins > 10:
        return math.floor(elapsed_mins)
    return 0


def compute_work_hours_r4(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded half-up to 2 decimals."""
    raw = (punch_out - punch_in).total_seconds() / 3600
    return round_half_up(raw, 2)


def compute_overtime_minutes(punch_out: datetime, shift_end: str, att_date: str) -> int:
    """
    R3: overtime = floor(minutes between shift_end and punch_out) if >= 30, else 0.
    For overnight shifts the shift_end is on the NEXT calendar day after att_date.
    """
    punch_ist = to_ist(punch_out)
    h, m = map(int, shift_end.split(":"))
    att = date.fromisoformat(att_date)
    end_ist = datetime(att.year, att.month, att.day, h, m, 0, tzinfo=IST)

    # If shift_end time is on a later calendar day (overnight), add a day
    # We detect this: if punch_ist is clearly before shift_end on att_date but we know
    # it's an overnight shift, we must add a day.
    # Simpler heuristic: if end_ist is before punch_in (start of same day), push end to next day.
    # But we don't have punch_in here. Instead check: if end_ist < 12:00 and punch_ist > 12:00 -> next day
    # The safest approach: if punch_ist time < shift_end time on att_date, the shift_end is next day
    # (only matters for overnight). We'll detect by checking if punch_ist is within 24h of att_date noon.

    # Check if the shift is overnight by comparing shift_end to 12:00 noon heuristic:
    # For an overnight shift end like 06:00, the shift end is on the next calendar day.
    # The punch_out for overnight should be on att_date+1.
    # If the computed end_ist is more than 12h before punch_ist, add a day:
    if (punch_ist - end_ist).total_seconds() > 12 * 3600:
        end_ist += timedelta(days=1)

    ot_secs = (punch_ist - end_ist).total_seconds()
    ot_mins = math.floor(ot_secs / 60)
    if ot_mins >= 30:
        return ot_mins
    return 0


def compute_half_day(work_hours: float) -> bool:
    """R5: half_day = True when rounded work_hours < 4.50."""
    return work_hours < 4.50


def round_half_up(value: float, decimals: int) -> float:
    """Half-up rounding (not banker's rounding)."""
    factor = 10 ** decimals
    return math.floor(value * factor + 0.5) / factor


# --------------------------------------------------------------------------- #
# Serialization helpers
# --------------------------------------------------------------------------- #
PRESENCE_FIELDS = {"punch_in", "punch_out"}


def serialize_log(doc: dict) -> dict:
    """Convert a MongoDB attendance_logs document to API shape."""
    result = {
        "emp_code":         doc["emp_code"],
        "date":             doc["date"],
        "status":           doc["status"],
        "punch_in":         dt_to_millis(doc.get("punch_in")),
        "punch_out":        dt_to_millis(doc.get("punch_out")),
        "work_hours":       doc.get("work_hours"),
        "late_minutes":     doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day":         doc.get("half_day", False),
        "history":          serialize_history(doc.get("history") or []),
    }
    return result


def serialize_history(history: list) -> list:
    out = []
    for entry in history:
        changes = {}
        for field, val in (entry.get("changes") or {}).items():
            if field in PRESENCE_FIELDS:
                changes[field] = {
                    "from": dt_to_millis(val["from"]),
                    "to":   dt_to_millis(val["to"]),
                }
            else:
                changes[field] = {"from": val["from"], "to": val["to"]}
        out.append({
            "at":      dt_to_millis(entry["at"]),
            "by":      entry["by"],
            "reason":  entry["reason"],
            "changes": changes,
        })
    return out


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code":    doc["emp_code"],
        "name":        doc["name"],
        "email":       doc["email"],
        "department":  doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end":   doc["shift_end"],
        "joined_on":   doc["joined_on"],
        "created_at":  dt_to_millis(doc["created_at"]),
    }


# --------------------------------------------------------------------------- #
# Pydantic Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code:    str
    name:        str
    email:       str
    department:  str
    shift_start: str = "09:30"
    shift_end:   str = "18:30"
    joined_on:   str

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, v):
        import re
        if not re.match(r"^EMP\d{4,6}$", v):
            raise ValueError("emp_code must match EMP followed by 4-6 digits")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_time(cls, v):
        import re
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", v):
            raise ValueError("time must be HH:MM in 24-hour format")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v):
        import re
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
            raise ValueError("invalid email")
        return v

    @model_validator(mode="after")
    def shift_start_ne_shift_end(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code:   str
    punched_at: Optional[int] = None
    status:     str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_millis(cls, v):
        if v is not None:
            if not isinstance(v, int) or isinstance(v, bool):
                raise ValueError("punched_at must be an integer epoch milliseconds")
            if v < EPOCH_MIN or v > EPOCH_MAX:
                raise ValueError("punched_at out of valid range (must be milliseconds, not seconds)")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v not in PRESENCE_STATUSES:
            raise ValueError(f"status must be one of {sorted(PRESENCE_STATUSES)}")
        return v


class PunchOutRequest(BaseModel):
    emp_code:   str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def validate_millis(cls, v):
        if v is not None:
            if not isinstance(v, int) or isinstance(v, bool):
                raise ValueError("punched_at must be an integer epoch milliseconds")
            if v < EPOCH_MIN or v > EPOCH_MAX:
                raise ValueError("punched_at out of valid range (must be milliseconds, not seconds)")
        return v


class RegularizeRequest(BaseModel):
    status:          Optional[str] = None
    punch_in:        Optional[int] = None
    punch_out:       Optional[int] = None
    reason:          str
    regularized_by:  str

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_millis(cls, v):
        if v is not None:
            if not isinstance(v, int) or isinstance(v, bool):
                raise ValueError("must be an integer epoch milliseconds")
            if v < EPOCH_MIN or v > EPOCH_MAX:
                raise ValueError("out of valid range (must be milliseconds, not seconds)")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v is not None and v not in ALL_STATUSES:
            raise ValueError(f"status must be one of {sorted(ALL_STATUSES)}")
        return v

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, v):
        if len(v) < 5 or len(v) > 200:
            raise ValueError("reason must be 5-200 characters")
        return v

    @field_validator("regularized_by")
    @classmethod
    def validate_by(cls, v):
        if len(v) < 1 or len(v) > 50:
            raise ValueError("regularized_by must be 1-50 characters")
        return v


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    db = get_db()
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, detail="MongoDB unavailable")


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    db = get_db()
    doc = body.model_dump()
    doc["created_at"] = datetime.now(tz=timezone.utc)
    try:
        db.employees.insert_one(doc)
    except pymongo_errors.DuplicateKeyError:
        raise HTTPException(409, detail="emp_code already exists")
    doc.pop("_id", None)
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page:       int = Query(default=1, ge=1),
    page_size:  int = Query(default=20, ge=1, le=100),
):
    db = get_db()
    q: dict[str, Any] = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip  = (page - 1) * page_size
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items":     [serialize_employee(e) for e in items],
        "total":     total,
        "page":      page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance – Punch In
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    db = get_db()
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    if body.punched_at is not None:
        ts = millis_to_dt(body.punched_at)
    else:
        ts = datetime.now(tz=timezone.utc).replace(microsecond=0)

    att_date = attendance_date_full(ts, emp["shift_start"], emp["shift_end"])
    late     = compute_late_minutes(ts, emp["shift_start"], att_date)

    doc = {
        "emp_code":         body.emp_code,
        "date":             att_date,
        "status":           body.status,
        "punch_in":         ts,
        "punch_out":        None,
        "work_hours":       None,
        "late_minutes":     late,
        "overtime_minutes": 0,
        "half_day":         False,
        "history":          [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except pymongo_errors.DuplicateKeyError:
        raise HTTPException(409, detail="Already punched in for this attendance date")

    doc.pop("_id", None)
    return serialize_log(doc)


# --------------------------------------------------------------------------- #
# Attendance – Punch Out
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    db = get_db()
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    if body.punched_at is not None:
        ts = millis_to_dt(body.punched_at)
    else:
        ts = datetime.now(tz=timezone.utc).replace(microsecond=0)

    # Find the most recent record where punch_in <= punched_at and punch_out is null
    record = db.attendance_logs.find_one(
        {
            "emp_code":  body.emp_code,
            "punch_in":  {"$lte": ts, "$ne": None},
            "punch_out": None,
        },
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(404, detail="No open punch-in found at or before punched_at")

    punch_in_dt: datetime = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    # Validate: punch_out must be after punch_in and within 24 hours
    diff_secs = (ts - punch_in_dt).total_seconds()
    if diff_secs <= 0:
        raise HTTPException(422, detail="punched_at must be after punch_in")
    if diff_secs > 86400:
        raise HTTPException(422, detail="punched_at must be within 24 hours of punch_in")

    wh = compute_work_hours_r4(punch_in_dt, ts)
    ot = compute_overtime_minutes(ts, emp["shift_end"], record["date"])
    hd = compute_half_day(wh)

    result = db.attendance_logs.find_one_and_update(
        {
            "_id":       record["_id"],
            "punch_out": None,          # guard against concurrent punch-outs
        },
        {
            "$set": {
                "punch_out":        ts,
                "work_hours":       wh,
                "overtime_minutes": ot,
                "half_day":         hd,
            }
        },
        return_document=True,
    )
    if result is None:
        raise HTTPException(409, detail="Record already punched out")

    return serialize_log(result)


# --------------------------------------------------------------------------- #
# Attendance – List
# --------------------------------------------------------------------------- #
@app.get("/attendance")
def list_attendance(
    emp_code:  Optional[str] = None,
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    status:    Optional[str] = None,
    page:      int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, detail="date_from must not be after date_to")
    if status and status not in ALL_STATUSES:
        raise HTTPException(422, detail=f"Invalid status value")

    db = get_db()
    q: dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        date_q: dict[str, str] = {}
        if date_from:
            date_q["$gte"] = date_from
        if date_to:
            date_q["$lte"] = date_to
        q["date"] = date_q
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip  = (page - 1) * page_size
    docs  = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items":     [serialize_log(d) for d in docs],
        "total":     total,
        "page":      page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance – Regularize (PATCH)
# --------------------------------------------------------------------------- #
@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date:     str,
    body:     RegularizeRequest,
):
    date_str = date
    db = get_db()

    # Validate date format
    try:
        date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(422, detail="date must be YYYY-MM-DD")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not record:
        raise HTTPException(404, detail="No attendance record found for this employee and date")

    # Validate: ABSENT/LEAVE with punch times
    new_status = body.status or record["status"]
    if new_status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, detail="ABSENT/LEAVE status cannot have punch times")

    # Presence status requires a punch_in
    if new_status in PRESENCE_STATUSES:
        existing_pi = record.get("punch_in")
        incoming_pi = body.punch_in
        if existing_pi is None and incoming_pi is None:
            raise HTTPException(422, detail="Presence status requires a punch_in")

    # Determine final punch_in and punch_out (as aware datetimes)
    def _to_aware(dt_val):
        if dt_val is None:
            return None
        if hasattr(dt_val, "tzinfo") and dt_val.tzinfo is not None:
            return dt_val
        return dt_val.replace(tzinfo=timezone.utc)

    old_pi  = _to_aware(record.get("punch_in"))
    old_po  = _to_aware(record.get("punch_out"))

    new_pi  = millis_to_dt(body.punch_in)  if body.punch_in  is not None else old_pi
    new_po  = millis_to_dt(body.punch_out) if body.punch_out is not None else old_po

    # ABSENT/LEAVE clears punch times
    if new_status in {"ABSENT", "LEAVE"}:
        new_pi = None
        new_po = None

    # Validate punch_in stays on the record's attendance date (R1)
    if new_pi is not None:
        pi_date = attendance_date_full(new_pi, emp["shift_start"], emp["shift_end"])
        if pi_date != date_str:
            raise HTTPException(422, detail="punch_in must remain on the record's attendance date")

    # Validate punch_out constraints
    if new_po is not None and new_pi is not None:
        diff_secs = (new_po - new_pi).total_seconds()
        if diff_secs <= 0:
            raise HTTPException(422, detail="punch_out must be after punch_in")
        if diff_secs > 86400:
            raise HTTPException(422, detail="punch_out must be within 24 hours of punch_in")

    # Recompute derived fields
    if new_status in {"ABSENT", "LEAVE"}:
        new_late = 0
        new_wh   = None
        new_ot   = 0
        new_hd   = False
    else:
        new_late = compute_late_minutes(new_pi, emp["shift_start"], date_str) if new_pi else 0
        if new_pi and new_po:
            new_wh = compute_work_hours_r4(new_pi, new_po)
            new_ot = compute_overtime_minutes(new_po, emp["shift_end"], date_str)
            new_hd = compute_half_day(new_wh)
        else:
            new_wh = None
            new_ot = record.get("overtime_minutes", 0)
            new_hd = record.get("half_day", False)

    # Build changes dict: only fields that actually changed
    def _norm_dt(v):
        """Normalise stored datetime for comparison (UTC aware, whole seconds)."""
        if v is None:
            return None
        if hasattr(v, "tzinfo") and v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.replace(microsecond=0)

    changes: dict[str, dict] = {}

    if new_status != record.get("status"):
        changes["status"] = {"from": record.get("status"), "to": new_status}

    old_pi_n = _norm_dt(old_pi)
    new_pi_n = _norm_dt(new_pi)
    if old_pi_n != new_pi_n:
        changes["punch_in"] = {"from": old_pi_n, "to": new_pi_n}

    old_po_n = _norm_dt(old_po)
    new_po_n = _norm_dt(new_po)
    if old_po_n != new_po_n:
        changes["punch_out"] = {"from": old_po_n, "to": new_po_n}

    if new_wh != record.get("work_hours"):
        changes["work_hours"] = {"from": record.get("work_hours"), "to": new_wh}

    old_late = record.get("late_minutes", 0)
    if new_late != old_late:
        changes["late_minutes"] = {"from": old_late, "to": new_late}

    old_ot = record.get("overtime_minutes", 0)
    if new_ot != old_ot:
        changes["overtime_minutes"] = {"from": old_ot, "to": new_ot}

    old_hd = record.get("half_day", False)
    if new_hd != old_hd:
        changes["half_day"] = {"from": old_hd, "to": new_hd}

    if not changes:
        raise HTTPException(422, detail="No fields would change")

    history_entry = {
        "at":      datetime.now(tz=timezone.utc),
        "by":      body.regularized_by,
        "reason":  body.reason,
        "changes": changes,
    }

    # Atomic update with optimistic concurrency: compare history length to detect concurrent edit
    current_history_len = len(record.get("history") or [])
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id":                              record["_id"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, current_history_len]},
        },
        {
            "$set": {
                "status":           new_status,
                "punch_in":         new_pi,
                "punch_out":        new_po,
                "work_hours":       new_wh,
                "late_minutes":     new_late,
                "overtime_minutes": new_ot,
                "half_day":         new_hd,
            },
            "$push": {"history": history_entry},
        },
        return_document=True,
    )
    if updated is None:
        raise HTTPException(409, detail="Concurrent edit detected; please retry")

    return serialize_log(updated)


# --------------------------------------------------------------------------- #
# Analytics – Employee Monthly Summary
# --------------------------------------------------------------------------- #
def _month_range(month: str):
    """Return (YYYY-MM-DD start, YYYY-MM-DD end) for a YYYY-MM month string."""
    import calendar
    y, m = int(month[:4]), int(month[5:7])
    last_day = calendar.monthrange(y, m)[1]
    return f"{month}-01", f"{month}-{last_day:02d}"


def _count_working_days_since(start_date_str: str, month_start: str, month_end: str) -> int:
    """Count Mon-Fri days in [max(start_date, month_start), month_end]."""
    from_d = max(date.fromisoformat(start_date_str), date.fromisoformat(month_start))
    to_d   = date.fromisoformat(month_end)
    if from_d > to_d:
        return 0
    count = 0
    cur = from_d
    while cur <= to_d:
        if cur.weekday() < 5:  # Mon=0 … Fri=4
            count += 1
        cur += timedelta(days=1)
    return count


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month:    str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    db = get_db()
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    m_start, m_end = _month_range(month)
    working_days = _count_working_days_since(emp["joined_on"], m_start, m_end)

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": m_start, "$lte": m_end}}},
        {
            "$group": {
                "_id": None,
                # present_days: PRESENT/WFH/ON_DUTY on Mon-Fri; half_day = 0.5
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                # isoDayOfWeek: 1=Mon … 7=Sun; working = 1-5
                                {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]},
                            ]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "leave_days":            {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]},  1, 0]}},
                "late_count":            {"$sum": {"$cond": [{"$gt":  ["$late_minutes", 0]},  1, 0]}},
                "total_late_minutes":    {"$sum": {"$ifNull": ["$late_minutes",     0]}},
                "total_overtime_minutes":{"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))
    if rows:
        r = rows[0]
        present_days        = round_half_up(r["present_days"], 2)
        leave_days          = r["leave_days"]
        late_count          = r["late_count"]
        total_late_minutes  = r["total_late_minutes"]
        total_ot_minutes    = r["total_overtime_minutes"]
    else:
        present_days = 0.0
        leave_days = late_count = total_late_minutes = total_ot_minutes = 0

    attendance_pct = (
        round_half_up(present_days / working_days * 100, 2)
        if working_days > 0 else None
    )

    return {
        "emp_code":               emp_code,
        "month":                  month,
        "working_days":           working_days,
        "present_days":           present_days,
        "leave_days":             leave_days,
        "late_count":             late_count,
        "total_late_minutes":     total_late_minutes,
        "total_overtime_minutes": total_ot_minutes,
        "attendance_pct":         attendance_pct,
    }


# --------------------------------------------------------------------------- #
# Analytics – Department Summary
# --------------------------------------------------------------------------- #
@app.get("/analytics/departments/summary")
def department_summary(
    month:      str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    db = get_db()
    m_start, m_end = _month_range(month)

    emp_match: dict[str, Any] = {"joined_on": {"$lte": m_end}}
    if department:
        emp_match["department"] = department

    pipeline = [
        # Stage 1: all employees whose joined_on <= month end
        {"$match": emp_match},
        {
            "$group": {
                "_id": "$department",
                "headcount":     {"$sum": 1},
                "emp_codes":     {"$push": "$emp_code"},
                "joined_ons":    {"$push": "$joined_on"},
            }
        },
        # Stage 2: lookup attendance logs for those departments
        {
            "$lookup": {
                "from": "attendance_logs",
                "let":  {"codes": "$emp_codes"},
                "pipeline": [
                    {"$match": {
                        "$expr": {"$in": ["$emp_code", "$$codes"]},
                        "date": {"$gte": m_start, "$lte": m_end},
                    }},
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmpty": True}},
        {
            "$group": {
                "_id":         "$_id",
                "headcount":   {"$first": "$headcount"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$in": [{"$ifNull": ["$logs.status", ""]}, ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "avg_work_hours_sum":   {
                    "$sum": {
                        "$cond": [
                            {"$and": [
                                {"$in": [{"$ifNull": ["$logs.status", ""]}, ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$gt": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                            ]},
                            {"$ifNull": ["$logs.work_hours", 0]},
                            0,
                        ]
                    }
                },
                "avg_work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {"$and": [
                                {"$in": [{"$ifNull": ["$logs.status", ""]}, ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$gt": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                            ]},
                            1,
                            0,
                        ]
                    }
                },
                "late_count":           {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes":   {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count":          {"$sum": {"$cond": [{"$eq": [{"$ifNull": ["$logs.status", ""]}, "LEAVE"]}, 1, 0]}},
                "on_duty_count":        {"$sum": {"$cond": [{"$eq": [{"$ifNull": ["$logs.status", ""]}, "ON_DUTY"]}, 1, 0]}},
            }
        },
        {"$sort": {"_id": 1}},
    ]

    rows = list(db.employees.aggregate(pipeline))

    items = []
    for r in rows:
        wh_count = r.get("avg_work_hours_count", 0)
        wh_sum   = r.get("avg_work_hours_sum",   0)
        avg_wh   = round_half_up(wh_sum / wh_count, 2) if wh_count > 0 else None
        items.append({
            "department":          r["_id"],
            "headcount":           r["headcount"],
            "present_days":        round_half_up(r.get("present_days", 0), 2),
            "avg_work_hours":      avg_wh,
            "late_count":          r.get("late_count", 0),
            "total_late_minutes":  r.get("total_late_minutes", 0),
            "leave_count":         r.get("leave_count", 0),
            "on_duty_count":       r.get("on_duty_count", 0),
        })

    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Analytics – Late Leaderboard
# --------------------------------------------------------------------------- #
@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month:      str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit:      int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    db = get_db()
    m_start, m_end = _month_range(month)

    # First join logs -> employees to filter by department and get name
    pipeline: list[Any] = [
        {"$match": {"date": {"$gte": m_start, "$lte": m_end}, "late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id":                "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count":         {"$sum": 1},
            }
        },
        # Join to employees for name/department
        {
            "$lookup": {
                "from":         "employees",
                "localField":   "_id",
                "foreignField": "emp_code",
                "as":           "emp",
            }
        },
        {"$unwind": "$emp"},  # drops logs with no employee (per spec)
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline += [
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        # Compute standard competition rank
        {
            "$group": {
                "_id": None,
                "rows": {"$push": {
                    "emp_code":            "$_id",
                    "name":                "$emp.name",
                    "department":          "$emp.department",
                    "total_late_minutes":  "$total_late_minutes",
                    "late_count":          "$late_count",
                }},
            }
        },
        {"$unwind": {"path": "$rows", "includeArrayIndex": "pos"}},
        {
            "$addFields": {
                "rows.pos": "$pos",
            }
        },
        {"$replaceRoot": {"newRoot": "$rows"}},
    ]

    raw = list(db.attendance_logs.aggregate(pipeline))

    # Compute standard competition ranking in Python (ties share rank, next skips)
    result_items = []
    rank = 1
    i    = 0
    while i < len(raw):
        # Find all rows with same total_late_minutes
        j = i
        while j < len(raw) and raw[j]["total_late_minutes"] == raw[i]["total_late_minutes"]:
            j += 1
        # Assign rank to i..j-1
        for k in range(i, j):
            if rank <= limit:
                result_items.append({
                    "rank":                rank,
                    "emp_code":            raw[k]["emp_code"],
                    "name":                raw[k]["name"],
                    "department":          raw[k]["department"],
                    "total_late_minutes":  raw[k]["total_late_minutes"],
                    "late_count":          raw[k]["late_count"],
                })
        rank += (j - i)
        i = j
        if rank > limit:
            break

    return {"month": month, "items": result_items}


# --------------------------------------------------------------------------- #
# Analytics – Department Daily Trend
# --------------------------------------------------------------------------- #
@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date:  str = Query(..., alias="from"),
    to_date:    str = Query(..., alias="to"),
):
    try:
        from_d = date.fromisoformat(from_date)
        to_d   = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(422, detail="Invalid date format; use YYYY-MM-DD")

    if to_d < from_d:
        raise HTTPException(422, detail="'to' must not be before 'from'")
    if (to_d - from_d).days > 91:
        raise HTTPException(422, detail="Date range exceeds 92 days")

    db = get_db()

    # Validate department exists
    exists = db.employees.find_one({"department": department})
    if not exists:
        raise HTTPException(404, detail="Department not found")

    # Use MongoDB $densify to gap-fill days from from_date to to_date.
    # We aggregate attendance_logs joined with employees filtered by department.
    pipeline: list[Any] = [
        # Match logs in date range for the department's employees (via lookup)
        {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
        {
            "$lookup": {
                "from":         "employees",
                "localField":   "emp_code",
                "foreignField": "emp_code",
                "as":           "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        # Group by date
        {
            "$group": {
                "_id":          "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            }
        },
        # Convert date string to a real date for $densify
        {
            "$addFields": {
                "date_dt": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}},
            }
        },
        # Gap-fill: add a row for every calendar day in range
        {
            "$densify": {
                "field":  "date_dt",
                "range": {
                    "step":  1,
                    "unit":  "day",
                    "bounds": [
                        datetime(from_d.year, from_d.month, from_d.day, tzinfo=timezone.utc),
                        datetime(to_d.year,   to_d.month,   to_d.day,   tzinfo=timezone.utc) + timedelta(days=1),
                    ],
                },
            }
        },
        {
            "$addFields": {
                # Restore _id (date string) from date_dt for densified rows
                "_id": {
                    "$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}
                },
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count":    {"$ifNull": ["$late_count",    0]},
            }
        },
        {"$sort": {"_id": 1}},
    ]

    raw_days = list(db.attendance_logs.aggregate(pipeline))

    # Build headcount: for each day, count employees with joined_on <= that day
    all_emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))

    def headcount_on(day_str: str) -> int:
        return sum(1 for e in all_emps if e["joined_on"] <= day_str)

    # Index raw_days by date
    raw_by_date = {r["_id"]: r for r in raw_days}

    # Build output, computing moving_avg_7d over attendance_rate values in window
    items = []
    rate_window: list[float] = []   # non-null attendance_rate values in last 7 days (incl current)

    cur = from_d
    while cur <= to_d:
        day_str = cur.isoformat()
        raw     = raw_by_date.get(day_str, {})
        is_wd   = cur.weekday() < 5
        hc      = headcount_on(day_str)
        pc      = raw.get("present_count", 0)
        lc      = raw.get("late_count",    0)

        if is_wd and hc > 0:
            rate = round_half_up(pc / hc, 4)
        else:
            rate = None

        # moving_avg_7d: mean of non-null rates in current + up to 6 preceding rows
        # Only rates within the requested range count
        if rate is not None:
            rate_window.append(rate)
        if len(rate_window) > 7:
            rate_window.pop(0)

        # moving avg only from non-null rates in window
        non_null = [r for r in rate_window if r is not None]
        if non_null:
            ma = round_half_up(sum(non_null) / len(non_null), 4)
        else:
            ma = None

        items.append({
            "date":           day_str,
            "is_working_day": is_wd,
            "headcount":      hc,
            "present_count":  round_half_up(pc, 2),
            "late_count":     lc,
            "attendance_rate": rate,
            "moving_avg_7d":  ma,
        })
        cur += timedelta(days=1)

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin – MongoDB Explain
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint:   str,
    emp_code:   Optional[str] = None,
    month:      Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit:      int = Query(default=10, ge=1, le=50),
    date_from:  Optional[str] = None,
    date_to:    Optional[str] = None,
    status:     Optional[str] = None,
    page:       int = Query(default=1, ge=1),
    page_size:  int = Query(default=20, ge=1, le=100),
    from_date:  Optional[str] = Query(default=None, alias="from"),
    to_date:    Optional[str] = Query(default=None, alias="to"),
):
    VALID = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in VALID:
        raise HTTPException(422, detail=f"endpoint must be one of {sorted(VALID)}")

    db = get_db()

    if endpoint == "attendance_list":
        q: dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            dq: dict[str, str] = {}
            if date_from:
                dq["$gte"] = date_from
            if date_to:
                dq["$lte"] = date_to
            q["date"] = dq
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        cursor = (
            db.attendance_logs.find(q)
            .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
            .skip(skip)
            .limit(page_size)
        )
        explain = db.command(
            "explain",
            {"find": "attendance_logs", "filter": q,
             "sort": {"date": -1, "emp_code": 1},
             "skip": skip, "limit": page_size},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, detail="emp_code and month are required for employee_monthly")
        m_start, m_end = _month_range(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": m_start, "$lte": m_end}}},
            {"$group": {"_id": None, "total": {"$sum": 1}}},
        ]
        explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, detail="month is required for department_summary")
        m_start, m_end = _month_range(month)
        emp_match_: dict[str, Any] = {"joined_on": {"$lte": m_end}}
        if department:
            emp_match_["department"] = department
        pipeline = [
            {"$match": emp_match_},
            {"$group": {"_id": "$department", "emp_codes": {"$push": "$emp_code"}}},
            {"$lookup": {
                "from": "attendance_logs",
                "let":  {"codes": "$emp_codes"},
                "pipeline": [
                    {"$match": {"$expr": {"$in": ["$emp_code", "$$codes"]},
                                "date": {"$gte": m_start, "$lte": m_end}}},
                ],
                "as": "logs",
            }},
            {"$limit": 1},
        ]
        explain = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": explain}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, detail="month is required for late_leaderboard")
        m_start, m_end = _month_range(month)
        match: dict[str, Any] = {"date": {"$gte": m_start, "$lte": m_end}, "late_minutes": {"$gt": 0}}
        pipeline = [
            {"$match": match},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
            {"$sort": {"total_late_minutes": -1}},
            {"$limit": limit},
        ]
        explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, detail="department, from and to are required for department_trend")
        try:
            from_d = date.fromisoformat(from_date)
            to_d   = date.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(422, detail="Invalid date format")
        if to_d < from_d:
            raise HTTPException(422, detail="'to' must not be before 'from'")
        pipeline = [
            {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
            {"$lookup": {
                "from": "employees", "localField": "emp_code",
                "foreignField": "emp_code", "as": "emp",
            }},
            {"$unwind": "$emp"},
            {"$match": {"emp.department": department}},
            {"$group": {"_id": "$date", "count": {"$sum": 1}}},
            {"$sort": {"_id": 1}},
        ]
        explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

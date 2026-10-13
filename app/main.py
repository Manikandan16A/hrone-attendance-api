"""
Employee Attendance & Analytics API
Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded for convenience)
"""
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))

# Epoch-millisecond range guard (100_000_000_000 ms = ~1973, 4_102_444_800_000 ms = ~2100)
EPOCH_MS_MIN = 100_000_000_000
EPOCH_MS_MAX = 4_102_444_800_000

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ---------------------------------------------------------------------------
# Index creation (idempotent – safe to call on every startup)
# ---------------------------------------------------------------------------
@app.on_event("startup")
def create_indexes() -> None:
    # employees: unique business key
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="idx_emp_code")

    # attendance_logs: unique natural key
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="idx_log_empcode_date",
    )
    # for punch-out lookup (most-recent open record) + list queries
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", ASCENDING)],
        name="idx_log_empcode_punchin",
    )
    # for list / analytics sorted by date desc + emp_code asc
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="idx_log_date_desc_emp",
    )
    # analytics: month range (date prefix) + status
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("status", ASCENDING)],
        name="idx_log_date_status",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> UTC-aware datetime, truncated to whole seconds."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def _dt_to_ms(dt: datetime) -> int:
    """datetime -> epoch milliseconds integer."""
    if dt.tzinfo is None:
        # PyMongo returns naive UTC datetimes; treat them as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _parse_shift(hhmm: str) -> tuple[int, int]:
    h, m = hhmm.split(":")
    return int(h), int(m)


def _is_overnight(shift_start: str, shift_end: str) -> bool:
    """True when shift_end <= shift_start (e.g. 22:00-06:00)."""
    sh, sm = _parse_shift(shift_start)
    eh, em = _parse_shift(shift_end)
    return (eh * 60 + em) <= (sh * 60 + sm)


def _attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    Return the IST calendar date string for this punch-in, applying R1:
    For an overnight shift, a punch-in whose IST time is earlier than shift_end
    belongs to the previous day's shift.
    """
    ist_dt = _ensure_utc(punch_in_dt).astimezone(IST)
    if _is_overnight(shift_start, shift_end):
        eh, em = _parse_shift(shift_end)
        punch_time_minutes = ist_dt.hour * 60 + ist_dt.minute
        shift_end_minutes = eh * 60 + em
        if punch_time_minutes < shift_end_minutes:
            # This punch-in is after midnight, belongs to previous day's shift
            return (ist_dt.date() - timedelta(days=1)).isoformat()
    return ist_dt.date().isoformat()


def _half_up_round(value: float, decimals: int) -> float:
    """Half-up rounding (not banker's rounding)."""
    factor = 10 ** decimals
    return math.floor(value * factor + 0.5) / factor


def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    """
    R2: Grace period 10 minutes. Late only if strictly more than 10 minutes after shift_start.
    Returns floor of minutes since shift_start (not since grace period end).
    """
    ist = _ensure_utc(punch_in).astimezone(IST)
    sh, sm = _parse_shift(shift_start)
    # Build shift start on the same IST calendar day as punch-in
    shift_start_dt = ist.replace(hour=sh, minute=sm, second=0, microsecond=0)
    elapsed_seconds = (ist - shift_start_dt).total_seconds()
    elapsed_minutes = int(elapsed_seconds // 60)  # floor
    if elapsed_seconds > 10 * 60:  # strictly more than 10 minutes
        return elapsed_minutes
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, half-up to 2 decimals."""
    pi = _ensure_utc(punch_in)
    po = _ensure_utc(punch_out)
    seconds = (po - pi).total_seconds()
    return _half_up_round(seconds / 3600, 2)


def compute_overtime_minutes(punch_out: datetime, shift_end: str, attendance_date: str, shift_start: str) -> int:
    """
    R3: Whole minutes (floor) between shift_end and punch_out, only when >= 30, else 0.
    For overnight shifts, shift_end falls on the next calendar day.
    """
    po = _ensure_utc(punch_out)
    eh, em = _parse_shift(shift_end)
    base_date = datetime.fromisoformat(attendance_date)
    if _is_overnight(shift_start, shift_end):
        # shift_end is on the next calendar day
        shift_end_dt = (base_date + timedelta(days=1)).replace(
            hour=eh, minute=em, second=0, microsecond=0, tzinfo=IST
        )
    else:
        shift_end_dt = base_date.replace(hour=eh, minute=em, second=0, microsecond=0, tzinfo=IST)

    diff_minutes = int((po.astimezone(IST) - shift_end_dt).total_seconds() // 60)
    return diff_minutes if diff_minutes >= 30 else 0


def compute_derived(
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
    shift_start: str,
    shift_end: str,
    attendance_date: str,
    status: str,
) -> Dict[str, Any]:
    """Compute late_minutes, work_hours, overtime_minutes, half_day from punch times."""
    if status in ("ABSENT", "LEAVE") or punch_in is None:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}

    late = compute_late_minutes(punch_in, shift_start)

    if punch_out is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}

    wh = compute_work_hours(punch_in, punch_out)
    ot = compute_overtime_minutes(punch_out, shift_end, attendance_date, shift_start)
    hd = wh < 4.50  # R5: half_day when rounded work_hours < 4.50
    return {"late_minutes": late, "work_hours": wh, "overtime_minutes": ot, "half_day": hd}


def _to_ms_safe(v: Any) -> Any:
    if isinstance(v, datetime):
        return _dt_to_ms(v)
    return v


def _serialize_record(doc: Dict) -> Dict:
    """Convert a MongoDB attendance_logs document to the API response shape."""
    doc.pop("_id", None)
    if doc.get("punch_in") is not None:
        doc["punch_in"] = _to_ms_safe(doc["punch_in"])
    if doc.get("punch_out") is not None:
        doc["punch_out"] = _to_ms_safe(doc["punch_out"])
    # history entries
    for entry in doc.get("history", []):
        entry["at"] = _to_ms_safe(entry["at"])
        changes = entry.get("changes", {})
        for field in ("punch_in", "punch_out"):
            if field in changes:
                if changes[field].get("from") is not None:
                    changes[field]["from"] = _to_ms_safe(changes[field]["from"])
                if changes[field].get("to") is not None:
                    changes[field]["to"] = _to_ms_safe(changes[field]["to"])
    # defaults for legacy records (DATA_MODEL.md §3 sample 9)
    doc.setdefault("history", [])
    doc.setdefault("half_day", False)
    doc.setdefault("late_minutes", 0)
    doc.setdefault("overtime_minutes", 0)
    return doc


def _serialize_employee(doc: Dict) -> Dict:
    doc.pop("_id", None)
    if "created_at" in doc and doc["created_at"] is not None:
        doc["created_at"] = _dt_to_ms(doc["created_at"])
    return doc


def _validate_epoch_ms(v: Optional[int]) -> Optional[int]:
    if v is None:
        return None
    if not (EPOCH_MS_MIN <= v <= EPOCH_MS_MAX):
        raise ValueError(
            f"punched_at must be epoch milliseconds in [{EPOCH_MS_MIN}, {EPOCH_MS_MAX}]"
        )
    return v


# ---------------------------------------------------------------------------
# Pydantic Models
# ---------------------------------------------------------------------------

class EmployeeCreate(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @model_validator(mode="after")
    def shift_times_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, v):
        return _validate_epoch_ms(v)

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v not in ("PRESENT", "WFH", "ON_DUTY"):
            raise ValueError("status must be one of PRESENT, WFH, ON_DUTY")
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, v):
        return _validate_epoch_ms(v)


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    regularized_by: str

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def validate_epoch(cls, v):
        return _validate_epoch_ms(v)

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v is not None and v not in ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"):
            raise ValueError("Invalid status")
        return v

    @model_validator(mode="after")
    def validate_absent_leave(self):
        if self.status in ("ABSENT", "LEAVE"):
            if self.punch_in is not None or self.punch_out is not None:
                raise ValueError(
                    "Cannot supply punch_in or punch_out with status ABSENT or LEAVE"
                )
        return self


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")


# ---------- Employees ----------

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(tz=timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return _serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q: Dict = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    for item in items:
        _serialize_employee(item)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ---------- Attendance ----------

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    now_utc = datetime.now(tz=timezone.utc)
    if body.punched_at is not None:
        ts = _ms_to_dt(body.punched_at)
    else:
        ts = now_utc.replace(microsecond=0)

    # Truncate to whole seconds (R1)
    ts = ts.replace(microsecond=0)

    attendance_date = _attendance_date(ts, emp["shift_start"], emp["shift_end"])
    late = compute_late_minutes(ts, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": ts,
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
        raise HTTPException(409, "Already punched in for this attendance date")

    return _serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    now_utc = datetime.now(tz=timezone.utc)
    if body.punched_at is not None:
        ts = _ms_to_dt(body.punched_at)
    else:
        ts = now_utc.replace(microsecond=0)

    # Truncate to whole seconds (R1)
    ts = ts.replace(microsecond=0)

    # Find the most recent record whose punch_in is at or before punched_at and not yet punched out
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts},
            "punch_out": None,
            "status": {"$in": ["PRESENT", "WFH", "ON_DUTY"]},
        },
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(404, "No open punch-in found for this employee")

    # Validate punched_at > punch_in and within 24h (R per spec)
    punch_in_dt: datetime = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)
    if ts <= punch_in_dt:
        raise HTTPException(422, "punched_at must be after punch_in")
    if (ts - punch_in_dt).total_seconds() > 86400:
        raise HTTPException(422, "punched_at must be within 24 hours of punch_in")

    derived = compute_derived(
        punch_in_dt, ts,
        emp["shift_start"], emp["shift_end"],
        record["date"], record["status"],
    )

    # Atomic update with optimistic-concurrency guard: punch_out still null
    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": ts,
                "work_hours": derived["work_hours"],
                "overtime_minutes": derived["overtime_minutes"],
                "half_day": derived["half_day"],
            }
        },
        return_document=True,
    )
    if result is None:
        raise HTTPException(409, "Record already punched out (concurrent request)")

    return _serialize_record(result)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")

    q: Dict = {}
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
    docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    for d in docs:
        _serialize_record(d)
    return {"items": docs, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "Attendance record not found")

    # Build the new field values
    current_status = record["status"]
    new_status = body.status if body.status is not None else current_status

    current_punch_in = _ensure_utc(record.get("punch_in"))
    current_punch_out = _ensure_utc(record.get("punch_out"))

    if new_status in ("ABSENT", "LEAVE"):
        new_punch_in = None
        new_punch_out = None
    else:
        new_punch_in = _ensure_utc(_ms_to_dt(body.punch_in).replace(microsecond=0)) if body.punch_in is not None else current_punch_in
        new_punch_out = _ensure_utc(_ms_to_dt(body.punch_out).replace(microsecond=0)) if body.punch_out is not None else current_punch_out

    # Validate: presence status requires punch_in
    if new_status in ("PRESENT", "WFH", "ON_DUTY") and new_punch_in is None:
        raise HTTPException(422, "A presence status requires punch_in")

    # Validate: punch_in must remain on the attendance date (R1)
    if new_punch_in is not None:
        resulting_date = _attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if resulting_date != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")

    # Validate: punch_out must be after punch_in and within 24h
    if new_punch_in is not None and new_punch_out is not None:
        if new_punch_out <= new_punch_in:
            raise HTTPException(422, "punch_out must be after punch_in")
        if (new_punch_out - new_punch_in).total_seconds() > 86400:
            raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    # Recompute derived fields
    derived = compute_derived(
        new_punch_in, new_punch_out,
        emp["shift_start"], emp["shift_end"],
        date, new_status,
    )

    # Detect what actually changed
    old_vals: Dict[str, Any] = {
        "status": current_status,
        "punch_in": current_punch_in,
        "punch_out": current_punch_out,
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }
    new_vals: Dict[str, Any] = {
        "status": new_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        "work_hours": derived["work_hours"],
        "late_minutes": derived["late_minutes"],
        "overtime_minutes": derived["overtime_minutes"],
        "half_day": derived["half_day"],
    }

    changes: Dict[str, Any] = {}
    for field in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        ov, nv = old_vals[field], new_vals[field]
        # Normalize datetimes to UTC-aware for comparison
        if isinstance(ov, datetime) and ov.tzinfo is None:
            ov = ov.replace(tzinfo=timezone.utc)
        if isinstance(nv, datetime) and nv.tzinfo is None:
            nv = nv.replace(tzinfo=timezone.utc)
        if ov != nv:
            # Serialize datetime values as epoch ms for the changes dict stored in DB
            def _to_api(v):
                if isinstance(v, datetime):
                    return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
                return v

            changes[field] = {"from": _to_api(old_vals[field]), "to": _to_api(new_vals[field])}

    if not changes:
        raise HTTPException(422, "No changes detected")

    history_entry = {
        "at": datetime.now(tz=timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Atomic update guarded by current history length (optimistic concurrency)
    current_history_len = len(record.get("history", []))
    update_result = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            # Guard: history length unchanged ensures we don't lose concurrent edits
            f"history.{current_history_len}": {"$exists": False},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": derived["work_hours"],
                "late_minutes": derived["late_minutes"],
                "overtime_minutes": derived["overtime_minutes"],
                "half_day": derived["half_day"],
            },
            "$push": {"history": history_entry},
        },
        return_document=True,
    )
    if update_result is None:
        raise HTTPException(409, "Concurrent modification detected; please retry")

    return _serialize_record(update_result)


# ---------------------------------------------------------------------------
# Analytics helpers
# ---------------------------------------------------------------------------

def _month_date_range(month: str) -> tuple[str, str]:
    """Return (first_day, last_day) for a YYYY-MM string."""
    year, m = int(month[:4]), int(month[5:])
    first = f"{year:04d}-{m:02d}-01"
    # Last day: go to first of next month minus 1 day
    if m == 12:
        last_dt = datetime(year + 1, 1, 1) - timedelta(days=1)
    else:
        last_dt = datetime(year, m + 1, 1) - timedelta(days=1)
    last = last_dt.date().isoformat()
    return first, last


def _count_working_days_in_range(start: str, end: str) -> int:
    """Count Mon-Fri days between start and end inclusive (no holiday calendar)."""
    from datetime import date
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    count = 0
    cur = s
    while cur <= e:
        if cur.weekday() < 5:  # 0=Mon … 4=Fri
            count += 1
        cur += timedelta(days=1)
    return count


# ---------------------------------------------------------------------------
# Analytics endpoints
# ---------------------------------------------------------------------------

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    first_day, last_day = _month_date_range(month)
    joined_on = emp["joined_on"]

    # R7: working days counted from joined_on if joined mid-month
    effective_start = max(first_day, joined_on)
    working_days = _count_working_days_in_range(effective_start, last_day) if effective_start <= last_day else 0

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": first_day, "$lte": last_day},
            }
        },
        {
            "$group": {
                "_id": None,
                # present_days: Mon-Fri only; half_day=0.5, full=1.0 (R5-R7)
                # We compute per-record and sum
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    # Check day of week: $dayOfWeek 1=Sun…7=Sat → Mon=2…Fri=6
                                    {
                                        "$and": [
                                            {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
                                            {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0,
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

    results = list(db.attendance_logs.aggregate(pipeline))
    if results:
        r = results[0]
        present_days = _half_up_round(r["present_days"], 2)
        leave_days = r["leave_days"]
        late_count = r["late_count"]
        total_late = r["total_late_minutes"]
        total_ot = r["total_overtime_minutes"]
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        attendance_pct = _half_up_round(present_days / working_days * 100, 2)
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


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    first_day, last_day = _month_date_range(month)

    emp_match: Dict = {"joined_on": {"$lte": last_day}}
    if department:
        emp_match["department"] = department

    # Step 1: headcount per department (employees with joined_on <= last_day of month)
    headcount_pipeline = [
        {"$match": emp_match},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}, "emp_codes": {"$push": "$emp_code"}}},
    ]
    dept_headcounts = {r["_id"]: r for r in db.employees.aggregate(headcount_pipeline)}

    if not dept_headcounts:
        return {"month": month, "items": []}

    # Step 2: aggregate attendance logs for these employees in the month
    all_emp_codes = []
    for dept_data in dept_headcounts.values():
        all_emp_codes.extend(dept_data["emp_codes"])

    log_match: Dict = {
        "emp_code": {"$in": all_emp_codes},
        "date": {"$gte": first_day, "$lte": last_day},
    }

    log_pipeline = [
        {"$match": log_match},
        # Lookup employee department
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {
            "$group": {
                "_id": "$emp.department",
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$and": [
                                        {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
                                        {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
                                    ]},
                                ]
                            },
                            {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "avg_work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {"$and": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$isNumber": "$work_hours"},
                            ]},
                            "$work_hours",
                            0,
                        ]
                    }
                },
                "avg_work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {"$and": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$isNumber": "$work_hours"},
                            ]},
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            }
        },
    ]
    log_results = {r["_id"]: r for r in db.attendance_logs.aggregate(log_pipeline)}

    items = []
    for dept_name in sorted(dept_headcounts.keys()):
        hc_data = dept_headcounts[dept_name]
        lr = log_results.get(dept_name, {})
        present_days = _half_up_round(lr.get("present_days", 0), 2)
        awh_sum = lr.get("avg_work_hours_sum", 0)
        awh_count = lr.get("avg_work_hours_count", 0)
        avg_wh = _half_up_round(awh_sum / awh_count, 2) if awh_count > 0 else None
        items.append({
            "department": dept_name,
            "headcount": hc_data["headcount"],
            "present_days": present_days,
            "avg_work_hours": avg_wh,
            "late_count": lr.get("late_count", 0),
            "total_late_minutes": lr.get("total_late_minutes", 0),
            "leave_count": lr.get("leave_count", 0),
            "on_duty_count": lr.get("on_duty_count", 0),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    first_day, last_day = _month_date_range(month)

    # Build employee filter
    emp_match: Dict = {}
    if department:
        emp_match["department"] = department

    pipeline = [
        # Start from attendance_logs
        {
            "$match": {
                "date": {"$gte": first_day, "$lte": last_day},
                "late_minutes": {"$gt": 0},
            }
        },
        # Group by emp_code
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        # Join with employees
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        # Filter by department if given
        *(
            [{"$match": {"emp.department": department}}]
            if department
            else []
        ),
        # Sort: total_late_minutes desc, emp_code asc
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        # Project fields we need for ranking
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))

    # Standard competition ranking
    ranked = []
    rank = 1
    for i, row in enumerate(rows):
        if i > 0 and row["total_late_minutes"] < rows[i - 1]["total_late_minutes"]:
            rank = i + 1
        row["rank"] = rank
        ranked.append(row)

    # Apply limit: include all rows whose rank <= limit
    filtered = [r for r in ranked if r["rank"] <= limit]

    return {"month": month, "items": filtered}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(alias="from"),
    to_date: str = Query(alias="to"),
):
    if to_date < from_date:
        raise HTTPException(422, "to must not be before from")

    from datetime import date as date_type
    fd = date_type.fromisoformat(from_date)
    td = date_type.fromisoformat(to_date)
    if (td - fd).days >= 92:
        raise HTTPException(422, "Date range must not exceed 92 days")

    # Verify department exists
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, f"Department '{department}' not found")

    # Use $densify to guarantee one row per calendar day
    pipeline = [
        # Match logs in range for the department
        {
            "$match": {
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        # Join with employees to filter by department
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
        # Convert date string to a real date for $densify
        {
            "$addFields": {
                "date_dt": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}},
            }
        },
        # Group by date
        {
            "$group": {
                "_id": "$date_dt",
                "date_str": {"$first": "$date"},
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
            }
        },
        # Densify: fill in missing calendar days
        {
            "$densify": {
                "field": "_id",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc),
                        datetime.fromisoformat(to_date).replace(tzinfo=timezone.utc) + timedelta(days=1),
                    ],
                },
            }
        },
        # Fill nulls introduced by densify
        {
            "$addFields": {
                "date_str": {
                    "$ifNull": [
                        "$date_str",
                        {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
                    ]
                },
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {"$sort": {"_id": 1}},
    ]

    rows_raw = list(db.attendance_logs.aggregate(pipeline))

    # Build a per-date headcount lookup (employees with joined_on <= day)
    # We do this in Python for simplicity (range is ≤92 days)
    all_dept_emps = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1, "_id": 0}))

    result_items = []
    # Sliding window for 7-day moving average
    non_null_rates: List[Optional[float]] = []

    cur = fd
    row_map = {r["date_str"]: r for r in rows_raw}

    while cur <= td:
        cur_str = cur.isoformat()
        is_working = cur.weekday() < 5  # Mon-Fri

        headcount = sum(1 for e in all_dept_emps if e["joined_on"] <= cur_str)
        row = row_map.get(cur_str, {})
        present_count = _half_up_round(row.get("present_count", 0), 4)
        late_count = row.get("late_count", 0)

        if is_working and headcount > 0:
            attendance_rate = _half_up_round(present_count / headcount, 4)
        else:
            attendance_rate = None

        non_null_rates.append(attendance_rate)
        # 7-day window (current + up to 6 preceding rows inside range)
        window = [r for r in non_null_rates[-7:] if r is not None]
        moving_avg = _half_up_round(sum(window) / len(window), 4) if window else None

        result_items.append({
            "date": cur_str,
            "is_working_day": is_working,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": attendance_rate,
            "moving_avg_7d": moving_avg,
        })
        cur += timedelta(days=1)

    return {"department": department, "items": result_items}


# ---------------------------------------------------------------------------
# Admin explain endpoint
# ---------------------------------------------------------------------------

ENDPOINT_NAMES = {
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if endpoint not in ENDPOINT_NAMES:
        raise HTTPException(422, f"Unknown endpoint: {endpoint}")

    if endpoint == "attendance_list":
        q: Dict = {}
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
        explain_result = db.command(
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
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_result}

    elif endpoint == "employee_monthly":
        if not month:
            raise HTTPException(422, "month is required for employee_monthly")
        if not emp_code:
            raise HTTPException(422, "emp_code is required for employee_monthly")
        first_day, last_day = _month_date_range(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}},
            {"$group": {"_id": None, "total_late_minutes": {"$sum": "$late_minutes"}}},
        ]
        explain_result = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_result}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary")
        first_day, last_day = _month_date_range(month)
        emp_match: Dict = {"joined_on": {"$lte": last_day}}
        if department:
            emp_match["department"] = department
        pipeline = [
            {"$match": emp_match},
            {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
        ]
        explain_result = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_result}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard")
        first_day, last_day = _month_date_range(month)
        pipeline = [
            {"$match": {"date": {"$gte": first_day, "$lte": last_day}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
        ]
        explain_result = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_result}

    elif endpoint == "department_trend":
        if not from_date or not to_date:
            raise HTTPException(422, "from and to are required for department_trend")
        if not department:
            raise HTTPException(422, "department is required for department_trend")
        pipeline = [
            {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
            {"$unwind": "$emp"},
            {"$match": {"emp.department": department}},
            {"$group": {"_id": "$date", "present_count": {"$sum": 1}}},
        ]
        explain_result = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_result}

    raise HTTPException(422, "Unhandled endpoint")

"""
Employee Attendance & Analytics API
Built with FastAPI & MongoDB.
"""

import calendar
import json
import os
from contextlib import asynccontextmanager
from datetime import date as dt_date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

# MongoDB configuration
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_indexes():
    """Create all required indexes at application startup idempotently."""
    # employees indexes
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    db.employees.create_index([("joined_on", 1), ("department", 1)])

    # attendance_logs indexes
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", -1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers & Rules (R1 - R8)
# --------------------------------------------------------------------------- #

def round_half_up(value: float | Decimal, decimals: int = 2) -> float:
    """Standard half-up rounding (R8)."""
    d = Decimal(str(value))
    target = Decimal("10") ** -decimals
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert a datetime to epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def from_epoch_ms(ms: int) -> datetime:
    """
    Convert epoch milliseconds to a UTC datetime truncated to whole seconds (R1).
    """
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def compute_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Attendance day is the IST calendar date of punch_in.
    Exception: For overnight shifts (shift_end <= shift_start), a punch-in whose
    IST time is strictly earlier than shift_end belongs to the shift that started
    the previous calendar day.
    """
    punch_in_ist = punch_in_dt.astimezone(IST)
    is_overnight = shift_end <= shift_start
    punch_hm = punch_in_ist.strftime("%H:%M")
    punch_cal_date = punch_in_ist.date()

    if is_overnight and punch_hm < shift_end:
        return (punch_cal_date - timedelta(days=1)).isoformat()
    return punch_cal_date.isoformat()


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, attendance_date: str) -> int:
    """
    R2: Grace period is strictly 10 minutes.
    A punch-in is late only if strictly more than 10:00 after shift_start.
    When late, late_minutes = floor elapsed minutes since shift_start.
    """
    start_h, start_m = map(int, shift_start.split(":"))
    y, m, d = map(int, attendance_date.split("-"))
    shift_start_dt = datetime(y, m, d, start_h, start_m, 0, tzinfo=IST)

    diff_seconds = (punch_in_dt.astimezone(IST) - shift_start_dt).total_seconds()
    if diff_seconds > 600:
        return int(diff_seconds // 60)
    return 0


def compute_overtime(punch_out_dt: datetime, shift_start: str, shift_end: str, attendance_date: str) -> int:
    """
    R3: Overtime = floor minutes between shift_end and punch_out,
    counted only when >= 30 minutes; otherwise 0.
    For overnight shifts, shift_end falls on the day after attendance_date.
    """
    end_h, end_m = map(int, shift_end.split(":"))
    y, m, d = map(int, attendance_date.split("-"))
    is_overnight = shift_end <= shift_start

    shift_end_dt = datetime(y, m, d, end_h, end_m, 0, tzinfo=IST)
    if is_overnight:
        shift_end_dt += timedelta(days=1)

    diff_seconds = (punch_out_dt.astimezone(IST) - shift_end_dt).total_seconds()
    if diff_seconds >= 1800:
        return int(diff_seconds // 60)
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """R4: Work hours = (punch_out - punch_in) in seconds / 3600, rounded 2 decimals half-up."""
    seconds = (punch_out_dt - punch_in_dt).total_seconds()
    val = Decimal(str(seconds)) / Decimal("3600")
    return round_half_up(val, 2)


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: Half day is true when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def serialize_employee(doc: dict) -> dict:
    """Convert employee document to OpenAPI schema format."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_ms(doc.get("created_at")),
    }


def serialize_attendance(doc: dict) -> dict:
    """Convert attendance record to OpenAPI schema format."""
    history_api = []
    for h in doc.get("history", []):
        changes_api = {}
        for field, change in h.get("changes", {}).items():
            f_val = change.get("from")
            t_val = change.get("to")
            if field in ("punch_in", "punch_out"):
                if isinstance(f_val, datetime):
                    f_val = to_epoch_ms(f_val)
                if isinstance(t_val, datetime):
                    t_val = to_epoch_ms(t_val)
            changes_api[field] = {"from": f_val, "to": t_val}

        h_at = h.get("at")
        if isinstance(h_at, datetime):
            h_at = to_epoch_ms(h_at)

        history_api.append({
            "at": h_at,
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": changes_api,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_api,
    }


# --------------------------------------------------------------------------- #
# Request Schemas
# --------------------------------------------------------------------------- #

class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    status: str = Field(default="PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)


class RegularizeIn(BaseModel):
    status: Optional[str] = Field(default=None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# System Endpoints
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    """Readiness probe: Returns 200 only if MongoDB answers a ping, else 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


# --------------------------------------------------------------------------- #
# Employee Endpoints
# --------------------------------------------------------------------------- #

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(status_code=422, detail="shift_start must differ from shift_end")

    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [serialize_employee(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance Endpoints
# --------------------------------------------------------------------------- #

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")

    if body.punched_at is not None:
        ts = from_epoch_ms(body.punched_at)
    else:
        ts = datetime.now(timezone.utc).replace(microsecond=0)

    attendance_date = compute_attendance_date(ts, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(ts, emp["shift_start"], attendance_date)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return serialize_attendance(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")

    if body.punched_at is not None:
        out_dt = from_epoch_ms(body.punched_at)
    else:
        out_dt = datetime.now(timezone.utc).replace(microsecond=0)

    # Find the most recent record whose punch_in <= punched_at
    latest = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": out_dt},
        },
        sort=[("punch_in", -1)],
    )

    if not latest:
        raise HTTPException(status_code=404, detail="no matching punch-in found")

    if latest.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="already punched out")

    in_dt = latest["punch_in"]
    if in_dt.tzinfo is None:
        in_dt = in_dt.replace(tzinfo=timezone.utc)

    if out_dt <= in_dt:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in")

    duration_sec = (out_dt - in_dt).total_seconds()
    if duration_sec > 86400:
        raise HTTPException(status_code=422, detail="punched_at cannot be more than 24 hours after punch_in")

    work_hrs = compute_work_hours(in_dt, out_dt)
    overtime = compute_overtime(out_dt, emp["shift_start"], emp["shift_end"], latest["date"])
    half_day = compute_half_day(work_hrs)

    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": latest["_id"],
            "punch_out": None,  # atomic guard against double punch-out race condition
        },
        {
            "$set": {
                "punch_out": out_dt,
                "work_hours": work_hrs,
                "overtime_minutes": overtime,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="already punched out")

    return serialize_attendance(updated)


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
        raise HTTPException(status_code=422, detail="date_from must not be after date_to")

    q: Dict[str, Any] = {}
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
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [serialize_attendance(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeIn = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(status_code=404, detail="attendance record not found")

    target_status = body.status if body.status is not None else rec["status"]

    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="punch_in and punch_out cannot be supplied when status is ABSENT or LEAVE",
            )
        target_punch_in = None
        target_punch_out = None
    else:
        target_punch_in = from_epoch_ms(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
        target_punch_out = from_epoch_ms(body.punch_out) if body.punch_out is not None else rec.get("punch_out")

        if target_punch_in is None:
            raise HTTPException(status_code=422, detail="presence status requires punch_in")

        # punch_in must stay on the record's attendance date (R1)
        calc_date = compute_attendance_date(target_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in corresponds to attendance date {calc_date}, which does not match record date {date}",
            )

        if target_punch_out is not None:
            if target_punch_out <= target_punch_in:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
            if (target_punch_out - target_punch_in).total_seconds() > 86400:
                raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    # Recompute derived fields
    if target_status in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(target_punch_in, emp["shift_start"], date)
        if target_punch_out is not None:
            new_work_hours = compute_work_hours(target_punch_in, target_punch_out)
            new_overtime_minutes = compute_overtime(target_punch_out, emp["shift_start"], emp["shift_end"], date)
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Detect what actually changed
    old_status = rec.get("status")
    old_punch_in = rec.get("punch_in")
    old_punch_out = rec.get("punch_out")
    old_work_hours = rec.get("work_hours")
    old_late_minutes = rec.get("late_minutes", 0)
    old_overtime_minutes = rec.get("overtime_minutes", 0)
    old_half_day = rec.get("half_day", False)

    changes_db = {}
    if target_status != old_status:
        changes_db["status"] = {"from": old_status, "to": target_status}

    def dt_equal(d1: Optional[datetime], d2: Optional[datetime]) -> bool:
        if d1 is None and d2 is None:
            return True
        if d1 is None or d2 is None:
            return False
        return int(d1.timestamp()) == int(d2.timestamp())

    if not dt_equal(target_punch_in, old_punch_in):
        changes_db["punch_in"] = {"from": old_punch_in, "to": target_punch_in}

    if not dt_equal(target_punch_out, old_punch_out):
        changes_db["punch_out"] = {"from": old_punch_out, "to": target_punch_out}

    if new_work_hours != old_work_hours:
        changes_db["work_hours"] = {"from": old_work_hours, "to": new_work_hours}

    if new_late_minutes != old_late_minutes:
        changes_db["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}

    if new_overtime_minutes != old_overtime_minutes:
        changes_db["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}

    if new_half_day != old_half_day:
        changes_db["half_day"] = {"from": old_half_day, "to": new_half_day}

    if not changes_db:
        raise HTTPException(status_code=422, detail="no changes detected")

    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_db,
    }

    curr_history_len = len(rec.get("history", []))
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            f"history.{curr_history_len}": {"$exists": False},  # optimistic concurrency guard
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": target_punch_in,
                "punch_out": target_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="concurrent update conflict")

    return serialize_attendance(updated)


# --------------------------------------------------------------------------- #
# Analytics Endpoints (100% MongoDB Aggregation Pipelines)
# --------------------------------------------------------------------------- #

def build_employee_monthly_pipeline(emp_code: str, month: str) -> List[Dict[str, Any]]:
    year, month_int = map(int, month.split("-"))
    last_day = calendar.monthrange(year, month_int)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date},
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
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$lte": [
                                            {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                                            5,
                                        ]
                                    },
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")

    year, month_int = map(int, month.split("-"))
    last_day = calendar.monthrange(year, month_int)[1]

    # Calculate working days (Mon-Fri) on or after joined_on
    working_days = 0
    for day in range(1, last_day + 1):
        cal_date = dt_date(year, month_int, day)
        if cal_date.weekday() < 5 and cal_date.isoformat() >= emp["joined_on"]:
            working_days += 1

    pipeline = build_employee_monthly_pipeline(emp_code, month)
    agg_res = list(db.attendance_logs.aggregate(pipeline))

    if agg_res:
        stats = agg_res[0]
        present_days = stats["present_days"]
        leave_days = stats["leave_days"]
        late_count = stats["late_count"]
        total_late = stats["total_late_minutes"]
        total_ot = stats["total_overtime_minutes"]
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    attendance_pct = None
    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)

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


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> List[Dict[str, Any]]:
    year, month_int = map(int, month.split("-"))
    last_day = calendar.monthrange(year, month_int)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    match_stage: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
    if department:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ifNull": ["$logs", False]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$lte": [
                                            {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                                            5,
                                        ]
                                    },
                                ]
                            },
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "work_hours_list": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ifNull": ["$logs", False]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            "$$REMOVE",
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ifNull": ["$logs", False]},
                                    {"$gt": ["$logs.late_minutes", 0]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {"$ifNull": ["$logs", False]},
                            {"$ifNull": ["$logs.late_minutes", 0]},
                            0,
                        ]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ifNull": ["$logs", False]},
                                    {"$eq": ["$logs.status", "LEAVE"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ifNull": ["$logs", False]},
                                    {"$eq": ["$logs.status", "ON_DUTY"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": {"$size": "$employees"},
                "present_days": "$present_days",
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$work_hours_list"}, 0]},
                        {"$avg": "$work_hours_list"},
                        None,
                    ]
                },
                "late_count": "$late_count",
                "total_late_minutes": "$total_late_minutes",
                "leave_count": "$leave_count",
                "on_duty_count": "$on_duty_count",
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    pipeline = build_department_summary_pipeline(month, department)
    results = list(db.employees.aggregate(pipeline))

    items = []
    for r in results:
        avg_wh = r["avg_work_hours"]
        if avg_wh is not None:
            avg_wh = round_half_up(avg_wh, 2)

        items.append({
            "department": r["department"],
            "headcount": r["headcount"],
            "present_days": r["present_days"],
            "avg_work_hours": avg_wh,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })

    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> List[Dict[str, Any]]:
    year, month_int = map(int, month.split("-"))
    last_day = calendar.monthrange(year, month_int)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
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

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$project": {
                "_id": 0,
                "rank": "$rank",
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": "$total_late_minutes",
                "late_count": "$late_count",
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1,
            }
        },
    ])
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


def build_department_trend_pipeline(department: str, from_: str, to: str) -> List[Dict[str, Any]]:
    from_dt = datetime.strptime(from_, "%Y-%m-%d")
    to_dt = datetime.strptime(to, "%Y-%m-%d")
    to_dt_plus_one = to_dt + timedelta(days=1)

    return [
        {
            "$match": {
                "date": {"$gte": from_, "$lte": to},
            }
        },
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
            "$project": {
                "date_dt": {"$dateFromString": {"dateString": "$date"}},
                "date": "$date",
                "status": "$status",
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
            }
        },
        {
            "$group": {
                "_id": "$date_dt",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }
                },
            }
        },
        # Ensure at least one seed document exists so $densify covers all days even if range has 0 logs
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {
                        "$project": {
                            "_id": from_dt,
                            "present_count": {"$literal": 0.0},
                            "late_count": {"$literal": 0},
                        }
                    },
                ],
            }
        },
        {
            "$group": {
                "_id": "$_id",
                "present_count": {"$max": "$present_count"},
                "late_count": {"$max": "$late_count"},
            }
        },
        {
            "$densify": {
                "field": "_id",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_plus_one],
                },
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "dayOfWeek": {"$isoDayOfWeek": "$_id"},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"d_str": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$d_str"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc",
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$date",
                "is_working_day": {"$lte": ["$dayOfWeek", 5]},
                "headcount": {"$ifNull": [{"$first": "$hc.count"}, 0]},
                "present_count": "$present_count",
                "late_count": "$late_count",
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$round": [
                                {"$divide": ["$present_count", "$headcount"]},
                                4,
                            ]
                        },
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0],
                        },
                    }
                },
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d", None]},
                        {"$round": ["$moving_avg_7d", 4]},
                        None,
                    ]
                },
            }
        },
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    try:
        from_date = datetime.strptime(from_, "%Y-%m-%d").date()
        to_date = datetime.strptime(to, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=422, detail="invalid date format")

    if to_date < from_date:
        raise HTTPException(status_code=422, detail="to must be on or after from")

    num_days = (to_date - from_date).days + 1
    if num_days > 92:
        raise HTTPException(status_code=422, detail="date range cannot exceed 92 days")

    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(status_code=404, detail="department not found")

    pipeline = build_department_trend_pipeline(department, from_, to)
    items = list(db.attendance_logs.aggregate(pipeline))

    return {
        "department": department,
        "items": items,
    }


# --------------------------------------------------------------------------- #
# Admin Diagnostic Endpoint
# --------------------------------------------------------------------------- #

def sanitize_explain(doc: dict) -> dict:
    return json.loads(json_util.dumps(doc))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
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
    Runs MongoDB's explain with executionStats verbosity for the given endpoint's query/pipeline.
    """
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must not be after date_to")
        q: Dict[str, Any] = {}
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
        command = {
            "explain": {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        }
        res = db.command(command)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": sanitize_explain(res)}

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(status_code=422, detail="emp_code is required for employee_monthly")
        if not month:
            raise HTTPException(status_code=422, detail="month is required for employee_monthly")

        pipeline = build_employee_monthly_pipeline(emp_code, month)
        command = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(command)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": sanitize_explain(res)}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")

        pipeline = build_department_summary_pipeline(month, department)
        command = {
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(command)
        return {"endpoint": endpoint, "collection": "employees", "explain": sanitize_explain(res)}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")

        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        command = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(command)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": sanitize_explain(res)}

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(status_code=422, detail="department is required for department_trend")
        if not from_ or not to:
            raise HTTPException(status_code=422, detail="from and to are required for department_trend")
        if to < from_:
            raise HTTPException(status_code=422, detail="to must be on or after from")

        pipeline = build_department_trend_pipeline(department, from_, to)
        command = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(command)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": sanitize_explain(res)}

    raise HTTPException(status_code=422, detail="unknown endpoint")

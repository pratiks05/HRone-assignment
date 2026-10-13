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
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]
IST = timezone(timedelta(hours=5, minutes=30))


def init_indexes():
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    db.employees.create_index([("joined_on", 1), ("department", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers & Rules (R1 - R8)
# --------------------------------------------------------------------------- #

def round_half_up(val: float | Decimal, dec: int = 2) -> float:
    return float(Decimal(str(val)).quantize(Decimal("10") ** -dec, rounding=ROUND_HALF_UP))


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    if not dt:
        return None
    return int((dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt).timestamp() * 1000)


def from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def get_att_date(punch_dt: datetime, s_start: str, s_end: str) -> str:
    ist_dt = punch_dt.astimezone(IST)
    if s_end <= s_start and ist_dt.strftime("%H:%M") < s_end:
        return (ist_dt.date() - timedelta(days=1)).isoformat()
    return ist_dt.date().isoformat()


def get_late(punch_dt: datetime, s_start: str, d_str: str) -> int:
    sh, sm = map(int, s_start.split(":"))
    y, m, d = map(int, d_str.split("-"))
    diff = (punch_dt.astimezone(IST) - datetime(y, m, d, sh, sm, 0, tzinfo=IST)).total_seconds()
    return int(diff // 60) if diff > 600 else 0


def get_overtime(punch_dt: datetime, s_start: str, s_end: str, d_str: str) -> int:
    eh, em = map(int, s_end.split(":"))
    y, m, d = map(int, d_str.split("-"))
    end_dt = datetime(y, m, d, eh, em, 0, tzinfo=IST) + (timedelta(days=1) if s_end <= s_start else timedelta(0))
    diff = (punch_dt.astimezone(IST) - end_dt).total_seconds()
    return int(diff // 60) if diff >= 1800 else 0


def get_work_hours(p_in: datetime, p_out: datetime) -> float:
    return round_half_up((p_out - p_in).total_seconds() / 3600, 2)


def ser_emp(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"], "name": doc["name"], "email": doc["email"],
        "department": doc["department"], "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"], "joined_on": doc["joined_on"],
        "created_at": to_ms(doc.get("created_at")),
    }


def ser_att(doc: dict) -> dict:
    history = []
    for h in doc.get("history", []):
        changes = {}
        for k, v in h.get("changes", {}).items():
            f, t = v.get("from"), v.get("to")
            if k in ("punch_in", "punch_out"):
                f = to_ms(f) if isinstance(f, datetime) else f
                t = to_ms(t) if isinstance(t, datetime) else t
            changes[k] = {"from": f, "to": t}
        history.append({
            "at": to_ms(h["at"]) if isinstance(h["at"], datetime) else h["at"],
            "by": h.get("by"), "reason": h.get("reason"), "changes": changes,
        })
    return {
        "emp_code": doc["emp_code"], "date": doc["date"], "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")), "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"), "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0), "half_day": doc.get("half_day", False),
        "history": history,
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
# Endpoints
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start must differ from shift_end")
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return ser_emp(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = {"department": department} if department else {}
    items = list(db.employees.find(q).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size))
    return {
        "items": [ser_emp(d) for d in items],
        "total": db.employees.count_documents(q),
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    ts = from_ms(body.punched_at) if body.punched_at else datetime.now(timezone.utc).replace(microsecond=0)
    att_date = get_att_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code, "date": att_date, "status": body.status,
        "punch_in": ts, "punch_out": None, "work_hours": None,
        "late_minutes": get_late(ts, emp["shift_start"], att_date),
        "overtime_minutes": 0, "half_day": False, "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return ser_att(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    out_dt = from_ms(body.punched_at) if body.punched_at else datetime.now(timezone.utc).replace(microsecond=0)
    latest = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": out_dt}},
        sort=[("punch_in", -1)],
    )
    if not latest:
        raise HTTPException(404, "no matching punch-in found")
    if latest.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    in_dt = latest["punch_in"].replace(tzinfo=timezone.utc) if latest["punch_in"].tzinfo is None else latest["punch_in"]
    if out_dt <= in_dt:
        raise HTTPException(422, "punched_at must be after punch_in")
    if (out_dt - in_dt).total_seconds() > 86400:
        raise HTTPException(422, "punched_at cannot be more than 24 hours after punch_in")

    wh = get_work_hours(in_dt, out_dt)
    ot = get_overtime(out_dt, emp["shift_start"], emp["shift_end"], latest["date"])
    updated = db.attendance_logs.find_one_and_update(
        {"_id": latest["_id"], "punch_out": None},
        {"$set": {"punch_out": out_dt, "work_hours": wh, "overtime_minutes": ot, "half_day": wh < 4.50}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "already punched out")
    return ser_att(updated)


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

    q: Dict[str, Any] = {}
    if emp_code: q["emp_code"] = emp_code
    if status: q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from: q["date"]["$gte"] = date_from
        if date_to: q["date"]["$lte"] = date_to

    items = list(db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size))
    return {
        "items": [ser_att(d) for d in items],
        "total": db.attendance_logs.count_documents(q),
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
    if not emp: raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec: raise HTTPException(404, "attendance record not found")

    target_status = body.status or rec["status"]
    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in or body.punch_out:
            raise HTTPException(422, "punch_in and punch_out cannot be supplied when status is ABSENT or LEAVE")
        p_in, p_out, wh, late, ot, half = None, None, None, 0, 0, False
    else:
        p_in = from_ms(body.punch_in) if body.punch_in else rec.get("punch_in")
        p_out = from_ms(body.punch_out) if body.punch_out else rec.get("punch_out")
        if not p_in:
            raise HTTPException(422, "presence status requires punch_in")
        if get_att_date(p_in, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, "punch_in does not match record attendance date")
        if p_out:
            if p_out <= p_in: raise HTTPException(422, "punch_out must be after punch_in")
            if (p_out - p_in).total_seconds() > 86400: raise HTTPException(422, "punch_out > 24 hours after punch_in")
            wh = get_work_hours(p_in, p_out)
            ot = get_overtime(p_out, emp["shift_start"], emp["shift_end"], date)
            half = wh < 4.50
        else:
            wh, ot, half = None, 0, False
        late = get_late(p_in, emp["shift_start"], date)

    def dt_eq(a, b):
        return (a is None and b is None) or (bool(a and b) and int(a.timestamp()) == int(b.timestamp()))

    changes = {}
    if target_status != rec.get("status"): changes["status"] = {"from": rec.get("status"), "to": target_status}
    if not dt_eq(p_in, rec.get("punch_in")): changes["punch_in"] = {"from": rec.get("punch_in"), "to": p_in}
    if not dt_eq(p_out, rec.get("punch_out")): changes["punch_out"] = {"from": rec.get("punch_out"), "to": p_out}
    if wh != rec.get("work_hours"): changes["work_hours"] = {"from": rec.get("work_hours"), "to": wh}
    if late != rec.get("late_minutes", 0): changes["late_minutes"] = {"from": rec.get("late_minutes", 0), "to": late}
    if ot != rec.get("overtime_minutes", 0): changes["overtime_minutes"] = {"from": rec.get("overtime_minutes", 0), "to": ot}
    if half != rec.get("half_day", False): changes["half_day"] = {"from": rec.get("half_day", False), "to": half}
    if not changes: raise HTTPException(422, "no changes detected")

    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by, "reason": body.reason, "changes": changes,
    }
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], f"history.{len(rec.get('history', []))}": {"$exists": False}},
        {
            "$set": {
                "status": target_status, "punch_in": p_in, "punch_out": p_out,
                "work_hours": wh, "late_minutes": late, "overtime_minutes": ot, "half_day": half,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated: raise HTTPException(409, "concurrent update conflict")
    return ser_att(updated)


# --------------------------------------------------------------------------- #
# Analytics Endpoints (MongoDB Aggregations)
# --------------------------------------------------------------------------- #

def build_employee_monthly_pipeline(emp_code: str, month: str) -> List[Dict[str, Any]]:
    y, m = map(int, month.split("-"))
    last_d = calendar.monthrange(y, m)[1]
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": f"{month}-01", "$lte": f"{month}-{last_d:02d}"}}},
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str = Path(...), month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "employee not found")

    y, m = map(int, month.split("-"))
    last_d = calendar.monthrange(y, m)[1]
    working_days = sum(1 for d in range(1, last_d + 1) if dt_date(y, m, d).weekday() < 5 and dt_date(y, m, d).isoformat() >= emp["joined_on"])

    agg = list(db.attendance_logs.aggregate(build_employee_monthly_pipeline(emp_code, month)))
    stats = agg[0] if agg else {"present_days": 0.0, "leave_days": 0, "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0}
    pct = round_half_up((stats["present_days"] / working_days) * 100, 2) if working_days > 0 else None

    return {
        "emp_code": emp_code, "month": month, "working_days": working_days,
        "present_days": stats["present_days"], "leave_days": stats["leave_days"],
        "late_count": stats["late_count"], "total_late_minutes": stats["total_late_minutes"],
        "total_overtime_minutes": stats["total_overtime_minutes"], "attendance_pct": pct,
    }


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> List[Dict[str, Any]]:
    y, m = map(int, month.split("-"))
    end_d = f"{month}-{calendar.monthrange(y, m)[1]:02d}"
    match_emp: Dict[str, Any] = {"joined_on": {"$lte": end_d}}
    if department: match_emp["department"] = department

    return [
        {"$match": match_emp},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$emp"]}, {"$gte": ["$date", f"{month}-01"]}, {"$lte": ["$date", end_d]}]}}}
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
                            {"$and": [{"$ifNull": ["$logs", False]}, {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 5]}]},
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "work_hours_list": {
                    "$push": {
                        "$cond": [
                            {"$and": [{"$ifNull": ["$logs", False]}, {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$ne": ["$logs.work_hours", None]}]},
                            "$logs.work_hours", "$$REMOVE",
                        ]
                    }
                },
                "late_count": {"$sum": {"$cond": [{"$and": [{"$ifNull": ["$logs", False]}, {"$gt": ["$logs.late_minutes", 0]}]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$cond": [{"$ifNull": ["$logs", False]}, {"$ifNull": ["$logs.late_minutes", 0]}, 0]}},
                "leave_count": {"$sum": {"$cond": [{"$and": [{"$ifNull": ["$logs", False]}, {"$eq": ["$logs.status", "LEAVE"]}]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$and": [{"$ifNull": ["$logs", False]}, {"$eq": ["$logs.status", "ON_DUTY"]}]}, 1, 0]}},
            }
        },
        {
            "$project": {
                "department": "$_id", "headcount": {"$size": "$employees"},
                "present_days": "$present_days",
                "avg_work_hours": {"$cond": [{"$gt": [{"$size": "$work_hours_list"}, 0]}, {"$avg": "$work_hours_list"}, None]},
                "late_count": "$late_count", "total_late_minutes": "$total_late_minutes",
                "leave_count": "$leave_count", "on_duty_count": "$on_duty_count",
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    res = list(db.employees.aggregate(build_department_summary_pipeline(month, department)))
    for r in res:
        if r["avg_work_hours"] is not None:
            r["avg_work_hours"] = round_half_up(r["avg_work_hours"], 2)
    return {"month": month, "items": res}


def build_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> List[Dict[str, Any]]:
    y, m = map(int, month.split("-"))
    end_d = f"{month}-{calendar.monthrange(y, m)[1]:02d}"
    pipeline: List[Dict[str, Any]] = [
        {"$match": {"date": {"$gte": f"{month}-01", "$lte": end_d}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline.extend([
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$project": {
                "_id": 0, "rank": "$rank", "emp_code": "$_id",
                "name": "$emp.name", "department": "$emp.department",
                "total_late_minutes": "$total_late_minutes", "late_count": "$late_count",
            }
        },
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    return {"month": month, "items": list(db.attendance_logs.aggregate(build_late_leaderboard_pipeline(month, limit, department)))}


def build_department_trend_pipeline(department: str, from_: str, to: str) -> List[Dict[str, Any]]:
    from_dt = datetime.strptime(from_, "%Y-%m-%d")
    to_dt = datetime.strptime(to, "%Y-%m-%d")
    return [
        {"$match": {"date": {"$gte": from_, "$lte": to}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": {"$dateFromString": {"dateString": "$date"}},
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            }
        },
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {"$project": {"_id": from_dt, "present_count": {"$literal": 0.0}, "late_count": {"$literal": 0}}},
                ],
            }
        },
        {"$group": {"_id": "$_id", "present_count": {"$max": "$present_count"}, "late_count": {"$max": "$late_count"}}},
        {"$densify": {"field": "_id", "range": {"step": 1, "unit": "day", "bounds": [from_dt, to_dt + timedelta(days=1)]}}},
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
                    {"$match": {"$expr": {"$and": [{"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$d_str"]}]}}},
                    {"$count": "count"},
                ],
                "as": "hc",
            }
        },
        {
            "$project": {
                "_id": 0, "date": "$date", "is_working_day": {"$lte": ["$dayOfWeek", 5]},
                "headcount": {"$ifNull": [{"$first": "$hc.count"}, 0]},
                "present_count": "$present_count", "late_count": "$late_count",
            }
        },
        {
            "$project": {
                "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {"$and": [{"$eq": ["$is_working_day", True]}, {"$gt": ["$headcount", 0]}]},
                        {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
                        None,
                    ]
                },
            }
        },
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {
            "$project": {
                "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1,
                "moving_avg_7d": {"$cond": [{"$ne": ["$moving_avg_7d", None]}, {"$round": ["$moving_avg_7d", 4]}, None]},
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
        from_d, to_d = datetime.strptime(from_, "%Y-%m-%d").date(), datetime.strptime(to, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(422, "invalid date format")
    if to_d < from_d: raise HTTPException(422, "to must be on or after from")
    if (to_d - from_d).days + 1 > 92: raise HTTPException(422, "date range cannot exceed 92 days")
    if not db.employees.find_one({"department": department}): raise HTTPException(404, "department not found")

    return {"department": department, "items": list(db.attendance_logs.aggregate(build_department_trend_pipeline(department, from_, to)))}


# --------------------------------------------------------------------------- #
# Admin Diagnostic Endpoint
# --------------------------------------------------------------------------- #

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
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must not be after date_to")
        q: Dict[str, Any] = {}
        if emp_code: q["emp_code"] = emp_code
        if status: q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from
            if date_to: q["date"]["$lte"] = date_to
        cmd = {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size, "limit": page_size}
        coll = "attendance_logs"

    elif endpoint == "employee_monthly":
        if not emp_code or not month: raise HTTPException(422, "emp_code and month are required")
        cmd, coll = {"aggregate": "attendance_logs", "pipeline": build_employee_monthly_pipeline(emp_code, month), "cursor": {}}, "attendance_logs"

    elif endpoint == "department_summary":
        if not month: raise HTTPException(422, "month is required")
        cmd, coll = {"aggregate": "employees", "pipeline": build_department_summary_pipeline(month, department), "cursor": {}}, "employees"

    elif endpoint == "late_leaderboard":
        if not month: raise HTTPException(422, "month is required")
        cmd, coll = {"aggregate": "attendance_logs", "pipeline": build_late_leaderboard_pipeline(month, limit, department), "cursor": {}}, "attendance_logs"

    elif endpoint == "department_trend":
        if not department or not from_ or not to: raise HTTPException(422, "department, from, and to are required")
        if to < from_: raise HTTPException(422, "to must be on or after from")
        cmd, coll = {"aggregate": "attendance_logs", "pipeline": build_department_trend_pipeline(department, from_, to), "cursor": {}}, "attendance_logs"

    res = db.command({"explain": cmd, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": coll, "explain": json.loads(json_util.dumps(res))}

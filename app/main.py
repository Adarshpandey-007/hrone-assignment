"""
Employee Attendance & Analytics API (FastAPI + MongoDB)

Production implementation adhering strictly to OpenAPI contract v2.0 and business rules R1-R10.
"""
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import math
import os
import re
from typing import Any, Dict, List, Literal, Optional, Union

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# Load .env without overriding pre-existing system environment variables
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Startup & Lifespan
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create indexes idempotently at startup
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("joined_on", 1)])

    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers & Business Rules (R1 - R10)
# --------------------------------------------------------------------------- #
def round_half_up_2(val: Optional[Union[float, Decimal, int]]) -> Optional[float]:
    if val is None:
        return None
    return float(Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round_half_up_4(val: Optional[Union[float, Decimal, int]]) -> Optional[float]:
    if val is None:
        return None
    return float(Decimal(str(val)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def truncate_to_seconds_ms(ms: int) -> int:
    return (ms // 1000) * 1000


def ms_to_utc_datetime(ms: Optional[int]) -> Optional[datetime]:
    if ms is None:
        return None
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def format_datetime_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> date:
    """Rule R1: Shift times and dates are IST.

    Overnight shifts: punch-in earlier than shift_end belongs to previous day's shift.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    d = punch_in_ist.date()
    t = punch_in_ist.time()
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    is_overnight = (time(eh, em) <= time(sh, sm))
    if is_overnight and t < time(eh, em):
        return d - timedelta(days=1)
    return d


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, att_date: date) -> int:
    """Rule R2: Grace period 10 minutes.

    Late only if punch-in > 10:00 after shift_start. Then late_minutes = floor elapsed minutes since shift_start.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    sh, sm = map(int, shift_start.split(":"))
    start_dt = datetime.combine(att_date, time(sh, sm), tzinfo=IST)
    diff_sec = int((punch_in_ist - start_dt).total_seconds())
    if diff_sec > 600:
        return int(math.floor(diff_sec / 60))
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """Rule R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    diff_sec = int((punch_out_utc - punch_in_utc).total_seconds())
    val = Decimal(diff_sec) / Decimal(3600)
    return float(val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out_utc: datetime, shift_start: str, shift_end: str, att_date: date) -> int:
    """Rule R3: Whole minutes (floor) from shift_end to punch_out, counted only if >= 30, else 0."""
    punch_out_ist = punch_out_utc.astimezone(IST)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    is_overnight = (time(eh, em) <= time(sh, sm))
    end_date = att_date + timedelta(days=1) if is_overnight else att_date
    end_dt = datetime.combine(end_date, time(eh, em), tzinfo=IST)
    diff_sec = int((punch_out_ist - end_dt).total_seconds())
    mins = int(math.floor(diff_sec / 60))
    return mins if mins >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """Rule R5: After punch-out, half_day is true when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def count_working_days_in_month(month_str: str, joined_on_str: str) -> int:
    """Rule R7: Mon-Fri, counted from joined_on for mid-month joiners."""
    y, m = map(int, month_str.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    first_day = date(y, m, 1)
    last_day = date(y, m, num_days)
    joined_date = date.fromisoformat(joined_on_str)
    if joined_date > last_day:
        return 0
    start_date = max(first_day, joined_date)
    return sum(1 for d in range((last_day - start_date).days + 1) if (start_date + timedelta(days=d)).weekday() < 5)


def format_history_entry(entry: dict) -> dict:
    at_val = entry.get("at")
    at_ms = format_datetime_ms(at_val) if isinstance(at_val, datetime) else at_val
    raw_changes = entry.get("changes", {})
    changes = {}
    for k, v in raw_changes.items():
        if isinstance(v, dict):
            c_from = v.get("from")
            c_to = v.get("to")
            if isinstance(c_from, datetime):
                c_from = format_datetime_ms(c_from)
            if isinstance(c_to, datetime):
                c_to = format_datetime_ms(c_to)
            changes[k] = {"from": c_from, "to": c_to}
        else:
            changes[k] = v
    return {
        "at": at_ms,
        "by": entry.get("by", ""),
        "reason": entry.get("reason", ""),
        "changes": changes,
    }


def format_attendance_record(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": format_datetime_ms(doc.get("punch_in")),
        "punch_out": format_datetime_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [format_history_entry(h) for h in doc.get("history", [])],
    }


def format_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": format_datetime_ms(doc.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_shift_and_date(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        try:
            date.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError("invalid joined_on date")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000, strict=True)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000, strict=True)


class RegularizeRequest(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000, strict=True)
    punch_out: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000, strict=True)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    now_utc = datetime.now(timezone.utc)
    now_ms = truncate_to_seconds_ms(int(now_utc.timestamp() * 1000))
    created_at_dt = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)

    doc = body.model_dump()
    doc["created_at"] = created_at_dt
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    return format_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [format_employee(doc) for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    raw_ts = body.punched_at if body.punched_at is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    ts_ms = truncate_to_seconds_ms(raw_ts)
    punch_in_utc = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)

    att_date = get_attendance_date(punch_in_utc, emp["shift_start"], emp["shift_end"])
    date_str = att_date.isoformat()

    late_minutes = compute_late_minutes(punch_in_utc, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return format_attendance_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    raw_ts = body.punched_at if body.punched_at is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    ts_ms = truncate_to_seconds_ms(raw_ts)
    punch_out_utc = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)

    # Find the employee's most recent record whose punch_in is at or before punched_at
    filter_q = {
        "emp_code": body.emp_code,
        "punch_in": {"$lte": punch_out_utc, "$ne": None},
    }
    rec = db.attendance_logs.find_one(filter_q, sort=[("punch_in", -1)])
    if not rec:
        raise HTTPException(404, "No matching punch-in found")

    if rec.get("punch_out") is not None:
        raise HTTPException(409, "Record already punched out")

    punch_in_utc = rec["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    if diff_sec <= 0:
        raise HTTPException(422, "punched_at must be strictly after punch_in")
    if diff_sec > 24 * 3600:
        raise HTTPException(422, "punched_at cannot be more than 24 hours after punch_in")

    att_date = date.fromisoformat(rec["date"])
    work_hours = compute_work_hours(punch_in_utc, punch_out_utc)
    overtime_minutes = compute_overtime(punch_out_utc, emp["shift_start"], emp["shift_end"], att_date)
    half_day = compute_half_day(work_hours)

    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "Record already punched out")

    return format_attendance_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")

    q = {}
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
    items = [format_attendance_record(doc) for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "Attendance record not found")

    cur_status = rec["status"]
    cur_punch_in = rec.get("punch_in")
    cur_punch_out = rec.get("punch_out")
    cur_work_hours = rec.get("work_hours")
    cur_late_minutes = rec.get("late_minutes", 0)
    cur_overtime_minutes = rec.get("overtime_minutes", 0)
    cur_half_day = rec.get("half_day", False)

    target_status = body.status if body.status is not None else cur_status

    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "punch_in and punch_out cannot be supplied when status is ABSENT or LEAVE")
        new_punch_in = None
        new_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence status requires punch_in
        if body.punch_in is not None:
            new_in_ms = truncate_to_seconds_ms(body.punch_in)
            new_punch_in = ms_to_utc_datetime(new_in_ms)
        else:
            new_punch_in = cur_punch_in

        if new_punch_in is None:
            raise HTTPException(422, "A presence status requires punch_in")

        if new_punch_in.tzinfo is None:
            new_punch_in = new_punch_in.replace(tzinfo=timezone.utc)

        # Check punch_in stays on record's attendance date per R1
        att_d = get_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if att_d.isoformat() != date:
            raise HTTPException(422, f"punch_in belongs to attendance date {att_d.isoformat()}, not {date}")

        # Target punch out
        if "punch_out" in body.model_fields_set:
            if body.punch_out is not None:
                new_out_ms = truncate_to_seconds_ms(body.punch_out)
                new_punch_out = ms_to_utc_datetime(new_out_ms)
            else:
                new_punch_out = None
        else:
            new_punch_out = cur_punch_out

        if new_punch_out is not None:
            if new_punch_out.tzinfo is None:
                new_punch_out = new_punch_out.replace(tzinfo=timezone.utc)
            diff_sec = (new_punch_out - new_punch_in).total_seconds()
            if diff_sec <= 0:
                raise HTTPException(422, "punch_out must be strictly after punch_in")
            if diff_sec > 24 * 3600:
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], att_d)
            new_overtime_minutes = compute_overtime(new_punch_out, emp["shift_start"], emp["shift_end"], att_d)
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], att_d)
            new_overtime_minutes = 0
            new_half_day = False

    # Compare before and after values for changes
    before = {
        "status": cur_status,
        "punch_in": format_datetime_ms(cur_punch_in),
        "punch_out": format_datetime_ms(cur_punch_out),
        "work_hours": cur_work_hours,
        "late_minutes": cur_late_minutes,
        "overtime_minutes": cur_overtime_minutes,
        "half_day": cur_half_day,
    }
    after = {
        "status": target_status,
        "punch_in": format_datetime_ms(new_punch_in),
        "punch_out": format_datetime_ms(new_punch_out),
        "work_hours": new_work_hours,
        "late_minutes": new_late_minutes,
        "overtime_minutes": new_overtime_minutes,
        "half_day": new_half_day,
    }

    changes = {}
    for k in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        if before[k] != after[k]:
            changes[k] = {"from": before[k], "to": after[k]}

    if not changes:
        raise HTTPException(422, "Request changes nothing")

    # In MongoDB history, punch_in and punch_out from/to are stored as BSON datetimes
    db_changes = {}
    for k, v in changes.items():
        if k in ("punch_in", "punch_out"):
            db_changes[k] = {
                "from": ms_to_utc_datetime(v["from"]),
                "to": ms_to_utc_datetime(v["to"]),
            }
        else:
            db_changes[k] = v

    now_utc = datetime.now(timezone.utc)
    now_ms = truncate_to_seconds_ms(int(now_utc.timestamp() * 1000))
    history_entry = {
        "at": datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": db_changes,
    }

    # Optimistic concurrency check: match existing history array state
    res = db.attendance_logs.update_one(
        {"_id": rec["_id"], "history": rec.get("history", [])},
        {
            "$set": {
                "status": target_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
    )
    if res.modified_count == 0:
        raise HTTPException(409, "Concurrent modification detected")

    updated_rec = db.attendance_logs.find_one({"_id": rec["_id"]})
    return format_attendance_record(updated_rec)


# --------------------------------------------------------------------------- #
# Analytics Endpoints (MongoDB Aggregations)
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month_str: str) -> List[dict]:
    y, m = map(int, month_str.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    from_date = f"{month_str}-01"
    to_date = f"{month_str}-{num_days:02d}"

    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        {
            "$project": {
                "status": 1,
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "dayOfWeek": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}},
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
                                    {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
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
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    working_days = count_working_days_in_month(month, emp["joined_on"])
    pipeline = build_employee_monthly_pipeline(emp_code, month)
    agg_res = list(db.attendance_logs.aggregate(pipeline))

    if agg_res:
        data = agg_res[0]
        present_days = float(data.get("present_days", 0.0))
        leave_days = int(data.get("leave_days", 0))
        late_count = int(data.get("late_count", 0))
        total_late_minutes = int(data.get("total_late_minutes", 0))
        total_overtime_minutes = int(data.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        attendance_pct = round_half_up_2((present_days / working_days) * 100)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(month_str: str, department: Optional[str] = None) -> List[dict]:
    y, m = map(int, month_str.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    from_date = f"{month_str}-01"
    to_date = f"{month_str}-{num_days:02d}"

    match_emp = {"joined_on": {"$lte": to_date}}
    if department:
        match_emp["department"] = department

    return [
        {"$match": match_emp},
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
                                    {"$gte": ["$date", from_date]},
                                    {"$lte": ["$date", to_date]},
                                ]
                            }
                        }
                    }
                ],
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
        {
            "$project": {
                "department": "$_id",
                "headcount": 1,
                "logs": {
                    "$reduce": {
                        "input": "$all_logs",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]},
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {
                                                "$in": [
                                                    {"$dayOfWeek": {"$dateFromString": {"dateString": "$$log.date", "format": "%Y-%m-%d"}}},
                                                    [2, 3, 4, 5, 6],
                                                ]
                                            },
                                            {"$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        ]
                                    },
                                    {"$cond": [{"$ifNull": ["$$log.half_day", False]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "present_work_hours": {
                    "$filter": {
                        "input": "$logs",
                        "as": "log",
                        "cond": {
                            "$and": [
                                {"$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$$log.work_hours", None]},
                            ]
                        },
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]},
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {"$ifNull": ["$$log.late_minutes", 0]},
                        }
                    }
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": 1,
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$present_work_hours"}, 0]},
                        {"$avg": "$present_work_hours.work_hours"},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    pipeline = build_department_summary_pipeline(month, department)
    items = list(db.employees.aggregate(pipeline))

    for it in items:
        it["present_days"] = float(it["present_days"])
        if it["avg_work_hours"] is not None:
            it["avg_work_hours"] = round_half_up_2(it["avg_work_hours"])

    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(month_str: str, limit: int, department: Optional[str] = None) -> List[dict]:
    y, m = map(int, month_str.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    from_date = f"{month_str}-01"
    to_date = f"{month_str}-{num_days:02d}"

    pipeline: List[dict] = [
        {
            "$match": {
                "date": {"$gte": from_date, "$lte": to_date},
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
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {
            "$match": {
                "rank": {"$lte": limit}
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
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


def build_department_trend_pipeline(department: str, from_str: str, to_str: str) -> List[dict]:
    from_dt = datetime.fromisoformat(from_str + "T00:00:00Z")
    to_dt = datetime.fromisoformat(to_str + "T00:00:00Z")
    to_dt_plus_1 = to_dt + timedelta(days=1)

    return [
        {
            "$match": {
                "date": {"$gte": from_str, "$lte": to_str}
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
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "date_obj": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_obj",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_plus_1],
                },
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_obj"}},
                "date_obj": 1,
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$cur_date"]},
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
                "date": 1,
                "date_obj": 1,
                "present_count": 1,
                "late_count": 1,
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]},
                "dayOfWeek": {"$dayOfWeek": "$date_obj"},
            }
        },
        {
            "$project": {
                "date": 1,
                "date_obj": 1,
                "is_working_day": {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date_obj": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(404, "Department not found")

    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    if d_to < d_from:
        raise HTTPException(422, "to date must be >= from date")
    if (d_to - d_from).days > 92:
        raise HTTPException(422, "date range cannot exceed 92 days")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    items = list(db.attendance_logs.aggregate(pipeline))

    for it in items:
        it["present_count"] = float(it["present_count"])
        if it["attendance_rate"] is not None:
            it["attendance_rate"] = round_half_up_4(it["attendance_rate"])
        if it["moving_avg_7d"] is not None:
            it["moving_avg_7d"] = round_half_up_4(it["moving_avg_7d"])

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be <= date_to")
        q = {}
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
            "explain": json_util.loads(json_util.dumps(explain_res)),
        }

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required for employee_monthly")
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        explain_res = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json_util.loads(json_util.dumps(explain_res)),
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary")
        pipeline = build_department_summary_pipeline(month, department)
        explain_res = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": json_util.loads(json_util.dumps(explain_res)),
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard")
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        explain_res = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json_util.loads(json_util.dumps(explain_res)),
        }

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from, and to are required for department_trend")
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
        if d_to < d_from:
            raise HTTPException(422, "to date must be >= from date")
        if (d_to - d_from).days > 92:
            raise HTTPException(422, "date range cannot exceed 92 days")
        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        explain_res = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json_util.loads(json_util.dumps(explain_res)),
        }

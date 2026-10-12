"""
Employee Attendance & Analytics API
Run: uvicorn app.main:app --port 8000
"""

import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

import json
from bson.json_util import dumps as bson_dumps

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# Load .env without overriding real environment variables.
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

if not MONGO_URI:
    raise RuntimeError("MONGO_URI is missing. Set it in your environment or .env file.")

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

PRESENT_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = PRESENT_STATUSES | {"ABSENT", "LEAVE"}

app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
)


# ------------------------------- Helpers -------------------------------- #

def now_utc():
    return datetime.now(UTC).replace(microsecond=0)


def as_utc(value):
    """Treat naive PyMongo datetimes as UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_ms(value):
    """Convert a datetime to epoch milliseconds."""
    value = as_utc(value)
    if value is None:
        return None
    return int(value.timestamp() * 1000)


def from_ms(value):
    """Validate and convert epoch milliseconds to UTC datetime."""
    if value is None:
        return now_utc()
    if type(value) is not int or not 100000000000 <= value <= 4102444800000:
        raise HTTPException(422, "Timestamp must be epoch milliseconds.")
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)


def parse_date(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError()
        return parsed
    except (TypeError, ValueError):
        raise HTTPException(422, "Date must use YYYY-MM-DD format.")


def parse_month(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}", value):
        raise HTTPException(422, "Month must use YYYY-MM format.")
    try:
        return date.fromisoformat(value + "-01")
    except ValueError:
        raise HTTPException(422, "Invalid month.")


def parse_clock(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise HTTPException(422, "Time must use HH:MM format.")
    return datetime.strptime(value, "%H:%M").time()


def round_half_up(value, places=2):
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def attendance_date(punch_in, shift_start, shift_end):
    local = as_utc(punch_in).astimezone(IST)
    start = parse_clock(shift_start)
    end = parse_clock(shift_end)

    if end <= start and local.time().replace(tzinfo=None) < end:
        return (local.date() - timedelta(days=1)).isoformat()

    return local.date().isoformat()


def shift_end_datetime(record_date, shift_start, shift_end):
    day = parse_date(record_date)
    start = parse_clock(shift_start)
    end = parse_clock(shift_end)
    end_day = day + timedelta(days=1) if end <= start else day
    local_end = datetime.combine(end_day, end, tzinfo=IST)
    return local_end.astimezone(UTC)




def compute_late_minutes(punch_in, shift_start, record_date):
    """Calculate late minutes using the attendance date and shift start."""
    local = as_utc(punch_in).astimezone(IST)
    start_clock = parse_clock(shift_start)
    day = parse_date(record_date)

    shift_start_dt = datetime.combine(day, start_clock, tzinfo=IST)
    elapsed = int((local - shift_start_dt).total_seconds())

    # Ten-minute grace period; lateness must be strictly greater than 10 minutes.
    if elapsed <= 600:
        return 0

    return max(0, elapsed // 60)



def compute_work_hours(punch_in, punch_out):
    seconds = int((as_utc(punch_out) - as_utc(punch_in)).total_seconds())
    return round_half_up(seconds / 3600, 2)


def compute_overtime(punch_out, shift_start, shift_end, record_date):
    end = shift_end_datetime(record_date, shift_start, shift_end)
    minutes = int((as_utc(punch_out) - end).total_seconds()) // 60
    return max(0, minutes) if minutes >= 30 else 0



def compute_derived(emp, record):
    """Recompute derived fields for a record using its employee's shift."""
    status = record.get("status", "PRESENT")
    punch_in = as_utc(record.get("punch_in"))
    punch_out = as_utc(record.get("punch_out"))

    if status not in PRESENT_STATUSES:
        return {
            "punch_in": None,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }

    if punch_in is None:
        raise HTTPException(422, "Presence status requires punch_in.")

    if punch_out is None:
        return {
            "punch_in": punch_in,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": compute_late_minutes(
                punch_in, emp["shift_start"], record["date"]
            ),
            "overtime_minutes": 0,
            "half_day": False,
        }

    seconds = int((punch_out - punch_in).total_seconds())
    if seconds <= 0 or seconds > 86400:
        raise HTTPException(
            422, "punch_out must be after punch_in and within 24 hours."
        )

    hours = compute_work_hours(punch_in, punch_out)

    return {
        "punch_in": punch_in,
        "punch_out": punch_out,
        "work_hours": hours,
        "late_minutes": compute_late_minutes(
            punch_in, emp["shift_start"], record["date"]
        ),
        "overtime_minutes": compute_overtime(
            punch_out, emp["shift_start"], emp["shift_end"], record["date"]
        ),
        "half_day": hours < 4.50,
    }


def serialize_employee(doc):
    result = dict(doc)
    result.pop("_id", None)
    if "created_at" in result:
        result["created_at"] = to_ms(result["created_at"])
    return result


def serialize_attendance(doc):
    result = dict(doc)
    result.pop("_id", None)
    result.setdefault("history", [])
    result.setdefault("half_day", False)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)

    for field in ("punch_in", "punch_out"):
        result[field] = to_ms(result.get(field))

    history = []
    for entry in result.get("history", []):
        item = dict(entry)
        item["at"] = to_ms(item.get("at"))
        changes = {}
        for key, values in item.get("changes", {}).items():
            changes[key] = {
                "from": to_ms(values.get("from")) if key in ("punch_in", "punch_out") else values.get("from"),
                "to": to_ms(values.get("to")) if key in ("punch_in", "punch_out") else values.get("to"),
            }
        item["changes"] = changes
        history.append(item)

    result["history"] = history
    return result


def validate_page(page, page_size):
    if page < 1 or page_size < 1 or page_size > 100:
        raise HTTPException(422, "page must be >= 1 and page_size must be between 1 and 100.")


# -------------------------------- Models -------------------------------- #

class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, value):
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("Invalid email address.")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, value):
        parse_clock(value)
        return value

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value):
        parse_date(value)
        return value

    @field_validator("shift_end")
    @classmethod
    def validate_shift_end(cls, value, info):
        start = info.data.get("shift_start")
        if start is not None and start == value:
            raise ValueError("shift_start and shift_end must differ.")
        return value


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[int] = None


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
# ------------------------- Database indexes ----------------------------- #

@app.on_event("startup")
def create_indexes():
    """Create indexes safely whenever the application starts."""
    client.admin.command("ping")

    db.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uq_employee_code",
    )
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_employee_department_code",
    )
    db.employees.create_index(
        [("joined_on", ASCENDING), ("department", ASCENDING)],
        name="ix_employee_joined_department",
    )

    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_employee_date",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_employee",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", DESCENDING)],
        name="ix_attendance_employee_date_desc",
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING)],
        name="ix_attendance_status_date",
    )


# ------------------------------- Health --------------------------------- #

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB is unavailable.")


# ------------------------------ Employees ------------------------------- #

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists.")

    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    validate_page(page, page_size)

    query = {}
    if department is not None:
        query["department"] = department

    total = db.employees.count_documents(query)
    items = list(
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    for item in items:
        if "created_at" in item:
            item["created_at"] = to_ms(item["created_at"])

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }



# ----------------------------- Punch in --------------------------------- #

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found.")

    punched_at = from_ms(body.punched_at)
    record_date = attendance_date(
        punched_at, emp["shift_start"], emp["shift_end"]
    )
    

    doc = {
        "emp_code": body.emp_code,
        "date": record_date,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            punched_at, emp["shift_start"], record_date
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            409, "Attendance already exists for this employee and date."
        )

    return serialize_attendance(doc)



# ----------------------------- Punch out -------------------------------- #

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found.")

    punched_at = from_ms(body.punched_at)

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "status": {"$in": list(PRESENT_STATUSES)},
            "punch_in": {"$ne": None, "$lte": punched_at},
        },
        sort=[("punch_in", DESCENDING)],
    )

    if record is None:
        raise HTTPException(404, "No eligible punch-in record found.")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "This attendance record is already closed.")

    seconds = int(
        (punched_at - as_utc(record["punch_in"])).total_seconds()
    )
    if seconds <= 0 or seconds > 86400:
        raise HTTPException(
            422, "punch_out must be after punch_in and within 24 hours."
        )

    derived = compute_derived(
        emp,
        {
            **record,
            "punch_out": punched_at,
        },
    )

    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punched_at,
                "work_hours": derived["work_hours"],
                "late_minutes": derived["late_minutes"],
                "overtime_minutes": derived["overtime_minutes"],
                "half_day": derived["half_day"],
            }
        },
    )

    if result.modified_count != 1:
        raise HTTPException(409, "Attendance record was updated concurrently.")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_attendance(updated)
# ---------------------------- Attendance list -------------------------- #

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    validate_page(page, page_size)

    query = {}

    if emp_code is not None:
        query["emp_code"] = emp_code

    if date_from is not None or date_to is not None:
        date_filter = {}
        if date_from is not None:
            parse_date(date_from)
            date_filter["$gte"] = date_from
        if date_to is not None:
            parse_date(date_to)
            date_filter["$lte"] = date_to
        if (
            date_from is not None
            and date_to is not None
            and date_from > date_to
        ):
            raise HTTPException(422, "date_from must not be after date_to.")
        query["date"] = date_filter

    if status is not None:
        if status not in ALL_STATUSES:
            raise HTTPException(422, "Invalid attendance status.")
        query["status"] = status

    total = db.attendance_logs.count_documents(query)

    records = list(
        db.attendance_logs.find(query, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    for record in records:
        record.setdefault("history", [])
        record.setdefault("half_day", False)
        record.setdefault("late_minutes", 0)
        record.setdefault("overtime_minutes", 0)
        record["punch_in"] = to_ms(record.get("punch_in"))
        record["punch_out"] = to_ms(record.get("punch_out"))

        for entry in record["history"]:
            entry["at"] = to_ms(entry.get("at"))
            for field, change in entry.get("changes", {}).items():
                if field in ("punch_in", "punch_out"):
                    change["from"] = to_ms(change.get("from"))
                    change["to"] = to_ms(change.get("to"))

    return {
        "items": records,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ----------------------- Attendance regularization ---------------------- #

@app.patch("/attendance/{emp_code}/{record_date}")
def regularize_attendance(
    emp_code: str,
    record_date: str,
    body: RegularizeIn,
):
    parse_date(record_date)

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found.")

    existing = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": record_date}
    )
    if existing is None:
        raise HTTPException(404, "Attendance record not found.")

    supplied = body.model_fields_set
    changes = {}

    if "status" in supplied:
        changes["status"] = body.status

    for field in ("punch_in", "punch_out"):
        if field in supplied:
            changes[field] = from_ms(getattr(body, field))

    if not changes:
        raise HTTPException(422, "No fields supplied to regularize.")

    new_record = dict(existing)
    new_record.update(changes)

    new_status = new_record.get("status")

    if new_status not in ALL_STATUSES:
        raise HTTPException(422, "Invalid attendance status.")

    if new_status not in PRESENT_STATUSES:
        if (
            "punch_in" in supplied and body.punch_in is not None
        ) or (
            "punch_out" in supplied and body.punch_out is not None
        ):
            raise HTTPException(
                422, "ABSENT and LEAVE records cannot have punch times."
            )
        new_record["punch_in"] = None
        new_record["punch_out"] = None
    else:
        if new_record.get("punch_in") is None:
            raise HTTPException(422, "Presence status requires punch_in.")

        punch_local = as_utc(new_record["punch_in"]).astimezone(IST)
        if punch_local.date().isoformat() != record_date:
            raise HTTPException(
                422, "punch_in must belong to the attendance date."
            )

        if new_record.get("punch_out") is not None:
            seconds = int(
                (
                    as_utc(new_record["punch_out"])
                    - as_utc(new_record["punch_in"])
                ).total_seconds()
            )
            if seconds <= 0 or seconds > 86400:
                raise HTTPException(
                    422, "punch_out must be after punch_in and within 24 hours."
                )

    derived = compute_derived(emp, new_record)
    update_fields = {**changes, **derived}

    actual_changes = {}
    for field, new_value in update_fields.items():
        old_value = existing.get(field)
        if field in ("punch_in", "punch_out"):
            old_value = as_utc(old_value)
            new_value = as_utc(new_value)
        if old_value != new_value:
            actual_changes[field] = {
                "from": old_value,
                "to": new_value,
            }

    if not actual_changes:
        raise HTTPException(422, "The requested change makes no difference.")

    history_entry = {
        "at": now_utc(),
        "reason": body.reason,
        "regularized_by": body.regularized_by,
        "changes": actual_changes,
    }

    # Keep concurrent edits from silently overwriting each other.
    old_history = existing.get("history", [])
    result = db.attendance_logs.update_one(
        {
            "_id": existing["_id"],
            "status": existing.get("status"),
            "punch_in": existing.get("punch_in"),
            "punch_out": existing.get("punch_out"),
            "history": old_history,
        },
        {
            "$set": update_fields,
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            409, "Attendance was changed by another request. Please retry."
        )

    updated = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": record_date}
    )
    return serialize_attendance(updated)
# ----------------------- Monthly employee analytics --------------------- #

def employee_monthly_pipeline(emp_code, month_start):
    next_month = (
        date(month_start.year + (month_start.month == 12),
             1 if month_start.month == 12 else month_start.month + 1,
             1)
    )
    start_s = month_start.isoformat()
    next_s = next_month.isoformat()

    return [
        {"$match": {"emp_code": emp_code}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
        {
            "$set": {
                "joined_date": {"$toDate": "$employee.joined_on"},
                "month_start": {"$dateFromString": {"dateString": start_s}},
                "month_end": {"$dateFromString": {"dateString": next_s}},
            }
        },
        {
            "$set": {
                "effective_start": {
                    "$cond": [
                        {"$gt": ["$joined_date", "$month_start"]},
                        "$joined_date",
                        "$month_start",
                    ]
                }
            }
        },
        {
            "$set": {
                "working_days": {
                    "$size": {
                        "$filter": {
                            "input": {
                                "$map": {
                                    "input": {
                                        "$range": [
                                            0,
                                            {
                                                "$max": [
                                                    0,
                                                    {
                                                        "$dateDiff": {
                                                            "startDate": "$effective_start",
                                                            "endDate": "$month_end",
                                                            "unit": "day",
                                                        }
                                                    },
                                                ]
                                            },
                                        ]
                                    },
                                    "as": "offset",
                                    "in": {
                                        "$dateAdd": {
                                            "startDate": "$effective_start",
                                            "unit": "day",
                                            "amount": "$$offset",
                                        }
                                    },
                                }
                            },
                            "as": "day",
                            "cond": {
                                "$and": [
                                    {"$gte": [{"$dayOfWeek": "$$day"}, 2]},
                                    {"$lte": [{"$dayOfWeek": "$$day"}, 6]},
                                ]
                            },
                        }
                    }
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "working_days": {"$first": "$working_days"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$gte": ["$date", start_s]},
                                    {"$lt": ["$date", next_s]},
                                    {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
                                    {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$gte": ["$date", start_s]},
                                    {"$lt": ["$date", next_s]},
                                    {"$eq": ["$status", "LEAVE"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$gte": ["$date", start_s]},
                                    {"$lt": ["$date", next_s]},
                                    {"$gt": ["$late_minutes", 0]},
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
                            {
                                "$and": [
                                    {"$gte": ["$date", start_s]},
                                    {"$lt": ["$date", next_s]},
                                ]
                            },
                            {"$ifNull": ["$late_minutes", 0]},
                            0,
                        ]
                    }
                },
                "total_overtime_minutes": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$gte": ["$date", start_s]},
                                    {"$lt": ["$date", next_s]},
                                ]
                            },
                            {"$ifNull": ["$overtime_minutes", 0]},
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": {"$literal": emp_code},
                "month": {"$literal": start_s[:7]},
                "working_days": 1,
                "present_days": 1,
                "leave_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "total_overtime_minutes": 1,
                "attendance_pct": {
                    "$cond": [
                        {"$gt": ["$working_days", 0]},
                        {
                            "$round": [
                                {
                                    "$multiply": [
                                        {"$divide": ["$present_days", "$working_days"]},
                                        100,
                                    ]
                                },
                                2,
                            ]
                        },
                        None,
                    ]
                },
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
):
    month_start = parse_month(month)

    if db.employees.find_one({"emp_code": emp_code}, {"_id": 1}) is None:
        raise HTTPException(404, "Employee not found.")

    pipeline = employee_monthly_pipeline(emp_code, month_start)
    result = list(db.attendance_logs.aggregate(pipeline))

    if not result:
        # Employee exists but has no attendance records in any month.
        next_month = date(
            month_start.year + (month_start.month == 12),
            1 if month_start.month == 12 else month_start.month + 1,
            1,
        )
        joined = parse_date(
            db.employees.find_one(
                {"emp_code": emp_code}, {"joined_on": 1}
            )["joined_on"]
        )
        effective_start = max(month_start, joined)
        working_days = 0
        day = effective_start
        while day < next_month:
            if day.weekday() < 5:
                working_days += 1
            day += timedelta(days=1)

        return {
            "emp_code": emp_code,
            "month": month,
            "working_days": working_days,
            "present_days": 0,
            "leave_days": 0,
            "late_count": 0,
            "total_late_minutes": 0,
            "total_overtime_minutes": 0,
            "attendance_pct": 0.0 if working_days else None,
        }

    return result[0]


# ------------------------- Department summary --------------------------- #

@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    department: Optional[str] = None,
):
    month_start = parse_month(month)
    next_month = date(
        month_start.year + (month_start.month == 12),
        1 if month_start.month == 12 else month_start.month + 1,
        1,
    )
    month_end = (next_month - timedelta(days=1)).isoformat()

    employee_match = {"joined_on": {"$lte": month_end}}
    if department is not None:
        employee_match["department"] = department

    pipeline = [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", month_start.isoformat()]},
                                    {"$lt": ["$date", next_month.isoformat()]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 2]},
                                    {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 6]},
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$logs.half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "work_hours_values": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            None,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$headcount"},
                "present_days": 1,
                "avg_work_hours": {
                    "$round": [
                        {"$avg": "$work_hours_values"},
                        2
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

    return {"month": month, "items": list(db.employees.aggregate(pipeline))}
# -------------------------- Late leaderboard ---------------------------- #

def late_leaderboard_pipeline(month_start, limit, department=None):
    next_month = date(
        month_start.year + (month_start.month == 12),
        1 if month_start.month == 12 else month_start.month + 1,
        1,
    )

    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lt": next_month.isoformat(),
                },
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]

    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "rank": 1,
            }
        },
    ])

    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    limit: int = Query(10, ge=1, le=100),
    department: Optional[str] = None,
):
    month_start = parse_month(month)

    pipeline = late_leaderboard_pipeline(
        month_start, limit, department
    )

    items = list(db.attendance_logs.aggregate(pipeline))

    return {
        "month": month,
        "items": items,
    }


# --------------------------- Department trend --------------------------- #

def department_trend_pipeline(department, from_date, to_date):
    start_dt = datetime.combine(from_date, time.min, tzinfo=UTC)
    end_dt = datetime.combine(to_date, time.min, tzinfo=UTC)

    return [
        {"$match": {"department": department}},
        {
            "$group": {
                "_id": None,
                "employees": {
                    "$push": {
                        "emp_code": "$emp_code",
                        "joined_on": "$joined_on",
                    }
                },
            }
        },
        {
            "$set": {
                "employee_codes": {
                    "$map": {
                        "input": "$employees",
                        "as": "employee",
                        "in": "$$employee.emp_code",
                    }
                },
                "date_values": {
                    "$map": {
                        "input": {
                            "$range": [
                                0,
                                {
                                    "$add": [
                                        {
                                            "$dateDiff": {
                                                "startDate": start_dt,
                                                "endDate": end_dt,
                                                "unit": "day",
                                            }
                                        },
                                        1,
                                    ]
                                },
                            ]
                        },
                        "as": "offset",
                        "in": {
                            "$dateToString": {
                                "format": "%Y-%m-%d",
                                "date": {
                                    "$dateAdd": {
                                        "startDate": start_dt,
                                        "unit": "day",
                                        "amount": "$$offset",
                                    }
                                },
                            }
                        },
                    }
                },
            }
        },
        {"$unwind": "$date_values"},
        {
            "$set": {
                "date": "$date_values",
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$employees",
                            "as": "employee",
                            "cond": {
                                "$lte": [
                                    "$$employee.joined_on",
                                    "$date_values",
                                ]
                            },
                        }
                    }
                },
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "day": "$date",
                    "codes": "$employee_codes",
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$day"]},
                                    {"$in": ["$emp_code", "$$codes"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$set": {
                "present_logs": {
                    "$filter": {
                        "input": "$logs",
                        "as": "log",
                        "cond": {
                            "$in": [
                                "$$log.status",
                                ["PRESENT", "WFH", "ON_DUTY"],
                            ]
                        },
                    }
                },
                "late_logs": {
                    "$filter": {
                        "input": "$logs",
                        "as": "log",
                        "cond": {
                            "$gt": [
                                {"$ifNull": ["$$log.late_minutes", 0]},
                                0,
                            ]
                        },
                    }
                },
                "is_working_day": {
                    "$in": [
                        {
                            "$dayOfWeek": {
                                "$dateFromString": {
                                    "dateString": "$date"
                                }
                            }
                        },
                        [2, 3, 4, 5, 6],
                    ]
                },
            }
        },
        {
            "$set": {
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$present_logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$eq": ["$$log.half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                        }
                    }
                },
                "late_count": {"$size": "$late_logs"},
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$round": [
                                {
                                    "$multiply": [
                                        {
                                            "$divide": [
                                                "$present_count",
                                                "$headcount",
                                            ]
                                        },
                                        100,
                                    ]
                                },
                                4,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
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
                "moving_avg_7d": {"$round": ["$moving_avg_7d", 4]},
            }
        },
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    start = parse_date(from_date)
    end = parse_date(to_date)

    if end < start:
        raise HTTPException(422, "'to' must not be before 'from'.")

    if (end - start).days > 91:
        raise HTTPException(422, "The requested range cannot exceed 92 days.")

    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "Department not found.")

    pipeline = department_trend_pipeline(department, start, end)
    items = list(db.employees.aggregate(pipeline))

    return {
        "department": department,
        "from": from_date,
        "to": to_date,
        "items": items,
    }
# ---------------------------- Admin explain ----------------------------- #

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    limit: int = Query(10, ge=1, le=100),
):
    validate_page(page, page_size)

    if endpoint == "attendance_list":
        query = {}

        if emp_code is not None:
            query["emp_code"] = emp_code

        if status is not None:
            if status not in ALL_STATUSES:
                raise HTTPException(422, "Invalid attendance status.")
            query["status"] = status

        if date_from is not None or date_to is not None:
            date_filter = {}
            if date_from is not None:
                parse_date(date_from)
                date_filter["$gte"] = date_from
            if date_to is not None:
                parse_date(date_to)
                date_filter["$lte"] = date_to
            if (
                date_from is not None
                and date_to is not None
                and date_from > date_to
            ):
                raise HTTPException(422, "date_from must not be after date_to.")
            query["date"] = date_filter

        explanation = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return json.loads(bson_dumps(explanation))

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                422, "employee_monthly requires emp_code and month."
            )

        month_start = parse_month(month)
        pipeline = employee_monthly_pipeline(emp_code, month_start)

        explanation = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return json.loads(bson_dumps(explanation))

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "department_summary requires month.")

        month_start = parse_month(month)
        next_month = date(
            month_start.year + (month_start.month == 12),
            1 if month_start.month == 12 else month_start.month + 1,
            1,
        )
        month_end = (next_month - timedelta(days=1)).isoformat()

        employee_match = {"joined_on": {"$lte": month_end}}
        if department is not None:
            employee_match["department"] = department

        pipeline = [
            {"$match": employee_match},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"code": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$code"]},
                                        {"$gte": ["$date", month_start.isoformat()]},
                                        {"$lt": ["$date", next_month.isoformat()]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
        ]

        explanation = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return json.loads(bson_dumps(explanation))

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "late_leaderboard requires month.")

        pipeline = late_leaderboard_pipeline(
            parse_month(month), limit, department
        )

        explanation = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return json.loads(bson_dumps(explanation))

    if endpoint == "department_trend":
        if not department or not date_from or not date_to:
            raise HTTPException(
                422,
                "department_trend requires department, date_from and date_to.",
            )

        start = parse_date(date_from)
        end = parse_date(date_to)

        if end < start or (end - start).days > 91:
            raise HTTPException(422, "Invalid department trend date range.")

        pipeline = department_trend_pipeline(department, start, end)

        explanation = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return json.loads(bson_dumps(explanation))

    raise HTTPException(422, "Unsupported endpoint.")

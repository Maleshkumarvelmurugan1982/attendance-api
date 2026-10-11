"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI (required), MONGO_DB (default "attendance_db").
      A local .env is loaded for convenience; real environment variables always win.

Layout of this file
  1. Configuration and database connection
  2. Business-rule helpers (R1 - R5)              pure functions, no I/O
  3. Conversion between MongoDB documents and the API (epoch ms, defaults for legacy fields)
  4. Request models and parameter types           validation -> 422
  5. Indexes and application start-up
  6. Query / pipeline builders                     shared by the endpoints and /admin/explain
  7. Endpoints
"""
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Annotated, Any, Literal, Optional

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from pydantic import AfterValidator, BaseModel, Field, StringConstraints, Strict, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Configuration
# --------------------------------------------------------------------------- #
load_dotenv(override=False)  # never overrides variables that are already set in the environment

MONGO_URI = os.environ.get("MONGO_URI")
if not MONGO_URI:
    raise RuntimeError("MONGO_URI is not set (export it or put it in a local .env file)")
MONGO_DB = os.environ.get("MONGO_DB") or "attendance_db"

# tz_aware=True: datetimes come back as aware UTC values, so arithmetic with IST values is always correct.
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[MONGO_DB]
employees = db["employees"]
logs = db["attendance_logs"]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

PRESENCE_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]
ABSENCE_STATUSES = ["ABSENT", "LEAVE"]

GRACE_SECONDS = 10 * 60            # R2
OVERTIME_MIN_MINUTES = 30          # R3
HALF_DAY_BELOW = Decimal("4.50")   # R5
MAX_SHIFT = timedelta(hours=24)    # punch-out must be within 24 h of punch-in
TREND_MAX_DAYS = 92


# --------------------------------------------------------------------------- #
# 2. Business-rule helpers (R1 - R5). All instants are aware UTC datetimes.
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # defensive: anything naive coming from MongoDB is UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def truncate_seconds(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def is_overnight(emp: dict) -> bool:
    return _hhmm(emp["shift_end"]) <= _hhmm(emp["shift_start"])


def attendance_date(punch_in: datetime, emp: dict) -> date:
    """R1: the IST calendar date of the punch-in, except that on an overnight shift a punch-in earlier
    than shift_end belongs to the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    if is_overnight(emp) and local.time() < _hhmm(emp["shift_end"]):
        return local.date() - timedelta(days=1)
    return local.date()


def shift_bounds(day: date, emp: dict) -> tuple[datetime, datetime]:
    """Shift start and end instants for the shift that starts on `day` (IST). Overnight ends the next day."""
    start = datetime.combine(day, _hhmm(emp["shift_start"]), IST)
    end = datetime.combine(day, _hhmm(emp["shift_end"]), IST)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in: datetime, emp: dict, day: date) -> int:
    """R2: late only if strictly more than 10:00 after shift_start; minutes counted from shift_start."""
    start, _ = shift_bounds(day, emp)
    elapsed = int((truncate_seconds(punch_in) - start).total_seconds())
    return elapsed // 60 if elapsed > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, emp: dict, day: date) -> int:
    """R3: whole minutes after shift_end, only when that is at least 30."""
    _, end = shift_bounds(day, emp)
    minutes = int((truncate_seconds(punch_out) - end).total_seconds()) // 60
    return minutes if minutes >= OVERTIME_MIN_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> Decimal:
    """R4: seconds / 3600 rounded to 2 decimals half-up, computed exactly with Decimal."""
    seconds = int((truncate_seconds(punch_out) - truncate_seconds(punch_in)).total_seconds())
    return (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def derive_fields(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime],
                  emp: dict, day: date) -> dict:
    """Derived values for a record (DATA_MODEL.md, 'For a record your own code writes')."""
    if status not in PRESENCE_STATUSES or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    out = {"late_minutes": compute_late_minutes(punch_in, emp, day),
           "work_hours": None, "overtime_minutes": 0, "half_day": False}
    if punch_out is not None:
        wh = compute_work_hours(punch_in, punch_out)
        out.update(work_hours=float(wh), overtime_minutes=compute_overtime(punch_out, emp, day),
                   half_day=wh < HALF_DAY_BELOW)  # R5 compares the rounded value
    return out


def month_bounds(month: str) -> tuple[date, date]:
    y, m = map(int, month.split("-"))
    first = date(y, m, 1)
    last = (date(y + (m == 12), m % 12 + 1, 1)) - timedelta(days=1)
    return first, last


def count_weekdays(first: date, last: date) -> int:
    """Mon-Fri days in [first, last] inclusive (R7). Pure calendar arithmetic, no documents involved."""
    if last < first:
        return 0
    days = (last - first).days + 1
    full_weeks, rest = divmod(days, 7)
    count = full_weeks * 5
    for i in range(rest):
        if (first + timedelta(days=full_weeks * 7 + i)).weekday() < 5:
            count += 1
    return count


def half_up(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


# ---- the same rounding inside MongoDB ($round is half-to-even, so it cannot be used for R8) ----
def mq_half_up(expr: Any, places: int) -> dict:
    """MongoDB expression: round a non-negative number half-up, computed in Decimal128 to avoid
    binary floating point errors. Returns a Decimal128 (null stays null)."""
    scale = Decimal128(str(10 ** places))
    return {"$divide": [
        {"$floor": {"$add": [{"$multiply": [{"$toDecimal": expr}, scale]}, Decimal128("0.5")]}},
        scale]}


def mq_weekday(date_field: str = "$date") -> dict:
    """MongoDB expression: true when a 'YYYY-MM-DD' string is Monday-Friday."""
    return {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": date_field, "format": "%Y-%m-%d"}}}, 5]}


def mq_present_value() -> dict:
    """MongoDB expression: 1 for a present-status record, 0.5 for a present half day, 0 otherwise (R5, R6)."""
    return {"$cond": [{"$in": ["$status", PRESENCE_STATUSES]},
                      {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
                      0]}


# --------------------------------------------------------------------------- #
# 3. Document <-> API conversion
# --------------------------------------------------------------------------- #
def _changes_out(changes: dict) -> dict:
    out = {}
    for field, diff in (changes or {}).items():
        diff = diff or {}
        conv = (lambda v: dt_to_ms(v) if isinstance(v, datetime) else v)
        out[field] = {"from": conv(diff.get("from")), "to": conv(diff.get("to"))}
    return out


def record_out(doc: dict) -> dict:
    """Attendance document -> AttendanceRecord. Missing legacy fields get their documented defaults."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [
            {"at": dt_to_ms(h.get("at")), "by": h.get("by"), "reason": h.get("reason"),
             "changes": _changes_out(h.get("changes"))}
            for h in (doc.get("history") or [])
        ],
    }


EMPLOYEE_FIELDS = ["emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on"]


def employee_out(doc: dict) -> dict:
    out = {k: doc.get(k) for k in EMPLOYEE_FIELDS}
    out["created_at"] = dt_to_ms(doc.get("created_at"))
    return out


def to_number(v: Any) -> Any:
    """Decimal128 / Decimal from an aggregation -> float for JSON."""
    if isinstance(v, Decimal128):
        return float(v.to_decimal())
    if isinstance(v, Decimal):
        return float(v)
    return v


def validation_error(msg: str, loc: tuple, value: Any = None):
    """Raise a 422 with FastAPI's standard validation body."""
    raise RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": value}])


# --------------------------------------------------------------------------- #
# 4. Request models and parameter types
# --------------------------------------------------------------------------- #
def _check_date(v: str) -> str:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", v):
        raise ValueError("date must be YYYY-MM-DD")
    date.fromisoformat(v)  # rejects impossible dates such as 2026-02-30
    return v


DateStr = Annotated[str, AfterValidator(_check_date)]
MonthStr = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")]
HHMM = Annotated[str, StringConstraints(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
# Integer milliseconds only: floats, strings and booleans are rejected, and so are seconds-looking values.
EpochMs = Annotated[int, Strict(), Field(ge=100_000_000_000, le=4_102_444_800_000)]

Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]


class Status(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class EmployeeCreate(BaseModel):
    emp_code: Annotated[str, StringConstraints(pattern=r"^EMP[0-9]{4,6}$")]
    name: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    email: Annotated[str, StringConstraints(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, StringConstraints(min_length=1, max_length=50)]
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: DateStr

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeRequest(BaseModel):
    """Derived fields sent by a client are ignored (pydantic drops unknown fields)."""
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: Annotated[str, StringConstraints(min_length=5, max_length=200)]
    regularized_by: Annotated[str, StringConstraints(min_length=1, max_length=50)]

    @model_validator(mode="after")
    def _no_explicit_nulls(self):
        # The contract types these as non-nullable: omitting a field means "unchanged", null is invalid.
        for f in ("status", "punch_in", "punch_out"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} may be omitted but not null")
        return self


# --------------------------------------------------------------------------- #
# 5. Indexes and start-up
# --------------------------------------------------------------------------- #
INDEXES = {
    "employees": [
        ([("emp_code", ASCENDING)], {"name": "ux_emp_code", "unique": True}),
        ([("department", ASCENDING), ("emp_code", ASCENDING)], {"name": "ix_department_emp_code"}),
        ([("department", ASCENDING), ("joined_on", ASCENDING)], {"name": "ix_department_joined_on"}),
        ([("joined_on", ASCENDING)], {"name": "ix_joined_on"}),
    ],
    "attendance_logs": [
        ([("emp_code", ASCENDING), ("date", ASCENDING)], {"name": "ux_emp_code_date", "unique": True}),
        ([("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "ix_date_emp_code"}),
        ([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "ix_status_date_emp_code"}),
        ([("emp_code", ASCENDING), ("punch_in", DESCENDING)], {"name": "ix_emp_code_punch_in"}),
    ],
}

_indexes_ready = False


def ensure_indexes() -> None:
    """Idempotent: re-creating an index with the same keys and options is a no-op."""
    global _indexes_ready
    for coll, specs in INDEXES.items():
        for keys, opts in specs:
            try:
                db[coll].create_index(keys, **opts)
            except OperationFailure as e:
                if e.code not in (85, 86):  # same keys already exist under another name / options
                    raise
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError:
        pass  # MongoDB not reachable yet: /health reports 503 and retries index creation
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# 6. Query / pipeline builders (used by the endpoints AND by /admin/explain)
# --------------------------------------------------------------------------- #
def build_attendance_list(emp_code, date_from, date_to, status, page, page_size) -> dict:
    if date_from and date_to and date_from > date_to:
        validation_error("date_from must not be after date_to", ("query", "date_from"), date_from)
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status.value if isinstance(status, Status) else status
    return {"filter": q, "sort": [("date", DESCENDING), ("emp_code", ASCENDING)],
            "skip": (page - 1) * page_size, "limit": page_size}


def build_employee_monthly(emp_code: str, month: str) -> list:
    first, last = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            # R5-R7: present-status records on Mon-Fri, a half day counts 0.5
            "present_days": {"$sum": {"$cond": [mq_weekday(), mq_present_value(), 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def build_department_summary(month: str, department: Optional[str]) -> list:
    """Runs on `employees` so that employees with no logs are part of the headcount (R9)."""
    first, last = month_bounds(month)
    match: dict = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        match["department"] = department
    has_hours = {"$and": [{"$in": ["$status", PRESENCE_STATUSES]},
                          {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]}
    return [
        {"$match": match},
        {"$project": {"_id": 0, "emp_code": 1, "department": 1}},
        # one small summary document per employee, computed next to the data via the (emp_code, date) index
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "s",
            "pipeline": [
                {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": {"$cond": [mq_weekday(), mq_present_value(), 0]}},
                    "wh_sum": {"$sum": {"$cond": [has_hours, {"$toDecimal": "$work_hours"}, 0]}},
                    "wh_count": {"$sum": {"$cond": [has_hours, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                    "late_sum": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                }},
            ],
        }},
        {"$unwind": {"path": "$s", "preserveNullAndEmptyArrays": True}},  # keep employees with zero logs
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$s.present_days"},
            "wh_sum": {"$sum": "$s.wh_sum"},
            "wh_count": {"$sum": "$s.wh_count"},
            "late_count": {"$sum": "$s.late_count"},
            "total_late_minutes": {"$sum": "$s.late_sum"},
            "leave_count": {"$sum": "$s.leave_count"},
            "on_duty_count": {"$sum": "$s.on_duty_count"},
        }},
        {"$sort": {"_id": 1}},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": 1,
            "present_days": {"$toDouble": mq_half_up("$present_days", 2)},
            # mean over records, not a mean of per-employee means
            "avg_work_hours": {"$cond": [{"$gt": ["$wh_count", 0]},
                                         {"$toDouble": mq_half_up({"$divide": ["$wh_sum", "$wh_count"]}, 2)},
                                         None]},
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
    ]


def build_late_leaderboard(month: str, limit: int, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total": {"$sum": "$late_minutes"}, "count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp",
                     "pipeline": [{"$project": {"_id": 0, "name": 1, "department": 1}}]}},
        {"$unwind": "$emp"},  # logs of unknown employees are dropped
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # standard competition ranking: ties share a rank, the next rank is skipped (1, 2, 2, 4)
        {"$setWindowFields": {"sortBy": {"total": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies after ranking, so ties at the cutoff stay
        {"$sort": {"total": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": "$total", "late_count": "$count"}},
    ]
    return pipeline


def build_department_trend(department: str, from_: str, to: str) -> list:
    """One row per calendar day in [from, to], gap-filled with $densify, 7-row moving average in a window."""
    start = datetime.combine(date.fromisoformat(from_), time(0), timezone.utc)
    end_excl = datetime.combine(date.fromisoformat(to), time(0), timezone.utc) + timedelta(days=1)
    rate = {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                      mq_half_up({"$divide": [{"$toDecimal": "$p"}, "$headcount"]}, 4),
                      None]}
    return [
        {"$match": {"department": department, "joined_on": {"$lte": to}}},
        {"$project": {"_id": 0, "emp_code": 1, "joined_on": 1}},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "logs",
            "pipeline": [
                {"$match": {"date": {"$gte": from_, "$lte": to}}},
                {"$project": {"_id": 0, "date": 1, "p": mq_present_value(),
                              "l": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}},
            ],
        }},
        # one "joined" event per employee (clamped to `from`) plus one event per log
        {"$project": {"ev": {"$concatArrays": [
            [{"date": {"$max": ["$joined_on", from_]}, "j": {"$literal": 1}, "p": {"$literal": 0},
              "l": {"$literal": 0}}],
            {"$map": {"input": "$logs",
                      "in": {"date": "$$this.date", "j": {"$literal": 0}, "p": "$$this.p", "l": "$$this.l"}}},
        ]}}},
        {"$unwind": "$ev"},
        # guarantees at least one document, so $densify has something to fill from
        {"$unionWith": {"pipeline": [{"$documents": [{"ev": {"date": from_, "j": 0, "p": 0, "l": 0}}]}]}},
        {"$group": {"_id": "$ev.date", "j": {"$sum": "$ev.j"}, "p": {"$sum": "$ev.p"}, "l": {"$sum": "$ev.l"}}},
        {"$addFields": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [start, end_excl]}}},
        {"$addFields": {"j": {"$ifNull": ["$j", 0]}, "p": {"$ifNull": ["$p", 0]}, "l": {"$ifNull": ["$l", 0]}}},
        # headcount on a day = everyone who joined on or before it (R9) = running sum of join events
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {
            "headcount": {"$sum": "$j", "window": {"documents": ["unbounded", "current"]}}}}},
        {"$addFields": {"is_working_day": {"$lte": [{"$isoDayOfWeek": "$d"}, 5]}}},
        {"$addFields": {"rate": rate}},
        # mean of the non-null rates in the current row and up to 6 rows before it, inside the range only
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {
            "ma": {"$avg": "$rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"d": 1}},
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d"}},
            "is_working_day": 1,
            "headcount": 1,
            "present_count": {"$toDouble": "$p"},
            "late_count": "$l",
            "attendance_rate": {"$toDouble": "$rate"},
            "moving_avg_7d": {"$toDouble": mq_half_up("$ma", 4)},
        }},
    ]


def validate_trend_range(from_: str, to: str) -> None:
    f, t = date.fromisoformat(from_), date.fromisoformat(to)
    if t < f:
        validation_error("'to' must not be before 'from'", ("query", "to"), to)
    if (t - f).days + 1 > TREND_MAX_DAYS:
        validation_error(f"range must not exceed {TREND_MAX_DAYS} days", ("query", "to"), to)


# --------------------------------------------------------------------------- #
# 7. Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


# ---- employees ------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    created = now_utc()
    doc["created_at"] = created.replace(microsecond=(created.microsecond // 1000) * 1000)  # BSON keeps ms
    try:
        employees.insert_one(doc)  # the unique index decides; no check-then-insert race
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    q = {} if department is None else {"department": department}
    cursor = (employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING)
              .skip((page - 1) * page_size).limit(page_size))
    items = [employee_out(d) for d in cursor]
    return {"items": items, "total": employees.count_documents(q), "page": page, "page_size": page_size}


def get_employee_or_404(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# ---- attendance ------------------------------------------------------------ #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else truncate_seconds(now_utc())
    day = attendance_date(ts, emp)
    doc = {
        "emp_code": emp["emp_code"],
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive_fields(body.status, ts, None, emp, day),
        "history": [],
    }
    try:
        logs.insert_one(doc)  # unique (emp_code, date): exactly one concurrent insert wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else truncate_seconds(now_utc())
    rec = logs.find_one({"emp_code": emp["emp_code"], "punch_in": {"$ne": None, "$lte": ts}},
                        sort=[("punch_in", DESCENDING)])
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = truncate_seconds(rec["punch_in"])
    if ts <= pin:
        validation_error("punched_at must be after punch_in", ("body", "punched_at"), body.punched_at)
    if ts - pin > MAX_SHIFT:
        validation_error("punched_at is more than 24 hours after punch_in", ("body", "punched_at"), body.punched_at)
    day = date.fromisoformat(rec["date"])
    derived = derive_fields(rec["status"], pin, ts, emp, day)
    updated = logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},  # only an open record can be closed: one of two racers wins
        {"$set": {"punch_out": ts, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    spec = build_attendance_list(emp_code, date_from, date_to, status, page, page_size)
    cursor = logs.find(spec["filter"], {"_id": 0}).sort(spec["sort"]).skip(spec["skip"]).limit(spec["limit"])
    return {"items": [record_out(d) for d in cursor], "total": logs.count_documents(spec["filter"]),
            "page": page, "page_size": page_size}


TRACKED_FIELDS = ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: Annotated[DateStr, Path()], body: RegularizeRequest):
    emp = get_employee_or_404(emp_code)
    rec = logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")
    if not ({"status", "punch_in", "punch_out"} & body.model_fields_set):
        validation_error("nothing to change: send status, punch_in or punch_out", ("body",))

    day = datetime.strptime(date, "%Y-%m-%d").date()
    new_status = body.status.value if body.status is not None else rec["status"]
    if new_status in ABSENCE_STATUSES:
        if body.punch_in is not None or body.punch_out is not None:
            validation_error(f"{new_status} clears punch times; do not send punch_in/punch_out", ("body",))
        new_in = new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
        if new_in is None:
            validation_error(f"status {new_status} requires a punch_in", ("body", "punch_in"))
        new_in = truncate_seconds(new_in)
        if attendance_date(new_in, emp) != day:
            validation_error("punch_in must stay on the record's attendance date", ("body", "punch_in"), body.punch_in)
        if new_out is not None:
            new_out = truncate_seconds(new_out)
            if new_out <= new_in or new_out - new_in > MAX_SHIFT:
                validation_error("punch_out must be after punch_in and within 24 hours", ("body", "punch_out"),
                                 body.punch_out)

    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
           **derive_fields(new_status, new_in, new_out, emp, day)}
    old = {"status": rec["status"], "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out"),
           "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
           "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day", False))}

    def same(a, b):
        if isinstance(a, datetime) or isinstance(b, datetime):
            return dt_to_ms(a) == dt_to_ms(b)
        return a == b

    changes = {f: {"from": old[f], "to": new[f]} for f in TRACKED_FIELDS if not same(old[f], new[f])}
    if not changes:
        validation_error("the request changes nothing", ("body",))

    at = now_utc()
    entry = {"at": at.replace(microsecond=(at.microsecond // 1000) * 1000), "by": body.regularized_by,
             "reason": body.reason, "changes": changes}
    n = len(rec.get("history") or [])
    guard = {  # optimistic concurrency: apply only if nobody changed the record since we read it
        "_id": rec["_id"],
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
    }
    if n:
        guard["history"] = {"$size": n}
    else:  # legacy records may have no history field at all
        guard["$or"] = [{"history": {"$exists": False}}, {"history": None}, {"history": {"$size": 0}}]
    updated = logs.find_one_and_update(guard, {"$set": new, "$push": {"history": entry}},
                                       return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "the record was changed concurrently; reload and retry")
    return record_out(updated)


# ---- analytics ------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Annotated[MonthStr, Query()]):
    emp = get_employee_or_404(emp_code)
    rows = list(logs.aggregate(build_employee_monthly(emp_code, month)))
    agg = rows[0] if rows else {}
    first, last = month_bounds(month)
    joined = date.fromisoformat(emp["joined_on"])
    working_days = count_weekdays(max(first, joined), last)  # R7: counted from joined_on
    present = Decimal(str(agg.get("present_days", 0)))
    pct = None if working_days == 0 else float(half_up(present * 100 / working_days, 2))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(half_up(present, 2)),
        "leave_days": agg.get("leave_days", 0),
        "late_count": agg.get("late_count", 0),
        "total_late_minutes": agg.get("total_late_minutes", 0),
        "total_overtime_minutes": agg.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: Annotated[MonthStr, Query()], department: Optional[str] = None):
    items = [{k: to_number(v) for k, v in row.items()}
             for row in employees.aggregate(build_department_summary(month, department))]
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: Annotated[MonthStr, Query()],
                     limit: Annotated[int, Query(ge=1, le=50)] = 10,
                     department: Optional[str] = None):
    items = list(logs.aggregate(build_late_leaderboard(month, limit, department)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str,
                     from_: Annotated[DateStr, Query(alias="from")],
                     to: Annotated[DateStr, Query()]):
    validate_trend_range(from_, to)
    if employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    items = [{k: to_number(v) for k, v in row.items()}
             for row in employees.aggregate(build_department_trend(department, from_, to))]
    return {"department": department, "items": items}


# ---- admin ----------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard",
                      "department_trend"],
    emp_code: Optional[str] = None,
    month: Annotated[Optional[MonthStr], Query()] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[DateStr], Query(alias="from")] = None,
    to: Optional[DateStr] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    def require(**params):
        for name, value in params.items():
            if value is None:
                validation_error("field required", ("query", name))

    if endpoint == "attendance_list":
        spec = build_attendance_list(emp_code, date_from, date_to, status, page, page_size)
        coll = "attendance_logs"
        cmd = {"find": coll, "filter": spec["filter"], "sort": dict(spec["sort"]),
               "skip": spec["skip"], "limit": spec["limit"]}
    else:
        if endpoint == "employee_monthly":
            require(emp_code=emp_code, month=month)
            coll, pipeline = "attendance_logs", build_employee_monthly(emp_code, month)
        elif endpoint == "department_summary":
            require(month=month)
            coll, pipeline = "employees", build_department_summary(month, department)
        elif endpoint == "late_leaderboard":
            require(month=month)
            coll, pipeline = "attendance_logs", build_late_leaderboard(month, limit, department)
        else:  # department_trend
            require(department=department, **{"from": from_, "to": to})
            validate_trend_range(from_, to)
            coll, pipeline = "employees", build_department_trend(department, from_, to)
        cmd = {"aggregate": coll, "pipeline": pipeline, "cursor": {}}

    result = db.command("explain", cmd, verbosity="executionStats")
    plain = json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS))
    return {"endpoint": endpoint, "collection": coll, "explain": plain}

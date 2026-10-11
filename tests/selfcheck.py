"""
Self-check against a REAL MongoDB (6.0+). Not part of the app; run it before submitting.

    MONGO_URI="mongodb://localhost:27017" python tests/selfcheck.py          # correctness checks
    MONGO_URI="mongodb://localhost:27017" python tests/selfcheck.py --big    # + ~100k records, timings

It always uses its own database, `attendance_selfcheck` (it DROPS that database first), whatever MONGO_DB says.

What it does
  1. Seeds awkward data directly into MongoDB: employees with no logs, mid-month and future joiners, overnight shifts,
     weekend records, ABSENT/LEAVE, half days, open records, legacy records without history/half_day/late_minutes,
     forced ties on late minutes, and logs of employees that do not exist.
  2. Calls every analytics endpoint (in-process, via FastAPI's TestClient) and compares the result with an
     independent plain-Python implementation of the rules in openapi.yaml.
  3. Re-runs the race tests against real MongoDB: parallel creates, punch-ins, punch-outs and regularizations.
  4. Calls /admin/explain for all five endpoints and checks for IXSCAN and no COLLSCAN.
"""
import os
import random
import sys
import threading
import time as clock
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

os.environ["MONGO_DB"] = "attendance_selfcheck"  # never touch the real database
if not os.environ.get("MONGO_URI"):
    sys.exit("Set MONGO_URI first, e.g. MONGO_URI=mongodb://localhost:27017")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402

BIG = "--big" in sys.argv
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
rng = random.Random(42)
failures = 0


def check(name, ok, detail=""):
    global failures
    failures += not ok
    print(("  PASS  " if ok else "  FAIL  ") + name + ("" if ok else f"\n        {detail}"))


def hu(x, places):
    return float(Decimal(x).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def daterange(a: date, b: date):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def month_bounds(month):
    y, mo = map(int, month.split("-"))
    first = date(y, mo, 1)
    return first, date(y + (mo == 12), mo % 12 + 1, 1) - timedelta(days=1)


# --------------------------------------------------------------------------- data
def build_dataset():
    emps = []

    def emp(code, dept, joined, shift=("09:30", "18:30")):
        emps.append({"emp_code": code, "name": "Name " + code, "email": code.lower() + "@ex.com", "department": dept,
                     "shift_start": shift[0], "shift_end": shift[1], "joined_on": joined,
                     "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)})

    for i in range(1, 13):
        emp(f"EMP{i:04d}", "Engineering", "2026-01-05")          # 0011, 0012 get no logs at all
    emp("EMP0013", "Engineering", "2026-07-15")                  # mid-month joiner
    emp("EMP0014", "Engineering", "2026-08-10")                  # joins after July
    for i in range(21, 29):
        emp(f"EMP{i:04d}", "Sales", "2026-07-01" if i == 21 else "2026-07-31" if i == 22 else "2025-11-01")
    for i in range(31, 36):
        emp(f"EMP{i:04d}", "Ops", "2026-03-01", ("22:00", "06:00"))  # overnight
    emp("EMP0041", "engineering", "2026-02-01")                  # case-sensitive department
    emp("EMP0051", "Future", "2026-09-01")                       # department with nobody joined yet in July
    if BIG:
        for i in range(1000, 1500):
            emp(f"EMP{i:04d}", rng.choice(["Bulk A", "Bulk B", "Bulk C", "Bulk D"]), "2025-06-01")

    no_logs = {"EMP0011", "EMP0012", "EMP0051"}
    # three-way tie for rank 1 (across departments) and a two-way tie at rank 4, above every random total
    tie_group = {"EMP0002": 800, "EMP0003": 800, "EMP0021": 800, "EMP0005": 700, "EMP0006": 700}
    docs = []
    start, end = (date(2025, 6, 1), date(2026, 8, 5)) if BIG else (date(2026, 6, 24), date(2026, 8, 5))
    for e in emps:
        if e["emp_code"] in no_logs:
            continue
        joined = date.fromisoformat(e["joined_on"])
        for d in daterange(max(start, joined), end):
            weekend = d.weekday() >= 5
            if rng.random() > (0.15 if weekend else 0.85):
                continue
            status = rng.choices(["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"], [60, 12, 8, 10, 10])[0]
            doc = {"emp_code": e["emp_code"], "date": d.isoformat(), "status": status, "punch_in": None,
                   "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0,
                   "half_day": False, "history": []}
            if status in PRESENCE:
                pin = datetime.combine(d, time(9, rng.randint(0, 59)), IST).astimezone(timezone.utc)
                doc["punch_in"] = pin
                doc["late_minutes"] = rng.choice([0, 0, 0, 0, 12, 15, 20, 35, 45])
                if rng.random() < 0.93:  # some records stay open
                    wh = rng.choice([3.5, 4.49, 4.5, 6.25, 8.0, 8.75, 9.12, 9.13, 9.08, 10.33])
                    doc["punch_out"] = pin + timedelta(hours=wh)
                    doc["work_hours"] = wh
                    doc["half_day"] = wh < 4.5
                    doc["overtime_minutes"] = rng.choice([0, 0, 30, 40, 75])
            if e["emp_code"] in tie_group and d.isoformat().startswith("2026-07"):
                doc["late_minutes"] = 0
            if rng.random() < 0.1:  # legacy shape
                for f in ("history", "half_day"):
                    doc.pop(f, None)
                if doc["late_minutes"] == 0 and rng.random() < 0.5:
                    doc.pop("late_minutes")
            docs.append(doc)
    # forced ties in July: give each tie-group member late records summing exactly to the target
    for code, total in tie_group.items():
        july = [x for x in docs if x["emp_code"] == code and x["date"].startswith("2026-07") and x["status"] in PRESENCE]
        for x in july[:4]:
            x["late_minutes"] = total // 4
    # logs of an employee that does not exist (must be ignored by the leaderboard)
    docs.append({"emp_code": "EMP9999", "date": "2026-07-08", "status": "PRESENT",
                 "punch_in": datetime(2026, 7, 8, 5, 0, tzinfo=timezone.utc), "punch_out": None, "work_hours": None,
                 "late_minutes": 999, "overtime_minutes": 0, "half_day": False, "history": []})
    return emps, docs


# --------------------------------------------------------------------------- reference implementation
_cache = {}


def by_emp(docs):
    """docs grouped by emp_code (built once) so the reference stays fast on 100k records."""
    if id(docs) not in _cache:
        g = {}
        for x in docs:
            g.setdefault(x["emp_code"], []).append(x)
        _cache[id(docs)] = g
    return _cache[id(docs)]


def present_value(x):
    if x["status"] not in PRESENCE:
        return Decimal(0)
    return Decimal("0.5") if x.get("half_day") else Decimal(1)


def weekday(s):
    return date.fromisoformat(s).weekday() < 5


def ref_monthly(emps, docs, code, month):
    e = next(x for x in emps if x["emp_code"] == code)
    first, last = month_bounds(month)
    mine = [x for x in by_emp(docs).get(code, []) if first.isoformat() <= x["date"] <= last.isoformat()]
    s = max(first, date.fromisoformat(e["joined_on"]))
    working = sum(1 for d in daterange(s, last) if d.weekday() < 5) if s <= last else 0
    present = sum((present_value(x) for x in mine if weekday(x["date"])), Decimal(0))
    return {"emp_code": code, "month": month, "working_days": working, "present_days": hu(present, 2),
            "leave_days": sum(x["status"] == "LEAVE" for x in mine),
            "late_count": sum((x.get("late_minutes") or 0) > 0 for x in mine),
            "total_late_minutes": sum(x.get("late_minutes") or 0 for x in mine),
            "total_overtime_minutes": sum(x.get("overtime_minutes") or 0 for x in mine),
            "attendance_pct": None if working == 0 else hu(present * 100 / working, 2)}


def ref_summary(emps, docs, month, dept=None):
    first, last = month_bounds(month)
    out = {}
    for e in emps:
        if e["joined_on"] > last.isoformat() or (dept is not None and e["department"] != dept):
            continue
        r = out.setdefault(e["department"], {"department": e["department"], "headcount": 0, "present": Decimal(0),
                                             "whs": Decimal(0), "whc": 0, "late_count": 0, "total_late_minutes": 0,
                                             "leave_count": 0, "on_duty_count": 0})
        r["headcount"] += 1
        for x in by_emp(docs).get(e["emp_code"], []):
            if not (first.isoformat() <= x["date"] <= last.isoformat()):
                continue
            if weekday(x["date"]):
                r["present"] += present_value(x)
            if x["status"] in PRESENCE and x.get("work_hours") is not None:
                r["whs"] += Decimal(str(x["work_hours"]))
                r["whc"] += 1
            late = x.get("late_minutes") or 0
            r["late_count"] += late > 0
            r["total_late_minutes"] += late
            r["leave_count"] += x["status"] == "LEAVE"
            r["on_duty_count"] += x["status"] == "ON_DUTY"
    items = []
    for k in sorted(out):
        r = out[k]
        items.append({"department": k, "headcount": r["headcount"], "present_days": hu(r["present"], 2),
                      "avg_work_hours": hu(r["whs"] / r["whc"], 2) if r["whc"] else None,
                      "late_count": r["late_count"], "total_late_minutes": r["total_late_minutes"],
                      "leave_count": r["leave_count"], "on_duty_count": r["on_duty_count"]})
    return {"month": month, "items": items}


def ref_leaderboard(emps, docs, month, limit, dept=None):
    first, last = month_bounds(month)
    by_code = {e["emp_code"]: e for e in emps}
    tot = {}
    for x in docs:
        late = x.get("late_minutes") or 0
        if late > 0 and first.isoformat() <= x["date"] <= last.isoformat() and x["emp_code"] in by_code:
            t = tot.setdefault(x["emp_code"], [0, 0])
            t[0] += late
            t[1] += 1
    rows = [(c, t) for c, t in tot.items() if dept is None or by_code[c]["department"] == dept]
    rows.sort(key=lambda r: (-r[1][0], r[0]))
    items = []
    for i, (c, t) in enumerate(rows):
        rank = 1 + sum(1 for _, u in rows if u[0] > t[0])  # standard competition ranking
        if rank <= limit:
            items.append({"rank": rank, "emp_code": c, "name": by_code[c]["name"],
                          "department": by_code[c]["department"], "total_late_minutes": t[0], "late_count": t[1]})
    return {"month": month, "items": items}


def ref_trend(emps, docs, dept, f, t):
    fd, td = date.fromisoformat(f), date.fromisoformat(t)
    members = [e for e in emps if e["department"] == dept and e["joined_on"] <= t]
    per_day = {}
    for e in members:
        for x in by_emp(docs).get(e["emp_code"], []):
            per_day.setdefault(x["date"], []).append(x)
    rows, rates = [], []
    for d in daterange(fd, td):
        ds = d.isoformat()
        hc = sum(e["joined_on"] <= ds for e in members)
        day_docs = per_day.get(ds, [])
        present = sum((present_value(x) for x in day_docs), Decimal(0))
        working = d.weekday() < 5
        rate = (present / hc).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP) if (working and hc > 0) else None
        rates.append(rate)
        window = [r for r in rates[-7:] if r is not None]
        ma = hu(sum(window, Decimal(0)) / len(window), 4) if window else None
        rows.append({"date": ds, "is_working_day": working, "headcount": hc, "present_count": float(present),
                     "late_count": sum((x.get("late_minutes") or 0) > 0 for x in day_docs),
                     "attendance_rate": None if rate is None else float(rate), "moving_avg_7d": ma})
    return {"department": dept, "items": rows}


def diff(got, want):
    if got == want:
        return ""
    if isinstance(want, dict) and isinstance(got, dict) and "items" in want:
        gi, wi = got.get("items", []), want["items"]
        if len(gi) != len(wi):
            return f"{len(gi)} items, expected {len(wi)}\n        got  {gi[:4]}\n        want {wi[:4]}"
        for a, b in zip(gi, wi):
            if a != b:
                return f"first differing item\n        got  {a}\n        want {b}"
    return f"\n        got  {got}\n        want {want}"


# --------------------------------------------------------------------------- run
def main():
    print(f"MongoDB {m.client.server_info()['version']}  database '{m.MONGO_DB}'  big={BIG}")
    m.client.drop_database(m.MONGO_DB)
    emps, docs = build_dataset()
    m.employees.insert_many([dict(e) for e in emps])
    for i in range(0, len(docs), 5000):
        m.logs.insert_many([dict(x) for x in docs[i:i + 5000]])
    print(f"seeded {len(emps)} employees, {len(docs)} attendance records")

    with TestClient(m.app) as c:
        check("health 200 and indexes created", c.get("/health").status_code == 200
              and "ux_emp_code_date" in m.logs.index_information())

        print("\nEmployee monthly")
        bad = 0
        for e in emps[:60]:
            for month in ("2026-06", "2026-07", "2026-08"):
                got = c.get(f"/analytics/employees/{e['emp_code']}/monthly", params={"month": month}).json()
                want = ref_monthly(emps, docs, e["emp_code"], month)
                if got != want:
                    bad += 1
                    if bad <= 3:
                        print("        ", e["emp_code"], month, diff(got, want))
        check(f"monthly matches reference for {min(60, len(emps)) * 3} employee-months", bad == 0, f"{bad} mismatches")
        check("monthly unknown employee 404",
              c.get("/analytics/employees/EMP9999/monthly", params={"month": "2026-07"}).status_code == 404)
        check("monthly bad month 422",
              c.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-13"}).status_code == 422)

        print("\nDepartment summary")
        for month in ("2026-06", "2026-07", "2026-08"):
            got = c.get("/analytics/departments/summary", params={"month": month}).json()
            check(f"summary {month} (no-log employees, joiners, case-sensitive depts)",
                  got == ref_summary(emps, docs, month), diff(got, ref_summary(emps, docs, month)))
        for dept in ("Engineering", "engineering", "Future", "Nope"):
            got = c.get("/analytics/departments/summary", params={"month": "2026-07", "department": dept}).json()
            want = ref_summary(emps, docs, "2026-07", dept)
            check(f"summary 2026-07 department={dept}", got == want, diff(got, want))

        print("\nLate leaderboard")
        for limit in (1, 2, 3, 4, 5, 10, 50):
            got = c.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": limit}).json()
            want = ref_leaderboard(emps, docs, "2026-07", limit)
            check(f"leaderboard 2026-07 limit={limit} ({len(want['items'])} rows)", got == want, diff(got, want))
        for dept in ("Engineering", "Sales", "Ops"):
            got = c.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 3, "department": dept}).json()
            want = ref_leaderboard(emps, docs, "2026-07", 3, dept)
            check(f"leaderboard department={dept} limit=3", got == want, diff(got, want))
        lb = c.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 50}).json()["items"]
        check("leaderboard ignores logs of unknown employees", all(r["emp_code"] != "EMP9999" for r in lb))
        ranks = [r["rank"] for r in lb]
        check("leaderboard has shared ranks with gaps (ties present in data)", len(set(ranks)) < len(ranks), ranks)

        print("\nDepartment trend")
        cases = [("Engineering", "2026-07-01", "2026-07-31"), ("Engineering", "2026-07-10", "2026-07-20"),
                 ("Sales", "2026-06-27", "2026-08-02"), ("Ops", "2026-07-04", "2026-07-05"),
                 ("Future", "2026-07-01", "2026-07-07"), ("engineering", "2026-05-01", "2026-07-31"),
                 ("Engineering", "2026-07-15", "2026-07-15")]
        for dept, f, t in cases:
            got = c.get(f"/analytics/departments/{dept}/trend", params={"from": f, "to": t}).json()
            want = ref_trend(emps, docs, dept, f, t)
            check(f"trend {dept} {f}..{t} ({len(want['items'])} days)", got == want, diff(got, want))
        check("trend unknown department 404",
              c.get("/analytics/departments/Nope/trend", params={"from": "2026-07-01", "to": "2026-07-02"}).status_code == 404)
        check("trend to < from 422",
              c.get("/analytics/departments/Ops/trend", params={"from": "2026-07-05", "to": "2026-07-01"}).status_code == 422)
        check("trend 93 days 422",
              c.get("/analytics/departments/Ops/trend", params={"from": "2026-05-01", "to": "2026-08-01"}).status_code == 422)

        print("\nExplain (IXSCAN, no COLLSCAN)")
        for ep, params in [("attendance_list", {}), ("attendance_list", {"emp_code": "EMP0001"}),
                           ("attendance_list", {"status": "LEAVE"}),
                           ("attendance_list", {"date_from": "2026-07-01", "date_to": "2026-07-31"}),
                           ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
                           ("department_summary", {"month": "2026-07"}),
                           ("department_summary", {"month": "2026-07", "department": "Engineering"}),
                           ("late_leaderboard", {"month": "2026-07"}),
                           ("department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-31"})]:
            r = c.get(f"/admin/explain/{ep}", params=params)
            text = r.text
            check(f"explain {ep} {params}", r.status_code == 200 and "IXSCAN" in text and "COLLSCAN" not in text,
                  f"status {r.status_code}, IXSCAN={'IXSCAN' in text}, COLLSCAN={'COLLSCAN' in text}")
        check("explain missing month 422", c.get("/admin/explain/late_leaderboard").status_code == 422)

        print("\nConcurrency on real MongoDB")

        def burst(n, fn):
            out = []
            ts = [threading.Thread(target=lambda: out.append(fn())) for _ in range(n)]
            [t.start() for t in ts]
            [t.join() for t in ts]
            return sorted(out)

        new_emp = {"emp_code": "EMP7777", "name": "Race", "email": "race@ex.com", "department": "Race",
                   "joined_on": "2026-01-01"}
        codes = burst(20, lambda: c.post("/employees", json=new_emp).status_code)
        check("20 parallel creates: one 201", codes == [201] + [409] * 19, codes)
        pin = int(datetime(2026, 7, 6, 9, 45, tzinfo=IST).timestamp() * 1000)
        codes = burst(20, lambda: c.post("/attendance/punch-in", json={"emp_code": "EMP7777", "punched_at": pin}).status_code)
        check("20 parallel punch-ins: one 201", codes == [201] + [409] * 19, codes)
        pout = pin + 9 * 3600 * 1000
        codes = burst(20, lambda: c.post("/attendance/punch-out", json={"emp_code": "EMP7777", "punched_at": pout}).status_code)
        check("20 parallel punch-outs: one 200", codes == [200] + [409] * 19, codes)
        counter = iter(range(1, 100))
        codes = burst(20, lambda: c.patch("/attendance/EMP7777/2026-07-06", json={
            "punch_in": pin + next(counter) * 60000, "reason": "parallel test", "regularized_by": "t"}).status_code)
        hist = len(m.logs.find_one({"emp_code": "EMP7777"})["history"])
        check("20 parallel PATCHes: each 200 has its history entry, others 409",
              codes.count(200) == hist and set(codes) <= {200, 409}, (codes, hist))

        if BIG:
            print("\nTimings on", m.logs.count_documents({}), "records")
            for path, params in [("/attendance", {"page": 50}), ("/attendance", {"status": "LEAVE", "page": 3}),
                                 ("/analytics/employees/EMP1001/monthly", {"month": "2026-07"}),
                                 ("/analytics/departments/summary", {"month": "2026-07"}),
                                 ("/analytics/leaderboard/late", {"month": "2026-07"}),
                                 ("/analytics/departments/Bulk A/trend", {"from": "2026-05-01", "to": "2026-07-31"})]:
                t0 = clock.perf_counter()
                r = c.get(path, params=params)
                ms = (clock.perf_counter() - t0) * 1000
                check(f"{path} {params}: {r.status_code} in {ms:.0f} ms", r.status_code == 200 and ms < 2000)

    print(f"\n{'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()

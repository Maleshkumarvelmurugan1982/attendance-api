# DECISIONS.md

`emp_code`: unique for employees. `(emp_code, date)`: unique for attendance_logs, keeps attendance list fast. `(date desc, emp_code)`: is an index scan that keeps attendance list fast. `(status, date desc, emp_code)`: is an index scan that keeps attendance list fast. `(emp_code, punch_in desc)`: Is an index scan for finding the open record at punch-out. `(joined_on)`: Let the summary and trend be performed with an index scan. `(department, joined_on)`: Let the summary and trend be performed with an index scan. I thought about adding an index to late_minutes, but I didn't, since the month filter will keep it small, and each additional index would slow down punches.

2. **Punch-in race.** No check-then-insert. Both requests insert; the unique (emp_code, date) index lets one insert, but the other creates a duplicate-key error, which is returned as 409.

3. **Ties.** `$rank` gives ties the same rank (1, 2, 2, 4). I rank, then filter on `rank <= limit`, so if it's a tie at the limit, I get the tie score too, and there may be more rows returned than there is in `limit`.

4. **Headcount.** The summary starts with employees, not logs, and `$unwind` with the option $preserveNullAndEmptyArrays keeps employees who have no logs, so they are also counted.

5. **100x data.** Store a one row a day summary (on every punch) rather than reading a month of logs to build the summary.
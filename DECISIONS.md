# &#x20; **DECISIONS.md**



**1.Indexes** : I created a unique index on employee code to prevent duplicate employees. The department and employee code index supports department-based queries, while the joining date and department index helps filter employees by joining date and department. For attendance, the unique employee code and date index prevents duplicate daily records. Other indexes support searches by date, employee, and attendance status. I considered adding an index on late minutes but rejected it because the leaderboard also filters records by date.



**2.Punch-in race** : If two identical requests arrive at the same time, both may find the employee and attempt to insert a record. The unique employee-date index allows only one insert to succeed. That request returns 201, while the other catches `DuplicateKeyError` and returns 409 Conflict.



**3.Ties** : The leaderboard uses `$rank` and includes everyone whose rank is within the limit. Employees tied at the cutoff are all returned, so the result may contain more employees than the requested limit.



**4.Headcount** : The department summary starts with employees and looks up their attendance logs. Therefore, employees with zero logs can still be counted.



**5.Scaling** : For 100 times more data, I would review query performance and optimize indexes based on actual query usage.


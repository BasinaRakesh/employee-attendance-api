# REVIEW.md



I compared the original starter code with the requirements in `PROBLEM_STATEMENT.docx` and `openapi.yaml`. The defects below describe problems in the original starter and the corresponding fixes in my implementation.



| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |

|---|---|---|---|---|

| 1 | `list_employees` | Pagination calculates the skip value as `page * page_size`, skipping the first page of records. | Request page 1 with page size 20 and check whether the first 20 employees are returned. | Calculate the skip as `(page - 1) * page_size` and validate pagination parameters. |

| 2 | `list_employees` | The total count ignores the department filter. | Request employees for one department and compare `total` with the number of employees in that department. | Apply the same filter to `count_documents` and the paginated query. |

| 3 | `list_attendance` | All matching attendance records are loaded into Python before sorting and pagination. | Test with a large dataset and inspect query execution and memory usage. | Apply filters, sorting, skip, and limit in MongoDB, and count matching records separately. |

| 4 | `list_attendance` | Sorting uses only the date, so records on the same date do not have a deterministic secondary order. | Create multiple attendance records for one date and check their order across repeated requests. | Sort by date descending and employee code ascending. |

| 5 | `punch_in` | An unknown employee is not handled explicitly before the code accesses the employee's shift. | Punch in using an employee code that does not exist. | Check that the employee exists and return HTTP 404 when it does not. |

| 6 | `punch_in` | Punch-in timestamps and attendance dates use local machine time rather than the required IST date handling and whole-second precision. | Test timestamps near IST midnight and timestamps containing milliseconds. | Validate epoch-millisecond timestamps, convert using UTC and IST explicitly, and truncate punch instants to whole seconds. |

| 7 | `punch_in` and database indexes | Checking for an existing record before inserting is not sufficient to prevent duplicate records under concurrent requests. | Send simultaneous punch-in requests for the same employee and attendance date. | Create a unique index on employee code and attendance date and handle duplicate-key errors with HTTP 409. |

| 8 | `compute_late_minutes` | Late-minute calculations do not correctly implement the specified IST and whole-second rules. | Test a 09:30 shift with punch-ins at 09:40:00, 09:40:01, and 10:15:59. | Calculate elapsed seconds from the correct IST shift start, apply the 10-minute grace rule, and floor elapsed minutes. |

| 9 | `compute_overtime` | The original function counts overtime from zero minutes and does not enforce the required 30-minute threshold or handle overnight shifts correctly. | Test punch-outs before shift end and 29 minutes, 30 minutes, and 31 minutes after shift end. | Calculate the correct shift-end instant, including overnight shifts, and count overtime only when the elapsed time is at least 30 minutes. |

| 10 | `compute_work_hours` | Built-in `round()` does not guarantee the required decimal half-up rounding. | Test work durations whose hour values fall on a two-decimal rounding boundary. | Use `Decimal` with `ROUND_HALF_UP` for work-hour calculations. |

| 11 | `EmployeeIn` and `PunchInIn` | The original request models lack the required field validation and restrict no attendance status values. | Submit malformed employee codes, invalid email addresses, invalid shift times, or an unsupported status. | Add Pydantic field validation, strict date/time parsing, and allowed status values. |

| 12 | MongoDB initialization | The original silently falls back to a local MongoDB URI if `MONGO_URI` is missing and does not configure a bounded server-selection timeout. | Start the application without `MONGO_URI` or with an unavailable database. | Require configured MongoDB connection details and use a bounded connection timeout. |

| 13 | API contract | The original starter does not implement punch-out, attendance regularization, analytics, or explain endpoints. | Call each endpoint listed in `openapi.yaml` and check whether it exists. | Implement the required endpoints, aggregation pipelines, validation, and error handling. |



## Reviewed areas that are not automatically defects



\- **`.env` loading:** Loading a local `.env` file is useful for development. The important requirement is that actual environment variables take precedence; the revised implementation uses `load_dotenv()` without overriding them.

\- **Database indexes:** An index should support a real query or uniqueness requirement. I use unique indexes for employee codes and employee/date attendance records, plus query-supporting indexes for common filters and sorting.

\- **Leaderboard ties:** Returning more rows than the requested limit is intentional when multiple employees tie at the cutoff rank. The rank is calculated first and the rank cutoff is applied afterward.

\- **Department headcount:** Employees who joined on or before the end of the requested month must count even when they have no attendance records. The summary pipeline starts from eligible employees.

\- **Trend dates:** Weekends and dates without attendance records must still appear in the trend. The trend pipeline generates the date series in MongoDB rather than filling gaps with a Python loop.






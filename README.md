# Employee Attendance API

A FastAPI application for employee attendance management, attendance corrections, and monthly analytics using MongoDB.

## Features

- Employee and attendance record endpoints
- Punch-in and punch-out handling
- Attendance regularization with validation and history
- Department monthly summaries
- Employee monthly analytics
- Late-arrival leaderboard
- Department attendance trends
- Query execution details through admin explain endpoints
- MongoDB indexes and duplicate-record protection

## Project files

- `app/main.py` — API implementation
- `requirements.txt` — Python dependencies
- `openapi.yaml` — API specification and business rules
- `DATA_MODEL.md` — MongoDB data model
- `sample_data/` — Sample employee and attendance records
- `sample_seed.py` — Sample data seeding script
- `REVIEW.md` — Implementation review
- `DECISIONS.md` — Design decisions
- `.env.example` — Example environment configuration

## Requirements

- Python 3.10 or newer
- MongoDB 6.0 or newer
- MongoDB connection URI

## Setup on Windows (PowerShell)

Create and activate a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Install dependencies:

```powershell
pip install -r requirements.txt
```

Create your local environment file:

```powershell
Copy-Item .env.example .env
```

Edit `.env` and set your own MongoDB connection details. Never commit this file or share its secrets.

Seed the sample data:

```powershell
python sample_seed.py
```

Start the API:

```powershell
uvicorn app.main:app --port 8000
```

## API documentation

Once the server is running:

- Health check: http://127.0.0.1:8000/health
- Swagger UI: http://127.0.0.1:8000/docs
- OpenAPI JSON: http://127.0.0.1:8000/openapi.json

## Security

Keep `.env`, database credentials, and other secrets out of version control. Use `.env.example` only as a template with placeholder values.

## Submission

The public repository contains the application code, dependency list, API specification, sample data, and project documentation. The original assignment problem statement is not included in this repository.

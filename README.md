# Employee Attendance & Analytics API

A FastAPI and MongoDB backend service implementing employee attendance tracking, regularization audit trails, and reporting analytics backed by aggregation pipelines.

## Requirements
- Python 3.11+
- MongoDB 6.0+ (local instance, Docker, or MongoDB Atlas)

## Setup & Running

1. **Create and activate a virtual environment:**
   ```bash
   python -m venv .venv
   # Windows (PowerShell):
   .venv\Scripts\Activate.ps1
   # Linux / macOS:
   source .venv/bin/activate
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure Environment Variables:**
   Create a `.env` file (or set environment variables):
   ```env
   MONGO_URI=mongodb://localhost:27017
   MONGO_DB=attendance_db
   ```
   *(For MongoDB Atlas, paste your Atlas connection string as `MONGO_URI`)*

4. **Start the API server:**
   ```bash
   uvicorn app.main:app --port 8000
   ```

All database indexes on `employees` and `attendance_logs` are created automatically and idempotently on application startup.

## API Documentation
Once running, interactive API docs are available at:
- Swagger UI: `http://localhost:8000/docs`
- Health check: `http://localhost:8000/health`

## Repository Structure
- `app/main.py`: Complete application implementation (FastAPI routes, business rules R1–R10, aggregation pipelines, index creation).
- `requirements.txt`: Minimal required Python packages.
- `REVIEW.md`: Comprehensive review of starter code defects, symptoms, and fixes.
- `DECISIONS.md`: Concise rationale on indexing, race condition handling, rankings, and scalability.

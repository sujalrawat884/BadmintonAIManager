# Badminton AI Manager

An async FastAPI service that keeps tabs on badminton club regulars, stores their bookings in MongoDB, and asks a LangGraph-powered Gemini agent to nudge players via WhatsApp when they miss a regular session.

## Features
- REST API to create and list booking records (stored in MongoDB with timezone-aware timestamps).
- APScheduler job that runs nightly at 22:00 (local server time) to launch the reminder workflow.
- LangGraph workflow backed by Gemini 2.5 Flash with two tools:
  - `get_booking_history` pulls the last 30 days of bookings and returns a CSV for the LLM to analyze.
  - `send_whatsapp_reminder` delivers WhatsApp messages through Twilio (or simulates if credentials are absent).
- Manual trigger endpoint so you can kick off the analysis immediately during testing.
- Data seeding utility (`scripts/seed_bookings.py`) that imports historical JSON exports or generates recurring mock bookings.

## Prerequisites
- Python 3.11+
- MongoDB instance (local or cloud)
- Google Generative AI key with access to Gemini 2.5 Flash
- Twilio WhatsApp Sandbox credentials (optional but required for real messages)

## Environment Variables
Create a `.env` file in the project root (or set vars in your shell):

```
GOOGLE_API_KEY=your_gemini_key
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_WHATSAPP_NUMBER=whatsapp:+14155238886
MONGODB_URL=mongodb://localhost:27017
DB_NAME=badminton_club
COLLECTION_NAME=bookings
```

- If Twilio creds are omitted, reminders run in "simulation" mode and only log the message text.
- `DB_NAME` / `COLLECTION_NAME` let you point the API and seeding script at alternate Mongo databases.

## Setup & Run
```
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload
```

The root endpoint (`GET /`) returns the scheduler status and next run time. Scheduler reloads automatically when `main.py` changes if you run with `--reload`.

## API Endpoints
| Method | Path | Purpose |
| --- | --- | --- |
| `POST /bookings` | Create a booking; body must match the `Booking` schema. |
| `GET /bookings` | Retrieve the 100 most recent bookings (newest first). |
| `POST /admin/trigger-check` | Queue the reminder workflow immediately (handy for tests). |
| `GET /` | Health snapshot showing scheduler state and next job time. |

### Sample `POST /bookings` payload
```
{
  "user_id": "demo_sri",
  "user_name": "Sri Sampath",
  "whatsapp_number": "whatsapp:+15550000001",
  "court_name": "Court A",
  "date": "2025-11-17",
  "is_regular_slot": true
}
```

## Reminder Workflow
1. APScheduler (or the manual trigger) calls `run_daily_streak_check`.
2. The function builds a natural-language prompt describing today's date and desired behavior.
3. LangGraph routes the conversation between Gemini and the two registered tools (`get_booking_history`, `send_whatsapp_reminder`).
4. If the model deems someone absent, it calls the WhatsApp tool, which logs or sends through Twilio. Logs include the Twilio SID for audit purposes.

## Seeding & Mock Data
Use `scripts/seed_bookings.py` to import exports or synthesize recurring players for testing.

```
# Import JSON export and add 8 weeks of synthetic players
python scripts/seed_bookings.py --file C:/data/sample_db_booking.json --mock-weeks 8

# Generate 10 weeks of mock data but skip the most recent week for demo_lara to force a reminder
python scripts/seed_bookings.py --mock-weeks 10 --skip-latest demo_lara

# Purge a user before reseeding (avoids duplicates)
python scripts/seed_bookings.py --purge-user demo_sri --file data/mock_absentee.json --mock-weeks 0

# Preview without writing to Mongo
python scripts/seed_bookings.py --file data/mock_absentee.json --dry-run
```

### Script flags
| Flag | Description |
| --- | --- |
| `--file PATH` | Load bookings from Extended JSON / NDJSON export. |
| `--mock-weeks N` | Generate `N` weeks of recurring mock bookings for four demo users (defaults to 8). |
| `--skip-latest USER_ID ...` | Omit the most recent week for the listed users to simulate absences. |
| `--purge-user USER_ID ...` | Delete existing bookings for the listed users before inserting new ones. |
| `--dry-run` | Parse and display record counts without touching MongoDB. |

## Twilio Notes
- For sandbox testing, make sure each recipient has joined your Twilio WhatsApp sandbox.
- Production numbers require WhatsApp Business approval.
- Message send logs appear twice when the LLM also decides to remind the same player; this is expected.

## Troubleshooting
- **Agent returns "No reminders needed"**: Ensure Mongo contains bookings within the last 30 days for the relevant weekday. Use the seeding script to insert mock streaks.
- **`/admin/trigger-check` responds 405**: The endpoint is `POST` only. Use `curl -X POST http://localhost:8000/admin/trigger-check`.
- **No Twilio credentials**: Reminders are simulated; check logs for the message preview.
- **Mongo connection errors**: Verify `MONGODB_URL`, that the database is reachable, and that you've activated your virtual environment before running `uvicorn`.

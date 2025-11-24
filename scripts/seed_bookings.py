from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta, time, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

from bson import json_util
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

MONGODB_URL = os.getenv("MONGODB_URL", "mongodb://localhost:27017")
DB_NAME = os.getenv("DB_NAME", "badminton_club")
COLLECTION_NAME = os.getenv("COLLECTION_NAME", "bookings")

PLAYER_PROFILES = [
    {
        "user_id": "demo_sri",
        "user_name": "Sri Sampath",
        "whatsapp_number": "whatsapp:+15550000001",
        "court_name": "Court A",
        "weekday": 0,  # Monday
    },
    {
        "user_id": "demo_lara",
        "user_name": "Lara Patel",
        "whatsapp_number": "whatsapp:+15550000002",
        "court_name": "Court B",
        "weekday": 2,  # Wednesday
    },
    {
        "user_id": "demo_bia",
        "user_name": "Bia Rodrigues",
        "whatsapp_number": "whatsapp:+15550000003",
        "court_name": "Court A",
        "weekday": 4,  # Friday
    },
    {
        "user_id": "demo_ken",
        "user_name": "Ken Ito",
        "whatsapp_number": "whatsapp:+15550000004",
        "court_name": "Court C",
        "weekday": 5,  # Saturday
    },
]


def _ensure_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    raise ValueError("Booking date missing or invalid")


def _transform_legacy(record: Dict[str, Any]) -> Dict[str, Any]:
    booking_dt = _ensure_datetime(record.get("booking_date") or record.get("date"))
    name = record.get("user_name")
    if not name:
        first = record.get("first_name")
        last = record.get("last_name")
        name = " ".join(part for part in [first, last] if part).strip() or "Unknown Player"

    phone = record.get("whatsapp_number") or record.get("phone") or "whatsapp:+10000000000"
    court_name = record.get("court_name") or str(record.get("court_id", "Court A"))
    user_id = str(record.get("user_id") or record.get("_id"))

    return {
        "user_id": user_id,
        "user_name": name,
        "whatsapp_number": phone,
        "court_name": court_name,
        "date": booking_dt.astimezone(timezone.utc),
        "is_regular_slot": bool(record.get("is_regular_slot", True)),
    }


def _read_json_records(path: Path) -> List[Dict[str, Any]]:
    raw_text = path.read_text(encoding="utf-8").strip()
    if not raw_text:
        return []

    candidates: Iterable[Any] = []
    try:
        parsed = json_util.loads(raw_text)
    except Exception:
        parsed = json_util.loads(f"[{raw_text}]")

    if isinstance(parsed, list):
        candidates = parsed
    else:
        candidates = [parsed]

    normalized = []
    for item in candidates:
        try:
            normalized.append(_transform_legacy(item))
        except Exception as exc:
            print(f"Skipping record due to error: {exc}")
    return normalized


def _generate_mock_records(weeks: int, skip_latest: List[str]) -> List[Dict[str, Any]]:
    today = datetime.now(timezone.utc).date()
    monday = today - timedelta(days=today.weekday())
    docs: List[Dict[str, Any]] = []

    for profile in PLAYER_PROFILES:
        for offset in range(weeks):
            booking_day = monday - timedelta(weeks=offset) + timedelta(days=profile["weekday"])
            if profile["user_id"] in skip_latest and offset == 0:
                continue  # leave a gap so the agent sends reminders

            booking_dt = datetime.combine(booking_day, time.min, tzinfo=timezone.utc)
            docs.append(
                {
                    "user_id": profile["user_id"],
                    "user_name": profile["user_name"],
                    "whatsapp_number": profile["whatsapp_number"],
                    "court_name": profile["court_name"],
                    "date": booking_dt,
                    "is_regular_slot": True,
                }
            )
    return docs


def _upsert_records(collection, records: Iterable[Dict[str, Any]]) -> int:
    inserted = 0
    for doc in records:
        result = collection.update_one(
            {"user_id": doc["user_id"], "date": doc["date"]},
            {"$set": doc},
            upsert=True,
        )
        if result.upserted_id is not None or result.modified_count:
            inserted += 1
    return inserted


def _purge_users(collection, user_ids: List[str]) -> int:
    if not user_ids:
        return 0
    result = collection.delete_many({"user_id": {"$in": user_ids}})
    return result.deleted_count


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed Badminton booking data for the AI manager")
    parser.add_argument("--file", type=Path, help="Path to exported JSON/NDJSON bookings", required=False)
    parser.add_argument("--mock-weeks", type=int, default=8, help="Weeks of synthetic recurring data to generate")
    parser.add_argument(
        "--skip-latest",
        nargs="*",
        default=["demo_lara"],
        help="User IDs to skip for the most recent week to simulate absentees",
    )
    parser.add_argument(
        "--purge-user",
        nargs="*",
        default=[],
        help="User IDs to purge before inserting new data",
    )
    parser.add_argument("--dry-run", action="store_true", help="Parse and preview without writing to MongoDB")
    args = parser.parse_args()

    client = MongoClient(MONGODB_URL, tz_aware=True, tzinfo=timezone.utc)
    collection = client[DB_NAME][COLLECTION_NAME]

    payload: List[Dict[str, Any]] = []
    if args.file:
        print(f"Loading seed data from {args.file}")
        payload.extend(_read_json_records(args.file))

    if args.mock_weeks > 0:
        payload.extend(_generate_mock_records(args.mock_weeks, args.skip_latest))

    deleted = 0
    if args.purge_user:
        deleted = _purge_users(collection, args.purge_user)
        print(f"Purged {deleted} existing records for {args.purge_user}")

    if not payload:
        if deleted:
            print("Purge completed. No new records to insert.")
        else:
            print("No records to insert. Provide --file or set --mock-weeks > 0.")
        return

    if args.dry_run:
        print(f"Prepared {len(payload)} records (dry-run). Example:\n{payload[0]}")
        return

    inserted = _upsert_records(collection, payload)
    print(f"Upserted {inserted} booking records into {DB_NAME}.{COLLECTION_NAME}")


if __name__ == "__main__":
    main()

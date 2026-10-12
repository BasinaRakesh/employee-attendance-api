"""Loads sample documents from sample_data/ into MongoDB Atlas.
Usage: python sample_seed.py
Reads MONGO_URI and MONGO_DB from the .env file.
"""

import os
import pathlib

from bson import json_util
from dotenv import load_dotenv
from pymongo import MongoClient

# Load .env from the same folder as this script
BASE_DIR = pathlib.Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

if not MONGO_URI:
    raise ValueError(
        "MONGO_URI is missing. Check that your .env file exists "
        "in the candidate_kit folder and contains your Atlas connection string."
    )

if not MONGO_URI.startswith("mongodb+srv://"):
    raise ValueError(
        "MONGO_URI is not an Atlas connection string. "
        "Check the first line of your .env file."
    )

sample_data_dir = BASE_DIR / "sample_data"
options = json_util.JSONOptions(tz_aware=True)

print(f"Connecting to MongoDB Atlas database: {MONGO_DB}")

with MongoClient(
    MONGO_URI,
    tz_aware=True,
    serverSelectionTimeoutMS=15000,
) as client:
    # Verify the connection before modifying collections
    client.admin.command("ping")
    db = client[MONGO_DB]

    for name in ("employees", "attendance_logs"):
        file_path = sample_data_dir / f"{name}.json"

        if not file_path.exists():
            raise FileNotFoundError(f"Sample data file not found: {file_path}")

        docs = json_util.loads(
            file_path.read_text(encoding="utf-8"),
            json_options=options,
        )

        # Replace the sample documents in these two collections
        db[name].delete_many({})

        if docs:
            db[name].insert_many(docs)

        print(f"Loaded {len(docs):>2} documents into '{db.name}.{name}'")

print("Sample data loaded successfully.")
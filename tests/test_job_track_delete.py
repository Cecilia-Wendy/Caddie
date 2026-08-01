"""Regression: deleting a unified workspace row must not recreate it."""

from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db


def main():
    original_db_path = db.DB_PATH
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db.DB_PATH = Path(tmp) / "caddie.db"
            db.init_db()

            aid = db.create_application({
                "company": "TestCo",
                "role": "PM",
                "industry": "互联网",
                "applied_date": "2026-07-01",
                "status": "screening",
                "source": "测试",
            })
            application = db.get_application(aid)
            track_id = application["track_id"]

            assert db.delete_job_track(track_id)
            assert db.get_application(aid) is None
            assert db.get_job_track(track_id) is None

            # list_job_tracks() runs ensure_application_tracks(); the deleted
            # row must stay deleted instead of being rebuilt from an orphan.
            assert not [
                item for item in db.list_job_tracks()
                if item.get("company") == "TestCo" and item.get("role") == "PM"
            ]
            print("JOB_TRACK_DELETE_OK")
    finally:
        db.DB_PATH = original_db_path


if __name__ == "__main__":
    main()

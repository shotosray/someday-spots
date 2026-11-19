from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
import os
import sqlite3
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

# This file is created automatically on the first local run.  It is intentionally
# not committed, and can later be replaced by a hosted database connection.
DB_PATH = Path(__file__).parent / "friend_plans_local.db"

def connection():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db

def initialize_db():
    with connection() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS friends (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL COLLATE NOCASE,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS activities (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          friend_id INTEGER NOT NULL REFERENCES friends(id) ON DELETE CASCADE,
          place_name TEXT NOT NULL,
          maps_url TEXT NOT NULL,
          planning_notes TEXT,
          status TEXT NOT NULL DEFAULT 'todo' CHECK(status IN ('todo', 'done')),
          rating INTEGER CHECK(rating BETWEEN 1 AND 5),
          review TEXT,
          visit_date TEXT,
          created_at TEXT NOT NULL,
          completed_at TEXT
        );
        """)

@asynccontextmanager
async def lifespan(app: FastAPI):
    initialize_db()
    yield

app = FastAPI(title="Someday Spots API", lifespan=lifespan)
allowed_origins = [origin.strip() for origin in os.getenv("ALLOWED_ORIGINS", "http://localhost:5173").split(",") if origin.strip()]
app.add_middleware(CORSMiddleware, allow_origins=allowed_origins, allow_methods=["*"], allow_headers=["*"])

class FriendCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str):
        value = value.strip()
        if not value: raise ValueError("Name is required")
        return value

class ActivityCreate(BaseModel):
    place_name: str = Field(min_length=1, max_length=140)
    maps_url: str
    planning_notes: str | None = Field(default=None, max_length=1500)
    @field_validator("place_name")
    @classmethod
    def clean_place(cls, value: str): return value.strip()
    @field_validator("maps_url")
    @classmethod
    def valid_url(cls, value: str):
        parsed = urlparse(value.strip())
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Enter a valid HTTPS Google Maps link")
        host = parsed.netloc.lower()
        if "google." not in host and host != "maps.app.goo.gl":
            raise ValueError("Enter a Google Maps link")
        return value.strip()

class CompleteActivity(BaseModel):
    rating: int | None = Field(default=None, ge=1, le=5)
    review: str | None = Field(default=None, max_length=1500)
    visit_date: date | None = None

def one_friend(db, friend_id: int):
    friend = db.execute("SELECT * FROM friends WHERE id = ?", (friend_id,)).fetchone()
    if not friend: raise HTTPException(404, "Friend not found")
    return friend

@app.get("/api/friends")
def list_friends(q: str = Query(default="", max_length=80)):
    with connection() as db:
        return [dict(row) for row in db.execute("SELECT f.*, COUNT(a.id) AS activity_count FROM friends f LEFT JOIN activities a ON a.friend_id=f.id WHERE f.name LIKE ? GROUP BY f.id ORDER BY f.name COLLATE NOCASE", (f"%{q.strip()}%",))]

@app.post("/api/friends", status_code=201)
def create_friend(body: FriendCreate):
    with connection() as db:
        cursor = db.execute("INSERT INTO friends(name, created_at) VALUES (?, ?)", (body.name, datetime.now(timezone.utc).isoformat()))
        return dict(db.execute("SELECT * FROM friends WHERE id=?", (cursor.lastrowid,)).fetchone())

@app.get("/api/friends/{friend_id}")
def get_friend(friend_id: int):
    with connection() as db: return dict(one_friend(db, friend_id))

@app.get("/api/friends/{friend_id}/activities")
def list_activities(friend_id: int, status: Literal["todo", "done"] = "todo"):
    with connection() as db:
        one_friend(db, friend_id)
        ordering = "completed_at DESC" if status == "done" else "created_at DESC"
        return [dict(row) for row in db.execute(f"SELECT * FROM activities WHERE friend_id=? AND status=? ORDER BY {ordering}", (friend_id, status))]

@app.post("/api/friends/{friend_id}/activities", status_code=201)
def create_activity(friend_id: int, body: ActivityCreate):
    with connection() as db:
        one_friend(db, friend_id)
        now = datetime.now(timezone.utc).isoformat()
        cur = db.execute("INSERT INTO activities(friend_id, place_name, maps_url, planning_notes, created_at) VALUES (?, ?, ?, ?, ?)", (friend_id, body.place_name, body.maps_url, body.planning_notes.strip() if body.planning_notes else None, now))
        return dict(db.execute("SELECT * FROM activities WHERE id=?", (cur.lastrowid,)).fetchone())

@app.post("/api/activities/{activity_id}/complete")
def complete_activity(activity_id: int, body: CompleteActivity):
    with connection() as db:
        activity = db.execute("SELECT * FROM activities WHERE id=?", (activity_id,)).fetchone()
        if not activity: raise HTTPException(404, "Activity not found")
        if activity["status"] == "done": raise HTTPException(409, "This activity is already done")
        db.execute("UPDATE activities SET status='done', rating=?, review=?, visit_date=?, completed_at=? WHERE id=?", (body.rating, body.review.strip() if body.review else None, body.visit_date.isoformat() if body.visit_date else None, datetime.now(timezone.utc).isoformat(), activity_id))
        return dict(db.execute("SELECT * FROM activities WHERE id=?", (activity_id,)).fetchone())

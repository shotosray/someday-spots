from datetime import date, datetime, timezone
import os
from typing import Annotated, Literal
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from supabase import create_client

app = FastAPI(title="Someday Spots API")
allowed_origins = [origin.strip() for origin in os.getenv("ALLOWED_ORIGINS", "http://localhost:5173").split(",") if origin.strip()]
app.add_middleware(CORSMiddleware, allow_origins=allowed_origins, allow_methods=["*"], allow_headers=["*"])


class FriendCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str):
        value = value.strip()
        if not value:
            raise ValueError("Name is required")
        return value


class ActivityCreate(BaseModel):
    place_name: str = Field(min_length=1, max_length=140)
    maps_url: str
    planning_notes: str | None = Field(default=None, max_length=1500)

    @field_validator("place_name")
    @classmethod
    def clean_place(cls, value: str):
        return value.strip()

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


def supabase():
    url = os.getenv("SUPABASE_URL")
    secret = os.getenv("SUPABASE_SECRET_KEY")
    if not url or not secret:
        raise HTTPException(503, "The server is not connected to Supabase yet")
    return create_client(url, secret)


def current_user_id(authorization: Annotated[str | None, Header()] = None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Please sign in to continue")
    try:
        user = supabase().auth.get_user(authorization.removeprefix("Bearer ")).user
    except Exception as exc:
        raise HTTPException(401, "Your session has expired. Please sign in again.") from exc
    if not user:
        raise HTTPException(401, "Your session has expired. Please sign in again.")
    return str(user.id)


CurrentUser = Annotated[str, Depends(current_user_id)]


def first_or_404(response, message: str):
    if not response.data:
        raise HTTPException(404, message)
    return response.data[0]


@app.get("/api/friends")
def list_friends(owner_id: CurrentUser, q: str = Query(default="", max_length=80)):
    client = supabase()
    try:
        friends = client.table("friends").select("id,name,created_at").eq("owner_id", owner_id).ilike("name", f"%{q.strip()}%").order("name").execute().data
        activities = client.table("activities").select("friend_id").eq("owner_id", owner_id).execute().data
    except Exception as exc:
        raise HTTPException(502, "Unable to load your spots right now") from exc
    counts = {}
    for activity in activities:
        counts[activity["friend_id"]] = counts.get(activity["friend_id"], 0) + 1
    return [{**friend, "activity_count": counts.get(friend["id"], 0)} for friend in friends]


@app.post("/api/friends", status_code=201)
def create_friend(body: FriendCreate, owner_id: CurrentUser):
    try:
        response = supabase().table("friends").insert({"owner_id": owner_id, "name": body.name}).execute()
        return first_or_404(response, "Friend could not be created")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, "Unable to save that friend. Please try again.") from exc


def owned_friend(client, friend_id: str, owner_id: str):
    response = client.table("friends").select("id,name,created_at").eq("id", friend_id).eq("owner_id", owner_id).execute()
    return first_or_404(response, "Friend not found")


@app.get("/api/friends/{friend_id}")
def get_friend(friend_id: str, owner_id: CurrentUser):
    return owned_friend(supabase(), friend_id, owner_id)


@app.get("/api/friends/{friend_id}/activities")
def list_activities(friend_id: str, owner_id: CurrentUser, status: Literal["todo", "done"] = "todo"):
    client = supabase()
    owned_friend(client, friend_id, owner_id)
    ordering = "completed_at" if status == "done" else "created_at"
    try:
        return client.table("activities").select("*").eq("owner_id", owner_id).eq("friend_id", friend_id).eq("status", status).order(ordering, desc=True).execute().data
    except Exception as exc:
        raise HTTPException(502, "Unable to load spots right now") from exc


@app.post("/api/friends/{friend_id}/activities", status_code=201)
def create_activity(friend_id: str, body: ActivityCreate, owner_id: CurrentUser):
    client = supabase()
    owned_friend(client, friend_id, owner_id)
    activity = {
        "owner_id": owner_id,
        "friend_id": friend_id,
        "place_name": body.place_name,
        "maps_url": body.maps_url,
        "planning_notes": body.planning_notes.strip() if body.planning_notes else None,
    }
    try:
        response = client.table("activities").insert(activity).execute()
        return first_or_404(response, "Spot could not be created")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, "Unable to save that spot. Please try again.") from exc


@app.post("/api/activities/{activity_id}/complete")
def complete_activity(activity_id: str, body: CompleteActivity, owner_id: CurrentUser):
    client = supabase()
    activity = first_or_404(client.table("activities").select("id,status").eq("id", activity_id).eq("owner_id", owner_id).execute(), "Spot not found")
    if activity["status"] == "done":
        raise HTTPException(409, "This spot is already done")
    completed = {
        "status": "done",
        "rating": body.rating,
        "review": body.review.strip() if body.review else None,
        "visit_date": body.visit_date.isoformat() if body.visit_date else None,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        response = client.table("activities").update(completed).eq("id", activity_id).eq("owner_id", owner_id).execute()
        return first_or_404(response, "Spot could not be updated")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, "Unable to save your review. Please try again.") from exc


import os
import json
import asyncio
import datetime
import logging
from datetime import timedelta

from config import (
    DEFAULT_UPLOAD_MODE,
    DEFAULT_SPLIT_SETTING,
    DEFAULT_CAPTION_ENABLED,
    DEFAULT_THUMBNAIL_GENERATION,
    DEFAULT_GENERATE_SCREENSHOTS,
    DEFAULT_SAMPLE_VIDEO
)

logger = logging.getLogger(__name__)


class Database:
    def __init__(self):
        self.file = "database.json"
        self.lock = asyncio.Lock()

        if not os.path.exists(self.file):
            self._save({
                "users": [],
                "urls": [],
                "daily_tasks": []
            })

        logger.info("JSON Database initialized")

    def _load(self):
        try:
            with open(self.file, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"users": [], "urls": [], "daily_tasks": []}

    def _save(self, data):
        with open(self.file, "w") as f:
            json.dump(data, f, default=str, indent=4)

    async def initialize(self):
        logger.info("JSON Database ready")

    async def create_indexes(self):
        pass

    async def add_user(self, user_id, username=None):
        async with self.lock:
            data = self._load()
            user = next(
                (u for u in data["users"] if u["user_id"] == user_id),
                None
            )

            if user is None:
                user = {
                    "user_id": user_id,
                    "username": username,
                    "upload_mode": DEFAULT_UPLOAD_MODE,
                    "split_enabled": DEFAULT_SPLIT_SETTING,
                    "caption": None,
                    "caption_enabled": DEFAULT_CAPTION_ENABLED,
                    "thumbnail": None,
                    "generate_screenshots": DEFAULT_GENERATE_SCREENSHOTS,
                    "generate_sample_video": DEFAULT_SAMPLE_VIDEO,
                    "banned": False,
                    "is_paid": False,
                    "subscription_start": None,
                    "paid_expiry": None
                }
                data["users"].append(user)
            else:
                user["username"] = username

            self._save(data)
            return True

    async def get_user(self, user_id):
        async with self.lock:
            data = self._load()
            user = next(
                (u for u in data["users"] if u["user_id"] == user_id),
                None
            )

            if user and user.get("is_paid") and user.get("paid_expiry"):
                expiry = datetime.datetime.fromisoformat(user["paid_expiry"])

                if expiry < datetime.datetime.now():
                    user["is_paid"] = False
                    self._save(data)

            return user

    async def update_user_settings(self, user_id, settings):
        async with self.lock:
            data = self._load()
            user = next(
                (u for u in data["users"] if u["user_id"] == user_id),
                None
            )

            if user:
                user.update(settings)
                self._save(data)
                return True
            return False

    async def ban_user(self, user_id, banned=True):
        return await self.update_user_settings(
            user_id, {"banned": banned}
        )

    async def set_paid_status(self, user_id, is_paid=True, expiry_date=None):
        settings = {
            "is_paid": is_paid,
            "subscription_start": (
                datetime.datetime.now().isoformat() if is_paid else None
            )
        }

        if expiry_date:
            settings["paid_expiry"] = expiry_date.isoformat()

        return await self.update_user_settings(user_id, settings)

    async def get_subscription_details(self, user_id):
        user = await self.get_user(user_id)

        if not user:
            return None

        expiry = user.get("paid_expiry")
        expiry = datetime.datetime.fromisoformat(expiry) if expiry else None

        days = 0
        if user.get("is_paid") and expiry:
            days = max(0, (expiry - datetime.datetime.now()).days)

        return {
            "is_paid": user.get("is_paid", False),
            "subscription_start": user.get("subscription_start"),
            "expiry_date": expiry,
            "days_remaining": days
        }

    async def set_thumbnail(self, user_id, file_id):
        return await self.update_user_settings(
            user_id, {"thumbnail": file_id}
        )

    async def set_caption(self, user_id, caption):
        return await self.update_user_settings(
            user_id, {"caption": caption}
        )

    async def delete_caption(self, user_id):
        return await self.update_user_settings(
            user_id,
            {"caption": None, "caption_enabled": False}
        )

    async def get_all_users(self):
        async with self.lock:
            return self._load()["users"]

    async def get_banned_users(self):
        async with self.lock:
            return [
                u for u in self._load()["users"]
                if u.get("banned", False)
            ]

    async def get_paid_users(self):
        async with self.lock:
            data = self._load()
            now = datetime.datetime.now()
            valid = []

            for user in data["users"]:
                expiry = user.get("paid_expiry")

                if user.get("is_paid"):
                    if expiry and datetime.datetime.fromisoformat(expiry) < now:
                        user["is_paid"] = False
                    else:
                        valid.append(user)

            self._save(data)
            return valid

    async def store_url(self, url_id, url, user_id, status="pending"):
        async with self.lock:
            data = self._load()

            url_data = {
                "url_id": url_id,
                "url": url,
                "user_id": user_id,
                "status": status,
                "timestamp": datetime.datetime.now().isoformat()
            }

            existing = next(
                (u for u in data["urls"] if u["url_id"] == url_id),
                None
            )

            if existing:
                existing.update(url_data)
            else:
                data["urls"].append(url_data)

            self._save(data)
            return True

    async def update_url_status(self, url_id, status):
        async with self.lock:
            data = self._load()

            for url in data["urls"]:
                if url["url_id"] == url_id:
                    url["status"] = status
                    self._save(data)
                    return True

            return False

    async def track_daily_task(self, user_id):
        async with self.lock:
            data = self._load()
            today = datetime.datetime.now().strftime("%Y-%m-%d")

            task = next(
                (
                    t for t in data["daily_tasks"]
                    if t["user_id"] == user_id and t["date"] == today
                ),
                None
            )

            if task:
                task["count"] += 1
            else:
                task = {
                    "user_id": user_id,
                    "date": today,
                    "count": 1
                }
                data["daily_tasks"].append(task)

            self._save(data)
            return task["count"]

    async def get_daily_task_count(self, user_id):
        async with self.lock:
            today = datetime.datetime.now().strftime("%Y-%m-%d")

            task = next(
                (
                    t for t in self._load()["daily_tasks"]
                    if t["user_id"] == user_id and t["date"] == today
                ),
                None
            )

            return task.get("count", 0) if task else 0

    async def get_url(self, url_id):
        async with self.lock:
            return next(
                (
                    u for u in self._load()["urls"]
                    if u["url_id"] == url_id
                ),
                None
            )

    async def close(self):
        logger.info("JSON Database closed")
        
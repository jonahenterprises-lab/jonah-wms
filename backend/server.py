"""
JONAH ENTERPRISES - Door Installation Work Management System
FastAPI + MongoDB backend with JWT auth, role-based access, GPS check-in/out,
work reports with photo evidence, admin approval workflow, payment ledger.
"""

import os
import io
import base64
import math
import secrets
import uuid
import logging
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated, List, Optional

import certifi
import httpx
import jwt
from dotenv import load_dotenv
from fastapi import FastAPI, APIRouter, Depends, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse
from fastapi.security import OAuth2PasswordBearer
from motor.motor_asyncio import AsyncIOMotorClient
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from PIL import Image, ImageDraw, ImageFont

# --- Setup ---------------------------------------------------------------
ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

JWT_SECRET = os.environ.get("JWT_SECRET")
if not JWT_SECRET:
    # Fail loudly instead of silently minting a per-process random secret — a random
    # fallback would invalidate every outstanding token on each restart and make
    # replicas reject each other's tokens, which is easy to miss until it happens.
    raise RuntimeError("JWT_SECRET environment variable must be set (see backend/.env)")
JWT_ALGO = "HS256"
ACCESS_MINUTES = 60 * 24 * 30  # 30 days for field workers

PUSH_BASE_URL = "https://integrations.emergentagent.com"
PUSH_KEY = os.environ.get("EMERGENT_PUSH_KEY", "placeholder")

# Explicit allow-list — never combine allow_origins=["*"] with allow_credentials=True,
# Starlette echoes back the request Origin in that combo, effectively allowing any site.
CORS_ORIGINS = [o.strip() for o in os.environ.get(
    "CORS_ORIGINS", "http://localhost:3000,http://localhost:5173,http://localhost:19006"
).split(",") if o.strip()]

# Obviously-fake demo workers/sites/reports — never seed these against a real deployment.
SEED_DEMO_DATA = os.environ.get("SEED_DEMO_DATA", "true").lower() == "true"

_mongo_url = os.environ["MONGO_URL"]
# Passing tlsCAFile implicitly forces TLS in PyMongo even for a plain mongodb:// URL
# with no tls param — fine (required, even) for Atlas's mongodb+srv:// URLs, but it
# silently breaks a local/self-hosted mongod that isn't TLS-configured. Only apply it
# for +srv connection strings, which is the standard hallmark of an Atlas connection.
_mongo_kwargs = {"tlsCAFile": certifi.where()} if _mongo_url.startswith("mongodb+srv://") else {}
client = AsyncIOMotorClient(_mongo_url, **_mongo_kwargs)
db = client[os.environ["DB_NAME"]]

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
DUMMY_HASH = pwd_ctx.hash("dummy-timing-hash")
oauth2 = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jonah")

app = FastAPI(title="JONAH Enterprises WMS")
api = APIRouter(prefix="/api")


# --- Models --------------------------------------------------------------
class Role(str, Enum):
    worker = "Worker"
    admin = "Admin"
    client = "Client"


class LeaveStatus(str, Enum):
    pending = "Pending"
    approved = "Approved"
    rejected = "Rejected"


class ApprovalStatus(str, Enum):
    draft = "Draft"
    submitted = "Submitted"
    pending = "Pending"
    approved = "Approved"
    rejected = "Rejected"
    correction = "Correction"


class LoginIn(BaseModel):
    username: str
    password: str


class ChangePwdIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


class ForgotPwdIn(BaseModel):
    username: str


class UserOut(BaseModel):
    id: str
    username: str
    role: Role
    name: Optional[str] = None
    worker_id: Optional[str] = None


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class WorkerIn(BaseModel):
    employee_id: str
    name: str
    mobile: str
    username: str
    password: str = Field(min_length=8)
    joining_date: Optional[str] = None
    default_rate: float = 250.0
    notes: Optional[str] = ""


class WorkerUpdate(BaseModel):
    name: Optional[str] = None
    mobile: Optional[str] = None
    default_rate: Optional[float] = None
    status: Optional[str] = None
    notes: Optional[str] = None


class SiteIn(BaseModel):
    site_name: str
    client_name: str
    address: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    contact_person: Optional[str] = ""
    contact_number: Optional[str] = ""
    status: str = "Active"
    notes: Optional[str] = ""
    door_type_rates: dict = {}  # {door_type_id: rate} — per-site override of a door type's default rate
    target_doors: dict = {}  # {door_type_id: assigned_count} — omitted key = no ceiling for that type
    assigned_worker_ids: List[str] = []  # empty = open to every worker (unchanged legacy behavior)


class DoorTypeIn(BaseModel):
    name: str
    default_rate: float = Field(gt=0)
    status: str = "Active"


class CheckInIn(BaseModel):
    site_id: str
    latitude: float
    longitude: float
    accuracy: Optional[float] = None
    client_sync_id: Optional[str] = None  # idempotency


class CheckOutIn(BaseModel):
    session_id: str
    latitude: float
    longitude: float
    accuracy: Optional[float] = None
    work_completed: bool = True
    remarks: Optional[str] = ""


class DoorLineItem(BaseModel):
    door_type_id: str
    count: int = Field(gt=0)


class WorkReportIn(BaseModel):
    session_id: str
    doors: List[DoorLineItem] = Field(min_length=1)  # one entry per door type installed
    notes: Optional[str] = ""
    work_completed: bool = True
    before_photos: List[str] = []  # base64 strings
    after_photos: List[str] = []
    materials: List[dict] = []  # [{name, qty, unit, unit_cost}]
    client_sync_id: Optional[str] = None


class WorkReportUpdateIn(BaseModel):
    # Self-edit is scoped to counts/notes only, not photos — re-watermarking an
    # already-watermarked photo would stack a second strip on it, so photo edits
    # are deliberately out of scope here (withdraw and resubmit fresh instead).
    doors: List[DoorLineItem] = Field(min_length=1)
    notes: Optional[str] = ""
    work_completed: bool = True


class ApprovalIn(BaseModel):
    remarks: Optional[str] = ""
    edited_door_count: Optional[int] = None
    edited_rate: Optional[float] = None


class PaymentIn(BaseModel):
    worker_id: str
    amount: float = Field(gt=0)
    payment_date: Optional[str] = None
    payment_method: str = "Cash"
    transaction_reference: Optional[str] = ""
    notes: Optional[str] = ""
    client_sync_id: Optional[str] = None  # idempotency — a double-click must not double-pay


class SettingsIn(BaseModel):
    company_name: Optional[str] = None
    address: Optional[str] = None
    mobile: Optional[str] = None
    email: Optional[str] = None
    currency: Optional[str] = None
    default_rate: Optional[float] = None
    logo_base64: Optional[str] = None


# --- Helpers -------------------------------------------------------------
def now_utc():
    return datetime.now(timezone.utc)


def issue_token(user_doc: dict) -> str:
    payload = {
        "sub": user_doc["id"],
        "username": user_doc["username"],
        "role": user_doc["role"],
        "tv": user_doc.get("token_version", 0),
        "iat": now_utc(),
        "exp": now_utc() + timedelta(minutes=ACCESS_MINUTES),
        "jti": secrets.token_hex(8),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


async def get_user(token: Annotated[Optional[str], Depends(oauth2)]) -> dict:
    unauth = HTTPException(status_code=401, detail="Not authenticated")
    if not token:
        raise unauth
    try:
        p = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
        u = await db.users.find_one({"id": p["sub"], "disabled": {"$ne": True}}, {"_id": 0})
        if not u:
            raise unauth
        # Tokens issued before a password change carry a stale "tv" and are rejected,
        # so a leaked token stops working as soon as the user changes their password.
        if p.get("tv", 0) != u.get("token_version", 0):
            raise unauth
        return u
    except jwt.PyJWTError:
        raise unauth


def require_role(*roles: Role):
    async def _guard(u=Depends(get_user)):
        if u["role"] not in [r.value for r in roles]:
            raise HTTPException(status_code=403, detail="Forbidden")
        return u
    return _guard


def public_user(u: dict) -> UserOut:
    return UserOut(
        id=u["id"],
        username=u["username"],
        role=u["role"],
        name=u.get("name"),
        worker_id=u.get("worker_id"),
    )


async def get_door_type_rate(door_type_id: str, site_id: str) -> tuple:
    """Rate for a door type at a given site: site-specific override if set, else the
    door type's own default_rate. Returns (name, rate)."""
    door_type = await db.door_types.find_one({"id": door_type_id}, {"_id": 0})
    if not door_type:
        raise HTTPException(400, f"Unknown door type: {door_type_id}")
    site = await db.sites.find_one({"id": site_id}, {"_id": 0})
    override = (site or {}).get("door_type_rates", {}).get(door_type_id)
    rate = float(override) if override is not None else float(door_type["default_rate"])
    return door_type["name"], rate


async def get_effective_rate(worker_id: str, site_id: str) -> float:
    """Legacy flat-rate fallback — used only where no door type applies (e.g. admin
    manually editing a report's total at approval time without changing its breakdown)."""
    worker = await db.workers.find_one({"id": worker_id}, {"_id": 0})
    if worker and worker.get("default_rate"):
        return float(worker["default_rate"])
    settings = await db.settings.find_one({"id": "global"}, {"_id": 0})
    if settings and settings.get("default_rate"):
        return float(settings["default_rate"])
    return 250.0


GEOFENCE_WARNING_METERS = 500  # generous buffer over typical phone GPS drift (10-50m)


def haversine_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371000  # Earth radius, meters
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


async def get_installed_count(site_id: str, door_type_id: str, exclude_report_id: Optional[str] = None) -> int:
    """Sum of this door type already recorded at this site across every report that
    isn't Rejected — Pending/Correction count too, so the ceiling can't be worked
    around by racing multiple pending submissions ahead of admin review.
    exclude_report_id lets an edit re-check the ceiling without double-counting
    the very report being edited against itself."""
    total = 0
    async for r in db.work_reports.find(
        {"site_id": site_id, "approval_status": {"$ne": ApprovalStatus.rejected.value}},
        {"_id": 0, "id": 1, "door_breakdown": 1},
    ):
        if exclude_report_id and r.get("id") == exclude_report_id:
            continue
        for item in r.get("door_breakdown", []):
            if item.get("door_type_id") == door_type_id:
                total += item.get("count", 0)
    return total


async def add_notification(user_id: str, title: str, message: str, action_url: Optional[str] = None):
    await db.notifications.insert_one({
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "title": title,
        "message": message,
        "is_read": False,
        "created_at": now_utc().isoformat(),
    })
    # fire push (non-blocking)
    try:
        data = {"title": title, "message": message}
        if action_url:
            data["action_url"] = action_url
        await send_push([user_id], data)
    except Exception as e:
        logger.warning(f"push send failed (non-blocking): {e}")


async def audit(user: dict, action: str, entity_type: str, entity_id: str, old=None, new=None):
    await db.audit_logs.insert_one({
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "user_name": user.get("name") or user.get("username"),
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "old_data": old,
        "new_data": new,
        "created_at": now_utc().isoformat(),
    })


def clean(doc: dict) -> dict:
    if doc and "_id" in doc:
        doc.pop("_id", None)
    return doc


# --- Photo watermark -----------------------------------------------------
MAX_PHOTO_B64_CHARS = 12_000_000  # ~9MB decoded — generous ceiling for a phone photo


def _photo_too_large(b64_data_uri: str) -> bool:
    raw = b64_data_uri.split(",", 1)[1] if "," in b64_data_uri else b64_data_uri
    return len(raw) > MAX_PHOTO_B64_CHARS


def _watermark_photo(b64_data_uri: str, worker_name: str, site_name: str,
                     lat: Optional[float], lng: Optional[float],
                     company: str = "JONAH ENTERPRISES") -> str:
    """Adds a semi-transparent brown/gold info strip to the bottom of a photo.
    Returns the watermarked photo as a base64 data URI (JPEG). If anything
    fails we return the original untouched so we never lose the worker's
    evidence."""
    try:
        # Parse "data:image/...;base64,XXXX" or bare base64
        raw = b64_data_uri.split(",", 1)[1] if "," in b64_data_uri else b64_data_uri
        img = Image.open(io.BytesIO(base64.b64decode(raw))).convert("RGB")
        w, h = img.size
        # Downscale huge photos (max width 1600) to keep documents small
        if w > 1600:
            ratio = 1600 / w
            img = img.resize((1600, int(h * ratio)))
            w, h = img.size

        strip_h = max(90, int(h * 0.14))
        strip = Image.new("RGBA", (w, strip_h), (43, 32, 25, 210))  # brown 82% alpha
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        overlay.paste(strip, (0, h - strip_h))
        draw = ImageDraw.Draw(overlay)

        try:
            font_lg = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", int(strip_h * 0.22))
            font_sm = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", int(strip_h * 0.15))
        except Exception:
            font_lg = ImageFont.load_default()
            font_sm = ImageFont.load_default()

        ts = now_utc().strftime("%d/%m/%Y %H:%M UTC")
        gps_txt = f"GPS: {lat:.5f}, {lng:.5f}" if lat is not None and lng is not None else "GPS: unavailable"
        pad = int(strip_h * 0.12)
        y0 = h - strip_h + pad
        draw.text((pad, y0), company, fill=(192, 152, 54, 255), font=font_lg)  # gold
        line2_y = y0 + int(strip_h * 0.30)
        draw.text((pad, line2_y), f"Site: {site_name}   Worker: {worker_name}",
                  fill=(245, 235, 225, 255), font=font_sm)
        line3_y = line2_y + int(strip_h * 0.22)
        draw.text((pad, line3_y), f"{ts}   {gps_txt}",
                  fill=(245, 235, 225, 255), font=font_sm)

        merged = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        out = io.BytesIO()
        merged.save(out, format="JPEG", quality=75, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()
    except Exception as e:
        logger.warning(f"Watermark failed, keeping original: {e}")
        return b64_data_uri


# --- Push notifications (Emergent managed) -------------------------------
_push_client = httpx.AsyncClient(
    base_url=PUSH_BASE_URL,
    headers={"X-Push-Key": PUSH_KEY},
    timeout=10.0,
)


async def send_push(recipients: List[str], data: dict, idempotency_key: Optional[str] = None) -> None:
    """Relay push notifications through the Emergent managed service.
    Never blocks the primary operation — callers should still catch."""
    if not recipients:
        return
    if "title" not in data or "message" not in data:
        raise ValueError("push data must include title and message")
    # chunk to 100 per playbook
    for i in range(0, len(recipients), 100):
        chunk = recipients[i:i + 100]
        payload = {"recipients": chunk, "data": data}
        if idempotency_key:
            payload["$idempotency_key"] = f"{idempotency_key}-{i}"
        try:
            resp = await _push_client.post("/api/v1/push/trigger", json=payload)
            if resp.status_code >= 400:
                logger.warning(f"Push relay non-2xx: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            logger.warning(f"Push relay error (non-blocking): {e}")


class RegisterPushIn(BaseModel):
    user_id: str
    platform: str
    device_token: str


# --- Auth endpoints ------------------------------------------------------
# In-process login throttle. This resets on restart and doesn't share state
# across multiple workers/replicas — good enough as a baseline, but a real
# multi-instance deployment should move this to Redis or similar.
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 15 * 60
_login_attempts: dict[str, list[float]] = {}


def _login_rate_limited(key: str) -> bool:
    now = now_utc().timestamp()
    attempts = [t for t in _login_attempts.get(key, []) if now - t < LOGIN_WINDOW_SECONDS]
    _login_attempts[key] = attempts
    return len(attempts) >= LOGIN_MAX_ATTEMPTS


def _record_login_failure(key: str):
    _login_attempts.setdefault(key, []).append(now_utc().timestamp())


@api.post("/auth/login", response_model=TokenOut)
async def login(body: LoginIn):
    key = body.username.lower().strip()
    if _login_rate_limited(key):
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")
    u = await db.users.find_one({"username": key}, {"_id": 0})
    valid = pwd_ctx.verify(body.password, u["password_hash"]) if u else pwd_ctx.verify(body.password, DUMMY_HASH)
    if not u or not valid or u.get("disabled"):
        _record_login_failure(key)
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    _login_attempts.pop(key, None)
    return TokenOut(access_token=issue_token(u), user=public_user(u))


@api.get("/auth/me", response_model=UserOut)
async def me(u=Depends(get_user)):
    return public_user(u)


@api.post("/auth/change-password")
async def change_pwd(body: ChangePwdIn, u=Depends(get_user)):
    if not pwd_ctx.verify(body.current_password, u["password_hash"]):
        raise HTTPException(status_code=400, detail="Current password incorrect")
    await db.users.update_one(
        {"id": u["id"]},
        {"$set": {"password_hash": pwd_ctx.hash(body.new_password)}, "$inc": {"token_version": 1}}
    )
    return {"ok": True}


@api.post("/auth/forgot-password")
async def forgot(body: ForgotPwdIn):
    # In dev: reset to default password "worker123" if user exists (admin-controlled).
    # Real prod would send email. We keep response identical.
    u = await db.users.find_one({"username": body.username.lower().strip()})
    if u:
        logger.info(f"Password reset requested for {body.username}")
    return {"message": "If the account exists, contact your administrator for a password reset."}


# --- Sites ---------------------------------------------------------------
@api.get("/sites")
async def list_sites(u=Depends(get_user), status_f: Optional[str] = Query(None, alias="status")):
    q = {}
    if status_f:
        q["status"] = status_f
    elif u["role"] == Role.worker.value:
        q["status"] = "Active"
    if u["role"] == Role.worker.value:
        # A site with no team set is open to everyone (legacy behavior for existing
        # sites); once an admin picks a team, only those workers see/can check into it.
        q["$or"] = [
            {"assigned_worker_ids": {"$exists": False}},
            {"assigned_worker_ids": {"$size": 0}},
            {"assigned_worker_ids": u.get("worker_id")},
        ]
    if u["role"] == Role.client.value:
        # Clients only ever see sites they're assigned to, regardless of ?status=
        q["id"] = {"$in": u.get("site_ids") or []}
    items = await db.sites.find(q, {"_id": 0}).to_list(1000)
    return items


@api.post("/sites")
async def create_site(body: SiteIn, u=Depends(require_role(Role.admin))):
    doc = body.dict()
    doc["id"] = str(uuid.uuid4())
    doc["created_at"] = now_utc().isoformat()
    await db.sites.insert_one(doc.copy())
    await audit(u, "create", "site", doc["id"], None, doc)
    return clean(doc)


@api.put("/sites/{site_id}")
async def update_site(site_id: str, body: SiteIn, u=Depends(require_role(Role.admin))):
    old = await db.sites.find_one({"id": site_id}, {"_id": 0})
    if not old:
        raise HTTPException(404, "Site not found")
    upd = body.dict()
    await db.sites.update_one({"id": site_id}, {"$set": upd})
    await audit(u, "update", "site", site_id, old, upd)
    return {**old, **upd}


@api.delete("/sites/{site_id}")
async def delete_site(site_id: str, u=Depends(require_role(Role.admin))):
    await db.sites.update_one({"id": site_id}, {"$set": {"status": "Disabled"}})
    await audit(u, "disable", "site", site_id)
    return {"ok": True}


# --- Door Types ------------------------------------------------------------
@api.get("/door-types")
async def list_door_types(u=Depends(get_user), status_f: Optional[str] = Query(None, alias="status")):
    q = {}
    if status_f:
        q["status"] = status_f
    elif u["role"] == Role.worker.value:
        q["status"] = "Active"  # workers only pick from active types when submitting reports
    return await db.door_types.find(q, {"_id": 0}).sort("name", 1).to_list(200)


@api.post("/door-types")
async def create_door_type(body: DoorTypeIn, u=Depends(require_role(Role.admin))):
    doc = body.dict()
    doc["id"] = str(uuid.uuid4())
    doc["created_at"] = now_utc().isoformat()
    await db.door_types.insert_one(doc.copy())
    await audit(u, "create", "door_type", doc["id"], None, doc)
    return clean(doc)


@api.put("/door-types/{door_type_id}")
async def update_door_type(door_type_id: str, body: DoorTypeIn, u=Depends(require_role(Role.admin))):
    old = await db.door_types.find_one({"id": door_type_id}, {"_id": 0})
    if not old:
        raise HTTPException(404, "Door type not found")
    upd = body.dict()
    await db.door_types.update_one({"id": door_type_id}, {"$set": upd})
    await audit(u, "update", "door_type", door_type_id, old, upd)
    return {**old, **upd}


@api.get("/sites/{site_id}/progress")
async def site_door_progress(site_id: str, u=Depends(get_user)):
    """Per-door-type target/installed/remaining at this site, for door types that
    have a target set. Lets the report form show remaining quantity before a
    worker submits, instead of only rejecting after the fact."""
    site = await db.sites.find_one({"id": site_id}, {"_id": 0})
    if not site:
        raise HTTPException(404, "Site not found")
    targets = site.get("target_doors", {}) or {}
    door_types = await db.door_types.find({"status": "Active"}, {"_id": 0}).to_list(200)
    rows = []
    for dt in door_types:
        target = targets.get(dt["id"])
        installed = await get_installed_count(site_id, dt["id"]) if target is not None else None
        rows.append({
            "door_type_id": dt["id"],
            "door_type_name": dt["name"],
            "target": target,
            "installed": installed,
            "remaining": max(target - installed, 0) if target is not None else None,
        })
    return rows


# --- Workers -------------------------------------------------------------
@api.get("/workers")
async def list_workers(u=Depends(require_role(Role.admin))):
    items = await db.workers.find({}, {"_id": 0}).to_list(1000)
    return items


@api.post("/workers")
async def create_worker(body: WorkerIn, u=Depends(require_role(Role.admin))):
    if await db.users.find_one({"username": body.username.lower().strip()}):
        raise HTTPException(400, "Username already exists")
    worker_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    user_doc = {
        "id": user_id,
        "username": body.username.lower().strip(),
        "password_hash": pwd_ctx.hash(body.password),
        "role": Role.worker.value,
        "name": body.name,
        "worker_id": worker_id,
        "disabled": False,
        "created_at": now_utc().isoformat(),
    }
    worker_doc = {
        "id": worker_id,
        "user_id": user_id,
        "employee_id": body.employee_id,
        "name": body.name,
        "mobile": body.mobile,
        "username": body.username.lower().strip(),
        "joining_date": body.joining_date or now_utc().date().isoformat(),
        "default_rate": body.default_rate,
        "status": "Active",
        "notes": body.notes or "",
        "created_at": now_utc().isoformat(),
    }
    await db.users.insert_one(user_doc.copy())
    await db.workers.insert_one(worker_doc.copy())
    await audit(u, "create", "worker", worker_id, None, {"name": body.name})
    return clean(worker_doc)


@api.put("/workers/{worker_id}")
async def update_worker(worker_id: str, body: WorkerUpdate, u=Depends(require_role(Role.admin))):
    old = await db.workers.find_one({"id": worker_id}, {"_id": 0})
    if not old:
        raise HTTPException(404, "Worker not found")
    upd = {k: v for k, v in body.dict().items() if v is not None}
    if upd:
        await db.workers.update_one({"id": worker_id}, {"$set": upd})
        if "status" in upd:
            disabled = upd["status"] != "Active"
            await db.users.update_one({"worker_id": worker_id}, {"$set": {"disabled": disabled}})
        if "name" in upd:
            await db.users.update_one({"worker_id": worker_id}, {"$set": {"name": upd["name"]}})
    await audit(u, "update", "worker", worker_id, old, upd)
    return {**old, **upd}


@api.post("/workers/{worker_id}/reset-password")
async def reset_worker_pwd(worker_id: str, body: dict, u=Depends(require_role(Role.admin))):
    new_pwd = body.get("new_password", "ChangeMe123")
    if len(new_pwd) < 8:
        raise HTTPException(400, "Password too short (min 8 characters)")
    r = await db.users.update_one(
        {"worker_id": worker_id},
        {"$set": {"password_hash": pwd_ctx.hash(new_pwd)}, "$inc": {"token_version": 1}}
    )
    if not r.matched_count:
        raise HTTPException(404, "Worker user not found")
    await audit(u, "reset-password", "worker", worker_id)
    return {"ok": True}


# --- Attendance ----------------------------------------------------------
@api.post("/attendance/check-in")
async def check_in(body: CheckInIn, u=Depends(require_role(Role.worker, Role.admin))):
    worker_id = u.get("worker_id")
    if not worker_id:
        raise HTTPException(400, "Only workers can check in")
    # idempotency — scoped to this worker so a replayed/guessed client_sync_id
    # can't be used to read back another worker's check-in data
    if body.client_sync_id:
        existing = await db.attendance_sessions.find_one(
            {"client_sync_id": body.client_sync_id, "worker_id": worker_id}, {"_id": 0}
        )
        if existing:
            return existing
    # active session guard
    active = await db.attendance_sessions.find_one(
        {"worker_id": worker_id, "check_out_time": None}, {"_id": 0}
    )
    if active:
        raise HTTPException(400, "You have an active site. Check out first.")
    site = await db.sites.find_one({"id": body.site_id}, {"_id": 0})
    if not site or site.get("status") != "Active":
        raise HTTPException(400, "Site is not active")
    assigned = site.get("assigned_worker_ids") or []
    if assigned and worker_id not in assigned:
        raise HTTPException(403, "You are not assigned to this site.")
    geo_distance_m = None
    geo_warning = False
    if site.get("latitude") is not None and site.get("longitude") is not None:
        geo_distance_m = round(haversine_meters(body.latitude, body.longitude, site["latitude"], site["longitude"]))
        geo_warning = geo_distance_m > GEOFENCE_WARNING_METERS
    session_id = str(uuid.uuid4())
    doc = {
        "id": session_id,
        "worker_id": worker_id,
        "worker_name": u.get("name"),
        "site_id": body.site_id,
        "site_name": site["site_name"],
        "check_in_time": now_utc().isoformat(),
        "check_in_latitude": body.latitude,
        "check_in_longitude": body.longitude,
        "check_in_accuracy": body.accuracy,
        "check_in_geo_warning": geo_warning,
        "check_in_geo_distance_m": geo_distance_m,
        "check_out_time": None,
        "check_out_latitude": None,
        "check_out_longitude": None,
        "check_out_geo_warning": False,
        "check_out_geo_distance_m": None,
        "work_completed": None,
        "remarks": "",
        "created_at": now_utc().isoformat(),
    }
    if body.client_sync_id:
        doc["client_sync_id"] = body.client_sync_id
    await db.attendance_sessions.insert_one(doc.copy())
    # notify admins — never blocks the check-in itself, just flags it for a human look
    msg = f"{u.get('name')} checked in at {site['site_name']}."
    if geo_warning:
        msg += f" ⚠️ {geo_distance_m}m from the site's recorded location — please verify."
    async for admin in db.users.find({"role": Role.admin.value}, {"_id": 0, "id": 1}):
        await add_notification(admin["id"], "Worker Check-In", msg)
    return clean(doc)


@api.post("/attendance/check-out")
async def check_out(body: CheckOutIn, u=Depends(require_role(Role.worker, Role.admin))):
    sess = await db.attendance_sessions.find_one({"id": body.session_id}, {"_id": 0})
    if not sess:
        raise HTTPException(404, "Session not found")
    if sess["worker_id"] != u.get("worker_id"):
        raise HTTPException(403, "Not your session")
    if sess.get("check_out_time"):
        raise HTTPException(400, "Already checked out")
    site = await db.sites.find_one({"id": sess["site_id"]}, {"_id": 0})
    geo_distance_m = None
    geo_warning = False
    if site and site.get("latitude") is not None and site.get("longitude") is not None:
        geo_distance_m = round(haversine_meters(body.latitude, body.longitude, site["latitude"], site["longitude"]))
        geo_warning = geo_distance_m > GEOFENCE_WARNING_METERS
    upd = {
        "check_out_time": now_utc().isoformat(),
        "check_out_latitude": body.latitude,
        "check_out_longitude": body.longitude,
        "check_out_accuracy": body.accuracy,
        "check_out_geo_warning": geo_warning,
        "check_out_geo_distance_m": geo_distance_m,
        "work_completed": body.work_completed,
        "remarks": body.remarks or "",
    }
    await db.attendance_sessions.update_one({"id": body.session_id}, {"$set": upd})
    msg = f"{u.get('name')} checked out from {sess['site_name']}."
    if geo_warning:
        msg += f" ⚠️ {geo_distance_m}m from the site's recorded location — please verify."
    async for admin in db.users.find({"role": Role.admin.value}, {"_id": 0, "id": 1}):
        await add_notification(admin["id"], "Worker Check-Out", msg)
    return {**sess, **upd}


class ForceCheckoutIn(BaseModel):
    remarks: Optional[str] = ""


@api.post("/attendance/{session_id}/force-checkout")
async def force_checkout(session_id: str, body: ForceCheckoutIn, u=Depends(require_role(Role.admin))):
    """Admin-initiated close for a session the worker forgot to check out of —
    no GPS captured since the admin wasn't physically there."""
    sess = await db.attendance_sessions.find_one({"id": session_id}, {"_id": 0})
    if not sess:
        raise HTTPException(404, "Session not found")
    if sess.get("check_out_time"):
        raise HTTPException(400, "Already checked out")
    upd = {
        "check_out_time": now_utc().isoformat(),
        "work_completed": None,  # unobserved — admin wasn't there to confirm
        "remarks": body.remarks or "Closed by admin — worker did not check out.",
        "force_closed_by": u["id"],
        "force_closed_by_name": u.get("name") or u.get("username"),
    }
    await db.attendance_sessions.update_one({"id": session_id}, {"$set": upd})
    await audit(u, "force-checkout", "attendance", session_id, sess, upd)
    wu = await db.users.find_one({"worker_id": sess["worker_id"]}, {"_id": 0, "id": 1})
    if wu:
        await add_notification(
            wu["id"], "Checked Out By Admin",
            f"Your session at {sess.get('site_name')} was closed by an admin — you didn't check out.",
        )
    return {**sess, **upd}


@api.get("/attendance")
async def list_attendance(
    u=Depends(get_user),
    worker_id: Optional[str] = None,
    site_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    q = {}
    if u["role"] == Role.worker.value:
        q["worker_id"] = u.get("worker_id")
    elif worker_id:
        q["worker_id"] = worker_id
    if site_id:
        q["site_id"] = site_id
    if date_from:
        q.setdefault("check_in_time", {})["$gte"] = date_from
    if date_to:
        q.setdefault("check_in_time", {})["$lte"] = date_to + "T23:59:59"
    items = await db.attendance_sessions.find(q, {"_id": 0}).sort("check_in_time", -1).to_list(1000)
    return items


@api.get("/attendance/active")
async def active_session(u=Depends(require_role(Role.worker, Role.admin))):
    if not u.get("worker_id"):
        return None
    s = await db.attendance_sessions.find_one(
        {"worker_id": u["worker_id"], "check_out_time": None}, {"_id": 0}
    )
    return s


# --- Work reports --------------------------------------------------------
@api.post("/work-reports")
async def create_report(body: WorkReportIn, u=Depends(require_role(Role.worker, Role.admin))):
    # idempotency — scoped to this worker, same reasoning as check_in above
    if body.client_sync_id:
        existing = await db.work_reports.find_one(
            {"client_sync_id": body.client_sync_id, "worker_id": u.get("worker_id")}, {"_id": 0}
        )
        if existing:
            return existing
    sess = await db.attendance_sessions.find_one({"id": body.session_id}, {"_id": 0})
    if not sess:
        raise HTTPException(404, "Session not found")
    if sess["worker_id"] != u.get("worker_id"):
        raise HTTPException(403, "Not your session")
    if len(body.before_photos) < 1 or len(body.after_photos) < 1:
        raise HTTPException(400, "At least 1 before and 1 after photo required")
    if any(_photo_too_large(p) for p in body.before_photos + body.after_photos):
        raise HTTPException(400, "One or more photos exceed the size limit (~9MB each)")
    # Enforce each door type's assigned quantity at this site, if the admin set one.
    # Checked before touching photos so a worker gets fast feedback instead of a
    # rejection after already uploading evidence for a count that was never going in.
    site = await db.sites.find_one({"id": sess["site_id"]}, {"_id": 0})
    targets = (site or {}).get("target_doors", {}) or {}
    for item in body.doors:
        target = targets.get(item.door_type_id)
        if target is None:
            continue
        installed = await get_installed_count(sess["site_id"], item.door_type_id)
        remaining = target - installed
        if item.count > remaining:
            door_type = await db.door_types.find_one({"id": item.door_type_id}, {"_id": 0})
            type_name = door_type["name"] if door_type else "this door type"
            raise HTTPException(
                400,
                f"Only {max(remaining, 0)} {type_name} remaining at this site "
                f"(target: {target}, already recorded: {installed}). Ask an admin to "
                f"raise the target if this is intentional.",
            )
    # server-computed breakdown & total (never trust client-supplied rates)
    door_breakdown = []
    door_count = 0
    total = 0.0
    for item in body.doors:
        name, rate = await get_door_type_rate(item.door_type_id, sess["site_id"])
        subtotal = round(item.count * rate, 2)
        door_breakdown.append({
            "door_type_id": item.door_type_id,
            "door_type_name": name,
            "rate": rate,
            "count": item.count,
            "subtotal": subtotal,
        })
        door_count += item.count
        total += subtotal
    total = round(total, 2)
    # Watermark all photos server-side
    lat_ci = sess.get("check_in_latitude")
    lng_ci = sess.get("check_in_longitude")
    wm_before = [_watermark_photo(p, sess.get("worker_name") or "-", sess.get("site_name") or "-", lat_ci, lng_ci) for p in body.before_photos]
    wm_after = [_watermark_photo(p, sess.get("worker_name") or "-", sess.get("site_name") or "-", lat_ci, lng_ci) for p in body.after_photos]
    report_id = str(uuid.uuid4())
    doc = {
        "id": report_id,
        "session_id": body.session_id,
        "worker_id": sess["worker_id"],
        "worker_name": sess.get("worker_name"),
        "site_id": sess["site_id"],
        "site_name": sess.get("site_name"),
        "work_date": now_utc().date().isoformat(),
        "door_count": door_count,
        "rate_per_door": round(total / door_count, 2) if door_count else 0,
        "door_breakdown": door_breakdown,
        "total_amount": total,
        "notes": body.notes or "",
        "work_completed": body.work_completed,
        "before_photos": wm_before,
        "after_photos": wm_after,
        "materials": body.materials or [],
        "approval_status": ApprovalStatus.pending.value,
        "approval_remarks": "",
        "approved_by": None,
        "approved_at": None,
        "created_at": now_utc().isoformat(),
    }
    if body.client_sync_id:
        doc["client_sync_id"] = body.client_sync_id
    await db.work_reports.insert_one(doc.copy())
    async for admin in db.users.find({"role": Role.admin.value}, {"_id": 0, "id": 1}):
        await add_notification(admin["id"], "New Work Report", f"{sess.get('worker_name')} submitted a report ({door_count} doors).")
    return clean(doc)


@api.put("/work-reports/{report_id}")
async def update_report(report_id: str, body: WorkReportUpdateIn, u=Depends(require_role(Role.worker))):
    """Worker self-edit of their own still-open report — fixes the 'wait for an
    admin to notice a typo' gap. Only while Pending or Correction; editing a
    Correction resets it to Pending since it needs a fresh look either way."""
    r = await db.work_reports.find_one({"id": report_id}, {"_id": 0})
    if not r:
        raise HTTPException(404, "Not found")
    if r["worker_id"] != u.get("worker_id"):
        raise HTTPException(403, "Not your report")
    if r["approval_status"] not in (ApprovalStatus.pending.value, ApprovalStatus.correction.value):
        raise HTTPException(400, "Only a Pending or Correction report can be edited")
    site_id = r["site_id"]
    site = await db.sites.find_one({"id": site_id}, {"_id": 0})
    targets = (site or {}).get("target_doors", {}) or {}
    for item in body.doors:
        target = targets.get(item.door_type_id)
        if target is None:
            continue
        installed = await get_installed_count(site_id, item.door_type_id, exclude_report_id=report_id)
        remaining = target - installed
        if item.count > remaining:
            door_type = await db.door_types.find_one({"id": item.door_type_id}, {"_id": 0})
            type_name = door_type["name"] if door_type else "this door type"
            raise HTTPException(
                400,
                f"Only {max(remaining, 0)} {type_name} remaining at this site "
                f"(target: {target}, already recorded: {installed}).",
            )
    door_breakdown = []
    door_count = 0
    total = 0.0
    for item in body.doors:
        name, rate = await get_door_type_rate(item.door_type_id, site_id)
        subtotal = round(item.count * rate, 2)
        door_breakdown.append({
            "door_type_id": item.door_type_id,
            "door_type_name": name,
            "rate": rate,
            "count": item.count,
            "subtotal": subtotal,
        })
        door_count += item.count
        total += subtotal
    total = round(total, 2)
    upd = {
        "door_count": door_count,
        "rate_per_door": round(total / door_count, 2) if door_count else 0,
        "door_breakdown": door_breakdown,
        "total_amount": total,
        "notes": body.notes or "",
        "work_completed": body.work_completed,
        "approval_status": ApprovalStatus.pending.value,
    }
    await db.work_reports.update_one({"id": report_id}, {"$set": upd})
    await audit(u, "update", "work_report", report_id, r, upd)
    async for admin in db.users.find({"role": Role.admin.value}, {"_id": 0, "id": 1}):
        await add_notification(admin["id"], "Work Report Updated", f"{r.get('worker_name')} updated a report ({door_count} doors).")
    return {**r, **upd}


@api.delete("/work-reports/{report_id}")
async def withdraw_report(report_id: str, u=Depends(require_role(Role.worker))):
    """Worker withdraws their own report entirely, e.g. to redo it with different
    photos — self-edit above deliberately can't touch photos, this is the escape
    hatch for when photos are the thing that's wrong."""
    r = await db.work_reports.find_one({"id": report_id}, {"_id": 0})
    if not r:
        raise HTTPException(404, "Not found")
    if r["worker_id"] != u.get("worker_id"):
        raise HTTPException(403, "Not your report")
    if r["approval_status"] not in (ApprovalStatus.pending.value, ApprovalStatus.correction.value):
        raise HTTPException(400, "Only a Pending or Correction report can be withdrawn")
    await db.work_reports.delete_one({"id": report_id})
    await audit(u, "delete", "work_report", report_id, r, None)
    return {"ok": True}


@api.get("/work-reports")
async def list_reports(
    u=Depends(get_user),
    approval_status: Optional[str] = None,
    worker_id: Optional[str] = None,
    site_id: Optional[str] = None,
):
    q = {}
    if u["role"] == Role.worker.value:
        q["worker_id"] = u.get("worker_id")
    elif worker_id:
        q["worker_id"] = worker_id
    if approval_status:
        q["approval_status"] = approval_status
    if site_id:
        q["site_id"] = site_id
    items = await db.work_reports.find(q, {"_id": 0}).sort("created_at", -1).to_list(500)
    return items


@api.get("/work-reports/{report_id}")
async def get_report(report_id: str, u=Depends(get_user)):
    r = await db.work_reports.find_one({"id": report_id}, {"_id": 0})
    if not r:
        raise HTTPException(404, "Not found")
    if u["role"] == Role.worker.value and r["worker_id"] != u.get("worker_id"):
        raise HTTPException(403, "Forbidden")
    return r


async def _approval_action(report_id: str, new_status: str, body: ApprovalIn, u: dict):
    r = await db.work_reports.find_one({"id": report_id}, {"_id": 0})
    if not r:
        raise HTTPException(404, "Not found")
    upd = {
        "approval_status": new_status,
        "approval_remarks": body.remarks or "",
        "approved_by": u["id"],
        "approved_by_name": u.get("name") or u.get("username"),
        "approved_at": now_utc().isoformat(),
    }
    if new_status == ApprovalStatus.approved.value:
        # allow admin edit at approval time
        dc = body.edited_door_count if body.edited_door_count is not None else r["door_count"]
        rate = body.edited_rate if body.edited_rate is not None else r["rate_per_door"]
        upd["door_count"] = dc
        upd["rate_per_door"] = rate
        upd["total_amount"] = round(dc * rate, 2)
    await db.work_reports.update_one({"id": report_id}, {"$set": upd})
    await audit(u, f"report-{new_status.lower()}", "work_report", report_id, r, upd)
    # notify worker
    worker_user = await db.users.find_one({"worker_id": r["worker_id"]}, {"_id": 0, "id": 1})
    if worker_user:
        title_map = {
            ApprovalStatus.approved.value: "Work Approved",
            ApprovalStatus.rejected.value: "Work Rejected",
            ApprovalStatus.correction.value: "Correction Requested",
        }
        msg_map = {
            ApprovalStatus.approved.value: f"Your work report has been approved. Amount: ₹{upd.get('total_amount', r['total_amount'])}.",
            ApprovalStatus.rejected.value: f"Your work report was rejected. {body.remarks or ''}",
            ApprovalStatus.correction.value: f"Correction requested. {body.remarks or ''}",
        }
        await add_notification(worker_user["id"], title_map[new_status], msg_map[new_status])
    return {**r, **upd}


@api.post("/work-reports/{report_id}/approve")
async def approve_report(report_id: str, body: ApprovalIn, u=Depends(require_role(Role.admin))):
    return await _approval_action(report_id, ApprovalStatus.approved.value, body, u)


@api.post("/work-reports/{report_id}/reject")
async def reject_report(report_id: str, body: ApprovalIn, u=Depends(require_role(Role.admin))):
    return await _approval_action(report_id, ApprovalStatus.rejected.value, body, u)


@api.post("/work-reports/{report_id}/correction")
async def correction_report(report_id: str, body: ApprovalIn, u=Depends(require_role(Role.admin))):
    return await _approval_action(report_id, ApprovalStatus.correction.value, body, u)


@api.post("/work-reports/{report_id}/unapprove")
async def unapprove_report(report_id: str, body: ApprovalIn, u=Depends(require_role(Role.admin))):
    """Reopen a mistakenly-approved report back to Pending. Restores door_count/
    total_amount to what was actually submitted (from door_breakdown), undoing any
    edit the admin made at approval time — reopening should mean starting review
    over, not keeping half of a correction. Does not touch any payment already
    recorded against it; that reconciliation is a separate manual step."""
    r = await db.work_reports.find_one({"id": report_id}, {"_id": 0})
    if not r:
        raise HTTPException(404, "Not found")
    if r.get("approval_status") != ApprovalStatus.approved.value:
        raise HTTPException(400, "Only an Approved report can be reopened")
    breakdown = r.get("door_breakdown") or []
    upd = {
        "approval_status": ApprovalStatus.pending.value,
        "approval_remarks": body.remarks or "Reopened by admin for re-review",
        "approved_by": None,
        "approved_by_name": None,
        "approved_at": None,
    }
    if breakdown:
        orig_count = sum(item.get("count", 0) for item in breakdown)
        orig_total = round(sum(item.get("subtotal", 0) for item in breakdown), 2)
        upd["door_count"] = orig_count
        upd["total_amount"] = orig_total
        upd["rate_per_door"] = round(orig_total / orig_count, 2) if orig_count else 0
    await db.work_reports.update_one({"id": report_id}, {"$set": upd})
    await audit(u, "report-reopened", "work_report", report_id, r, upd)
    wu = await db.users.find_one({"worker_id": r["worker_id"]}, {"_id": 0, "id": 1})
    if wu:
        await add_notification(
            wu["id"], "Report Reopened",
            f"Your approved report was reopened for review. {body.remarks or ''}",
        )
    return {**r, **upd}


# --- Payments ------------------------------------------------------------
@api.post("/payments")
async def create_payment(body: PaymentIn, u=Depends(require_role(Role.admin))):
    if body.amount <= 0:
        raise HTTPException(400, "Amount must be positive")
    # idempotency — a double-click or slow-network retry must not double-pay a worker
    if body.client_sync_id:
        existing = await db.payments.find_one({"client_sync_id": body.client_sync_id}, {"_id": 0})
        if existing:
            return existing
    worker = await db.workers.find_one({"id": body.worker_id}, {"_id": 0})
    if not worker:
        raise HTTPException(404, "Worker not found")
    doc = {
        "id": str(uuid.uuid4()),
        "worker_id": body.worker_id,
        "worker_name": worker["name"],
        "payment_date": body.payment_date or now_utc().date().isoformat(),
        "amount": round(body.amount, 2),
        "payment_method": body.payment_method,
        "transaction_reference": body.transaction_reference or "",
        "notes": body.notes or "",
        "created_by": u["id"],
        "created_at": now_utc().isoformat(),
    }
    if body.client_sync_id:
        doc["client_sync_id"] = body.client_sync_id
    await db.payments.insert_one(doc.copy())
    await audit(u, "create", "payment", doc["id"], None, doc)
    # notify worker
    worker_user = await db.users.find_one({"worker_id": body.worker_id}, {"_id": 0, "id": 1})
    if worker_user:
        await add_notification(worker_user["id"], "Payment Added", f"Payment of ₹{doc['amount']} recorded.")
    return clean(doc)


@api.get("/payments")
async def list_payments(u=Depends(get_user), worker_id: Optional[str] = None):
    q = {}
    if u["role"] == Role.worker.value:
        q["worker_id"] = u.get("worker_id")
    elif worker_id:
        q["worker_id"] = worker_id
    items = await db.payments.find(q, {"_id": 0}).sort("payment_date", -1).to_list(500)
    return items


# --- Notifications -------------------------------------------------------
@api.get("/notifications")
async def list_notifications(u=Depends(get_user)):
    items = await db.notifications.find({"user_id": u["id"]}, {"_id": 0}).sort("created_at", -1).limit(100).to_list(100)
    return items


@api.post("/notifications/{nid}/read")
async def read_notif(nid: str, u=Depends(get_user)):
    await db.notifications.update_one({"id": nid, "user_id": u["id"]}, {"$set": {"is_read": True}})
    return {"ok": True}


@api.post("/notifications/read-all")
async def read_all(u=Depends(get_user)):
    await db.notifications.update_many({"user_id": u["id"]}, {"$set": {"is_read": True}})
    return {"ok": True}


# --- Dashboards / Ledger -------------------------------------------------
async def _worker_earnings(worker_id: str):
    approved = await db.work_reports.aggregate([
        {"$match": {"worker_id": worker_id, "approval_status": ApprovalStatus.approved.value}},
        {"$group": {"_id": None, "total": {"$sum": "$total_amount"}, "doors": {"$sum": "$door_count"}}}
    ]).to_list(1)
    paid = await db.payments.aggregate([
        {"$match": {"worker_id": worker_id}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
    ]).to_list(1)
    approved_amt = approved[0]["total"] if approved else 0
    doors = approved[0]["doors"] if approved else 0
    paid_amt = paid[0]["total"] if paid else 0
    return {
        "approved_earnings": round(approved_amt, 2),
        "paid_amount": round(paid_amt, 2),
        "pending_amount": round(approved_amt - paid_amt, 2),
        "total_doors": doors,
    }


@api.get("/worker/dashboard")
async def worker_dashboard(u=Depends(require_role(Role.worker, Role.admin))):
    if not u.get("worker_id"):
        raise HTTPException(400, "Not a worker")
    wid = u["worker_id"]
    today = now_utc().date().isoformat()
    month_start = now_utc().date().replace(day=1).isoformat()
    active = await db.attendance_sessions.find_one({"worker_id": wid, "check_out_time": None}, {"_id": 0})
    today_sessions = await db.attendance_sessions.count_documents({
        "worker_id": wid, "check_in_time": {"$gte": today}
    })
    today_reports = await db.work_reports.aggregate([
        {"$match": {"worker_id": wid, "work_date": today}},
        {"$group": {"_id": None, "doors": {"$sum": "$door_count"},
                    "approved_amount": {"$sum": {"$cond": [{"$eq": ["$approval_status", "Approved"]}, "$total_amount", 0]}}}}
    ]).to_list(1)
    month_reports = await db.work_reports.aggregate([
        {"$match": {"worker_id": wid, "work_date": {"$gte": month_start}}},
        {"$group": {"_id": None, "doors": {"$sum": "$door_count"},
                    "approved_amount": {"$sum": {"$cond": [{"$eq": ["$approval_status", "Approved"]}, "$total_amount", 0]}}}}
    ]).to_list(1)
    month_sites = await db.attendance_sessions.distinct("site_id", {
        "worker_id": wid, "check_in_time": {"$gte": month_start}
    })
    earnings = await _worker_earnings(wid)
    return {
        "check_in_status": "checked_in" if active else "checked_out",
        "active_session": active,
        "today": {
            "sites_visited": today_sessions,
            "doors_installed": today_reports[0]["doors"] if today_reports else 0,
            "approved_amount": round(today_reports[0]["approved_amount"], 2) if today_reports else 0,
        },
        "month": {
            "sites_visited": len(month_sites),
            "doors_installed": month_reports[0]["doors"] if month_reports else 0,
            "approved_amount": round(month_reports[0]["approved_amount"], 2) if month_reports else 0,
            **earnings,
        },
    }


@api.get("/admin/dashboard")
async def admin_dashboard(u=Depends(require_role(Role.admin))):
    today = now_utc().date().isoformat()
    month_start = now_utc().date().replace(day=1).isoformat()
    year_start = now_utc().date().replace(month=1, day=1).isoformat()
    total_workers = await db.workers.count_documents({})
    active_sites = await db.sites.count_documents({"status": "Active"})
    active_now = await db.attendance_sessions.count_documents({"check_out_time": None})
    checked_in_today_ids = await db.attendance_sessions.distinct("worker_id", {"check_in_time": {"$gte": today}})
    sites_today = await db.attendance_sessions.distinct("site_id", {"check_in_time": {"$gte": today}})

    async def doors_between(start):
        r = await db.work_reports.aggregate([
            {"$match": {"work_date": {"$gte": start}}},
            {"$group": {"_id": None, "doors": {"$sum": "$door_count"}}}
        ]).to_list(1)
        return r[0]["doors"] if r else 0

    approved_agg = await db.work_reports.aggregate([
        {"$match": {"approval_status": "Approved"}},
        {"$group": {"_id": None, "total": {"$sum": "$total_amount"}}}
    ]).to_list(1)
    paid_agg = await db.payments.aggregate([
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
    ]).to_list(1)
    approved_total = approved_agg[0]["total"] if approved_agg else 0
    paid_total = paid_agg[0]["total"] if paid_agg else 0
    pending_reports = await db.work_reports.count_documents({"approval_status": "Pending"})

    return {
        "workers": {
            "total": total_workers,
            "active_today": len(checked_in_today_ids),
            "currently_checked_in": active_now,
            "inactive_today": total_workers - len(checked_in_today_ids),
        },
        "sites": {
            "total_active": active_sites,
            "with_workers_today": len(sites_today),
        },
        "doors": {
            "today": await doors_between(today),
            "month": await doors_between(month_start),
            "year": await doors_between(year_start),
        },
        "financial": {
            "approved_total": round(approved_total, 2),
            "paid_total": round(paid_total, 2),
            "pending_total": round(approved_total - paid_total, 2),
        },
        "pending_reports": pending_reports,
    }


@api.get("/workers/{worker_id}/ledger")
async def worker_ledger(worker_id: str, u=Depends(require_role(Role.admin, Role.worker))):
    if u["role"] == Role.worker.value and u.get("worker_id") != worker_id:
        raise HTTPException(403, "Forbidden")
    entries = []
    async for r in db.work_reports.find(
        {"worker_id": worker_id, "approval_status": "Approved"}, {"_id": 0}
    ):
        entries.append({
            "date": r["work_date"],
            "description": f"{r['door_count']} doors @ ₹{r['rate_per_door']}",
            "site": r.get("site_name"),
            "type": "credit",
            "amount": r["total_amount"],
        })
    async for p in db.payments.find({"worker_id": worker_id}, {"_id": 0}):
        entries.append({
            "date": p["payment_date"],
            "description": f"Payment via {p['payment_method']}" + (f" ({p['transaction_reference']})" if p.get("transaction_reference") else ""),
            "site": None,
            "type": "debit",
            "amount": p["amount"],
        })
    entries.sort(key=lambda x: x["date"])
    balance = 0
    for e in entries:
        if e["type"] == "credit":
            balance += e["amount"]
        else:
            balance -= e["amount"]
        e["balance"] = round(balance, 2)
    earnings = await _worker_earnings(worker_id)
    return {"entries": entries, "summary": earnings}


@api.get("/workers/{worker_id}/summary")
async def worker_summary(worker_id: str, u=Depends(require_role(Role.admin, Role.worker))):
    if u["role"] == Role.worker.value and u.get("worker_id") != worker_id:
        raise HTTPException(403, "Forbidden")
    return await _worker_earnings(worker_id)


# --- Settings ------------------------------------------------------------
@api.get("/settings")
async def get_settings(u=Depends(get_user)):
    s = await db.settings.find_one({"id": "global"}, {"_id": 0})
    if not s:
        s = {"id": "global", "company_name": "JONAH ENTERPRISES", "currency": "₹", "default_rate": 250.0}
    return s


@api.put("/settings")
async def update_settings(body: SettingsIn, u=Depends(require_role(Role.admin))):
    upd = {k: v for k, v in body.dict().items() if v is not None}
    await db.settings.update_one({"id": "global"}, {"$set": upd}, upsert=True)
    s = await db.settings.find_one({"id": "global"}, {"_id": 0})
    return s


# --- Push registration ---------------------------------------------------
@api.post("/register-push", status_code=201)
async def register_push(body: RegisterPushIn, u=Depends(get_user)):
    """Upsert the user's device token in the Emergent push service.
    We require an authenticated user + body.user_id must match to prevent spoofing."""
    if body.user_id != u["id"]:
        raise HTTPException(403, "user_id mismatch")
    try:
        resp = await _push_client.post("/api/v1/push/users/register", json=body.dict())
        if resp.status_code == 401:
            logger.warning("EMERGENT_PUSH_KEY missing or invalid on register")
        # We do NOT persist the token locally — SuprSend resolves it internally.
    except Exception as e:
        logger.warning(f"push register non-blocking failure: {e}")
    return {"status": "registered"}


# --- Weekly Payout Preview -----------------------------------------------
@api.get("/admin/payout-preview")
async def payout_preview(u=Depends(require_role(Role.admin))):
    """Returns one row per worker with a positive pending balance so an admin
    can batch-process this week's payouts in a single screen."""
    # aggregate approved per worker
    approved = {r["_id"]: r for r in await db.work_reports.aggregate([
        {"$match": {"approval_status": "Approved"}},
        {"$group": {"_id": "$worker_id", "total": {"$sum": "$total_amount"}}}
    ]).to_list(1000)}
    paid = {r["_id"]: r for r in await db.payments.aggregate([
        {"$group": {"_id": "$worker_id", "total": {"$sum": "$amount"},
                    "last_date": {"$max": "$payment_date"}}}
    ]).to_list(1000)}
    workers = await db.workers.find({"status": "Active"}, {"_id": 0}).to_list(1000)
    rows = []
    for w in workers:
        a = float(approved.get(w["id"], {}).get("total", 0) or 0)
        p = float(paid.get(w["id"], {}).get("total", 0) or 0)
        pending = round(a - p, 2)
        if pending <= 0:
            continue
        # find preferred method = most-used in last 5 payments
        last_pays = await db.payments.find({"worker_id": w["id"]}, {"_id": 0, "payment_method": 1}).sort("payment_date", -1).limit(5).to_list(5)
        methods = [p["payment_method"] for p in last_pays if p.get("payment_method")]
        preferred = max(set(methods), key=methods.count) if methods else "UPI"
        rows.append({
            "worker_id": w["id"],
            "worker_name": w["name"],
            "employee_id": w.get("employee_id"),
            "mobile": w.get("mobile"),
            "approved_total": round(a, 2),
            "paid_total": round(p, 2),
            "pending_amount": pending,
            "preferred_method": preferred,
            "last_paid_date": paid.get(w["id"], {}).get("last_date"),
        })
    rows.sort(key=lambda r: r["pending_amount"], reverse=True)
    total = round(sum(r["pending_amount"] for r in rows), 2)
    return {"rows": rows, "total_pending": total, "worker_count": len(rows)}


class BatchPayItem(BaseModel):
    worker_id: str
    amount: float = Field(gt=0)
    payment_method: str = "UPI"
    transaction_reference: Optional[str] = ""
    notes: Optional[str] = ""


class BatchPayIn(BaseModel):
    payments: List[BatchPayItem]
    payment_date: Optional[str] = None
    client_sync_id: Optional[str] = None  # idempotency for the whole batch, distinct from
    # per-payment client_sync_id since one batch legitimately creates several payment docs


@api.post("/payments/batch")
async def batch_payments(body: BatchPayIn, u=Depends(require_role(Role.admin))):
    """Create multiple payment entries in one call — used by the weekly payout screen."""
    if not body.payments:
        raise HTTPException(400, "No payments provided")
    # idempotency — a double-click on "Pay Selected" must not re-run the whole batch
    if body.client_sync_id:
        existing = await db.payments.find({"batch_sync_id": body.client_sync_id}, {"_id": 0}).to_list(1000)
        if existing:
            return {"created": len(existing), "payments": existing}
    date = body.payment_date or now_utc().date().isoformat()
    created = []
    for item in body.payments:
        worker = await db.workers.find_one({"id": item.worker_id}, {"_id": 0})
        if not worker:
            continue  # skip unknown, don't fail whole batch
        doc = {
            "id": str(uuid.uuid4()),
            "worker_id": item.worker_id,
            "worker_name": worker["name"],
            "payment_date": date,
            "amount": round(item.amount, 2),
            "payment_method": item.payment_method,
            "transaction_reference": item.transaction_reference or "",
            "notes": item.notes or "Batch payout",
            "created_by": u["id"],
            "created_at": now_utc().isoformat(),
        }
        if body.client_sync_id:
            doc["batch_sync_id"] = body.client_sync_id
        await db.payments.insert_one(doc.copy())
        await audit(u, "create", "payment", doc["id"], None, doc)
        wu = await db.users.find_one({"worker_id": item.worker_id}, {"_id": 0, "id": 1})
        if wu:
            await add_notification(wu["id"], "Payment Added", f"Payment of ₹{doc['amount']} recorded.")
        created.append(clean(doc))
    return {"created": len(created), "payments": created}


# --- Audit log -----------------------------------------------------------
@api.get("/audit-logs")
async def list_audit(
    u=Depends(require_role(Role.admin)),
    entity_type: Optional[str] = None,
    action: Optional[str] = None,
    limit: int = 200,
):
    q = {}
    if entity_type: q["entity_type"] = entity_type
    if action: q["action"] = action
    items = await db.audit_logs.find(q, {"_id": 0}).sort("created_at", -1).limit(max(1, min(limit, 500))).to_list(500)
    return items


# --- Branches ------------------------------------------------------------
class BranchIn(BaseModel):
    name: str
    code: str
    address: Optional[str] = ""
    phone: Optional[str] = ""


@api.get("/branches")
async def list_branches(u=Depends(get_user)):
    return await db.branches.find({}, {"_id": 0}).sort("name", 1).to_list(200)


@api.post("/branches")
async def create_branch(body: BranchIn, u=Depends(require_role(Role.admin))):
    doc = {"id": str(uuid.uuid4()), **body.dict(), "created_at": now_utc().isoformat()}
    await db.branches.insert_one(doc.copy())
    await audit(u, "create", "branch", doc["id"], None, doc)
    return clean(doc)


@api.put("/branches/{bid}")
async def update_branch(bid: str, body: BranchIn, u=Depends(require_role(Role.admin))):
    old = await db.branches.find_one({"id": bid}, {"_id": 0})
    if not old: raise HTTPException(404, "Branch not found")
    upd = body.dict()
    await db.branches.update_one({"id": bid}, {"$set": upd})
    await audit(u, "update", "branch", bid, old, upd)
    return {**old, **upd}


# --- Leaves --------------------------------------------------------------
class LeaveIn(BaseModel):
    start_date: str
    end_date: str
    reason: str
    leave_type: Optional[str] = "Personal"


class LeaveDecision(BaseModel):
    remarks: Optional[str] = ""


@api.post("/leaves")
async def create_leave(body: LeaveIn, u=Depends(require_role(Role.worker, Role.admin))):
    if not u.get("worker_id"):
        raise HTTPException(400, "Only workers can request leave")
    if body.end_date < body.start_date:
        raise HTTPException(400, "end_date must be >= start_date")
    doc = {
        "id": str(uuid.uuid4()),
        "worker_id": u["worker_id"],
        "worker_name": u.get("name"),
        "start_date": body.start_date,
        "end_date": body.end_date,
        "reason": body.reason,
        "leave_type": body.leave_type or "Personal",
        "status": LeaveStatus.pending.value,
        "remarks": "",
        "created_at": now_utc().isoformat(),
    }
    await db.leaves.insert_one(doc.copy())
    async for admin in db.users.find({"role": Role.admin.value}, {"_id": 0, "id": 1}):
        await add_notification(admin["id"], "Leave Request", f"{u.get('name')} requested leave {body.start_date} → {body.end_date}.")
    return clean(doc)


@api.get("/leaves")
async def list_leaves(u=Depends(get_user), status_f: Optional[str] = Query(None, alias="status"),
                      worker_id: Optional[str] = None):
    q = {}
    if u["role"] == Role.worker.value:
        q["worker_id"] = u.get("worker_id")
    elif worker_id:
        q["worker_id"] = worker_id
    if status_f: q["status"] = status_f
    return await db.leaves.find(q, {"_id": 0}).sort("start_date", -1).to_list(500)


async def _leave_decide(lid: str, new_status: str, remarks: str, u: dict):
    old = await db.leaves.find_one({"id": lid}, {"_id": 0})
    if not old: raise HTTPException(404, "Leave not found")
    upd = {"status": new_status, "remarks": remarks, "decided_by": u["id"],
           "decided_by_name": u.get("name") or u.get("username"),
           "decided_at": now_utc().isoformat()}
    await db.leaves.update_one({"id": lid}, {"$set": upd})
    await audit(u, f"leave-{new_status.lower()}", "leave", lid, old, upd)
    wu = await db.users.find_one({"worker_id": old["worker_id"]}, {"_id": 0, "id": 1})
    if wu:
        await add_notification(wu["id"], f"Leave {new_status}", f"Your leave {old['start_date']} → {old['end_date']} has been {new_status.lower()}.")
    return {**old, **upd}


@api.post("/leaves/{lid}/approve")
async def approve_leave(lid: str, body: LeaveDecision, u=Depends(require_role(Role.admin))):
    return await _leave_decide(lid, LeaveStatus.approved.value, body.remarks or "", u)


@api.post("/leaves/{lid}/reject")
async def reject_leave(lid: str, body: LeaveDecision, u=Depends(require_role(Role.admin))):
    return await _leave_decide(lid, LeaveStatus.rejected.value, body.remarks or "", u)


# --- GST Invoices --------------------------------------------------------
class InvoiceItemIn(BaseModel):
    description: str
    quantity: float = Field(gt=0)
    unit_price: float = Field(ge=0)
    hsn_code: Optional[str] = ""


class InvoiceIn(BaseModel):
    site_id: Optional[str] = None
    client_name: str
    client_gstin: Optional[str] = ""
    client_address: Optional[str] = ""
    invoice_date: Optional[str] = None
    due_date: Optional[str] = None
    items: List[InvoiceItemIn]
    gst_rate: float = 18.0  # percent
    notes: Optional[str] = ""


async def _next_invoice_number() -> str:
    year = now_utc().strftime("%y")
    counter_key = f"inv-{year}"
    r = await db.counters.find_one_and_update(
        {"id": counter_key},
        {"$inc": {"value": 1}},
        upsert=True,
        return_document=True,
    )
    n = (r or {}).get("value", 1)
    return f"JE/{year}/{n:04d}"


@api.post("/invoices")
async def create_invoice(body: InvoiceIn, u=Depends(require_role(Role.admin))):
    if not body.items:
        raise HTTPException(400, "At least one line item required")
    subtotal = round(sum(it.quantity * it.unit_price for it in body.items), 2)
    gst_amount = round(subtotal * (body.gst_rate / 100.0), 2)
    total = round(subtotal + gst_amount, 2)
    number = await _next_invoice_number()
    doc = {
        "id": str(uuid.uuid4()),
        "invoice_number": number,
        "site_id": body.site_id,
        "client_name": body.client_name,
        "client_gstin": body.client_gstin or "",
        "client_address": body.client_address or "",
        "invoice_date": body.invoice_date or now_utc().date().isoformat(),
        "due_date": body.due_date or "",
        "items": [it.dict() for it in body.items],
        "subtotal": subtotal,
        "gst_rate": body.gst_rate,
        "gst_amount": gst_amount,
        "total": total,
        "notes": body.notes or "",
        "status": "Unpaid",
        "created_by": u["id"],
        "created_at": now_utc().isoformat(),
    }
    await db.invoices.insert_one(doc.copy())
    await audit(u, "create", "invoice", doc["id"], None, doc)
    return clean(doc)


@api.put("/invoices/{iid}")
async def update_invoice(iid: str, body: InvoiceIn, u=Depends(require_role(Role.admin))):
    old = await db.invoices.find_one({"id": iid}, {"_id": 0})
    if not old:
        raise HTTPException(404, "Not found")
    if not body.items:
        raise HTTPException(400, "At least one line item required")
    # totals are always server-recomputed from the edited line items, never trusted from the client
    subtotal = round(sum(it.quantity * it.unit_price for it in body.items), 2)
    gst_amount = round(subtotal * (body.gst_rate / 100.0), 2)
    total = round(subtotal + gst_amount, 2)
    upd = {
        "site_id": body.site_id,
        "client_name": body.client_name,
        "client_gstin": body.client_gstin or "",
        "client_address": body.client_address or "",
        "invoice_date": body.invoice_date or old["invoice_date"],
        "due_date": body.due_date or "",
        "items": [it.dict() for it in body.items],
        "subtotal": subtotal,
        "gst_rate": body.gst_rate,
        "gst_amount": gst_amount,
        "total": total,
        "notes": body.notes or "",
    }
    await db.invoices.update_one({"id": iid}, {"$set": upd})
    await audit(u, "update", "invoice", iid, old, upd)
    return {**old, **upd}


@api.delete("/invoices/{iid}")
async def delete_invoice(iid: str, u=Depends(require_role(Role.admin))):
    old = await db.invoices.find_one({"id": iid}, {"_id": 0})
    if not old:
        raise HTTPException(404, "Not found")
    await db.invoices.delete_one({"id": iid})
    await audit(u, "delete", "invoice", iid, old, None)
    return {"ok": True}


@api.get("/invoices")
async def list_invoices(u=Depends(require_role(Role.admin, Role.client))):
    q = {}
    if u["role"] == Role.client.value:
        q["site_id"] = {"$in": u.get("site_ids", []) or []}
    return await db.invoices.find(q, {"_id": 0}).sort("invoice_date", -1).to_list(500)


@api.get("/invoices/{iid}")
async def get_invoice(iid: str, u=Depends(require_role(Role.admin, Role.client))):
    inv = await db.invoices.find_one({"id": iid}, {"_id": 0})
    if not inv: raise HTTPException(404, "Not found")
    if u["role"] == Role.client.value and inv.get("site_id") not in (u.get("site_ids") or []):
        raise HTTPException(403, "Forbidden")
    return inv


def _build_invoice_pdf(inv: dict, company: dict) -> bytes:
    from reportlab.lib import colors as rlc
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=32, rightMargin=32, topMargin=32, bottomMargin=32)
    styles = getSampleStyleSheet()
    brand = ParagraphStyle("brand", parent=styles["Heading1"], textColor=rlc.HexColor("#423226"))
    story = [
        Paragraph(company.get("company_name", "JONAH ENTERPRISES"), brand),
        Paragraph(company.get("address") or "", styles["Normal"]),
        Paragraph(f"{company.get('mobile') or ''}   {company.get('email') or ''}", styles["Normal"]),
        Spacer(1, 10),
        Paragraph(f"<b>TAX INVOICE — {inv['invoice_number']}</b>", styles["Heading2"]),
        Paragraph(f"Date: {inv['invoice_date']}   Due: {inv.get('due_date') or '-'}", styles["Normal"]),
        Spacer(1, 8),
        Paragraph(f"<b>Bill to:</b> {inv['client_name']}", styles["Normal"]),
        Paragraph(inv.get("client_address") or "", styles["Normal"]),
        Paragraph(f"GSTIN: {inv.get('client_gstin') or '-'}", styles["Normal"]),
        Spacer(1, 10),
    ]
    header = ["Description", "HSN", "Qty", "Unit ₹", "Amount ₹"]
    rows = [[it["description"], it.get("hsn_code") or "-", f"{it['quantity']:g}", f"{it['unit_price']:.2f}", f"{it['quantity'] * it['unit_price']:.2f}"] for it in inv["items"]]
    footer_rows = [
        ["", "", "", "Subtotal", f"{inv['subtotal']:.2f}"],
        ["", "", "", f"GST @ {inv['gst_rate']:.1f}%", f"{inv['gst_amount']:.2f}"],
        ["", "", "", "TOTAL", f"₹ {inv['total']:.2f}"],
    ]
    table_data = [header] + rows + footer_rows
    t = Table(table_data, colWidths=[220, 60, 45, 70, 90])
    style = TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), rlc.HexColor("#423226")),
        ("TEXTCOLOR", (0, 0), (-1, 0), rlc.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
        ("ROWBACKGROUNDS", (0, 1), (-1, len(rows)), [rlc.HexColor("#FAF9F6"), rlc.white]),
        ("GRID", (0, 0), (-1, -1), 0.25, rlc.HexColor("#CFC9BC")),
        ("BACKGROUND", (0, -1), (-1, -1), rlc.HexColor("#EADDBC")),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
    ])
    t.setStyle(style)
    story.append(t)
    if inv.get("notes"):
        story.append(Spacer(1, 12))
        story.append(Paragraph(f"<b>Notes:</b> {inv['notes']}", styles["Normal"]))
    doc.build(story)
    buf.seek(0)
    return buf.getvalue()


@api.get("/invoices/{iid}/pdf")
async def invoice_pdf(iid: str, token: Optional[str] = None):
    if not token: raise HTTPException(401, "Missing download token")
    await _verify_download_token(token)
    inv = await db.invoices.find_one({"id": iid}, {"_id": 0})
    if not inv: raise HTTPException(404, "Not found")
    company = await _company_settings()
    data = _build_invoice_pdf(inv, company)
    fname = f"{inv['invoice_number'].replace('/', '_')}.pdf"
    return StreamingResponse(io.BytesIO(data), media_type="application/pdf",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# --- Salary Slip --------------------------------------------------------
@api.get("/salary-slip/{worker_id}")
async def salary_slip(worker_id: str, month: Optional[str] = None, token: Optional[str] = None):
    """month=YYYY-MM. Auth via ?token= (download token)."""
    if not token:
        raise HTTPException(401, "Missing download token")
    payload = await _verify_download_token(token)
    # Worker-scoped tokens (minted by /worker/salary-slip-token) may only fetch their own slip.
    if payload.get("role") == Role.worker.value and payload.get("worker_id") != worker_id:
        raise HTTPException(403, "Forbidden")
    month = month or now_utc().strftime("%Y-%m")
    worker = await db.workers.find_one({"id": worker_id}, {"_id": 0})
    if not worker: raise HTTPException(404, "Worker not found")

    start = f"{month}-01"
    # naive end: 32 days later then next month's -01
    year, m = map(int, month.split("-"))
    ny, nm = (year + 1, 1) if m == 12 else (year, m + 1)
    end = f"{ny:04d}-{nm:02d}-01"

    doors = 0; total = 0.0; days = set(); sessions = 0
    async for r in db.work_reports.find(
        {"worker_id": worker_id, "approval_status": "Approved",
         "work_date": {"$gte": start, "$lt": end}}, {"_id": 0}
    ):
        doors += r.get("door_count", 0)
        total += float(r.get("total_amount", 0))
        days.add(r.get("work_date"))
    async for a in db.attendance_sessions.find(
        {"worker_id": worker_id, "check_in_time": {"$gte": start, "$lt": end}}, {"_id": 0, "id": 1}
    ):
        sessions += 1

    paid = 0.0
    async for p in db.payments.find(
        {"worker_id": worker_id, "payment_date": {"$gte": start, "$lt": end}}, {"_id": 0}
    ):
        paid += float(p.get("amount", 0))

    from reportlab.lib import colors as rlc
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    company = await _company_settings()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=32, rightMargin=32, topMargin=32, bottomMargin=32)
    styles = getSampleStyleSheet()
    brand = ParagraphStyle("brand", parent=styles["Heading1"], textColor=rlc.HexColor("#423226"))
    story = [
        Paragraph(company.get("company_name", "JONAH ENTERPRISES"), brand),
        Paragraph(company.get("address") or "", styles["Normal"]),
        Spacer(1, 10),
        Paragraph(f"<b>PAYMENT SLIP — {month}</b>", styles["Heading2"]),
        Paragraph(f"Worker: {worker['name']} ({worker.get('employee_id', '-')})", styles["Normal"]),
        Paragraph(f"Mobile: {worker.get('mobile', '-')}", styles["Normal"]),
        Spacer(1, 10),
    ]
    tbl = Table([
        ["Metric", "Value"],
        ["Working Days", len(days)],
        ["Site Sessions", sessions],
        ["Doors Installed", doors],
        ["Rate Per Door", f"₹ {worker.get('default_rate', 0):.2f}"],
        ["Gross Approved Earnings", f"₹ {total:.2f}"],
        ["Amount Paid This Month", f"₹ {paid:.2f}"],
        ["Net Balance Carried", f"₹ {total - paid:.2f}"],
    ], colWidths=[240, 220])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), rlc.HexColor("#423226")),
        ("TEXTCOLOR", (0, 0), (-1, 0), rlc.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.25, rlc.HexColor("#CFC9BC")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [rlc.HexColor("#FAF9F6"), rlc.white]),
        ("BACKGROUND", (0, -1), (-1, -1), rlc.HexColor("#EADDBC")),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 11),
        ("ALIGN", (1, 1), (1, -1), "RIGHT"),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 20))
    story.append(Paragraph("This is a system-generated payment slip. Signature is not required.", styles["Italic"]))
    doc.build(story)
    buf.seek(0)
    fname = f"slip_{worker.get('employee_id', worker_id)}_{month}.pdf"
    return StreamingResponse(io.BytesIO(buf.getvalue()), media_type="application/pdf",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# --- Client Portal -------------------------------------------------------
class ClientUserIn(BaseModel):
    username: str
    password: str = Field(min_length=8)
    display_name: str
    site_ids: List[str]


@api.post("/client-users")
async def create_client_user(body: ClientUserIn, u=Depends(require_role(Role.admin))):
    if await db.users.find_one({"username": body.username.lower().strip()}):
        raise HTTPException(400, "Username already exists")
    doc = {
        "id": str(uuid.uuid4()),
        "username": body.username.lower().strip(),
        "password_hash": pwd_ctx.hash(body.password),
        "role": Role.client.value,
        "name": body.display_name,
        "site_ids": body.site_ids,
        "disabled": False,
        "created_at": now_utc().isoformat(),
    }
    await db.users.insert_one(doc.copy())
    await audit(u, "create", "client_user", doc["id"], None, {"username": body.username, "sites": body.site_ids})
    return {"id": doc["id"], "username": doc["username"], "name": doc["name"]}


@api.get("/client/dashboard")
async def client_dashboard(u=Depends(require_role(Role.client, Role.admin))):
    site_ids = u.get("site_ids") or []
    if u["role"] == Role.admin.value:
        # admin viewing "as client" simply sees all sites
        sites = await db.sites.find({}, {"_id": 0}).to_list(200)
    else:
        sites = await db.sites.find({"id": {"$in": site_ids}}, {"_id": 0}).to_list(200)
    doors_agg = await db.work_reports.aggregate([
        {"$match": {"site_id": {"$in": [s["id"] for s in sites]}, "approval_status": "Approved"}},
        {"$group": {"_id": "$site_id", "doors": {"$sum": "$door_count"},
                    "amount": {"$sum": "$total_amount"}, "visits": {"$sum": 1}}}
    ]).to_list(1000)
    by = {a["_id"]: a for a in doors_agg}
    rows = []
    for s in sites:
        m = by.get(s["id"], {})
        rows.append({
            "site_id": s["id"], "site_name": s["site_name"], "address": s.get("address"),
            "doors": m.get("doors", 0), "amount": round(m.get("amount", 0), 2),
            "visits": m.get("visits", 0), "status": s.get("status"),
        })
    return {
        "sites": rows,
        "total_doors": sum(r["doors"] for r in rows),
        "total_amount": round(sum(r["amount"] for r in rows), 2),
        "site_count": len(rows),
    }


@api.get("/client/reports")
async def client_reports(u=Depends(require_role(Role.client, Role.admin)),
                         site_id: Optional[str] = None):
    q = {"approval_status": "Approved"}
    if u["role"] == Role.client.value:
        allowed = u.get("site_ids") or []
        if site_id:
            if site_id not in allowed: raise HTTPException(403, "Forbidden")
            q["site_id"] = site_id
        else:
            q["site_id"] = {"$in": allowed}
    elif site_id:
        q["site_id"] = site_id
    items = await db.work_reports.find(q, {"_id": 0, "before_photos": 0, "after_photos": 0}).sort("work_date", -1).limit(200).to_list(200)
    return items


# --- Charts (admin) ------------------------------------------------------
@api.get("/admin/charts")
async def admin_charts(u=Depends(require_role(Role.admin))):
    """Aggregates for dashboard charts: last-6-months doors installed,
    last-6-months payments, top workers by doors, site-wise installations."""
    now = now_utc().date()

    # Build the last 6 months (YYYY-MM keys, oldest first)
    months = []
    y, m = now.year, now.month
    for _ in range(6):
        months.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m = 12; y -= 1
    months.reverse()
    month_start = months[0] + "-01"

    # Monthly doors (approved reports only for accounting truth)
    doors_by_month = {mm: 0 for mm in months}
    async for r in db.work_reports.find(
        {"approval_status": "Approved", "work_date": {"$gte": month_start}},
        {"_id": 0, "work_date": 1, "door_count": 1},
    ):
        key = (r.get("work_date") or "")[:7]
        if key in doors_by_month:
            doors_by_month[key] += r.get("door_count", 0)

    # Monthly payments
    pay_by_month = {mm: 0.0 for mm in months}
    async for p in db.payments.find(
        {"payment_date": {"$gte": month_start}},
        {"_id": 0, "payment_date": 1, "amount": 1},
    ):
        key = (p.get("payment_date") or "")[:7]
        if key in pay_by_month:
            pay_by_month[key] += float(p.get("amount", 0))

    # Top workers by approved doors
    top_workers_agg = await db.work_reports.aggregate([
        {"$match": {"approval_status": "Approved"}},
        {"$group": {"_id": "$worker_id", "worker_name": {"$first": "$worker_name"},
                    "doors": {"$sum": "$door_count"}, "amount": {"$sum": "$total_amount"}}},
        {"$sort": {"doors": -1}},
        {"$limit": 6},
    ]).to_list(6)

    # Site-wise installations
    sites_agg = await db.work_reports.aggregate([
        {"$match": {"approval_status": "Approved"}},
        {"$group": {"_id": "$site_id", "site_name": {"$first": "$site_name"},
                    "doors": {"$sum": "$door_count"}, "amount": {"$sum": "$total_amount"}}},
        {"$sort": {"doors": -1}},
        {"$limit": 8},
    ]).to_list(8)

    return {
        "monthly_doors": [{"month": mm, "value": doors_by_month[mm]} for mm in months],
        "monthly_payments": [{"month": mm, "value": round(pay_by_month[mm], 2)} for mm in months],
        "top_workers": [{"worker_id": t["_id"], "worker_name": t["worker_name"], "doors": t["doors"], "amount": round(t["amount"], 2)} for t in top_workers_agg],
        "site_installations": [{"site_id": s["_id"], "site_name": s["site_name"], "doors": s["doors"], "amount": round(s["amount"], 2)} for s in sites_agg],
    }


# --- Reports & Export ----------------------------------------------------
def _rupees(n) -> str:
    try:
        return f"₹{float(n or 0):,.2f}"
    except Exception:
        return "₹0.00"


async def _rpt_worker_earnings(date_from: Optional[str], date_to: Optional[str], worker_id: Optional[str]):
    q = {"approval_status": "Approved"}
    if date_from: q.setdefault("work_date", {})["$gte"] = date_from
    if date_to: q.setdefault("work_date", {})["$lte"] = date_to
    if worker_id: q["worker_id"] = worker_id
    rows = []
    totals = {"doors": 0, "amount": 0.0}
    by_worker: dict = {}
    async for r in db.work_reports.find(q, {"_id": 0}):
        wid = r["worker_id"]
        if wid not in by_worker:
            by_worker[wid] = {"worker": r.get("worker_name", "-"), "doors": 0, "amount": 0.0}
        by_worker[wid]["doors"] += r.get("door_count", 0)
        by_worker[wid]["amount"] += float(r.get("total_amount", 0))
        totals["doors"] += r.get("door_count", 0)
        totals["amount"] += float(r.get("total_amount", 0))
    for wid, v in by_worker.items():
        # attach paid & pending
        paid = 0.0
        async for p in db.payments.find({"worker_id": wid}, {"_id": 0, "amount": 1}):
            paid += float(p.get("amount", 0))
        rows.append([v["worker"], v["doors"], _rupees(v["amount"]), _rupees(paid), _rupees(v["amount"] - paid)])
    header = ["Worker", "Doors", "Approved Amount", "Paid", "Pending"]
    footer = ["TOTAL", totals["doors"], _rupees(totals["amount"]), "", ""]
    return header, rows, footer, "Worker Earnings Report"


async def _rpt_payments(date_from, date_to, worker_id):
    q = {}
    if date_from: q.setdefault("payment_date", {})["$gte"] = date_from
    if date_to: q.setdefault("payment_date", {})["$lte"] = date_to
    if worker_id: q["worker_id"] = worker_id
    rows = []
    total = 0.0
    async for p in db.payments.find(q, {"_id": 0}).sort("payment_date", -1):
        rows.append([p.get("payment_date"), p.get("worker_name"), p.get("payment_method"),
                     p.get("transaction_reference") or "-", _rupees(p.get("amount"))])
        total += float(p.get("amount", 0))
    header = ["Date", "Worker", "Method", "Reference", "Amount"]
    footer = ["TOTAL", "", "", "", _rupees(total)]
    return header, rows, footer, "Payments Report"


async def _rpt_attendance(date_from, date_to, worker_id, site_id):
    q = {}
    if date_from: q.setdefault("check_in_time", {})["$gte"] = date_from
    if date_to: q.setdefault("check_in_time", {})["$lte"] = date_to + "T23:59:59"
    if worker_id: q["worker_id"] = worker_id
    if site_id: q["site_id"] = site_id
    rows = []
    async for a in db.attendance_sessions.find(q, {"_id": 0}).sort("check_in_time", -1):
        ci = a.get("check_in_time", "")[:19].replace("T", " ")
        co = (a.get("check_out_time") or "")[:19].replace("T", " ") if a.get("check_out_time") else "-"
        rows.append([a.get("worker_name"), a.get("site_name"), ci, co,
                     f"{a.get('check_in_latitude', 0):.5f}, {a.get('check_in_longitude', 0):.5f}"])
    header = ["Worker", "Site", "Check-In", "Check-Out", "Check-In GPS"]
    return header, rows, None, "Attendance Report"


async def _rpt_site_installations(date_from, date_to, site_id):
    q = {"approval_status": "Approved"}
    if date_from: q.setdefault("work_date", {})["$gte"] = date_from
    if date_to: q.setdefault("work_date", {})["$lte"] = date_to
    if site_id: q["site_id"] = site_id
    by_site: dict = {}
    async for r in db.work_reports.find(q, {"_id": 0}):
        sid = r["site_id"]
        if sid not in by_site:
            by_site[sid] = {"site": r.get("site_name", "-"), "doors": 0, "amount": 0.0, "visits": 0}
        by_site[sid]["doors"] += r.get("door_count", 0)
        by_site[sid]["amount"] += float(r.get("total_amount", 0))
        by_site[sid]["visits"] += 1
    rows, td, ta = [], 0, 0.0
    for v in by_site.values():
        rows.append([v["site"], v["visits"], v["doors"], _rupees(v["amount"])])
        td += v["doors"]; ta += v["amount"]
    return ["Site", "Visits", "Doors", "Amount"], rows, ["TOTAL", "", td, _rupees(ta)], "Site-wise Installations Report"


async def _rpt_worker_performance(date_from, date_to, worker_id):
    q_reports = {}
    if date_from: q_reports.setdefault("work_date", {})["$gte"] = date_from
    if date_to: q_reports.setdefault("work_date", {})["$lte"] = date_to
    if worker_id: q_reports["worker_id"] = worker_id
    by_worker: dict = {}
    async for r in db.work_reports.find(q_reports, {"_id": 0}):
        wid = r["worker_id"]
        w = by_worker.setdefault(wid, {"name": r.get("worker_name", "-"), "doors": 0, "approved_amount": 0.0,
                                       "approved_reports": 0, "rejected_reports": 0, "correction_reports": 0,
                                       "days": set(), "sites": set()})
        w["days"].add(r.get("work_date"))
        w["sites"].add(r.get("site_id"))
        st = r.get("approval_status")
        if st == "Approved":
            w["approved_reports"] += 1
            w["doors"] += r.get("door_count", 0)
            w["approved_amount"] += float(r.get("total_amount", 0))
        elif st == "Rejected":
            w["rejected_reports"] += 1
        elif st == "Correction":
            w["correction_reports"] += 1

    rows = []
    for w in by_worker.values():
        days = len(w["days"])
        avg = round(w["doors"] / days, 2) if days else 0
        rows.append([w["name"], days, len(w["sites"]), w["doors"], avg,
                     _rupees(w["approved_amount"]), w["approved_reports"], w["rejected_reports"], w["correction_reports"]])
    header = ["Worker", "Days", "Sites", "Doors", "Avg/Day", "Approved Amount", "Approved", "Rejected", "Correction"]
    return header, rows, None, "Worker Performance Report"


async def _rpt_attendance_summary(date_from, date_to, worker_id, site_id):
    q = {}
    if date_from: q.setdefault("check_in_time", {})["$gte"] = date_from
    if date_to: q.setdefault("check_in_time", {})["$lte"] = date_to + "T23:59:59"
    if worker_id: q["worker_id"] = worker_id
    if site_id: q["site_id"] = site_id
    by_worker: dict = {}
    async for a in db.attendance_sessions.find(q, {"_id": 0}):
        wid = a["worker_id"]
        w = by_worker.setdefault(wid, {"name": a.get("worker_name", "-"), "sessions": 0, "sites": set(),
                                       "days": set(), "total_hours": 0.0})
        w["sessions"] += 1
        w["sites"].add(a.get("site_id"))
        w["days"].add((a.get("check_in_time") or "")[:10])
        if a.get("check_out_time"):
            try:
                ci = datetime.fromisoformat(a["check_in_time"].replace("Z", "+00:00"))
                co = datetime.fromisoformat(a["check_out_time"].replace("Z", "+00:00"))
                hrs = (co - ci).total_seconds() / 3600.0
                if hrs > 0: w["total_hours"] += hrs
            except Exception:
                pass
    rows = []
    for w in by_worker.values():
        rows.append([w["name"], len(w["days"]), w["sessions"], len(w["sites"]), f"{w['total_hours']:.1f}"])
    return ["Worker", "Days", "Sessions", "Sites", "Total Hours"], rows, None, "Attendance Summary Report"


REPORTS = {
    "worker_earnings": _rpt_worker_earnings,
    "payments": _rpt_payments,
    "attendance": _rpt_attendance,
    "site_installations": _rpt_site_installations,
    "worker_performance": _rpt_worker_performance,
    "attendance_summary": _rpt_attendance_summary,
}


async def _resolve_report(key: str, filters: dict):
    if key not in REPORTS:
        raise HTTPException(404, "Unknown report")
    fn = REPORTS[key]
    if key == "worker_earnings":
        return await fn(filters.get("date_from"), filters.get("date_to"), filters.get("worker_id"))
    if key == "payments":
        return await fn(filters.get("date_from"), filters.get("date_to"), filters.get("worker_id"))
    if key == "attendance":
        return await fn(filters.get("date_from"), filters.get("date_to"), filters.get("worker_id"), filters.get("site_id"))
    if key == "site_installations":
        return await fn(filters.get("date_from"), filters.get("date_to"), filters.get("site_id"))
    if key == "worker_performance":
        return await fn(filters.get("date_from"), filters.get("date_to"), filters.get("worker_id"))
    if key == "attendance_summary":
        return await fn(filters.get("date_from"), filters.get("date_to"), filters.get("worker_id"), filters.get("site_id"))
    raise HTTPException(400, "Bad report")


def _download_token(payload: dict) -> str:
    payload = {**payload, "jti": secrets.token_hex(8), "exp": now_utc() + timedelta(minutes=5)}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


async def _verify_download_token(token: str) -> dict:
    try:
        p = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired token")
    jti = p.get("jti")
    if not jti:
        raise HTTPException(401, "Invalid token")
    # One-time use: these tokens travel in a URL query string and can end up in
    # access logs / browser history, so a replay within the 5-minute window is
    # rejected even though the signature and expiry are still valid.
    prior = await db.used_download_tokens.find_one_and_update(
        {"id": jti},
        {"$setOnInsert": {"id": jti, "expires_at": now_utc() + timedelta(minutes=6)}},
        upsert=True,
    )
    if prior is not None:
        raise HTTPException(401, "Download link already used")
    return p


@api.post("/reports/download-token")
async def create_download_token(u=Depends(require_role(Role.admin))):
    """Short-lived download token used by the mobile client to open export URLs."""
    return {"token": _download_token({"sub": u["id"], "role": u["role"]})}


@api.post("/worker/salary-slip-token")
async def worker_salary_slip_token(u=Depends(require_role(Role.worker))):
    """Narrowly-scoped download token — only valid for this worker's own salary slip,
    unlike the admin token above which can open any report/invoice/slip."""
    return {"token": _download_token({"sub": u["id"], "role": u["role"], "worker_id": u["worker_id"]})}


async def _company_settings():
    s = await db.settings.find_one({"id": "global"}, {"_id": 0})
    return s or {"company_name": "JONAH ENTERPRISES"}


def _build_xlsx(title: str, header, rows, footer, meta) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    wb = Workbook()
    ws = wb.active
    ws.title = title[:31]
    ws["A1"] = meta["company_name"]
    ws["A1"].font = Font(size=16, bold=True, color="423226")
    ws["A2"] = title
    ws["A2"].font = Font(size=13, bold=True)
    ws["A3"] = f"Generated: {now_utc().strftime('%Y-%m-%d %H:%M UTC')}"
    if meta.get("filters"):
        ws["A4"] = "Filters: " + meta["filters"]
    start = 6
    for i, h in enumerate(header):
        cell = ws.cell(row=start, column=i + 1, value=h)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill(start_color="423226", end_color="423226", fill_type="solid")
        cell.alignment = Alignment(horizontal="center")
    for ri, row in enumerate(rows):
        for ci, val in enumerate(row):
            ws.cell(row=start + 1 + ri, column=ci + 1, value=val)
    if footer:
        for ci, val in enumerate(footer):
            cell = ws.cell(row=start + 1 + len(rows), column=ci + 1, value=val)
            cell.font = Font(bold=True)
            cell.fill = PatternFill(start_color="EADDBC", end_color="EADDBC", fill_type="solid")
    for col_i in range(1, len(header) + 1):
        ws.column_dimensions[chr(64 + col_i)].width = 22
    buf = io.BytesIO()
    wb.save(buf); buf.seek(0)
    return buf.getvalue()


def _build_pdf(title: str, header, rows, footer, meta) -> bytes:
    from reportlab.lib import colors as rlc
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=24, rightMargin=24, topMargin=24, bottomMargin=24)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("t", parent=styles["Heading1"], textColor=rlc.HexColor("#423226"))
    story = [
        Paragraph(meta["company_name"], title_style),
        Paragraph(title, styles["Heading2"]),
        Paragraph(f"Generated: {now_utc().strftime('%Y-%m-%d %H:%M UTC')}", styles["Normal"]),
    ]
    if meta.get("filters"):
        story.append(Paragraph(f"Filters: {meta['filters']}", styles["Normal"]))
    story.append(Spacer(1, 10))
    table_data = [header] + [list(r) for r in rows] + ([list(footer)] if footer else [])
    t = Table(table_data, repeatRows=1)
    style = TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), rlc.HexColor("#423226")),
        ("TEXTCOLOR", (0, 0), (-1, 0), rlc.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [rlc.HexColor("#FAF9F6"), rlc.white]),
        ("GRID", (0, 0), (-1, -1), 0.25, rlc.HexColor("#CFC9BC")),
    ])
    if footer:
        style.add("BACKGROUND", (0, -1), (-1, -1), rlc.HexColor("#EADDBC"))
        style.add("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold")
    t.setStyle(style)
    story.append(t)
    doc.build(story)
    buf.seek(0)
    return buf.getvalue()


@api.get("/reports/export")
async def export_report(
    report: str = Query(...),
    fmt: str = Query("xlsx", pattern="^(xlsx|pdf)$"),
    token: Optional[str] = None,  # short-lived download token (used by browser link)
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    worker_id: Optional[str] = None,
    site_id: Optional[str] = None,
):
    # Downloads authenticate via a short-lived ?token= (created by /reports/download-token)
    # rather than the Authorization header — needed because mobile browsers/openURL
    # can't easily attach custom headers.
    if not token:
        raise HTTPException(401, "Missing download token")
    await _verify_download_token(token)

    header, rows, footer, title = await _resolve_report(report, {
        "date_from": date_from, "date_to": date_to, "worker_id": worker_id, "site_id": site_id,
    })
    filter_bits = []
    if date_from: filter_bits.append(f"from {date_from}")
    if date_to: filter_bits.append(f"to {date_to}")
    meta = {"company_name": (await _company_settings()).get("company_name", "JONAH ENTERPRISES"),
            "filters": ", ".join(filter_bits) if filter_bits else None}

    if fmt == "xlsx":
        data = _build_xlsx(title, header, rows, footer, meta)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ext = "xlsx"
    else:
        data = _build_pdf(title, header, rows, footer, meta)
        media = "application/pdf"
        ext = "pdf"
    fname = f"{report}_{now_utc().strftime('%Y%m%d_%H%M')}.{ext}"
    return StreamingResponse(io.BytesIO(data), media_type=media, headers={
        "Content-Disposition": f'attachment; filename="{fname}"'
    })


# --- Startup / seed ------------------------------------------------------
@app.on_event("startup")
async def startup():
    await db.users.create_index("username", unique=True)
    await db.workers.create_index("employee_id", unique=True)
    # TTL index so used one-time download tokens self-clean shortly after expiry
    await db.used_download_tokens.create_index("expires_at", expireAfterSeconds=0)
    # Use partial index so only actual string sync_ids are enforced unique
    # (sparse indexes do NOT skip null values — they only skip missing fields).
    for coll_name in ("attendance_sessions", "work_reports", "payments"):
        coll = db[coll_name]
        # Drop stale sparse index if present, then recreate as partial.
        try:
            for idx_name, idx_info in (await coll.index_information()).items():
                keys = [k for k, _ in idx_info.get("key", [])]
                if keys == ["client_sync_id"]:
                    await coll.drop_index(idx_name)
        except Exception:
            pass
        await coll.create_index(
            "client_sync_id",
            unique=True,
            partialFilterExpression={"client_sync_id": {"$type": "string"}},
        )

    # Compound indexes on the fields actually filtered/aggregated on constantly —
    # invisible at today's data volume, a full collection scan on every dashboard
    # load and report once history grows into the thousands of documents.
    await db.work_reports.create_index([("worker_id", 1), ("work_date", -1)])
    await db.work_reports.create_index([("site_id", 1), ("work_date", -1)])
    await db.work_reports.create_index("approval_status")
    await db.attendance_sessions.create_index([("worker_id", 1), ("check_in_time", -1)])
    await db.attendance_sessions.create_index([("site_id", 1), ("check_in_time", -1)])
    await db.attendance_sessions.create_index("check_out_time")
    await db.payments.create_index([("worker_id", 1), ("payment_date", -1)])
    await db.notifications.create_index([("user_id", 1), ("created_at", -1)])
    await db.leaves.create_index([("worker_id", 1), ("status", 1)])

    # settings
    if not await db.settings.find_one({"id": "global"}):
        await db.settings.insert_one({
            "id": "global",
            "company_name": "JONAH ENTERPRISES",
            "address": "Bengaluru, India",
            "mobile": "+91 98765 43210",
            "email": "contact@jonahenterprises.in",
            "currency": "₹",
            "default_rate": 250.0,
        })

    # seed a default door type — required config, not demo clutter: without at
    # least one, workers can't submit any report at all (min_length=1 on doors[]).
    if await db.door_types.count_documents({}) == 0:
        await db.door_types.insert_one({
            "id": str(uuid.uuid4()),
            "name": "Standard Door",
            "default_rate": 250.0,
            "status": "Active",
            "created_at": now_utc().isoformat(),
        })
        logger.info("Seeded default door type: Standard Door @ 250.0")

    # seed admin — bootstrap account needed to log in at all. Never a hardcoded
    # password: use ADMIN_INITIAL_PASSWORD if provided, otherwise generate a
    # random one-time password and log it so it's never a guessable default.
    if not await db.users.find_one({"username": "admin"}):
        admin_pwd = os.environ.get("ADMIN_INITIAL_PASSWORD")
        generated = admin_pwd is None
        if generated:
            admin_pwd = secrets.token_urlsafe(12)
        await db.users.insert_one({
            "id": str(uuid.uuid4()),
            "username": "admin",
            "password_hash": pwd_ctx.hash(admin_pwd),
            "role": Role.admin.value,
            "name": "Admin",
            "disabled": False,
            "created_at": now_utc().isoformat(),
        })
        if generated:
            logger.warning(f"Seeded admin user. Initial password: {admin_pwd}  (change it immediately via /auth/change-password)")
        else:
            logger.info("Seeded admin user with password from ADMIN_INITIAL_PASSWORD")

    if not SEED_DEMO_DATA:
        return

    # seed workers
    if await db.workers.count_documents({}) == 0:
        demo = [
            {"employee_id": "EMP001", "name": "Ravi Kumar", "mobile": "+91 90000 00001", "username": "ravi", "password": "ravi123", "default_rate": 250.0},
            {"employee_id": "EMP002", "name": "Suresh Patel", "mobile": "+91 90000 00002", "username": "suresh", "password": "suresh123", "default_rate": 275.0},
            {"employee_id": "EMP003", "name": "Vinod Sharma", "mobile": "+91 90000 00003", "username": "vinod", "password": "vinod123", "default_rate": 250.0},
        ]
        for w in demo:
            wid = str(uuid.uuid4())
            uid = str(uuid.uuid4())
            await db.users.insert_one({
                "id": uid,
                "username": w["username"],
                "password_hash": pwd_ctx.hash(w["password"]),
                "role": Role.worker.value,
                "name": w["name"],
                "worker_id": wid,
                "disabled": False,
                "created_at": now_utc().isoformat(),
            })
            await db.workers.insert_one({
                "id": wid,
                "user_id": uid,
                "employee_id": w["employee_id"],
                "name": w["name"],
                "mobile": w["mobile"],
                "username": w["username"],
                "joining_date": now_utc().date().isoformat(),
                "default_rate": w["default_rate"],
                "status": "Active",
                "notes": "",
                "created_at": now_utc().isoformat(),
            })
        logger.info("Seeded 3 workers")

    # seed sites
    if await db.sites.count_documents({}) == 0:
        for s in [
            {"site_name": "Prestige Lakeside", "client_name": "Prestige Group", "address": "Whitefield, Bengaluru", "latitude": 12.9698, "longitude": 77.7500, "contact_person": "Rahul", "contact_number": "+91 98111 11111"},
            {"site_name": "Brigade Meadows", "client_name": "Brigade Group", "address": "Kanakapura Rd, Bengaluru", "latitude": 12.8500, "longitude": 77.5500, "contact_person": "Anil", "contact_number": "+91 98111 22222"},
            {"site_name": "Sobha Dream Acres", "client_name": "Sobha Ltd", "address": "Panathur, Bengaluru", "latitude": 12.9400, "longitude": 77.7100, "contact_person": "Deepak", "contact_number": "+91 98111 33333"},
            {"site_name": "Godrej Woodsville", "client_name": "Godrej Properties", "address": "Hosur Rd, Bengaluru", "latitude": 12.8800, "longitude": 77.6600, "contact_person": "Meera", "contact_number": "+91 98111 44444"},
        ]:
            await db.sites.insert_one({**s, "id": str(uuid.uuid4()), "status": "Active", "notes": "", "created_at": now_utc().isoformat()})
        logger.info("Seeded 4 sites")

    # seed sample work reports + payments (minimal for demo)
    if await db.work_reports.count_documents({}) == 0:
        admin_user = await db.users.find_one({"role": Role.admin.value}, {"_id": 0})
        admin_id = admin_user["id"] if admin_user else "admin"
        workers = await db.workers.find({}, {"_id": 0}).to_list(10)
        sites = await db.sites.find({}, {"_id": 0}).to_list(10)
        if workers and sites:
            w = workers[0]
            s = sites[0]
            # closed session
            sess_id = str(uuid.uuid4())
            await db.attendance_sessions.insert_one({
                "id": sess_id, "worker_id": w["id"], "worker_name": w["name"],
                "site_id": s["id"], "site_name": s["site_name"],
                "check_in_time": (now_utc() - timedelta(days=2, hours=8)).isoformat(),
                "check_in_latitude": s["latitude"], "check_in_longitude": s["longitude"],
                "check_in_accuracy": 10.0,
                "check_out_time": (now_utc() - timedelta(days=2, hours=1)).isoformat(),
                "check_out_latitude": s["latitude"], "check_out_longitude": s["longitude"],
                "check_out_accuracy": 12.0, "work_completed": True, "remarks": "Completed 8 doors",
                "created_at": now_utc().isoformat(),
            })
            await db.work_reports.insert_one({
                "id": str(uuid.uuid4()), "session_id": sess_id,
                "worker_id": w["id"], "worker_name": w["name"],
                "site_id": s["id"], "site_name": s["site_name"],
                "work_date": (now_utc() - timedelta(days=2)).date().isoformat(),
                "door_count": 8, "rate_per_door": w["default_rate"],
                "total_amount": 8 * w["default_rate"],
                "notes": "All 8 main entrance doors installed",
                "work_completed": True,
                "before_photos": [], "after_photos": [],
                "approval_status": "Approved",
                "approval_remarks": "Verified, good work",
                "approved_by": admin_id, "approved_by_name": "Admin",
                "approved_at": (now_utc() - timedelta(days=1)).isoformat(),
                "created_at": (now_utc() - timedelta(days=2)).isoformat(),
            })
            await db.payments.insert_one({
                "id": str(uuid.uuid4()), "worker_id": w["id"], "worker_name": w["name"],
                "payment_date": (now_utc() - timedelta(days=1)).date().isoformat(),
                "amount": 1500.0, "payment_method": "UPI",
                "transaction_reference": "UPI/DEMO/001",
                "notes": "Partial advance",
                "created_by": admin_id,
                "created_at": now_utc().isoformat(),
            })
            logger.info("Seeded sample report & payment")


@app.on_event("shutdown")
async def shutdown():
    client.close()


app.include_router(api)
app.add_middleware(
    CORSMiddleware,
    allow_credentials=False,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = ROOT_DIR / "frontend"
if FRONTEND_DIR.is_dir():
    @app.middleware("http")
    async def no_cache_frontend(request, call_next):
        # Mobile Safari in particular will keep serving a stale cached JS bundle
        # long after the file changes on disk unless the server explicitly forbids it.
        response = await call_next(request)
        if not request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response

    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

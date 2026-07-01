"""
routers/admin.py — API quản trị
Tất cả endpoints đều yêu cầu header: X-Admin-Token
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func, desc

from database import get_db
from config import get_settings
from models import LicenseKey, UsageLog, Order, CreditPackage
from services.license import (
    create_license_key,
    add_credits,
    revoke_key,
    reset_machine_lock,
    get_key,
    LicenseError,
)

router = APIRouter(prefix="/admin", tags=["admin"])


# ─── Auth ─────────────────────────────────────────────────────

def require_admin(x_admin_token: str = Header(...)):
    settings = get_settings()
    if x_admin_token != settings.admin_token:
        raise HTTPException(status_code=401, detail="Invalid admin token")


# ─── Schemas ──────────────────────────────────────────────────

class CreateKeyRequest(BaseModel):
    credits: int
    note: str = ""
    quantity: int = 1       # tạo nhiều key cùng lúc


class AddCreditsRequest(BaseModel):
    key: str
    credits: int
    note: str = ""


class RevokeKeyRequest(BaseModel):
    key: str
    reason: str = ""


class PackageCreate(BaseModel):
    name: str
    credits: int
    price_vnd: int
    description: str = ""
    sort_order: int = 0


# ─── License key management ───────────────────────────────────

@router.post("/keys/create", dependencies=[Depends(require_admin)])
def create_keys(body: CreateKeyRequest, db: Session = Depends(get_db)):
    """Tạo 1 hoặc nhiều license key cùng lúc."""
    if body.quantity < 1 or body.quantity > 100:
        raise HTTPException(400, "quantity phải từ 1 đến 100")

    keys = []
    for _ in range(body.quantity):
        k = create_license_key(db, credits=body.credits, note=body.note)
        keys.append({
            "key": k.key,
            "credits": k.credits_total,
            "created_at": k.created_at,
        })

    return {"created": len(keys), "keys": keys}


@router.post("/keys/add-credits", dependencies=[Depends(require_admin)])
def admin_add_credits(body: AddCreditsRequest, db: Session = Depends(get_db)):
    """Cấp thêm credit cho key đã có."""
    try:
        k = add_credits(db, body.key, body.credits, body.note)
    except LicenseError as e:
        raise HTTPException(404, e.message)

    return {
        "key": k.key,
        "credits_remaining": k.credits_remaining,
        "credits_total": k.credits_total,
    }


@router.post("/keys/revoke", dependencies=[Depends(require_admin)])
def admin_revoke(body: RevokeKeyRequest, db: Session = Depends(get_db)):
    """Thu hồi key vi phạm."""
    try:
        k = revoke_key(db, body.key, body.reason)
    except LicenseError as e:
        raise HTTPException(404, e.message)

    return {"key": k.key, "revoked": True, "reason": k.revoke_reason}


class ResetMachineRequest(BaseModel):
    key: str


@router.post("/keys/reset-machine", dependencies=[Depends(require_admin)])
def admin_reset_machine(body: ResetMachineRequest, db: Session = Depends(get_db)):
    """
    Gỡ khóa máy cho key — dùng khi khách đổi máy mới,
    cài lại Windows, hoặc cần hỗ trợ chuyển máy hợp lệ.
    Lần dùng tiếp theo, key tự khóa vào máy mới.
    """
    try:
        k = reset_machine_lock(db, body.key)
    except LicenseError as e:
        raise HTTPException(404, e.message)

    return {"key": k.key, "machine_unlocked": True}


@router.get("/keys", dependencies=[Depends(require_admin)])
def list_keys(
    page: int = 1,
    limit: int = 50,
    status: Optional[str] = None,   # active | revoked | all
    db: Session = Depends(get_db),
):
    """Danh sách tất cả license key, có phân trang."""
    q = db.query(LicenseKey)

    if status == "active":
        q = q.filter_by(is_active=True, is_revoked=False)
    elif status == "revoked":
        q = q.filter_by(is_revoked=True)

    total = q.count()
    keys  = q.order_by(desc(LicenseKey.created_at)) \
             .offset((page - 1) * limit) \
             .limit(limit) \
             .all()

    return {
        "total": total,
        "page": page,
        "limit": limit,
        "keys": [
            {
                "key":               k.key,
                "credits_remaining": k.credits_remaining,
                "credits_total":     k.credits_total,
                "credits_used":      k.credits_used,
                "is_active":         k.is_active,
                "is_revoked":        k.is_revoked,
                "note":              k.note,
                "machine_id":        k.machine_id,
                "last_used_at":      k.last_used_at,
                "created_at":        k.created_at,
            }
            for k in keys
        ],
    }


@router.get("/keys/{key}", dependencies=[Depends(require_admin)])
def key_detail(key: str, db: Session = Depends(get_db)):
    """Chi tiết 1 key kèm 20 usage log gần nhất."""
    try:
        k = get_key(db, key)
    except LicenseError as e:
        raise HTTPException(404, e.message)

    logs = (
        db.query(UsageLog)
        .filter_by(license_key_id=k.id)
        .order_by(desc(UsageLog.created_at))
        .limit(20)
        .all()
    )

    return {
        "key":               k.key,
        "credits_remaining": k.credits_remaining,
        "credits_total":     k.credits_total,
        "credits_used":      k.credits_used,
        "is_active":         k.is_active,
        "is_revoked":        k.is_revoked,
        "revoke_reason":     k.revoke_reason,
        "note":              k.note,
        "machine_id":        k.machine_id,
        "last_used_at":      k.last_used_at,
        "created_at":        k.created_at,
        "usage_logs": [
            {
                "action":        l.action,
                "credits_spent": l.credits_spent,
                "detail":        l.detail,
                "ip_address":    l.ip_address,
                "created_at":    l.created_at,
            }
            for l in logs
        ],
    }


# ─── Stats tổng quan ──────────────────────────────────────────

@router.get("/stats", dependencies=[Depends(require_admin)])
def stats(db: Session = Depends(get_db)):
    """Dashboard numbers: tổng key, tổng usage, tổng doanh thu."""
    total_keys    = db.query(func.count(LicenseKey.id)).scalar()
    active_keys   = db.query(func.count(LicenseKey.id)).filter_by(is_active=True, is_revoked=False).scalar()
    revoked_keys  = db.query(func.count(LicenseKey.id)).filter_by(is_revoked=True).scalar()
    total_usage   = db.query(func.sum(UsageLog.credits_spent)).filter(UsageLog.credits_spent > 0).scalar() or 0
    total_revenue = db.query(func.sum(Order.amount_vnd)).filter_by(status="paid").scalar() or 0
    total_orders  = db.query(func.count(Order.id)).filter_by(status="paid").scalar()

    return {
        "keys": {
            "total":   total_keys,
            "active":  active_keys,
            "revoked": revoked_keys,
        },
        "usage": {
            "total_images_processed": total_usage,
        },
        "revenue": {
            "total_vnd":   total_revenue,
            "total_orders": total_orders,
        },
    }


# ─── Credit packages ──────────────────────────────────────────

@router.post("/packages", dependencies=[Depends(require_admin)])
def create_package(body: PackageCreate, db: Session = Depends(get_db)):
    """Tạo gói credit mới."""
    pkg = CreditPackage(
        name=body.name,
        credits=body.credits,
        price_vnd=body.price_vnd,
        description=body.description,
        sort_order=body.sort_order,
    )
    db.add(pkg)
    db.commit()
    db.refresh(pkg)
    return {"id": pkg.id, "name": pkg.name, "credits": pkg.credits, "price_vnd": pkg.price_vnd}


@router.get("/packages", dependencies=[Depends(require_admin)])
def list_packages(db: Session = Depends(get_db)):
    pkgs = db.query(CreditPackage).order_by(CreditPackage.sort_order).all()
    return [
        {"id": p.id, "name": p.name, "credits": p.credits,
         "price_vnd": p.price_vnd, "is_active": p.is_active}
        for p in pkgs
    ]

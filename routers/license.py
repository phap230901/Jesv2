"""
routers/license.py — API cho desktop app gọi
Endpoints:
  POST /license/validate      — kiểm tra key hợp lệ, trả về thông tin
  POST /license/consume       — trừ credit sau khi xử lý ảnh
  GET  /license/{key}/status  — xem credit còn lại
"""

from datetime import datetime
from fastapi import APIRouter, Depends, Request, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from services.license import (
    validate_key,
    validate_and_check_credits,
    consume_credit,
    LicenseError,
)

router = APIRouter(prefix="/license", tags=["license"])


# ─── Schemas ──────────────────────────────────────────────────

class ValidateRequest(BaseModel):
    key: str
    machine_id: str = ""
    required: int = 1   # số credit cần kiểm tra (dùng cho check-credits)


class ConsumeRequest(BaseModel):
    key: str
    credits: int = 1
    machine_id: str = ""
    detail: str = ""        # tên file xử lý


class LicenseStatusResponse(BaseModel):
    key: str
    is_active: bool
    credits_remaining: int
    credits_total: int
    credits_used: int
    last_used_at: datetime | None
    created_at: datetime


# ─── Helpers ──────────────────────────────────────────────────

def _get_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def _license_error_response(e: LicenseError):
    raise HTTPException(
        status_code=403,
        detail={"code": e.code, "message": e.message},
    )


# ─── Endpoints ────────────────────────────────────────────────

@router.post("/validate")
def validate(
    body: ValidateRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    App gọi endpoint này khi khởi động để kiểm tra key.
    Trả về thông tin key nếu hợp lệ.
    """
    try:
        key = validate_key(db, body.key, machine_id=body.machine_id or None)
    except LicenseError as e:
        _license_error_response(e)

    return {
        "valid": True,
        "key": key.key,
        "credits_remaining": key.credits_remaining,
        "credits_total": key.credits_total,
        "credits_used": key.credits_used,
        "last_used_at": key.last_used_at,
        "created_at": key.created_at,
    }


@router.post("/consume")
def consume(
    body: ConsumeRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    App gọi sau khi xử lý ảnh thành công để trừ credit.
    Trả về credit còn lại sau khi trừ.
    """
    try:
        key = consume_credit(
            db,
            key_str=body.key,
            credits=body.credits,
            ip_address=_get_ip(request),
            machine_id=body.machine_id or None,
            detail=body.detail,
        )
    except LicenseError as e:
        _license_error_response(e)

    return {
        "success": True,
        "credits_remaining": key.credits_remaining,
        "credits_used": key.credits_used,
    }


@router.post("/check-credits")
def check_credits(
    body: ValidateRequest,
    db: Session = Depends(get_db),
):
    """
    App gọi trước khi bắt đầu batch để kiểm tra đủ credit không.
    Không trừ credit.
    """
    try:
        key = validate_and_check_credits(
            db, body.key,
            required=body.required,
            machine_id=body.machine_id or None,
        )
    except LicenseError as e:
        _license_error_response(e)

    return {
        "sufficient": True,
        "credits_remaining": key.credits_remaining,
    }


@router.get("/{key}/status")
def key_status(
    key: str,
    db: Session = Depends(get_db),
):
    """Xem trạng thái và credit của key (app dùng để hiển thị UI)."""
    try:
        k = validate_key(db, key)
    except LicenseError as e:
        _license_error_response(e)

    return LicenseStatusResponse(
        key=k.key,
        is_active=k.is_active,
        credits_remaining=k.credits_remaining,
        credits_total=k.credits_total,
        credits_used=k.credits_used,
        last_used_at=k.last_used_at,
        created_at=k.created_at,
    )

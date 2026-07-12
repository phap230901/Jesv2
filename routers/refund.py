"""
routers/refund.py — Hệ thống yêu cầu hoàn credit
POST /refund/request              — khách gửi yêu cầu refund
GET  /refund/my?key=DES-XXX       — khách xem lịch sử refund của mình
GET  /admin/refund/list           — admin xem tất cả yêu cầu
POST /admin/refund/{id}/approve   — admin duyệt, tự hoàn credit
POST /admin/refund/{id}/reject    — admin từ chối kèm lý do
"""

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, ForeignKey
from datetime import datetime

from database import get_db, Base
from config import get_settings
from models import LicenseKey
from services.license import get_key, add_credits, LicenseError

router_public = APIRouter(prefix="/refund",       tags=["refund"])
router_admin  = APIRouter(prefix="/admin/refund", tags=["refund-admin"])


# ── Model ─────────────────────────────────────────────────────

class RefundRequest(Base):
    __tablename__ = "refund_requests"

    id              = Column(Integer,     primary_key=True, autoincrement=True)
    license_key     = Column(String(20),  nullable=False, index=True)
    machine_id      = Column(String(64),  nullable=True)
    filename        = Column(String(500), nullable=False)
    output_path     = Column(String(500), nullable=True)   # đường dẫn ảnh kết quả
    original_url    = Column(Text,        nullable=True)   # url ảnh gốc (imgbb)
    output_url      = Column(Text,        nullable=True)   # url ảnh kết quả (imgbb)
    reason          = Column(String(100), nullable=False)  # lý do ngắn gọn
    note            = Column(Text,        nullable=True)   # ghi chú thêm của khách
    credits_amount  = Column(Integer,     default=10)      # số credit yêu cầu hoàn
    status          = Column(String(20),  default="pending")  # pending|approved|rejected
    admin_note      = Column(Text,        nullable=True)   # lý do từ chối của admin
    created_at      = Column(DateTime,    default=datetime.utcnow)
    reviewed_at     = Column(DateTime,    nullable=True)


# ── Auth ──────────────────────────────────────────────────────

def require_admin(x_admin_token: str = Header(...)):
    if x_admin_token != get_settings().admin_token:
        raise HTTPException(401, "Invalid admin token")


# ── Schemas ───────────────────────────────────────────────────

class RefundRequestBody(BaseModel):
    key:            str
    machine_id:     str = ""
    filename:       str
    original_url:   str = ""
    output_url:     str = ""
    reason:         str              # "Ảnh bị lỗi / nhiễu" | "Sai màu sắc" | ...
    note:           str = ""
    credits_amount: int = 10


class RejectBody(BaseModel):
    admin_note: str


# ── Public endpoints ──────────────────────────────────────────

@router_public.post("/request")
def submit_refund(body: RefundRequestBody, db: Session = Depends(get_db)):
    """Khách gửi yêu cầu hoàn credit."""

    # Validate key tồn tại
    key_obj = db.query(LicenseKey).filter_by(key=body.key).first()
    if not key_obj:
        raise HTTPException(404, "Key không tồn tại")
    if key_obj.is_revoked:
        raise HTTPException(403, "Key đã bị thu hồi")

    # Tránh spam: 1 ảnh chỉ được gửi 1 yêu cầu refund
    existing = db.query(RefundRequest).filter_by(
        license_key=body.key,
        filename=body.filename,
    ).first()
    if existing:
        raise HTTPException(400, f"Ảnh '{body.filename}' đã có yêu cầu refund (trạng thái: {existing.status})")

    req = RefundRequest(
        license_key    = body.key,
        machine_id     = body.machine_id,
        filename       = body.filename,
        original_url   = body.original_url,
        output_url     = body.output_url,
        reason         = body.reason,
        note           = body.note,
        credits_amount = body.credits_amount,
    )
    db.add(req)
    db.commit()
    db.refresh(req)

    return {
        "ok":     True,
        "id":     req.id,
        "status": req.status,
        "message": "Yêu cầu đã được gửi. Admin sẽ review trong thời gian sớm nhất.",
    }


@router_public.get("/my")
def get_my_refunds(key: str, db: Session = Depends(get_db)):
    """Khách xem lịch sử yêu cầu refund của mình."""
    reqs = (
        db.query(RefundRequest)
        .filter_by(license_key=key)
        .order_by(RefundRequest.created_at.desc())
        .limit(50)
        .all()
    )
    return [_fmt(r) for r in reqs]


# ── Admin endpoints ───────────────────────────────────────────

@router_admin.get("/list", dependencies=[Depends(require_admin)])
def list_refunds(status: str = "pending", db: Session = Depends(get_db)):
    """Admin xem danh sách yêu cầu refund theo trạng thái."""
    q = db.query(RefundRequest)
    if status != "all":
        q = q.filter_by(status=status)
    reqs = q.order_by(RefundRequest.created_at.desc()).limit(200).all()
    return [_fmt(r) for r in reqs]


@router_admin.get("/stats", dependencies=[Depends(require_admin)])
def refund_stats(db: Session = Depends(get_db)):
    """Tổng quan số liệu refund."""
    from sqlalchemy import func
    total    = db.query(func.count(RefundRequest.id)).scalar()
    pending  = db.query(func.count(RefundRequest.id)).filter_by(status="pending").scalar()
    approved = db.query(func.count(RefundRequest.id)).filter_by(status="approved").scalar()
    rejected = db.query(func.count(RefundRequest.id)).filter_by(status="rejected").scalar()
    credits_refunded = (
        db.query(func.sum(RefundRequest.credits_amount))
        .filter_by(status="approved")
        .scalar() or 0
    )
    return {
        "total":            total,
        "pending":          pending,
        "approved":         approved,
        "rejected":         rejected,
        "credits_refunded": credits_refunded,
    }


@router_admin.post("/{refund_id}/approve", dependencies=[Depends(require_admin)])
def approve_refund(refund_id: int, db: Session = Depends(get_db)):
    """
    Admin duyệt yêu cầu — tự động hoàn credit về key của khách.
    """
    req = db.query(RefundRequest).filter_by(id=refund_id).first()
    if not req:
        raise HTTPException(404, "Không tìm thấy yêu cầu")
    if req.status != "pending":
        raise HTTPException(400, f"Yêu cầu đã được xử lý (trạng thái: {req.status})")

    # Hoàn credit
    try:
        key_obj = add_credits(db, req.license_key, req.credits_amount)
    except LicenseError as e:
        raise HTTPException(400, str(e))

    req.status      = "approved"
    req.reviewed_at = datetime.utcnow()
    db.commit()

    # Gửi thông báo tới khách
    _send_refund_notify(
        db, req.license_key,
        title="✅ Yêu cầu hoàn credit được duyệt",
        message=f"Yêu cầu hoàn credit cho ảnh '{req.filename}' đã được chấp thuận. "
                f"{req.credits_amount} credit đã được hoàn vào tài khoản của bạn.",
        ntype="success",
    )

    return {
        "ok":                True,
        "refund_id":         refund_id,
        "credits_refunded":  req.credits_amount,
        "credits_remaining": key_obj.credits_remaining,
        "message":           f"Đã hoàn {req.credits_amount} credit cho key {req.license_key}",
    }


@router_admin.post("/{refund_id}/reject", dependencies=[Depends(require_admin)])
def reject_refund(refund_id: int, body: RejectBody, db: Session = Depends(get_db)):
    """Admin từ chối yêu cầu kèm lý do."""
    req = db.query(RefundRequest).filter_by(id=refund_id).first()
    if not req:
        raise HTTPException(404, "Không tìm thấy yêu cầu")
    if req.status != "pending":
        raise HTTPException(400, f"Yêu cầu đã được xử lý (trạng thái: {req.status})")
    if not body.admin_note.strip():
        raise HTTPException(400, "Phải điền lý do từ chối")

    req.status      = "rejected"
    req.admin_note  = body.admin_note.strip()
    req.reviewed_at = datetime.utcnow()
    db.commit()

    # Gửi thông báo tới khách
    _send_refund_notify(
        db, req.license_key,
        title="❌ Yêu cầu hoàn credit không được duyệt",
        message=f"Yêu cầu hoàn credit cho ảnh '{req.filename}' đã bị từ chối. "
                f"Lý do: {body.admin_note.strip()}",
        ntype="warning",
    )

    return {
        "ok":       True,
        "refund_id": refund_id,
        "message":  "Đã từ chối yêu cầu",
    }


# ── Helper ────────────────────────────────────────────────────

def _send_refund_notify(db, license_key: str,
                        title: str, message: str, ntype: str = "info"):
    """Tạo thông báo trong bảng notifications để khách nhận qua chuông."""
    try:
        from routers.notify import Notification
        n = Notification(
            title=f"[{license_key[:12]}] {title}",
            message=message,
            type=ntype,
        )
        db.add(n)
        db.commit()
    except Exception as e:
        print(f"[refund notify] {e}")


def _fmt(r: RefundRequest) -> dict:
    return {
        "id":             r.id,
        "license_key":    r.license_key,
        "filename":       r.filename,
        "original_url":   r.original_url or "",
        "output_url":     r.output_url or "",
        "reason":         r.reason,
        "note":           r.note or "",
        "credits_amount": r.credits_amount,
        "status":         r.status,
        "admin_note":     r.admin_note or "",
        "created_at":     r.created_at.isoformat() if r.created_at else "",
        "reviewed_at":    r.reviewed_at.isoformat() if r.reviewed_at else "",
    }

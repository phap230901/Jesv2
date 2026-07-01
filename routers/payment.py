"""
routers/payment.py — Payment endpoints
  POST /payment/create       — tạo đơn + link QR
  POST /payment/webhook      — PayOS callback (tự động cấp key)
  GET  /payment/status/{code} — kiểm tra trạng thái đơn
  GET  /payment/success      — redirect sau thanh toán thành công
  GET  /payment/cancel       — redirect sau khi huỷ
"""

import random
import string
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from database import get_db
from models import Order, CreditPackage, LicenseKey
from services.payos import (
    create_payment_link,
    verify_webhook_signature,
    get_payment_info,
)
from services.license import create_license_key, add_credits, get_key, LicenseError

router = APIRouter(prefix="/payment", tags=["payment"])


# ─── Schemas ──────────────────────────────────────────────────

class CreateOrderRequest(BaseModel):
    package_id:  int
    buyer_email: str = ""
    buyer_name:  str = ""
    buyer_phone: str = ""
    # Nếu đã có key thì nạp thêm credit vào key đó, không tạo key mới
    existing_key: str = ""


class CreateOrderResponse(BaseModel):
    order_code:   str
    checkout_url: str
    qr_code:      str
    amount:       int
    credits:      int
    package_name: str
    expires_in:   int   # phút


# ─── Helpers ──────────────────────────────────────────────────

def _gen_order_code() -> int:
    """Tạo order code 8 chữ số ngẫu nhiên (PayOS yêu cầu int)."""
    return random.randint(10_000_000, 99_999_999)


def _ensure_unique_order_code(db: Session) -> int:
    for _ in range(10):
        code = _gen_order_code()
        if not db.query(Order).filter_by(order_code=str(code)).first():
            return code
    raise RuntimeError("Không thể tạo order code duy nhất")


# ─── Create order ─────────────────────────────────────────────

@router.post("/create", response_model=CreateOrderResponse)
def create_order(
    body: CreateOrderRequest,
    db: Session = Depends(get_db),
):
    """
    Tạo đơn hàng và link thanh toán PayOS.
    Desktop app hoặc trang web gọi endpoint này.
    """
    # Lấy gói
    pkg = db.query(CreditPackage).filter_by(
        id=body.package_id, is_active=True
    ).first()
    if not pkg:
        raise HTTPException(404, "Gói credit không tồn tại")

    # Validate existing_key nếu có
    if body.existing_key:
        try:
            get_key(db, body.existing_key)
        except LicenseError:
            raise HTTPException(400, "License key không tồn tại")

    order_code = _ensure_unique_order_code(db)
    description = f"DES {pkg.name} {pkg.credits}cr"[:25]

    # Tạo link PayOS
    try:
        payos_data = create_payment_link(
            order_code=order_code,
            amount=int(pkg.price_vnd),
            description=description,
            buyer_name=body.buyer_name,
            buyer_email=body.buyer_email,
            buyer_phone=body.buyer_phone,
            items=[{
                "name":     pkg.name,
                "quantity": 1,
                "price":    int(pkg.price_vnd),
            }],
            expire_minutes=30,
        )
    except Exception as e:
        raise HTTPException(502, f"PayOS error: {e}")

    # Lưu đơn vào DB
    order = Order(
        order_code=str(order_code),
        package_id=pkg.id,
        amount_vnd=pkg.price_vnd,
        credits=pkg.credits,
        status="pending",
        payos_payment_link_id=payos_data.get("paymentLinkId", ""),
        buyer_email=body.buyer_email,
        buyer_note=body.existing_key,   # lưu existing_key vào buyer_note
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    return CreateOrderResponse(
        order_code=str(order_code),
        checkout_url=payos_data["checkoutUrl"],
        qr_code=payos_data.get("qrCode", ""),
        amount=int(pkg.price_vnd),
        credits=pkg.credits,
        package_name=pkg.name,
        expires_in=30,
    )


# ─── Webhook ──────────────────────────────────────────────────

@router.post("/webhook")
async def payos_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    PayOS gọi endpoint này sau khi thanh toán thành công/thất bại.
    QUAN TRỌNG: Phải trả về {"code": "00"} trong vòng 5 giây.
    """
    try:
        body = await request.json()
    except Exception:
        return {"code": "01", "desc": "Invalid JSON"}

    # Xác minh chữ ký
    if not verify_webhook_signature(body):
        return {"code": "01", "desc": "Invalid signature"}

    data       = body.get("data", {})
    order_code = str(data.get("orderCode", ""))
    code       = data.get("code", "")           # "00" = thành công
    tx_id      = data.get("reference", "")

    # Trả về 200 ngay, xử lý ở background
    background_tasks.add_task(
        _process_payment,
        db,
        order_code,
        code,
        tx_id,
        data,
    )

    return {"code": "00", "desc": "success"}


def _process_payment(
    db: Session,
    order_code: str,
    code: str,
    tx_id: str,
    data: dict,
):
    """Xử lý sau khi webhook xác nhận — chạy trong background task."""
    order = db.query(Order).filter_by(order_code=order_code).first()
    if not order:
        print(f"[webhook] Order {order_code} không tìm thấy")
        return

    if order.status == "paid":
        print(f"[webhook] Order {order_code} đã xử lý rồi, bỏ qua")
        return

    if code != "00":
        # Thanh toán thất bại hoặc bị huỷ
        order.status = "failed"
        db.commit()
        print(f"[webhook] Order {order_code} thất bại, code={code}")
        return

    # === Thanh toán thành công ===
    order.status               = "paid"
    order.payos_transaction_id = tx_id
    order.paid_at              = datetime.utcnow()

    existing_key_str = order.buyer_note  # lưu ở bước create

    if existing_key_str:
        # Nạp thêm credit vào key có sẵn
        try:
            key = add_credits(
                db,
                existing_key_str,
                order.credits,
                note=f"Order {order_code}",
            )
            order.license_key_id = key.id
        except LicenseError as e:
            print(f"[webhook] add_credits lỗi: {e}")
    else:
        # Tạo key mới
        key = create_license_key(
            db,
            credits=order.credits,
            note=f"Order {order_code} | {order.buyer_email}",
        )
        order.license_key_id = key.id

    db.commit()
    print(f"[webhook] Order {order_code} OK — key={key.key} +{order.credits} credits")

    # Gửi email thông báo (Phase 3 mở rộng)
    # _send_key_email(order.buyer_email, key.key, order.credits)


# ─── Status check ─────────────────────────────────────────────

@router.get("/status/{order_code}")
def payment_status(order_code: str, db: Session = Depends(get_db)):
    """
    Desktop app polling endpoint này sau khi mở link QR.
    Trả về key nếu đã thanh toán xong.
    """
    order = db.query(Order).filter_by(order_code=order_code).first()
    if not order:
        raise HTTPException(404, "Đơn hàng không tồn tại")

    result = {
        "order_code": order_code,
        "status":     order.status,
        "credits":    order.credits,
        "paid_at":    order.paid_at,
    }

    if order.status == "paid" and order.license_key_id:
        key = db.query(LicenseKey).filter_by(id=order.license_key_id).first()
        if key:
            result["license_key"]       = key.key
            result["credits_remaining"] = key.credits_remaining

    return result


# ─── Redirect pages ───────────────────────────────────────────

SUCCESS_HTML = """
<!DOCTYPE html><html lang="vi">
<head>
  <meta charset="UTF-8">
  <title>Thanh toán thành công</title>
  <style>
    body {{ font-family: sans-serif; display:flex; flex-direction:column;
           align-items:center; justify-content:center; min-height:100vh;
           margin:0; background:#f0fdf4; }}
    .card {{ background:white; border-radius:16px; padding:40px 48px;
             text-align:center; box-shadow:0 4px 24px rgba(0,0,0,.08); }}
    h1 {{ color:#16a34a; margin:0 0 8px; }}
    p  {{ color:#555; margin:4px 0; }}
    .key {{ font-size:22px; font-weight:600; letter-spacing:3px;
            background:#f0fdf4; border:1px solid #86efac;
            padding:12px 24px; border-radius:8px; margin:20px 0; }}
    small {{ color:#aaa; font-size:12px; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>✅ Thanh toán thành công!</h1>
    <p>Đơn hàng <strong>{order_code}</strong> đã được xác nhận.</p>
    <p>License key của bạn:</p>
    <div class="key">{license_key}</div>
    <p>Credit: <strong>{credits}</strong> ảnh</p>
    <p>Mở app DES AUTO → Settings → nhập key để bắt đầu.</p>
    <small>Lưu key này lại cẩn thận. Bạn có thể dùng key ở nhiều máy.</small>
  </div>
</body></html>
"""

PENDING_HTML = """
<!DOCTYPE html><html lang="vi">
<head>
  <meta charset="UTF-8">
  <title>Đang xử lý...</title>
  <meta http-equiv="refresh" content="5">
  <style>
    body {{ font-family:sans-serif; display:flex; align-items:center;
           justify-content:center; min-height:100vh; margin:0; background:#fefce8; }}
    .card {{ background:white; border-radius:16px; padding:40px 48px;
             text-align:center; box-shadow:0 4px 24px rgba(0,0,0,.08); }}
    h1 {{ color:#ca8a04; }}
    p  {{ color:#555; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>⏳ Đang xác nhận thanh toán...</h1>
    <p>Trang sẽ tự refresh sau 5 giây.</p>
    <p>Đơn hàng: <strong>{order_code}</strong></p>
  </div>
</body></html>
"""

CANCEL_HTML = """
<!DOCTYPE html><html lang="vi">
<head>
  <meta charset="UTF-8">
  <title>Đã huỷ thanh toán</title>
  <style>
    body {{ font-family:sans-serif; display:flex; align-items:center;
           justify-content:center; min-height:100vh; margin:0; background:#fef2f2; }}
    .card {{ background:white; border-radius:16px; padding:40px 48px;
             text-align:center; box-shadow:0 4px 24px rgba(0,0,0,.08); }}
    h1 {{ color:#dc2626; }}
    a  {{ color:#2563eb; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>❌ Đã huỷ thanh toán</h1>
    <p>Bạn đã huỷ giao dịch. Không có khoản nào bị trừ.</p>
    <p><a href="javascript:history.back()">← Quay lại</a></p>
  </div>
</body></html>
"""


@router.get("/success", response_class=HTMLResponse)
def payment_success(orderCode: str = "", db: Session = Depends(get_db)):
    """PayOS redirect về đây sau khi thanh toán."""
    if not orderCode:
        return HTMLResponse("<h1>Không tìm thấy đơn hàng</h1>", status_code=400)

    order = db.query(Order).filter_by(order_code=orderCode).first()
    if not order:
        return HTMLResponse("<h1>Đơn hàng không tồn tại</h1>", status_code=404)

    if order.status == "paid" and order.license_key_id:
        key = db.query(LicenseKey).filter_by(id=order.license_key_id).first()
        return HTMLResponse(SUCCESS_HTML.format(
            order_code=orderCode,
            license_key=key.key if key else "Đang xử lý...",
            credits=order.credits,
        ))

    # Webhook chưa về kịp → hiện pending, tự refresh
    return HTMLResponse(PENDING_HTML.format(order_code=orderCode))


@router.get("/cancel", response_class=HTMLResponse)
def payment_cancel():
    return HTMLResponse(CANCEL_HTML)

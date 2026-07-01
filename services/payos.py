"""
services/payos.py — PayOS integration
Docs: https://payos.vn/docs/

Flow:
  1. App/web gọi create_payment_link() → nhận URL QR
  2. Người dùng quét QR chuyển khoản
  3. PayOS gọi webhook → verify_webhook_signature() → xử lý
  4. Tự động cấp credit + tạo license key
"""

import hashlib
import hmac
import httpx
from datetime import datetime

from config import get_settings

PAYOS_API_BASE = "https://api-merchant.payos.vn"


def _get_headers() -> dict:
    s = get_settings()
    return {
        "x-client-id":  s.payos_client_id,
        "x-api-key":    s.payos_api_key,
        "Content-Type": "application/json",
    }


def _make_checksum(data: dict, checksum_key: str) -> str:
    """Tạo chữ ký HMAC-SHA256 theo chuẩn PayOS (sort key alphabet)."""
    sorted_items = sorted(data.items())
    raw = "&".join(f"{k}={v}" for k, v in sorted_items)
    return hmac.new(
        checksum_key.encode(),
        raw.encode(),
        hashlib.sha256,
    ).hexdigest()


def _checksum_for_create(
    order_code: int,
    amount: int,
    description: str,
    return_url: str,
    cancel_url: str,
) -> str:
    s = get_settings()
    return _make_checksum(
        {
            "amount":      amount,
            "cancelUrl":   cancel_url,
            "description": description,
            "orderCode":   order_code,
            "returnUrl":   return_url,
        },
        s.payos_checksum_key,
    )


# ─── Create payment link ───────────────────────────────────────

def create_payment_link(
    order_code: int,
    amount: int,
    description: str,
    buyer_name: str = "",
    buyer_email: str = "",
    buyer_phone: str = "",
    items: list = None,
    return_url: str = "",
    cancel_url: str = "",
    expire_minutes: int = 30,
) -> dict:
    """
    Tạo link thanh toán PayOS.
    Returns dict gồm: checkoutUrl, qrCode, paymentLinkId
    """
    s = get_settings()

    if not return_url:
        return_url = f"{s.app_url}/payment/success"
    if not cancel_url:
        cancel_url = f"{s.app_url}/payment/cancel"

    if not items:
        items = [{"name": description, "quantity": 1, "price": amount}]

    description = description[:25]  # PayOS giới hạn 25 ký tự

    payload = {
        "orderCode":   order_code,
        "amount":      amount,
        "description": description,
        "buyerName":   buyer_name,
        "buyerEmail":  buyer_email,
        "buyerPhone":  buyer_phone,
        "items":       items,
        "returnUrl":   return_url,
        "cancelUrl":   cancel_url,
        "expiredAt":   int(datetime.utcnow().timestamp()) + expire_minutes * 60,
        "signature":   _checksum_for_create(
            order_code, amount, description, return_url, cancel_url
        ),
    }

    r = httpx.post(
        f"{PAYOS_API_BASE}/v2/payment-requests",
        headers=_get_headers(),
        json=payload,
        timeout=15,
    )
    data = r.json()

    if data.get("code") != "00":
        raise ValueError(f"PayOS error {data.get('code')}: {data.get('desc')}")

    return data["data"]


# ─── Verify webhook ───────────────────────────────────────────

def verify_webhook_signature(webhook_body: dict) -> bool:
    """Xác minh chữ ký webhook từ PayOS. Trả True nếu hợp lệ."""
    s = get_settings()
    received = webhook_body.get("signature", "")
    d = webhook_body.get("data", {})

    fields = {
        "orderCode":              d.get("orderCode", ""),
        "amount":                 d.get("amount", ""),
        "description":            d.get("description", ""),
        "accountNumber":          d.get("accountNumber", ""),
        "reference":              d.get("reference", ""),
        "transactionDateTime":    d.get("transactionDateTime", ""),
        "currency":               d.get("currency", ""),
        "paymentLinkId":          d.get("paymentLinkId", ""),
        "code":                   d.get("code", ""),
        "desc":                   d.get("desc", ""),
        "counterAccountBankId":   d.get("counterAccountBankId", ""),
        "counterAccountBankName": d.get("counterAccountBankName", ""),
        "counterAccountName":     d.get("counterAccountName", ""),
        "counterAccountNumber":   d.get("counterAccountNumber", ""),
        "virtualAccountName":     d.get("virtualAccountName", ""),
        "virtualAccountNumber":   d.get("virtualAccountNumber", ""),
    }

    expected = _make_checksum(fields, s.payos_checksum_key)
    return hmac.compare_digest(expected, received)


# ─── Query / cancel ───────────────────────────────────────────

def get_payment_info(order_code: int) -> dict:
    """Truy vấn trạng thái đơn từ PayOS (dùng để đối soát thủ công)."""
    r = httpx.get(
        f"{PAYOS_API_BASE}/v2/payment-requests/{order_code}",
        headers=_get_headers(),
        timeout=10,
    )
    data = r.json()
    if data.get("code") != "00":
        raise ValueError(f"PayOS error: {data.get('desc')}")
    return data["data"]


def cancel_payment(order_code: int, reason: str = "") -> dict:
    """Huỷ link thanh toán."""
    r = httpx.delete(
        f"{PAYOS_API_BASE}/v2/payment-requests/{order_code}",
        headers=_get_headers(),
        json={"cancellationReason": reason},
        timeout=10,
    )
    return r.json()

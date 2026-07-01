"""
services/license.py — Business logic cho license key
- Tạo key dạng DES-XXXX-XXXX-XXXX
- Validate key từ app
- Trừ credit khi xử lý ảnh
- Cấp thêm credit
"""

import random
import string
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from models import LicenseKey, UsageLog, User


# ─── Tạo key ──────────────────────────────────────────────────

def _random_segment(length: int = 4) -> str:
    chars = string.ascii_uppercase + string.digits
    # Loại bỏ ký tự dễ nhầm: 0/O, 1/I/L
    chars = chars.replace("0", "").replace("O", "")
    chars = chars.replace("1", "").replace("I", "").replace("L", "")
    return "".join(random.choices(chars, k=length))


def generate_key() -> str:
    """Tạo key dạng DES-XXXX-XXXX-XXXX."""
    return f"DES-{_random_segment()}-{_random_segment()}-{_random_segment()}"


def create_license_key(
    db: Session,
    credits: int,
    note: str = "",
    user_id: Optional[int] = None,
) -> LicenseKey:
    """Tạo license key mới với số credit cho trước."""
    # Đảm bảo key không trùng
    for _ in range(10):
        key_str = generate_key()
        if not db.query(LicenseKey).filter_by(key=key_str).first():
            break

    key = LicenseKey(
        key=key_str,
        user_id=user_id,
        credits_total=credits,
        credits_used=0,
        credits_remaining=credits,
        note=note,
    )
    db.add(key)
    db.commit()
    db.refresh(key)
    return key


# ─── Validate & sử dụng ───────────────────────────────────────

class LicenseError(Exception):
    """Lỗi license — trả về message cho client."""
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def get_key(db: Session, key_str: str) -> LicenseKey:
    """Lấy key từ DB, raise LicenseError nếu không tồn tại."""
    key = db.query(LicenseKey).filter_by(key=key_str).first()
    if not key:
        raise LicenseError("KEY_NOT_FOUND", "License key không tồn tại.")
    return key


def validate_key(
    db: Session,
    key_str: str,
    machine_id: str = None,
) -> LicenseKey:
    """
    Kiểm tra key hợp lệ để dùng app.
    Raise LicenseError nếu có vấn đề.

    Machine lock:
    - Nếu key chưa từng được dùng (machine_id NULL) → tự động khóa
      vào machine_id của lần gọi đầu tiên.
    - Nếu key đã khóa vào 1 máy khác → từ chối.
    - machine_id=None (không truyền) → bỏ qua kiểm tra machine lock,
      dùng cho các API admin/nội bộ không cần khóa máy.
    """
    key = get_key(db, key_str)

    if key.is_revoked:
        raise LicenseError(
            "KEY_REVOKED",
            f"License key đã bị thu hồi. Lý do: {key.revoke_reason or 'Không rõ'}",
        )

    if not key.is_active:
        raise LicenseError("KEY_INACTIVE", "License key không còn hoạt động.")

    if machine_id:
        if not key.machine_id:
            # Lần đầu dùng key — khóa vào máy này luôn
            key.machine_id = machine_id
            db.commit()
            db.refresh(key)
        elif key.machine_id != machine_id:
            raise LicenseError(
                "MACHINE_MISMATCH",
                "Key này đã được kích hoạt trên máy khác. "
                "Mỗi key chỉ dùng được trên 1 máy duy nhất. "
                "Liên hệ để được hỗ trợ chuyển máy nếu cần.",
            )

    return key


def validate_and_check_credits(
    db: Session,
    key_str: str,
    required: int = 1,
    machine_id: str = None,
) -> LicenseKey:
    """Validate key (kèm machine lock) VÀ kiểm tra đủ credit."""
    key = validate_key(db, key_str, machine_id=machine_id)

    if key.credits_remaining < required:
        raise LicenseError(
            "INSUFFICIENT_CREDITS",
            f"Không đủ credit. Còn {key.credits_remaining}, cần {required}.",
        )

    return key


def consume_credit(
    db: Session,
    key_str: str,
    credits: int = 1,
    ip_address: str = None,
    machine_id: str = None,
    detail: str = None,
) -> LicenseKey:
    """
    Trừ credit sau khi xử lý ảnh thành công.
    Ghi UsageLog.
    """
    key = validate_and_check_credits(
        db, key_str, required=credits, machine_id=machine_id
    )

    key.credits_used      += credits
    key.credits_remaining -= credits
    key.last_used_at       = datetime.utcnow()

    log = UsageLog(
        license_key_id=key.id,
        action="process_image",
        credits_spent=credits,
        ip_address=ip_address,
        machine_id=machine_id,
        detail=detail,
    )
    db.add(log)
    db.commit()
    db.refresh(key)
    return key


def add_credits(
    db: Session,
    key_str: str,
    credits: int,
    note: str = "",
) -> LicenseKey:
    """Cấp thêm credit cho key (admin hoặc sau thanh toán)."""
    key = get_key(db, key_str)

    key.credits_total     += credits
    key.credits_remaining += credits

    # Ghi log với action khác để phân biệt
    log = UsageLog(
        license_key_id=key.id,
        action="add_credits",
        credits_spent=-credits,   # âm = nạp thêm
        detail=note,
    )
    db.add(log)
    db.commit()
    db.refresh(key)
    return key


def revoke_key(
    db: Session,
    key_str: str,
    reason: str = "",
) -> LicenseKey:
    """Thu hồi key (admin)."""
    key = get_key(db, key_str)
    key.is_revoked    = True
    key.is_active     = False
    key.revoke_reason = reason
    db.commit()
    db.refresh(key)
    return key


def reset_machine_lock(db: Session, key_str: str) -> LicenseKey:
    """
    Gỡ khóa máy (admin) — dùng khi khách đổi máy mới,
    cài lại Windows, hoặc cần hỗ trợ chuyển máy hợp lệ.
    Lần dùng tiếp theo, key sẽ tự khóa vào máy mới đó.
    """
    key = get_key(db, key_str)
    key.machine_id = None
    db.commit()
    db.refresh(key)
    return key

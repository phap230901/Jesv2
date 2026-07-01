"""
models.py — Database schema
Bảng: users, license_keys, credit_packages, orders, usage_logs
"""

from datetime import datetime
from sqlalchemy import (
    Column, String, Integer, BigInteger,
    Boolean, DateTime, Text, ForeignKey, Numeric
)
from sqlalchemy.orm import relationship
from database import Base


# ─── Users ────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id         = Column(Integer, primary_key=True, index=True)
    email      = Column(String(255), unique=True, index=True, nullable=True)
    note       = Column(String(500), nullable=True)   # ghi chú admin
    is_active  = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    license_keys = relationship("LicenseKey", back_populates="user")
    orders       = relationship("Order", back_populates="user")


# ─── License keys ──────────────────────────────────────────────

class LicenseKey(Base):
    __tablename__ = "license_keys"

    id              = Column(Integer, primary_key=True, index=True)
    key             = Column(String(50), unique=True, index=True, nullable=False)
    user_id         = Column(Integer, ForeignKey("users.id"), nullable=True)

    # Credits
    credits_total   = Column(Integer, default=0)   # tổng credit đã mua
    credits_used    = Column(Integer, default=0)   # đã dùng
    credits_remaining = Column(Integer, default=0) # còn lại (= total - used)

    # Trạng thái
    is_active       = Column(Boolean, default=True)
    is_revoked      = Column(Boolean, default=False)
    revoke_reason   = Column(String(500), nullable=True)

    # Metadata
    note            = Column(String(500), nullable=True)  # ghi chú admin
    machine_id      = Column(String(200), nullable=True)  # lock theo máy (optional)
    last_used_at    = Column(DateTime, nullable=True)
    created_at      = Column(DateTime, default=datetime.utcnow)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user       = relationship("User", back_populates="license_keys")
    usage_logs = relationship("UsageLog", back_populates="license_key")


# ─── Credit packages ───────────────────────────────────────────

class CreditPackage(Base):
    __tablename__ = "credit_packages"

    id          = Column(Integer, primary_key=True, index=True)
    name        = Column(String(100), nullable=False)      # "Starter", "Basic", "Pro"
    credits     = Column(Integer, nullable=False)          # số credit
    price_vnd   = Column(BigInteger, nullable=False)       # giá VNĐ
    description = Column(Text, nullable=True)
    is_active   = Column(Boolean, default=True)
    sort_order  = Column(Integer, default=0)
    created_at  = Column(DateTime, default=datetime.utcnow)

    orders = relationship("Order", back_populates="package")


# ─── Orders ────────────────────────────────────────────────────

class Order(Base):
    __tablename__ = "orders"

    id              = Column(Integer, primary_key=True, index=True)
    order_code      = Column(String(50), unique=True, index=True, nullable=False)
    user_id         = Column(Integer, ForeignKey("users.id"), nullable=True)
    package_id      = Column(Integer, ForeignKey("credit_packages.id"), nullable=False)

    # Số tiền
    amount_vnd      = Column(BigInteger, nullable=False)
    credits         = Column(Integer, nullable=False)

    # Trạng thái: pending | paid | failed | cancelled
    status          = Column(String(20), default="pending", index=True)

    # PayOS
    payos_payment_link_id = Column(String(200), nullable=True)
    payos_transaction_id  = Column(String(200), nullable=True)

    # Key được cấp sau khi thanh toán
    license_key_id  = Column(Integer, ForeignKey("license_keys.id"), nullable=True)

    # Email người mua (không cần tài khoản)
    buyer_email     = Column(String(255), nullable=True)
    buyer_note      = Column(String(500), nullable=True)

    created_at      = Column(DateTime, default=datetime.utcnow)
    paid_at         = Column(DateTime, nullable=True)

    user        = relationship("User", back_populates="orders")
    package     = relationship("CreditPackage", back_populates="orders")
    license_key = relationship("LicenseKey", foreign_keys=[license_key_id])


# ─── Usage logs ────────────────────────────────────────────────

class UsageLog(Base):
    __tablename__ = "usage_logs"

    id              = Column(Integer, primary_key=True, index=True)
    license_key_id  = Column(Integer, ForeignKey("license_keys.id"), nullable=False, index=True)
    action          = Column(String(50), default="process_image")  # loại thao tác
    credits_spent   = Column(Integer, default=1)
    ip_address      = Column(String(50), nullable=True)
    machine_id      = Column(String(200), nullable=True)
    detail          = Column(Text, nullable=True)   # tên file, lỗi, ...
    created_at      = Column(DateTime, default=datetime.utcnow, index=True)

    license_key = relationship("LicenseKey", back_populates="usage_logs")

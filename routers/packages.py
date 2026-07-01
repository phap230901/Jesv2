"""
routers/packages.py — Public API xem gói credit
Desktop app và trang mua hàng dùng endpoint này.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from database import get_db
from models import CreditPackage

router = APIRouter(prefix="/packages", tags=["packages"])


@router.get("")
def list_packages(db: Session = Depends(get_db)):
    """Danh sách gói credit đang active (public, không cần auth)."""
    pkgs = (
        db.query(CreditPackage)
        .filter_by(is_active=True)
        .order_by(CreditPackage.sort_order)
        .all()
    )
    return [
        {
            "id":          p.id,
            "name":        p.name,
            "credits":     p.credits,
            "price_vnd":   p.price_vnd,
            "description": p.description,
        }
        for p in pkgs
    ]

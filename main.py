"""
main.py — DES AUTO Backend API v2
Phase 1: License key system
Phase 2: PayOS payment + credit packages
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy import text
import os

from database import engine, Base
from config import get_settings
from routers import license, admin, packages, payment

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    _seed_default_packages()
    yield


def _seed_default_packages():
    from database import SessionLocal
    from models import CreditPackage
    db = SessionLocal()
    try:
        if db.query(CreditPackage).count() == 0:
            defaults = [
                CreditPackage(name="Starter", credits=100,  price_vnd=99_000,  description="100 ảnh",  sort_order=1),
                CreditPackage(name="Basic",   credits=500,  price_vnd=399_000, description="500 ảnh",  sort_order=2),
                CreditPackage(name="Pro",     credits=1000, price_vnd=699_000, description="1000 ảnh", sort_order=3),
            ]
            db.add_all(defaults)
            db.commit()
            print("[seed] Đã tạo 3 gói credit mặc định")
    finally:
        db.close()


app = FastAPI(
    title="DES AUTO API",
    version="2.0.0",
    description="Backend API — license key, credit & payment system",
    lifespan=lifespan,
    docs_url="/docs" if settings.app_env != "production" else None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Routers ──────────────────────────────────────────────────

app.include_router(license.router)
app.include_router(admin.router)
app.include_router(packages.router)
app.include_router(payment.router)

# ─── Static files (trang mua hàng) ───────────────────────────

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/buy")
def buy_page():
    """Trang mua credit — người dùng vào đây để mua key."""
    return FileResponse(os.path.join(STATIC_DIR, "buy.html"))


# ─── Health & root ────────────────────────────────────────────

@app.get("/health")
def health():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    return {
        "status": "ok" if db_ok else "degraded",
        "db": "connected" if db_ok else "error",
        "env": settings.app_env,
        "version": "2.0.0",
    }


@app.get("/")
def root():
    return {
        "message":  "DES AUTO API v2",
        "buy_page": "/buy",
        "docs":     "/docs" if settings.app_env != "production" else "disabled",
    }

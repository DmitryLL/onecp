import os
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from passlib.hash import pbkdf2_sha256

from database import engine, SessionLocal, Base
from models import AdminUser, Dish
from routes.auth import router as auth_router
from routes.menu import router as menu_router
from routes.orders import router as orders_router
from routes.admin import router as admin_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("onecp")


def init_db():
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        # Create default admin if not exists
        admin = db.query(AdminUser).filter(AdminUser.username == "admin").first()
        if not admin:
            admin_pass = os.getenv("ADMIN_PASSWORD", "onecp2026")
            admin = AdminUser(
                username="admin",
                password_hash=pbkdf2_sha256.hash(admin_pass),
            )
            db.add(admin)
            logger.info("Created default admin user")

        # Seed default dishes if empty
        if db.query(Dish).count() == 0:
            default_dishes = [
                Dish(name="Капучино", price=250, old_price=None, emoji="☕", category="Кофе", sort_order=1),
                Dish(name="Латте", price=280, old_price=None, emoji="☕", category="Кофе", sort_order=2),
                Dish(name="Американо", price=200, old_price=None, emoji="☕", category="Кофе", sort_order=3),
                Dish(name="Раф", price=320, old_price=380, emoji="☕", category="Кофе", sort_order=4),
                Dish(name="Мокко", price=350, old_price=None, emoji="☕", category="Кофе", sort_order=5),
                Dish(name="Чизкейк", price=290, old_price=None, emoji="🍰", category="Десерты", sort_order=10),
                Dish(name="Тирамису", price=350, old_price=400, emoji="🍰", category="Десерты", sort_order=11),
                Dish(name="Круассан", price=180, old_price=None, emoji="🥐", category="Выпечка", sort_order=20),
                Dish(name="Сэндвич с курицей", price=320, old_price=None, emoji="🥪", category="Еда", sort_order=30),
                Dish(name="Салат Цезарь", price=380, old_price=450, emoji="🥗", category="Еда", sort_order=31),
            ]
            db.add_all(default_dishes)
            logger.info("Seeded default dishes")

        db.commit()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="OneCp API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(menu_router)
app.include_router(orders_router)
app.include_router(admin_router)


@app.get("/api/health")
def health():
    return {"status": "ok"}

import os
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from passlib.hash import pbkdf2_sha256
from apscheduler.schedulers.background import BackgroundScheduler

from database import engine, SessionLocal, Base
from models import AdminUser, Dish, SiteSettings, Question
from routes.auth import router as auth_router
from routes.menu import router as menu_router
from routes.orders import router as orders_router
from routes.admin import router as admin_router
from email_report import send_report

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("onecp")

scheduler = BackgroundScheduler()


def init_db():
    Base.metadata.create_all(bind=engine)

    # Add new columns if they don't exist (no Alembic migrations)
    from sqlalchemy import text, inspect
    with engine.connect() as conn:
        insp = inspect(engine)
        existing = [c["name"] for c in insp.get_columns("dishes")]
        for col, coltype in [("proteins", "DOUBLE PRECISION"), ("fats", "DOUBLE PRECISION"), ("carbs", "DOUBLE PRECISION")]:
            if col not in existing:
                conn.execute(text(f"ALTER TABLE dishes ADD COLUMN {col} {coltype}"))
                logger.info(f"Added column dishes.{col}")
        conn.commit()

    db = SessionLocal()
    try:
        # Create or update admin user
        admin_pass = os.getenv("ADMIN_PASSWORD", "onecp2026")
        admin = db.query(AdminUser).filter(AdminUser.username == "admin").first()
        if not admin:
            admin = AdminUser(
                username="admin",
                password_hash=pbkdf2_sha256.hash(admin_pass),
            )
            db.add(admin)
            logger.info("Created default admin user")
        else:
            # Always sync password with env var
            if not pbkdf2_sha256.verify(admin_pass, admin.password_hash):
                admin.password_hash = pbkdf2_sha256.hash(admin_pass)
                logger.info("Updated admin password from env")

        # Seed default dishes if empty
        if db.query(Dish).count() == 0:
            default_dishes = [
                Dish(name="Капучино", description="Классический капучино с нежной молочной пенкой", price=250, category="Кофе", weight=300, weight_unit="мл", sort_order=1),
                Dish(name="Латте", description="Мягкий кофе с большим количеством молока", price=280, category="Кофе", weight=350, weight_unit="мл", sort_order=2),
                Dish(name="Американо", description="Крепкий чёрный кофе", price=200, category="Кофе", weight=250, weight_unit="мл", sort_order=3),
                Dish(name="Раф", description="Сливочный кофейный напиток с ванильным вкусом", price=320, category="Кофе", weight=300, weight_unit="мл", sort_order=4),
                Dish(name="Мокко", description="Кофе с шоколадом и молоком", price=350, category="Кофе", weight=300, weight_unit="мл", sort_order=5),
                Dish(name="Чизкейк", description="Нежный сливочный чизкейк", price=290, category="Десерты", weight=150, weight_unit="г", sort_order=10),
                Dish(name="Тирамису", description="Итальянский десерт с кофейной пропиткой", price=350, category="Десерты", weight=160, weight_unit="г", sort_order=11),
                Dish(name="Круассан", description="Хрустящий круассан с маслом", price=180, category="Выпечка", weight=80, weight_unit="г", sort_order=20),
                Dish(name="Сэндвич с курицей", description="Сэндвич с куриным филе, салатом и соусом", price=320, category="Еда", weight=250, weight_unit="г", sort_order=30),
                Dish(name="Салат Цезарь", description="Классический салат с курицей и пармезаном", price=380, category="Еда", weight=220, weight_unit="г", sort_order=31),
            ]
            db.add_all(default_dishes)
            logger.info("Seeded default dishes")

        # Seed default site settings if empty
        if db.query(SiteSettings).count() == 0:
            defaults = {
                "heroTitle": "Добро пожаловать в OneCp",
                "heroSub": "Качественные товары по лучшим ценам с быстрой доставкой по всей России",
                "heroBtn": "Перейти к каталогу",
                "catalogTitle": "Каталог товаров",
                "catalogSub": "Выберите товар и добавьте в корзину",
                "aboutTitle": "Почему мы?",
                "aboutSub": "Наши преимущества",
                "address": "г. Москва, ул. Примерная, д. 1, оф. 101",
                "phone": "+7 (800) 123-45-67",
                "email": "info@onecp.ru",
                "schedule": "Пн-Пт: 9:00 — 20:00\nСб-Вс: 10:00 — 18:00",
                "footer": "© 2026 OneCp. Все права защищены.",
            }
            for key, value in defaults.items():
                db.add(SiteSettings(key=key, value=value))
            logger.info("Seeded default site settings")

        db.commit()
    finally:
        db.close()


def check_report_schedule():
    """Called every minute — sends report if current time matches configured time."""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone(timedelta(hours=10)))  # Vladivostok
    current_hm = now.strftime("%H:%M")
    db = SessionLocal()
    try:
        row = db.query(SiteSettings).filter(SiteSettings.key == "reportTime").first()
        if row and row.value and row.value.strip() == current_hm:
            send_report()
    except Exception as e:
        logger.error(f"[SCHEDULER] Error: {e}")
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler.add_job(check_report_schedule, "interval", minutes=1, id="email_report_check")
    scheduler.start()
    logger.info("Scheduler started")
    yield
    scheduler.shutdown()


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

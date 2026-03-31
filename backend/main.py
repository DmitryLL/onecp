import os
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from passlib.hash import pbkdf2_sha256
from apscheduler.schedulers.background import BackgroundScheduler

from database import engine, SessionLocal, Base
from models import AdminUser, AdminUserLocation, Dish, DishSet, DishSetItem, SiteSettings, Question, CalendarDay, CalendarDaySet, Location
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
        # Migrate weight from float to varchar if needed
        weight_cols = {c["name"]: c for c in insp.get_columns("dishes")}
        if "weight" in weight_cols and str(weight_cols["weight"]["type"]) != "VARCHAR(50)":
            conn.execute(text("ALTER TABLE dishes ALTER COLUMN weight TYPE VARCHAR(50) USING weight::text"))
            logger.info("Migrated dishes.weight to VARCHAR(50)")
        # Migrate questions: email -> phone, add status/comment
        if "questions" in insp.get_table_names():
            q_cols = [c["name"] for c in insp.get_columns("questions")]
            if "email" in q_cols and "phone" not in q_cols:
                conn.execute(text("ALTER TABLE questions RENAME COLUMN email TO phone"))
                logger.info("Renamed questions.email to questions.phone")
            if "status" not in q_cols:
                conn.execute(text("ALTER TABLE questions ADD COLUMN status VARCHAR(30) DEFAULT 'new'"))
                logger.info("Added column questions.status")
            if "comment" not in q_cols:
                conn.execute(text("ALTER TABLE questions ADD COLUMN comment TEXT"))
                logger.info("Added column questions.comment")
            # Drop old columns if exist
            q_cols2 = [c["name"] for c in insp.get_columns("questions")]
            for old_col in ["answer", "answered_at"]:
                if old_col in q_cols2:
                    conn.execute(text(f"ALTER TABLE questions DROP COLUMN {old_col}"))
                    logger.info(f"Dropped column questions.{old_col}")
        # Add image_url to locations
        if "locations" in insp.get_table_names():
            loc_cols = [c["name"] for c in insp.get_columns("locations")]
            if "image_url" not in loc_cols:
                conn.execute(text("ALTER TABLE locations ADD COLUMN image_url VARCHAR(500)"))
                logger.info("Added column locations.image_url")
            if "bottom_image_url" not in loc_cols:
                conn.execute(text("ALTER TABLE locations ADD COLUMN bottom_image_url VARCHAR(500)"))
                logger.info("Added column locations.bottom_image_url")
        # Add password_hash to customers
        cust_cols = [c["name"] for c in insp.get_columns("customers")]
        if "password_hash" not in cust_cols:
            conn.execute(text("ALTER TABLE customers ADD COLUMN password_hash VARCHAR(255)"))
            logger.info("Added column customers.password_hash")
        # Migrate customers: phone -> email (old migration)
        cust_cols2 = [c["name"] for c in insp.get_columns("customers")]
        if "phone" in cust_cols2 and "email" not in cust_cols2:
            conn.execute(text("ALTER TABLE customers ADD COLUMN email VARCHAR(200)"))
            conn.execute(text("UPDATE customers SET email = phone WHERE email IS NULL"))
            conn.execute(text("ALTER TABLE customers DROP COLUMN phone"))
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_customers_email ON customers (email)"))
            logger.info("Migrated customers.phone to customers.email")
        # Create email_codes table if not exists
        if "email_codes" not in insp.get_table_names():
            conn.execute(text("""
                CREATE TABLE email_codes (
                    id SERIAL PRIMARY KEY,
                    email VARCHAR(200) NOT NULL,
                    code VARCHAR(6) NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    used BOOLEAN DEFAULT FALSE
                )
            """))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_email_codes_email ON email_codes (email)"))
            logger.info("Created email_codes table")
        # Drop old sms_codes table if exists
        if "sms_codes" in insp.get_table_names():
            conn.execute(text("DROP TABLE sms_codes"))
            logger.info("Dropped old sms_codes table")
        # Add phone column to customers (for registration)
        cust_cols3 = [c["name"] for c in insp.get_columns("customers")]
        if "phone" not in cust_cols3:
            conn.execute(text("ALTER TABLE customers ADD COLUMN phone VARCHAR(20)"))
            logger.info("Added column customers.phone")
        # Add role to admin_users
        admin_cols = [c["name"] for c in insp.get_columns("admin_users")]
        if "role" not in admin_cols:
            conn.execute(text("ALTER TABLE admin_users ADD COLUMN role VARCHAR(20) DEFAULT 'admin'"))
            logger.info("Added column admin_users.role")
        # Create admin_user_locations table
        if "admin_user_locations" not in insp.get_table_names():
            conn.execute(text("""
                CREATE TABLE admin_user_locations (
                    id SERIAL PRIMARY KEY,
                    admin_user_id INTEGER NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
                    location_id INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE
                )
            """))
            logger.info("Created admin_user_locations table")
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

        # Seed default locations if empty
        if db.query(Location).count() == 0:
            default_locations = [
                Location(name="Sber", address="Фонтанная 18", slug="Sber", description="Точка выдачи в головном офисе СБЕР", sort_order=1),
                Location(name="Sky City", address="Алеутская 45", slug="Skycity", description="Точка выдачи в БЦ Sky City", sort_order=2),
                Location(name="International BayView Towers", address="Енисейская 23", slug="InternationalBayViewtowers", description="Точка выдачи в International BayView Towers", sort_order=3),
            ]
            db.add_all(default_locations)
            logger.info("Seeded default locations")

        db.commit()
    finally:
        db.close()


def generate_calendar_daily():
    """Called at 3:00 AM Vladivostok — creates new calendar day."""
    from routes.admin import ensure_calendar_14_days
    db = SessionLocal()
    try:
        ensure_calendar_14_days(db)
        logger.info("[SCHEDULER] Calendar days generated")
    except Exception as e:
        logger.error(f"[SCHEDULER] Calendar error: {e}")
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
    scheduler.add_job(generate_calendar_daily, "cron", hour=3, minute=0, timezone="Asia/Vladivostok", id="calendar_daily")
    scheduler.start()
    logger.info("Scheduler started")
    yield
    scheduler.shutdown()


app = FastAPI(title="OneCp API", lifespan=lifespan)

# CORS: allow only our domain (override via CORS_ORIGINS env var)
_cors_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "https://order.coffeeplace.one").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

# Security headers + request size limit (2MB)
MAX_BODY_SIZE = 2 * 1024 * 1024

@app.middleware("http")
async def security_middleware(request: Request, call_next):
    # Block oversized request bodies (except file uploads which go up to 10MB via nginx)
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_BODY_SIZE:
        if "/image" not in request.url.path:
            return Response("Request too large", status_code=413)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    if request.url.path.startswith("/api/auth") or request.url.path.startswith("/api/admin"):
        response.headers["Cache-Control"] = "no-store"
    return response

app.include_router(auth_router)
app.include_router(menu_router)
app.include_router(orders_router)
app.include_router(admin_router)


@app.get("/api/health")
def health():
    return {"status": "ok"}

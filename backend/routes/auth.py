import os
import random
import secrets
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session
from jose import jwt
from passlib.hash import pbkdf2_sha256

from database import get_db
from models import Customer, EmailCode, Order, OrderItem

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = logging.getLogger("onecp")

JWT_SECRET = os.getenv("JWT_SECRET", "")
if not JWT_SECRET:
    JWT_SECRET = secrets.token_hex(32)
    logger.critical("JWT_SECRET not set! Generated random secret — tokens will NOT survive restart. Set JWT_SECRET env var!")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = 60 * 24  # 24 hours

# Simple in-memory IP rate limiter
_rate_limits: dict[str, list[float]] = {}

def check_rate_limit(key: str, max_requests: int, window_seconds: int):
    """Raise 429 if too many requests from this key in the window."""
    import time
    now = time.time()
    entries = _rate_limits.get(key, [])
    entries = [t for t in entries if now - t < window_seconds]
    if len(entries) >= max_requests:
        raise HTTPException(status_code=429, detail="Слишком много запросов, попробуйте позже")
    entries.append(now)
    _rate_limits[key] = entries


def normalize_email(email: str) -> str:
    email = email.strip().lower()
    if not email or "@" not in email:
        raise ValueError("Введите корректный email")
    local, domain = email.rsplit("@", 1)
    if not local or not domain or "." not in domain:
        raise ValueError("Введите корректный email")
    return email


class SendCodeRequest(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        return normalize_email(v)


class VerifyCodeRequest(BaseModel):
    email: str
    code: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        return normalize_email(v)


class UpdateNameRequest(BaseModel):
    name: str


def create_token(customer_id: int, email: str) -> str:
    exp = datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MINUTES)
    return jwt.encode({"sub": str(customer_id), "email": email, "exp": exp}, JWT_SECRET, algorithm=JWT_ALGORITHM)


def get_current_customer(db: Session = Depends(get_db), token: str = None) -> Customer:
    """Dependency — extracts customer from Authorization header."""
    pass  # overridden below


def get_current_customer_dep(request: Request, db: Session = Depends(get_db)) -> Customer:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = auth[7:]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        customer_id = int(payload["sub"])
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=401, detail="Customer not found")
    return customer


def _get_smtp_settings(db: Session) -> dict | None:
    """Get SMTP settings from SiteSettings (same as email_report)."""
    from models import SiteSettings
    rows = db.query(SiteSettings).filter(
        SiteSettings.key.in_(["reportSmtpHost", "reportSmtpPort", "reportSmtpEmail", "reportSmtpPassword"])
    ).all()
    settings = {r.key: r.value for r in rows}
    email = settings.get("reportSmtpEmail", "").strip()
    password = settings.get("reportSmtpPassword", "").strip()
    if not email or not password:
        return None
    return settings


def send_email_code(recipient: str, code: str, db: Session):
    """Send verification code via email using SMTP settings from SiteSettings."""
    settings = _get_smtp_settings(db)
    if not settings:
        logger.info(f"[MOCK EMAIL] To: {recipient}, Code: {code}")
        return

    smtp_email = settings["reportSmtpEmail"].strip()
    smtp_pass = settings["reportSmtpPassword"].strip()
    smtp_host = settings.get("reportSmtpHost", "").strip()
    smtp_port = settings.get("reportSmtpPort", "").strip()

    if smtp_host and ":" in smtp_host:
        parts = smtp_host.split(":")
        smtp_host = parts[0]
        if not smtp_port:
            smtp_port = parts[1]
    if not smtp_host:
        from email_report import detect_smtp
        smtp_host, smtp_port = detect_smtp(smtp_email)
    else:
        smtp_port = int(smtp_port) if smtp_port else 465

    msg = MIMEMultipart()
    msg["From"] = smtp_email
    msg["To"] = recipient
    msg["Subject"] = "One Coffee Place — код подтверждения"

    body = f"""Ваш код подтверждения: {code}

Код действителен в течение 5 минут.

Если вы не запрашивали код, просто проигнорируйте это письмо.

— One Coffee Place"""

    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        server = smtplib.SMTP_SSL(smtp_host, int(smtp_port), timeout=15)
        server.login(smtp_email, smtp_pass)
        server.sendmail(smtp_email, [recipient], msg.as_string())
        server.quit()
        logger.info(f"[EMAIL] Verification code sent to {recipient}")
    except Exception as e:
        logger.error(f"[EMAIL] Failed to send to {recipient}: {e}")
        raise Exception(f"Не удалось отправить email: {e}")


@router.post("/send-code")
def send_code(body: SendCodeRequest, request: Request, db: Session = Depends(get_db)):
    # IP rate limit: max 5 requests per 5 minutes per IP
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit(f"email:{client_ip}", max_requests=5, window_seconds=300)
    # Rate limit: max 1 code per 60 seconds
    recent = (
        db.query(EmailCode)
        .filter(EmailCode.email == body.email, EmailCode.used == False)
        .order_by(EmailCode.created_at.desc())
        .first()
    )
    if recent and (datetime.now(timezone.utc) - recent.created_at.replace(tzinfo=timezone.utc)).total_seconds() < 60:
        raise HTTPException(status_code=429, detail="Подождите минуту перед повторной отправкой")

    code = f"{random.randint(1000, 9999)}"

    email_code = EmailCode(email=body.email, code=code)
    db.add(email_code)
    db.commit()

    try:
        send_email_code(body.email, code, db)
    except Exception as e:
        logger.error(f"[EMAIL] Failed to send to {body.email}: {e}")
        raise HTTPException(status_code=502, detail=f"Не удалось отправить код: {e}")

    return {"ok": True, "message": "Код отправлен на email"}


@router.post("/verify-code")
def verify_code(body: VerifyCodeRequest, request: Request, db: Session = Depends(get_db)):
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit(f"verify:{client_ip}", max_requests=10, window_seconds=300)
    email_code = (
        db.query(EmailCode)
        .filter(EmailCode.email == body.email, EmailCode.code == body.code, EmailCode.used == False)
        .order_by(EmailCode.created_at.desc())
        .first()
    )

    if not email_code:
        raise HTTPException(status_code=400, detail="Неверный код")

    # Code expires after 5 minutes
    age = (datetime.now(timezone.utc) - email_code.created_at.replace(tzinfo=timezone.utc)).total_seconds()
    if age > 300:
        raise HTTPException(status_code=400, detail="Код истёк, запросите новый")

    email_code.used = True

    # Find or create customer
    customer = db.query(Customer).filter(Customer.email == body.email).first()
    if not customer:
        customer = Customer(email=body.email)
        db.add(customer)

    db.commit()
    db.refresh(customer)

    token = create_token(customer.id, customer.email)

    return {
        "ok": True,
        "token": token,
        "customer": {
            "id": customer.id,
            "email": customer.email,
            "name": customer.name,
            "phone": customer.phone,
        },
    }


@router.get("/me")
def get_me(customer: Customer = Depends(get_current_customer_dep)):
    return {
        "id": customer.id,
        "email": customer.email,
        "name": customer.name,
        "phone": customer.phone,
        "has_password": customer.password_hash is not None,
    }


@router.put("/me")
def update_me(body: UpdateNameRequest, customer: Customer = Depends(get_current_customer_dep), db: Session = Depends(get_db)):
    customer.name = body.name.strip()
    db.commit()
    return {"ok": True, "name": customer.name}


@router.get("/my-orders")
def get_my_orders(customer: Customer = Depends(get_current_customer_dep), db: Session = Depends(get_db)):
    from sqlalchemy.orm import joinedload
    orders = (
        db.query(Order)
        .filter(Order.customer_id == customer.id)
        .options(joinedload(Order.items).joinedload(OrderItem.dish))
        .order_by(Order.created_at.desc())
        .all()
    )
    return [
        {
            "id": o.id,
            "status": o.status,
            "total": o.total,
            "comment": o.comment,
            "address": o.address,
            "createdAt": o.created_at.isoformat() if o.created_at else None,
            "items": [
                {"name": it.dish.name if it.dish else "—", "qty": it.quantity, "price": it.price}
                for it in o.items
            ],
        }
        for o in orders
    ]


class RegisterRequest(BaseModel):
    email: str
    code: str
    name: str
    phone: str = ""
    password: str
    password_confirm: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        return normalize_email(v)


class LoginRequest(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        return normalize_email(v)


@router.post("/register")
def register(body: RegisterRequest, db: Session = Depends(get_db)):
    if body.password != body.password_confirm:
        raise HTTPException(status_code=400, detail="Пароли не совпадают")
    if len(body.password) < 4:
        raise HTTPException(status_code=400, detail="Пароль должен быть минимум 4 символа")
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="Укажите имя")

    # Verify email code
    email_code = (
        db.query(EmailCode)
        .filter(EmailCode.email == body.email, EmailCode.code == body.code, EmailCode.used == False)
        .order_by(EmailCode.created_at.desc())
        .first()
    )
    if not email_code:
        raise HTTPException(status_code=400, detail="Неверный код")
    age = (datetime.now(timezone.utc) - email_code.created_at.replace(tzinfo=timezone.utc)).total_seconds()
    if age > 300:
        raise HTTPException(status_code=400, detail="Код истёк, запросите новый")
    email_code.used = True

    # Find or create customer
    customer = db.query(Customer).filter(Customer.email == body.email).first()
    if not customer:
        customer = Customer(email=body.email)
        db.add(customer)
        db.flush()

    if customer.password_hash:
        raise HTTPException(status_code=400, detail="Аккаунт уже зарегистрирован, используйте вход")

    customer.password_hash = pbkdf2_sha256.hash(body.password)
    customer.name = body.name.strip()
    customer.phone = body.phone.strip() if body.phone else None
    db.commit()
    db.refresh(customer)

    token = create_token(customer.id, customer.email)
    return {
        "ok": True,
        "token": token,
        "customer": {"id": customer.id, "email": customer.email, "name": customer.name, "phone": customer.phone},
    }


@router.post("/login")
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)):
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit(f"login:{client_ip}", max_requests=10, window_seconds=300)
    customer = db.query(Customer).filter(Customer.email == body.email).first()
    if not customer or not customer.password_hash or not pbkdf2_sha256.verify(body.password, customer.password_hash):
        raise HTTPException(status_code=401, detail="Неверный email или пароль")

    token = create_token(customer.id, customer.email)
    return {
        "ok": True,
        "token": token,
        "customer": {"id": customer.id, "email": customer.email, "name": customer.name, "phone": customer.phone},
    }

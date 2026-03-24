import os
import random
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session
from jose import jwt
from passlib.hash import pbkdf2_sha256

from database import get_db
from models import Customer, SmsCode

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = logging.getLogger("onecp")

JWT_SECRET = os.getenv("JWT_SECRET", "onecp-secret-change-me")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days

SMS_PROVIDER = os.getenv("SMS_PROVIDER", "mock")  # mock | smsru
SMSRU_API_KEY = os.getenv("SMSRU_API_KEY", "")


def normalize_phone(phone: str) -> str:
    digits = "".join(c for c in phone if c.isdigit())
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    if not digits.startswith("7") or len(digits) != 11:
        raise ValueError("Введите корректный номер телефона (10 цифр после +7)")
    return "+" + digits


class SendCodeRequest(BaseModel):
    phone: str

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        return normalize_phone(v)


class VerifyCodeRequest(BaseModel):
    phone: str
    code: str

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        return normalize_phone(v)


class UpdateNameRequest(BaseModel):
    name: str


def create_token(customer_id: int, phone: str) -> str:
    exp = datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MINUTES)
    return jwt.encode({"sub": str(customer_id), "phone": phone, "exp": exp}, JWT_SECRET, algorithm=JWT_ALGORITHM)


def get_current_customer(db: Session = Depends(get_db), token: str = None) -> Customer:
    """Dependency — extracts customer from Authorization header."""
    pass  # overridden below


from fastapi import Request


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


def send_sms(phone: str, code: str):
    """Send SMS via configured provider. Raises Exception on failure."""
    if SMS_PROVIDER == "mock":
        logger.info(f"[MOCK SMS] Phone: {phone}, Code: {code}")
        return

    if SMS_PROVIDER == "smsru":
        import urllib.request
        import urllib.parse
        import urllib.error
        import json
        phone_digits = phone.lstrip("+")
        params = urllib.parse.urlencode({
            "api_id": SMSRU_API_KEY,
            "to": phone_digits,
            "msg": f"One Coffee Place: ваш код {code}",
            "json": 1,
        })
        try:
            resp = urllib.request.urlopen(f"https://sms.ru/sms/send?{params}", timeout=15)
            body = resp.read().decode("utf-8")
            logger.info(f"[SMS.RU] Phone: {phone_digits}, Response: {body}")
            data = json.loads(body)

            # Check overall request status
            if data.get("status") != "OK":
                error_msg = data.get("status_text", body)
                logger.error(f"[SMS.RU] Request failed: {error_msg}")
                raise Exception(f"SMS.RU error: {error_msg}")

            # Check per-phone delivery status
            sms_data = data.get("sms", {})
            phone_status = sms_data.get(phone_digits, {})
            if isinstance(phone_status, dict) and phone_status.get("status") == "ERROR":
                error_msg = phone_status.get("status_text", "Unknown error")
                status_code = phone_status.get("status_code", "")
                logger.error(f"[SMS.RU] Delivery failed for {phone_digits}: {error_msg} (code: {status_code})")
                raise Exception(f"SMS не доставлена: {error_msg}")

            logger.info(f"[SMS.RU] SMS sent OK to {phone_digits}")

        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"[SMS.RU] Bad response: {e}")
            raise Exception("Ошибка ответа от SMS провайдера")
        except urllib.error.URLError as e:
            logger.error(f"[SMS.RU] Network error: {e}")
            raise Exception("Не удалось подключиться к SMS провайдеру")


@router.post("/send-code")
def send_code(body: SendCodeRequest, db: Session = Depends(get_db)):
    # Rate limit: max 1 code per 60 seconds
    recent = (
        db.query(SmsCode)
        .filter(SmsCode.phone == body.phone, SmsCode.used == False)
        .order_by(SmsCode.created_at.desc())
        .first()
    )
    if recent and (datetime.now(timezone.utc) - recent.created_at.replace(tzinfo=timezone.utc)).total_seconds() < 60:
        raise HTTPException(status_code=429, detail="Подождите минуту перед повторной отправкой")

    code = f"{random.randint(1000, 9999)}"

    sms_code = SmsCode(phone=body.phone, code=code)
    db.add(sms_code)
    db.commit()

    try:
        send_sms(body.phone, code)
    except Exception as e:
        logger.error(f"[SMS] Failed to send to {body.phone}: {e}")
        raise HTTPException(status_code=502, detail=f"Не удалось отправить SMS: {e}")

    result = {"ok": True, "message": "Код отправлен"}
    if SMS_PROVIDER == "mock":
        result["debug_code"] = code
    return result


@router.post("/verify-code")
def verify_code(body: VerifyCodeRequest, db: Session = Depends(get_db)):
    sms_code = (
        db.query(SmsCode)
        .filter(SmsCode.phone == body.phone, SmsCode.code == body.code, SmsCode.used == False)
        .order_by(SmsCode.created_at.desc())
        .first()
    )

    if not sms_code:
        raise HTTPException(status_code=400, detail="Неверный код")

    # Code expires after 5 minutes
    age = (datetime.now(timezone.utc) - sms_code.created_at.replace(tzinfo=timezone.utc)).total_seconds()
    if age > 300:
        raise HTTPException(status_code=400, detail="Код истёк, запросите новый")

    sms_code.used = True

    # Find or create customer
    customer = db.query(Customer).filter(Customer.phone == body.phone).first()
    if not customer:
        customer = Customer(phone=body.phone)
        db.add(customer)

    db.commit()
    db.refresh(customer)

    token = create_token(customer.id, customer.phone)

    return {
        "ok": True,
        "token": token,
        "customer": {
            "id": customer.id,
            "phone": customer.phone,
            "name": customer.name,
        },
    }


@router.get("/me")
def get_me(customer: Customer = Depends(get_current_customer_dep)):
    return {
        "id": customer.id,
        "phone": customer.phone,
        "name": customer.name,
        "has_password": customer.password_hash is not None,
    }


@router.put("/me")
def update_me(body: UpdateNameRequest, customer: Customer = Depends(get_current_customer_dep), db: Session = Depends(get_db)):
    customer.name = body.name.strip()
    db.commit()
    return {"ok": True, "name": customer.name}


class RegisterRequest(BaseModel):
    phone: str
    code: str
    name: str
    password: str
    password_confirm: str

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        return normalize_phone(v)


class LoginRequest(BaseModel):
    phone: str
    password: str

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        return normalize_phone(v)


@router.post("/register")
def register(body: RegisterRequest, db: Session = Depends(get_db)):
    if body.password != body.password_confirm:
        raise HTTPException(status_code=400, detail="Пароли не совпадают")
    if len(body.password) < 4:
        raise HTTPException(status_code=400, detail="Пароль должен быть минимум 4 символа")
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="Укажите имя")

    # Verify SMS code
    sms_code = (
        db.query(SmsCode)
        .filter(SmsCode.phone == body.phone, SmsCode.code == body.code, SmsCode.used == False)
        .order_by(SmsCode.created_at.desc())
        .first()
    )
    if not sms_code:
        raise HTTPException(status_code=400, detail="Неверный код")
    age = (datetime.now(timezone.utc) - sms_code.created_at.replace(tzinfo=timezone.utc)).total_seconds()
    if age > 300:
        raise HTTPException(status_code=400, detail="Код истёк, запросите новый")
    sms_code.used = True

    # Find or create customer
    customer = db.query(Customer).filter(Customer.phone == body.phone).first()
    if not customer:
        customer = Customer(phone=body.phone)
        db.add(customer)
        db.flush()

    if customer.password_hash:
        raise HTTPException(status_code=400, detail="Аккаунт уже зарегистрирован, используйте вход")

    customer.password_hash = pbkdf2_sha256.hash(body.password)
    customer.name = body.name.strip()
    db.commit()
    db.refresh(customer)

    token = create_token(customer.id, customer.phone)
    return {
        "ok": True,
        "token": token,
        "customer": {"id": customer.id, "phone": customer.phone, "name": customer.name},
    }


@router.post("/login")
def login(body: LoginRequest, db: Session = Depends(get_db)):
    customer = db.query(Customer).filter(Customer.phone == body.phone).first()
    if not customer or not customer.password_hash:
        raise HTTPException(status_code=401, detail="Аккаунт не найден, зарегистрируйтесь")
    if not pbkdf2_sha256.verify(body.password, customer.password_hash):
        raise HTTPException(status_code=401, detail="Неверный пароль")

    token = create_token(customer.id, customer.phone)
    return {
        "ok": True,
        "token": token,
        "customer": {"id": customer.id, "phone": customer.phone, "name": customer.name},
    }

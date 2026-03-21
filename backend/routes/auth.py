import os
import random
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session
from jose import jwt

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
        raise ValueError("Invalid phone")
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
    if SMS_PROVIDER == "mock":
        logger.info(f"[MOCK SMS] Phone: {phone}, Code: {code}")
        return

    if SMS_PROVIDER == "smsru":
        import urllib.request
        import urllib.parse
        import json
        params = urllib.parse.urlencode({
            "api_id": SMSRU_API_KEY,
            "to": phone.lstrip("+"),
            "msg": f"OneCp: ваш код подтверждения {code}",
            "json": 1,
        })
        try:
            resp = urllib.request.urlopen(f"https://sms.ru/sms/send?{params}", timeout=10)
            body = resp.read().decode("utf-8")
            logger.info(f"[SMS.RU] Response: {body}")
            try:
                data = json.loads(body)
                if data.get("status") != "OK":
                    logger.error(f"[SMS.RU] Send failed: {data.get('status_text', body)}")
            except Exception:
                pass
        except Exception as e:
            logger.error(f"[SMS.RU] Request error: {e}")


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

    send_sms(body.phone, code)

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
    }


@router.put("/me")
def update_me(body: UpdateNameRequest, customer: Customer = Depends(get_current_customer_dep), db: Session = Depends(get_db)):
    customer.name = body.name.strip()
    db.commit()
    return {"ok": True, "name": customer.name}

import os
import uuid
import shutil
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from pydantic import BaseModel
from sqlalchemy.orm import Session
from jose import jwt
from passlib.hash import pbkdf2_sha256

UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/onecp/uploads")

from database import get_db
from models import AdminUser, Dish, DishSet, DishSetItem, Order, OrderItem, Customer, SiteSettings, Question
from sqlalchemy.orm import joinedload

router = APIRouter(prefix="/api/admin", tags=["admin"])

JWT_SECRET = os.getenv("JWT_SECRET", "onecp-secret-change-me")
JWT_ALGORITHM = "HS256"


def get_admin(request: Request, db: Session = Depends(get_db)) -> AdminUser:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = auth[7:]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        if payload.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Not admin")
        admin_id = int(payload["sub"])
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")
    admin = db.query(AdminUser).filter(AdminUser.id == admin_id).first()
    if not admin:
        raise HTTPException(status_code=401, detail="Admin not found")
    return admin


class AdminLoginRequest(BaseModel):
    username: str
    password: str


class DishIn(BaseModel):
    name: str
    description: str | None = None
    price: float
    emoji: str | None = None
    image_url: str | None = None
    category: str | None = None
    weight: float | None = None
    weight_unit: str | None = None
    proteins: float | None = None
    fats: float | None = None
    carbs: float | None = None
    available: bool = True
    sort_order: int = 0


class OrderStatusUpdate(BaseModel):
    status: str


class SettingIn(BaseModel):
    key: str
    value: str


# ===== AUTH =====

@router.post("/login")
def admin_login(body: AdminLoginRequest, db: Session = Depends(get_db)):
    admin = db.query(AdminUser).filter(AdminUser.username == body.username).first()
    if not admin or not pbkdf2_sha256.verify(body.password, admin.password_hash):
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")

    from datetime import datetime, timedelta, timezone
    exp = datetime.now(timezone.utc) + timedelta(hours=24)
    token = jwt.encode({"sub": str(admin.id), "role": "admin", "exp": exp}, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return {"ok": True, "token": token, "username": admin.username}


# ===== DISHES =====

@router.get("/dishes")
def list_dishes(admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    dishes = db.query(Dish).order_by(Dish.sort_order, Dish.id).all()
    return [
        {
            "id": d.id,
            "name": d.name,
            "description": d.description,
            "price": d.price,
            "emoji": d.emoji,
            "imageUrl": d.image_url,
            "category": d.category,
            "weight": d.weight,
            "weightUnit": d.weight_unit,
            "proteins": d.proteins,
            "fats": d.fats,
            "carbs": d.carbs,
            "available": d.available,
            "sortOrder": d.sort_order,
        }
        for d in dishes
    ]


@router.post("/dishes")
def create_dish(body: DishIn, admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    dish = Dish(
        name=body.name,
        description=body.description,
        price=body.price,
        old_price=None,
        emoji=body.emoji,
        image_url=body.image_url,
        category=body.category,
        weight=body.weight,
        weight_unit=body.weight_unit,
        proteins=body.proteins,
        fats=body.fats,
        carbs=body.carbs,
        available=body.available,
        sort_order=body.sort_order,
    )
    db.add(dish)
    db.commit()
    db.refresh(dish)
    return {"ok": True, "id": dish.id}


@router.put("/dishes/{dish_id}")
def update_dish(dish_id: int, body: DishIn, admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    dish = db.query(Dish).filter(Dish.id == dish_id).first()
    if not dish:
        raise HTTPException(status_code=404, detail="Блюдо не найдено")
    dish.name = body.name
    dish.description = body.description
    dish.price = body.price
    dish.category = body.category
    dish.weight = body.weight
    dish.weight_unit = body.weight_unit
    dish.proteins = body.proteins
    dish.fats = body.fats
    dish.carbs = body.carbs
    dish.available = body.available
    dish.sort_order = body.sort_order
    # image_url is managed by the upload/delete endpoints, not here
    db.commit()
    return {"ok": True}


@router.post("/dishes/{dish_id}/image")
def upload_dish_image(dish_id: int, file: UploadFile = File(...), admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    dish = db.query(Dish).filter(Dish.id == dish_id).first()
    if not dish:
        raise HTTPException(status_code=404, detail="Блюдо не найдено")

    # Validate file type
    allowed = {"image/jpeg", "image/png", "image/webp", "image/gif"}
    if file.content_type not in allowed:
        raise HTTPException(status_code=400, detail="Допустимые форматы: JPEG, PNG, WebP, GIF")

    # Create uploads dir
    os.makedirs(UPLOAD_DIR, exist_ok=True)

    # Generate unique filename
    ext = file.filename.rsplit(".", 1)[-1] if "." in file.filename else "jpg"
    filename = f"dish_{dish_id}_{uuid.uuid4().hex[:8]}.{ext}"
    filepath = os.path.join(UPLOAD_DIR, filename)

    # Delete old image if exists
    if dish.image_url:
        old_path = os.path.join(UPLOAD_DIR, os.path.basename(dish.image_url))
        if os.path.exists(old_path):
            os.remove(old_path)

    # Save file
    with open(filepath, "wb") as f:
        shutil.copyfileobj(file.file, f)

    dish.image_url = f"/uploads/{filename}"
    db.commit()
    return {"ok": True, "imageUrl": dish.image_url}


@router.delete("/dishes/{dish_id}/image")
def delete_dish_image(dish_id: int, admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    dish = db.query(Dish).filter(Dish.id == dish_id).first()
    if not dish:
        raise HTTPException(status_code=404, detail="Блюдо не найдено")
    if dish.image_url:
        old_path = os.path.join(UPLOAD_DIR, os.path.basename(dish.image_url))
        if os.path.exists(old_path):
            os.remove(old_path)
        dish.image_url = None
        db.commit()
    return {"ok": True}


@router.delete("/dishes/{dish_id}")
def delete_dish(dish_id: int, admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    dish = db.query(Dish).filter(Dish.id == dish_id).first()
    if not dish:
        raise HTTPException(status_code=404, detail="Блюдо не найдено")
    db.delete(dish)
    db.commit()
    return {"ok": True}


# ===== ORDERS =====

@router.get("/orders")
def list_orders(admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    orders = db.query(Order).options(joinedload(Order.items).joinedload(OrderItem.dish), joinedload(Order.customer)).order_by(Order.created_at.desc()).limit(200).all()
    status_labels = {
        "new": "Новый", "confirmed": "Подтверждён", "cooking": "Готовится",
        "ready": "Готов", "delivered": "Доставлен", "cancelled": "Отменён",
    }
    return [
        {
            "id": o.id,
            "customerPhone": o.customer.phone if o.customer else "—",
            "customerName": o.customer.name if o.customer else "—",
            "status": o.status,
            "statusLabel": status_labels.get(o.status, o.status),
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


@router.put("/orders/{order_id}/status")
def update_order_status(order_id: int, body: OrderStatusUpdate, admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    if body.status not in ("new", "confirmed", "cooking", "ready", "delivered", "cancelled"):
        raise HTTPException(status_code=400, detail="Неверный статус")
    order.status = body.status
    db.commit()
    return {"ok": True}


# ===== CUSTOMERS =====

@router.get("/customers")
def list_customers(admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    customers = db.query(Customer).order_by(Customer.created_at.desc()).limit(200).all()
    return [
        {"id": c.id, "phone": c.phone, "name": c.name, "createdAt": c.created_at.isoformat() if c.created_at else None}
        for c in customers
    ]


# ===== SETTINGS =====

@router.get("/settings")
def get_settings(admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    settings = db.query(SiteSettings).all()
    return {s.key: s.value for s in settings}


@router.put("/settings")
def update_settings(body: list[SettingIn], admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    for item in body:
        existing = db.query(SiteSettings).filter(SiteSettings.key == item.key).first()
        if existing:
            existing.value = item.value
        else:
            db.add(SiteSettings(key=item.key, value=item.value))
    db.commit()
    return {"ok": True}


@router.post("/test-report")
def test_report(admin: AdminUser = Depends(get_admin)):
    from email_report import send_report
    try:
        send_report(force=True)
        return {"ok": True, "message": "Отчёт отправлен"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ===== QUESTIONS =====

@router.get("/questions")
def list_questions(admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    questions = db.query(Question).order_by(Question.created_at.desc()).limit(200).all()
    return [
        {
            "id": q.id,
            "name": q.name,
            "email": q.email,
            "message": q.message,
            "answer": q.answer,
            "answeredAt": q.answered_at.isoformat() if q.answered_at else None,
            "createdAt": q.created_at.isoformat() if q.created_at else None,
        }
        for q in questions
    ]


class AnswerIn(BaseModel):
    answer: str


@router.put("/questions/{question_id}/answer")
def answer_question(question_id: int, body: AnswerIn, admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    q = db.query(Question).filter(Question.id == question_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="Вопрос не найден")

    q.answer = body.answer.strip()
    from datetime import datetime, timezone
    q.answered_at = datetime.now(timezone.utc)
    db.commit()

    # Send answer via email
    settings = {s.key: s.value for s in db.query(SiteSettings).filter(SiteSettings.key.in_(["replySmtpHost", "replySmtpPort", "replySmtpEmail", "replySmtpPassword"])).all()}
    smtp_host = settings.get("replySmtpHost", "").strip()
    smtp_pass = settings.get("replySmtpPassword", "").strip()
    smtp_email = settings.get("replySmtpEmail", "").strip()
    smtp_port = settings.get("replySmtpPort", "465").strip()

    if smtp_host and smtp_email and smtp_pass:
        # Handle host:port in host field
        if ":" in smtp_host:
            parts = smtp_host.split(":")
            smtp_host = parts[0]
            if not smtp_port or smtp_port == "465":
                smtp_port = parts[1]

        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(f"Здравствуйте, {q.name}!\n\nВы задавали вопрос:\n{q.message}\n\nОтвет:\n{q.answer}\n\n— One Coffee Place", "plain", "utf-8")
        msg["From"] = smtp_email
        msg["To"] = q.email
        msg["Subject"] = "Ответ на ваш вопрос — One Coffee Place"
        try:
            with smtplib.SMTP_SSL(smtp_host, int(smtp_port), timeout=15) as server:
                server.login(smtp_email, smtp_pass)
                server.sendmail(smtp_email, [q.email], msg.as_string())
            return {"ok": True, "message": "Ответ сохранён и отправлен на " + q.email}
        except Exception as e:
            return {"ok": True, "message": f"Ответ сохранён, но письмо не отправлено: {e}"}
    else:
        return {"ok": True, "message": "Ответ сохранён (почта для ответов не настроена)"}


@router.delete("/questions/{question_id}")
def delete_question(question_id: int, admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    q = db.query(Question).filter(Question.id == question_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="Вопрос не найден")
    db.delete(q)
    db.commit()
    return {"ok": True}


# ===== DISH SETS =====

class DishSetItemIn(BaseModel):
    dish_id: int
    quantity: int = 1


class DishSetIn(BaseModel):
    name: str
    description: str | None = None
    price: float
    items: list[DishSetItemIn] = []
    available: bool = True
    sort_order: int = 0


def format_dish_set(s: DishSet) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "description": s.description,
        "price": s.price,
        "imageUrl": s.image_url,
        "available": s.available,
        "sortOrder": s.sort_order,
        "items": [
            {
                "id": item.id,
                "dishId": item.dish_id,
                "dishName": item.dish.name if item.dish else "—",
                "quantity": item.quantity,
                "dishPrice": item.dish.price if item.dish else 0,
            }
            for item in s.items
        ],
    }


@router.get("/sets")
def list_sets(admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    sets = db.query(DishSet).options(joinedload(DishSet.items).joinedload(DishSetItem.dish)).order_by(DishSet.sort_order, DishSet.id).all()
    return [format_dish_set(s) for s in sets]


@router.post("/sets")
def create_set(body: DishSetIn, admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    ds = DishSet(
        name=body.name,
        description=body.description,
        price=body.price,
        available=body.available,
        sort_order=body.sort_order,
    )
    for item in body.items:
        ds.items.append(DishSetItem(dish_id=item.dish_id, quantity=item.quantity))
    db.add(ds)
    db.commit()
    db.refresh(ds)
    return {"ok": True, "id": ds.id}


@router.put("/sets/{set_id}")
def update_set(set_id: int, body: DishSetIn, admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    ds = db.query(DishSet).filter(DishSet.id == set_id).first()
    if not ds:
        raise HTTPException(status_code=404, detail="Набор не найден")
    ds.name = body.name
    ds.description = body.description
    ds.price = body.price
    ds.available = body.available
    ds.sort_order = body.sort_order
    # Replace items
    db.query(DishSetItem).filter(DishSetItem.set_id == set_id).delete()
    for item in body.items:
        db.add(DishSetItem(set_id=set_id, dish_id=item.dish_id, quantity=item.quantity))
    db.commit()
    return {"ok": True}


@router.post("/sets/{set_id}/image")
def upload_set_image(set_id: int, file: UploadFile = File(...), admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    ds = db.query(DishSet).filter(DishSet.id == set_id).first()
    if not ds:
        raise HTTPException(status_code=404, detail="Набор не найден")
    allowed = {"image/jpeg", "image/png", "image/webp", "image/gif"}
    if file.content_type not in allowed:
        raise HTTPException(status_code=400, detail="Допустимые форматы: JPEG, PNG, WebP, GIF")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    ext = file.filename.rsplit(".", 1)[-1] if "." in file.filename else "jpg"
    filename = f"set_{set_id}_{uuid.uuid4().hex[:8]}.{ext}"
    filepath = os.path.join(UPLOAD_DIR, filename)
    if ds.image_url:
        old_path = os.path.join(UPLOAD_DIR, os.path.basename(ds.image_url))
        if os.path.exists(old_path):
            os.remove(old_path)
    with open(filepath, "wb") as f:
        shutil.copyfileobj(file.file, f)
    ds.image_url = f"/uploads/{filename}"
    db.commit()
    return {"ok": True, "imageUrl": ds.image_url}


@router.delete("/sets/{set_id}/image")
def delete_set_image(set_id: int, admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    ds = db.query(DishSet).filter(DishSet.id == set_id).first()
    if not ds:
        raise HTTPException(status_code=404, detail="Набор не найден")
    if ds.image_url:
        old_path = os.path.join(UPLOAD_DIR, os.path.basename(ds.image_url))
        if os.path.exists(old_path):
            os.remove(old_path)
        ds.image_url = None
        db.commit()
    return {"ok": True}


@router.delete("/sets/{set_id}")
def delete_set(set_id: int, admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    ds = db.query(DishSet).filter(DishSet.id == set_id).first()
    if not ds:
        raise HTTPException(status_code=404, detail="Набор не найден")
    db.delete(ds)
    db.commit()
    return {"ok": True}

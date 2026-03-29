import os
import uuid
import shutil
from io import BytesIO
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy.sql import func
from jose import jwt
from passlib.hash import pbkdf2_sha256
from PIL import Image

UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/onecp/uploads")
MAX_IMAGE_SIZE = 800  # max width/height in pixels
WEBP_QUALITY = 80
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10MB

# Magic bytes for allowed image formats
IMAGE_SIGNATURES = [
    (b'\xff\xd8\xff', "image/jpeg"),
    (b'\x89PNG\r\n\x1a\n', "image/png"),
    (b'RIFF', "image/webp"),
    (b'GIF87a', "image/gif"),
    (b'GIF89a', "image/gif"),
]


def validate_image_upload(file_data: bytes):
    """Validate image by file size and magic bytes."""
    if len(file_data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Файл слишком большой (макс. 10 МБ)")
    if not any(file_data.startswith(sig) for sig, _ in IMAGE_SIGNATURES):
        raise HTTPException(status_code=400, detail="Файл повреждён или имеет неподдерживаемый формат")


def compress_image(file_data: bytes, max_size: int = MAX_IMAGE_SIZE, quality: int = WEBP_QUALITY) -> tuple[bytes, str]:
    """Compress image to WebP, resize if larger than max_size."""
    img = Image.open(BytesIO(file_data))
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGBA")
    else:
        img = img.convert("RGB")
    # Resize if too large
    w, h = img.size
    if w > max_size or h > max_size:
        img.thumbnail((max_size, max_size), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="WEBP", quality=quality)
    return buf.getvalue(), "webp"

from database import get_db
from models import AdminUser, Dish, DishSet, DishSetItem, Order, OrderItem, Customer, SiteSettings, Question, EmailCode, CalendarDay, CalendarDaySet, Location
from sqlalchemy.orm import joinedload

router = APIRouter(prefix="/api/admin", tags=["admin"])

from routes.auth import JWT_SECRET, JWT_ALGORITHM


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
    weight: str | None = None
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
def admin_login(body: AdminLoginRequest, request: Request, db: Session = Depends(get_db)):
    from routes.auth import check_rate_limit
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit(f"admin_login:{client_ip}", max_requests=5, window_seconds=300)
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

    # Read and validate
    raw = file.file.read()
    validate_image_upload(raw)
    compressed, ext = compress_image(raw)
    filename = f"dish_{dish_id}_{uuid.uuid4().hex[:8]}.{ext}"
    filepath = os.path.join(UPLOAD_DIR, filename)

    # Delete old image if exists
    if dish.image_url:
        old_path = os.path.join(UPLOAD_DIR, os.path.basename(dish.image_url))
        if os.path.exists(old_path):
            os.remove(old_path)

    with open(filepath, "wb") as f:
        f.write(compressed)

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
    used = db.query(OrderItem).filter(OrderItem.dish_id == dish_id).first()
    if used:
        raise HTTPException(status_code=400, detail="Нельзя удалить — блюдо есть в заказах. Переместите в архив.")
    try:
        db.delete(dish)
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=400, detail="Не удалось удалить блюдо — оно используется в других записях")
    return {"ok": True}


@router.put("/dishes/{dish_id}/archive")
def archive_dish(dish_id: int, admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    dish = db.query(Dish).filter(Dish.id == dish_id).first()
    if not dish:
        raise HTTPException(status_code=404, detail="Блюдо не найдено")
    dish.available = False
    # Remove from sets
    from models import DishSetItem
    db.query(DishSetItem).filter(DishSetItem.dish_id == dish_id).delete()
    db.commit()
    return {"ok": True}


@router.put("/dishes/{dish_id}/restore")
def restore_dish(dish_id: int, admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    dish = db.query(Dish).filter(Dish.id == dish_id).first()
    if not dish:
        raise HTTPException(status_code=404, detail="Блюдо не найдено")
    dish.available = True
    db.commit()
    return {"ok": True}


@router.delete("/dishes/{dish_id}/force")
def force_delete_dish(dish_id: int, admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    dish = db.query(Dish).filter(Dish.id == dish_id).first()
    if not dish:
        raise HTTPException(status_code=404, detail="Блюдо не найдено")
    db.query(DishSetItem).filter(DishSetItem.dish_id == dish_id).delete()
    db.query(OrderItem).filter(OrderItem.dish_id == dish_id).delete()
    db.delete(dish)
    db.commit()
    return {"ok": True}


# ===== ORDERS =====

@router.get("/orders")
def list_orders(admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    orders = db.query(Order).options(joinedload(Order.items).joinedload(OrderItem.dish), joinedload(Order.customer)).order_by(Order.created_at.desc()).limit(1000).all()
    status_labels = {
        "new": "Новый", "confirmed": "Подтверждён", "cooking": "Готовится",
        "ready": "Готов", "delivered": "Доставлен", "cancelled": "Отменён",
    }
    return [
        {
            "id": o.id,
            "customerEmail": o.customer.email if o.customer else "—",
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


@router.delete("/purge-all")
def purge_all(admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    db.query(OrderItem).delete()
    db.query(Order).delete()
    db.query(EmailCode).delete()
    db.query(Customer).delete()
    db.query(DishSetItem).delete()
    db.query(DishSet).delete()
    db.query(Dish).delete()
    db.query(Question).delete()
    db.commit()
    return {"ok": True}


# ===== CUSTOMERS =====

@router.get("/customers/{email}/orders")
def get_customer_orders(email: str, admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    customer = db.query(Customer).filter(Customer.email == email).first()
    if not customer:
        return []
    orders = (
        db.query(Order)
        .filter(Order.customer_id == customer.id)
        .options(joinedload(Order.items).joinedload(OrderItem.dish))
        .order_by(Order.created_at.desc())
        .all()
    )
    status_labels = {
        "new": "Новый", "confirmed": "Подтверждён", "cooking": "Готовится",
        "ready": "Готов", "delivered": "Доставлен", "cancelled": "Отменён",
    }
    return [
        {
            "id": o.id,
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


@router.get("/customers")
def list_customers(admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    customers = (
        db.query(Customer)
        .filter(Customer.password_hash.isnot(None))
        .order_by(Customer.created_at.desc())
        .all()
    )
    result = []
    for c in customers:
        order_count = db.query(Order).filter(Order.customer_id == c.id).count()
        total_spent = db.query(func.coalesce(func.sum(Order.total), 0)).filter(Order.customer_id == c.id).scalar()
        result.append({
            "id": c.id,
            "email": c.email,
            "name": c.name,
            "phone": c.phone,
            "createdAt": c.created_at.isoformat() if c.created_at else None,
            "orderCount": order_count,
            "totalSpent": float(total_spent),
        })
    return result


@router.delete("/customers-and-orders")
def clear_customers_and_orders(admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    db.query(OrderItem).delete()
    db.query(Order).delete()
    db.query(Customer).delete()
    db.query(EmailCode).delete()
    db.commit()
    return {"ok": True}


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
            "phone": q.phone,
            "message": q.message,
            "status": q.status or "new",
            "comment": q.comment,
            "createdAt": q.created_at.isoformat() if q.created_at else None,
        }
        for q in questions
    ]


class QuestionStatusIn(BaseModel):
    status: str | None = None
    comment: str | None = None


@router.put("/questions/{question_id}")
def update_question(question_id: int, body: QuestionStatusIn, admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    q = db.query(Question).filter(Question.id == question_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="Вопрос не найден")
    if body.status is not None:
        q.status = body.status
    if body.comment is not None:
        q.comment = body.comment.strip()
    db.commit()
    return {"ok": True}


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
    price: float = 0
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
                "dishCategory": item.dish.category if item.dish else None,
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
    # Only replace items if explicitly provided
    if body.items:
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
    raw = file.file.read()
    validate_image_upload(raw)
    compressed, ext = compress_image(raw)
    filename = f"set_{set_id}_{uuid.uuid4().hex[:8]}.{ext}"
    filepath = os.path.join(UPLOAD_DIR, filename)
    if ds.image_url:
        old_path = os.path.join(UPLOAD_DIR, os.path.basename(ds.image_url))
        if os.path.exists(old_path):
            os.remove(old_path)
    with open(filepath, "wb") as f:
        f.write(compressed)
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


class SetItemAddIn(BaseModel):
    dish_id: int
    quantity: int = 1


@router.post("/sets/{set_id}/items")
def add_set_item(set_id: int, body: SetItemAddIn, admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    ds = db.query(DishSet).filter(DishSet.id == set_id).first()
    if not ds:
        raise HTTPException(status_code=404, detail="Набор не найден")
    dish = db.query(Dish).filter(Dish.id == body.dish_id).first()
    if not dish:
        raise HTTPException(status_code=404, detail="Блюдо не найдено")
    existing = db.query(DishSetItem).filter(DishSetItem.set_id == set_id, DishSetItem.dish_id == body.dish_id).first()
    if existing:
        existing.quantity += body.quantity
    else:
        db.add(DishSetItem(set_id=set_id, dish_id=body.dish_id, quantity=body.quantity))
    db.commit()
    ds = db.query(DishSet).filter(DishSet.id == set_id).options(joinedload(DishSet.items).joinedload(DishSetItem.dish)).first()
    return format_dish_set(ds)


@router.delete("/sets/{set_id}/items/{item_id}")
def remove_set_item(set_id: int, item_id: int, admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    item = db.query(DishSetItem).filter(DishSetItem.id == item_id, DishSetItem.set_id == set_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Элемент не найден")
    db.delete(item)
    db.commit()
    ds = db.query(DishSet).filter(DishSet.id == set_id).options(joinedload(DishSet.items).joinedload(DishSetItem.dish)).first()
    return format_dish_set(ds)


@router.put("/sets/{set_id}/toggle")
def toggle_set(set_id: int, admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    ds = db.query(DishSet).filter(DishSet.id == set_id).first()
    if not ds:
        raise HTTPException(status_code=404, detail="Набор не найден")
    ds.available = not ds.available
    db.commit()
    return {"ok": True, "available": ds.available}


# ===== CALENDAR =====

from datetime import date, timedelta


def get_available_sets(db: Session):
    return db.query(DishSet).filter(DishSet.available == True).order_by(DishSet.sort_order, DishSet.id).all()


def generate_calendar_day(db: Session, target_date: date):
    """Generate a single calendar day based on sortOrder rotation logic.

    Looks at the previous day's set sortOrder, then picks the next available
    set with a higher sortOrder. If none found, wraps around to the lowest.
    """
    existing = db.query(CalendarDay).filter(CalendarDay.date == target_date).first()
    if existing:
        return existing

    available = get_available_sets(db)  # sorted by sort_order, id
    if not available:
        day = CalendarDay(date=target_date)
        db.add(day)
        db.flush()
        return day

    # Find previous day's set sortOrder
    prev_day = (
        db.query(CalendarDay)
        .filter(CalendarDay.date < target_date)
        .order_by(CalendarDay.date.desc())
        .first()
    )

    next_set = available[0]  # default: first by sortOrder
    if prev_day and prev_day.sets:
        prev_set_id = prev_day.sets[0].set_id
        prev_set = db.query(DishSet).filter(DishSet.id == prev_set_id).first()
        prev_order = prev_set.sort_order if prev_set else 0
        # Find next available set with sortOrder > prev_order
        candidates = [s for s in available if s.sort_order > prev_order]
        if candidates:
            next_set = candidates[0]
        else:
            next_set = available[0]  # wrap around to first

    day = CalendarDay(date=target_date)
    db.add(day)
    db.flush()
    db.add(CalendarDaySet(calendar_day_id=day.id, set_id=next_set.id))

    return day


def ensure_calendar_14_days(db: Session):
    """Generate calendar days for 14 days ahead if missing."""
    from datetime import datetime, timezone, timedelta as td
    vlad_tz = timezone(td(hours=10))
    today = datetime.now(vlad_tz).date()

    for i in range(14):
        target = today + timedelta(days=i)
        generate_calendar_day(db, target)
    db.commit()


@router.get("/calendar")
def get_calendar(admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    ensure_calendar_14_days(db)
    from datetime import datetime, timezone, timedelta as td
    vlad_tz = timezone(td(hours=10))
    today = datetime.now(vlad_tz).date()
    end = today + timedelta(days=14)

    days = (
        db.query(CalendarDay)
        .filter(CalendarDay.date >= today, CalendarDay.date < end)
        .options(joinedload(CalendarDay.sets).joinedload(CalendarDaySet.dish_set))
        .order_by(CalendarDay.date)
        .all()
    )

    available = get_available_sets(db)

    return {
        "days": [
            {
                "id": d.id,
                "date": d.date.isoformat(),
                "sets": [
                    {"id": cs.set_id, "name": cs.dish_set.name if cs.dish_set else "—", "price": cs.dish_set.price if cs.dish_set else 0, "sortOrder": cs.dish_set.sort_order if cs.dish_set else 0}
                    for cs in d.sets if cs.dish_set
                ],
            }
            for d in days
        ],
        "availableSets": [
            {"id": s.id, "name": s.name, "price": s.price, "sortOrder": s.sort_order}
            for s in available
        ],
    }


class CalendarDayUpdate(BaseModel):
    set_ids: list[int]


@router.put("/calendar/{day_date}")
def update_calendar_day(day_date: str, body: CalendarDayUpdate, admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    target = date.fromisoformat(day_date)
    day = db.query(CalendarDay).filter(CalendarDay.date == target).first()
    if not day:
        day = CalendarDay(date=target)
        db.add(day)
        db.flush()

    # Clear existing sets
    db.query(CalendarDaySet).filter(CalendarDaySet.calendar_day_id == day.id).delete()

    # Add new sets
    for sid in body.set_ids:
        db.add(CalendarDaySet(calendar_day_id=day.id, set_id=sid))

    db.commit()
    return {"ok": True}


@router.get("/calendar/dates")
def get_calendar_dates(admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    """Return all calendar day dates as a flat list."""
    days = db.query(CalendarDay.date).order_by(CalendarDay.date).all()
    return {"dates": [d.date.isoformat() for d in days]}


@router.post("/calendar/regenerate")
def regenerate_calendar(admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    """Delete all future days and regenerate."""
    from datetime import datetime, timezone, timedelta as td
    vlad_tz = timezone(td(hours=10))
    today = datetime.now(vlad_tz).date()

    future_days = db.query(CalendarDay).filter(CalendarDay.date >= today).all()
    for d in future_days:
        db.query(CalendarDaySet).filter(CalendarDaySet.calendar_day_id == d.id).delete()
        db.delete(d)
    db.commit()

    ensure_calendar_14_days(db)
    return {"ok": True}


# ===== LOCATIONS =====

class LocationIn(BaseModel):
    name: str
    address: str
    slug: str
    description: str | None = None
    report_email: str | None = None
    sort_order: int = 0
    active: bool = True


@router.get("/locations")
def list_locations(admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    locs = db.query(Location).order_by(Location.sort_order, Location.id).all()
    return [
        {
            "id": loc.id,
            "name": loc.name,
            "address": loc.address,
            "slug": loc.slug,
            "description": loc.description,
            "reportEmail": loc.report_email,
            "imageUrl": loc.image_url,
            "bottomImageUrl": loc.bottom_image_url,
            "sortOrder": loc.sort_order,
            "active": loc.active,
        }
        for loc in locs
    ]


class LocationReorder(BaseModel):
    ids: list[int]


@router.put("/locations/reorder")
def reorder_locations(body: LocationReorder, admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    for i, loc_id in enumerate(body.ids):
        loc = db.query(Location).filter(Location.id == loc_id).first()
        if loc:
            loc.sort_order = i
    db.commit()
    return {"ok": True}


@router.post("/locations")
def create_location(body: LocationIn, admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    import re
    slug = body.slug.strip().lstrip("/")
    if not slug or not re.match(r'^[a-zA-Z0-9_-]+$', slug):
        raise HTTPException(status_code=400, detail="Slug может содержать только латинские буквы, цифры, дефис и подчёркивание")
    existing = db.query(Location).filter(Location.slug == slug).first()
    if existing:
        raise HTTPException(status_code=400, detail="Точка с таким slug уже существует")
    loc = Location(
        name=body.name.strip(),
        address=body.address.strip(),
        slug=slug,
        description=(body.description or "").strip() or None,
        report_email=(body.report_email or "").strip() or None,
        sort_order=body.sort_order,
        active=body.active,
    )
    db.add(loc)
    db.commit()
    db.refresh(loc)
    return {"ok": True, "id": loc.id}


@router.put("/locations/{loc_id}")
def update_location(loc_id: int, body: LocationIn, admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    import re
    loc = db.query(Location).filter(Location.id == loc_id).first()
    if not loc:
        raise HTTPException(status_code=404, detail="Точка не найдена")
    slug = body.slug.strip().lstrip("/")
    if not slug or not re.match(r'^[a-zA-Z0-9_-]+$', slug):
        raise HTTPException(status_code=400, detail="Slug может содержать только латинские буквы, цифры, дефис и подчёркивание")
    dup = db.query(Location).filter(Location.slug == slug, Location.id != loc_id).first()
    if dup:
        raise HTTPException(status_code=400, detail="Точка с таким slug уже существует")
    loc.name = body.name.strip()
    loc.address = body.address.strip()
    loc.slug = slug
    loc.description = (body.description or "").strip() or None
    loc.report_email = (body.report_email or "").strip() or None
    loc.sort_order = body.sort_order
    loc.active = body.active
    db.commit()
    return {"ok": True}


@router.put("/locations/{loc_id}/archive")
def archive_location(loc_id: int, admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    loc = db.query(Location).filter(Location.id == loc_id).first()
    if not loc:
        raise HTTPException(status_code=404, detail="Точка не найдена")
    loc.active = False
    db.commit()
    return {"ok": True}


@router.put("/locations/{loc_id}/restore")
def restore_location(loc_id: int, admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    loc = db.query(Location).filter(Location.id == loc_id).first()
    if not loc:
        raise HTTPException(status_code=404, detail="Точка не найдена")
    loc.active = True
    db.commit()
    return {"ok": True}


@router.post("/locations/{loc_id}/image")
def upload_location_image(loc_id: int, file: UploadFile = File(...), admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    loc = db.query(Location).filter(Location.id == loc_id).first()
    if not loc:
        raise HTTPException(status_code=404, detail="Точка не найдена")
    allowed = {"image/jpeg", "image/png", "image/webp", "image/gif"}
    if file.content_type not in allowed:
        raise HTTPException(status_code=400, detail="Допустимые форматы: JPEG, PNG, WebP, GIF")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    raw = file.file.read()
    validate_image_upload(raw)
    compressed, ext = compress_image(raw, max_size=2560, quality=90)
    filename = f"loc_{loc_id}_{uuid.uuid4().hex[:8]}.{ext}"
    filepath = os.path.join(UPLOAD_DIR, filename)
    if loc.image_url:
        old_path = os.path.join(UPLOAD_DIR, os.path.basename(loc.image_url))
        if os.path.exists(old_path):
            os.remove(old_path)
    with open(filepath, "wb") as f:
        f.write(compressed)
    loc.image_url = f"/uploads/{filename}"
    db.commit()
    return {"ok": True, "imageUrl": loc.image_url}


@router.delete("/locations/{loc_id}/image")
def delete_location_image(loc_id: int, admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    loc = db.query(Location).filter(Location.id == loc_id).first()
    if not loc:
        raise HTTPException(status_code=404, detail="Точка не найдена")
    if loc.image_url:
        old_path = os.path.join(UPLOAD_DIR, os.path.basename(loc.image_url))
        if os.path.exists(old_path):
            os.remove(old_path)
        loc.image_url = None
        db.commit()
    return {"ok": True}


@router.post("/locations/{loc_id}/bottom-image")
def upload_location_bottom_image(loc_id: int, file: UploadFile = File(...), admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    loc = db.query(Location).filter(Location.id == loc_id).first()
    if not loc:
        raise HTTPException(status_code=404, detail="Точка не найдена")
    allowed = {"image/jpeg", "image/png", "image/webp", "image/gif"}
    if file.content_type not in allowed:
        raise HTTPException(status_code=400, detail="Допустимые форматы: JPEG, PNG, WebP, GIF")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    raw = file.file.read()
    validate_image_upload(raw)
    compressed, ext = compress_image(raw, max_size=2560, quality=90)
    filename = f"loc_bottom_{loc_id}_{uuid.uuid4().hex[:8]}.{ext}"
    filepath = os.path.join(UPLOAD_DIR, filename)
    if loc.bottom_image_url:
        old_path = os.path.join(UPLOAD_DIR, os.path.basename(loc.bottom_image_url))
        if os.path.exists(old_path):
            os.remove(old_path)
    with open(filepath, "wb") as f:
        f.write(compressed)
    loc.bottom_image_url = f"/uploads/{filename}"
    db.commit()
    return {"ok": True, "imageUrl": loc.bottom_image_url}


@router.delete("/locations/{loc_id}/bottom-image")
def delete_location_bottom_image(loc_id: int, admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    loc = db.query(Location).filter(Location.id == loc_id).first()
    if not loc:
        raise HTTPException(status_code=404, detail="Точка не найдена")
    if loc.bottom_image_url:
        old_path = os.path.join(UPLOAD_DIR, os.path.basename(loc.bottom_image_url))
        if os.path.exists(old_path):
            os.remove(old_path)
        loc.bottom_image_url = None
        db.commit()
    return {"ok": True}


# ===== EXCEL REPORTS =====

@router.get("/export/guests")
def export_guests_excel(date: str, location: str = "", admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    from datetime import datetime, date as date_type, timedelta, timezone
    from email_report import build_guests_excel, build_guests_excel_multi, get_delivery_date, get_locations_map
    from urllib.parse import quote
    try:
        target_date = date_type.fromisoformat(date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Неверная дата")

    cutoff = datetime.combine(target_date - timedelta(days=3), datetime.min.time()).replace(tzinfo=timezone.utc)
    orders = (
        db.query(Order)
        .options(joinedload(Order.items).joinedload(OrderItem.dish), joinedload(Order.customer))
        .filter(Order.created_at >= cutoff)
        .all()
    )
    filtered = [o for o in orders if o.created_at and get_delivery_date(o.created_at) == target_date]

    if location:
        data = build_guests_excel(filtered, location, target_date)
        if not data:
            raise HTTPException(status_code=404, detail="Нет заказов")
        filename = f"Отчет по гостям на {target_date.strftime('%d.%m.%Y')} ({location}).xlsx"
    else:
        locations_map = get_locations_map(db)
        data = build_guests_excel_multi(filtered, list(locations_map.keys()), target_date)
        if not data:
            raise HTTPException(status_code=404, detail="Нет заказов")
        filename = f"Отчет по гостям на {target_date.strftime('%d.%m.%Y')} (все точки).xlsx"

    encoded_filename = quote(filename)
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"},
    )


@router.get("/export/kitchen")
def export_kitchen_excel(date: str, admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    from datetime import datetime, date as date_type, timedelta, timezone
    from email_report import build_kitchen_excel, get_delivery_date, get_locations_map
    from urllib.parse import quote
    try:
        target_date = date_type.fromisoformat(date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Неверная дата")

    cutoff = datetime.combine(target_date - timedelta(days=3), datetime.min.time()).replace(tzinfo=timezone.utc)
    orders = (
        db.query(Order)
        .options(joinedload(Order.items).joinedload(OrderItem.dish), joinedload(Order.customer))
        .filter(Order.created_at >= cutoff)
        .all()
    )
    filtered = [o for o in orders if o.created_at and get_delivery_date(o.created_at) == target_date]
    if not filtered:
        raise HTTPException(status_code=404, detail="Нет заказов")

    locations_map = get_locations_map(db)
    data = build_kitchen_excel(filtered, target_date, locations_map)
    if not data:
        raise HTTPException(status_code=404, detail="Нет заказов")

    filename = f"Отчет для кухни на {target_date.strftime('%d.%m.%Y')} (все точки).xlsx"
    encoded_filename = quote(filename)
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"},
    )


@router.post("/notify/{location_id}")
def send_pickup_notify(location_id: int, admin: AdminUser = Depends(get_admin), db: Session = Depends(get_db)):
    """Send pickup-ready notification to customers with orders at given location for today."""
    from email_report import send_pickup_notification
    try:
        sent = send_pickup_notification(location_id)
        return {"ok": True, "sent": sent, "message": f"Уведомление отправлено {sent} клиентам"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

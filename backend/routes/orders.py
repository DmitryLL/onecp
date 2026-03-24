from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from database import get_db
from models import Customer, Dish, Order, OrderItem, SmsCode
from routes.auth import normalize_phone, get_current_customer_dep

router = APIRouter(prefix="/api/orders", tags=["orders"])


class OrderItemIn(BaseModel):
    dish_id: int
    quantity: int = 1


class CreateOrderRequest(BaseModel):
    phone: str
    sms_code: str
    items: list[OrderItemIn]
    comment: str | None = Field(None, max_length=500)
    address: str | None = Field(None, max_length=200)
    customer_name: str

    @field_validator("customer_name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        import re
        v = v.strip()
        if not v:
            raise ValueError("Укажите имя")
        if len(v) > 30:
            raise ValueError("Имя не более 30 символов")
        if not re.match(r'^[a-zA-Zа-яА-ЯёЁ \-]+$', v):
            raise ValueError("Имя может содержать только буквы, пробел и дефис")
        return v

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        return normalize_phone(v)


@router.post("/")
def create_order(
    body: CreateOrderRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    from routes.auth import check_rate_limit
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit(f"order:{client_ip}", max_requests=10, window_seconds=300)
    if not body.items:
        raise HTTPException(status_code=400, detail="Корзина пуста")

    # Verify SMS code
    sms_code = (
        db.query(SmsCode)
        .filter(SmsCode.phone == body.phone, SmsCode.code == body.sms_code, SmsCode.used == False)
        .order_by(SmsCode.created_at.desc())
        .first()
    )
    if not sms_code:
        raise HTTPException(status_code=400, detail="Неверный код подтверждения")
    age = (datetime.now(timezone.utc) - sms_code.created_at.replace(tzinfo=timezone.utc)).total_seconds()
    if age > 300:
        raise HTTPException(status_code=400, detail="Код истёк, запросите новый")
    sms_code.used = True

    # Find or create customer by phone
    customer = db.query(Customer).filter(Customer.phone == body.phone).first()
    if not customer:
        customer = Customer(phone=body.phone)
        db.add(customer)
        db.flush()
    if body.customer_name and body.customer_name.strip():
        customer.name = body.customer_name.strip()

    dish_ids = [item.dish_id for item in body.items]
    dishes = db.query(Dish).filter(Dish.id.in_(dish_ids), Dish.available == True).all()
    dish_map = {d.id: d for d in dishes}

    order_items = []
    total = 0.0
    for item in body.items:
        dish = dish_map.get(item.dish_id)
        if not dish:
            raise HTTPException(status_code=400, detail=f"Блюдо #{item.dish_id} не найдено")
        if item.quantity < 1:
            raise HTTPException(status_code=400, detail="Количество должно быть >= 1")
        line_total = dish.price * item.quantity
        total += line_total
        order_items.append(OrderItem(dish_id=dish.id, quantity=item.quantity, price=dish.price))

    order = Order(
        customer_id=customer.id,
        total=total,
        comment=body.comment,
        address=body.address,
    )
    order.items = order_items
    db.add(order)
    db.commit()
    db.refresh(order)

    return {
        "ok": True,
        "order": format_order(order),
    }



class CreateOrderAuthRequest(BaseModel):
    items: list[OrderItemIn]
    comment: str | None = Field(None, max_length=500)
    address: str | None = Field(None, max_length=200)


@router.post("/auth")
def create_order_authenticated(
    body: CreateOrderAuthRequest,
    customer: Customer = Depends(get_current_customer_dep),
    db: Session = Depends(get_db),
):
    if not body.items:
        raise HTTPException(status_code=400, detail="Корзина пуста")

    dish_ids = [item.dish_id for item in body.items]
    dishes = db.query(Dish).filter(Dish.id.in_(dish_ids), Dish.available == True).all()
    dish_map = {d.id: d for d in dishes}

    order_items = []
    total = 0.0
    for item in body.items:
        dish = dish_map.get(item.dish_id)
        if not dish:
            raise HTTPException(status_code=400, detail=f"Блюдо #{item.dish_id} не найдено")
        if item.quantity < 1:
            raise HTTPException(status_code=400, detail="Количество должно быть >= 1")
        line_total = dish.price * item.quantity
        total += line_total
        order_items.append(OrderItem(dish_id=dish.id, quantity=item.quantity, price=dish.price))

    order = Order(
        customer_id=customer.id,
        total=total,
        comment=body.comment,
        address=body.address,
    )
    order.items = order_items
    db.add(order)
    db.commit()
    db.refresh(order)

    return {
        "ok": True,
        "order": format_order(order),
    }


def format_order(order: Order) -> dict:
    status_labels = {
        "new": "Новый",
        "confirmed": "Подтверждён",
        "cooking": "Готовится",
        "ready": "Готов",
        "delivered": "Доставлен",
        "cancelled": "Отменён",
    }
    return {
        "id": order.id,
        "status": order.status,
        "statusLabel": status_labels.get(order.status, order.status),
        "total": order.total,
        "comment": order.comment,
        "address": order.address,
        "createdAt": order.created_at.isoformat() if order.created_at else None,
        "items": [
            {
                "dishId": item.dish_id,
                "name": item.dish.name if item.dish else "—",
                "emoji": item.dish.emoji if item.dish else "",
                "quantity": item.quantity,
                "price": item.price,
            }
            for item in order.items
        ],
    }

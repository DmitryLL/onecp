from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from models import Customer, Dish, Order, OrderItem
from routes.auth import get_current_customer_dep

router = APIRouter(prefix="/api/orders", tags=["orders"])


class OrderItemIn(BaseModel):
    dish_id: int
    quantity: int = 1


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

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from database import get_db
from models import Dish

router = APIRouter(prefix="/api/menu", tags=["menu"])


@router.get("/")
def get_menu(db: Session = Depends(get_db)):
    dishes = (
        db.query(Dish)
        .filter(Dish.available == True)
        .order_by(Dish.sort_order, Dish.id)
        .all()
    )
    return [
        {
            "id": d.id,
            "name": d.name,
            "description": d.description,
            "price": d.price,
            "oldPrice": d.old_price,
            "emoji": d.emoji,
            "imageUrl": d.image_url,
            "category": d.category,
        }
        for d in dishes
    ]


@router.get("/categories")
def get_categories(db: Session = Depends(get_db)):
    cats = (
        db.query(Dish.category)
        .filter(Dish.available == True, Dish.category != None)
        .distinct()
        .all()
    )
    return [c[0] for c in cats if c[0]]

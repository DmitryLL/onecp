from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import Dish, SiteSettings, Question

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
            "emoji": d.emoji,
            "imageUrl": d.image_url,
            "category": d.category,
            "weight": d.weight,
            "weightUnit": d.weight_unit,
            "proteins": d.proteins,
            "fats": d.fats,
            "carbs": d.carbs,
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


@router.get("/settings")
def get_site_settings(db: Session = Depends(get_db)):
    settings = db.query(SiteSettings).all()
    return {s.key: s.value for s in settings}


class QuestionIn(BaseModel):
    name: str
    email: str
    message: str


@router.post("/question")
def submit_question(body: QuestionIn, db: Session = Depends(get_db)):
    if not body.name.strip() or not body.email.strip() or not body.message.strip():
        raise HTTPException(status_code=400, detail="Заполните все поля")
    q = Question(name=body.name.strip(), email=body.email.strip(), message=body.message.strip())
    db.add(q)
    db.commit()
    return {"ok": True}

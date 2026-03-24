from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import Dish, DishSet, DishSetItem, SiteSettings, Question, CalendarDay, CalendarDaySet
from sqlalchemy.orm import joinedload

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


@router.get("/sets")
def get_sets(db: Session = Depends(get_db)):
    from datetime import datetime, timezone, timedelta
    vlad_tz = timezone(timedelta(hours=10))
    today = datetime.now(vlad_tz).date()

    # Get today's calendar day
    cal_day = (
        db.query(CalendarDay)
        .filter(CalendarDay.date == today)
        .options(joinedload(CalendarDay.sets))
        .first()
    )

    if cal_day and cal_day.sets:
        # Show only sets assigned to today
        set_ids = [cs.set_id for cs in cal_day.sets]
        sets = (
            db.query(DishSet)
            .filter(DishSet.id.in_(set_ids), DishSet.available == True)
            .options(joinedload(DishSet.items).joinedload(DishSetItem.dish))
            .order_by(DishSet.sort_order, DishSet.id)
            .all()
        )
    else:
        # Fallback: show all available sets
        sets = (
            db.query(DishSet)
            .filter(DishSet.available == True)
            .options(joinedload(DishSet.items).joinedload(DishSetItem.dish))
            .order_by(DishSet.sort_order, DishSet.id)
            .all()
        )

    # Return dishes from today's set(s) as flat list
    dishes_seen = set()
    result = []
    for s in sets:
        for item in s.items:
            d = item.dish
            if not d or d.id in dishes_seen:
                continue
            dishes_seen.add(d.id)
            result.append({
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
            })
    return result


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
    phone: str
    message: str


@router.post("/question")
def submit_question(body: QuestionIn, db: Session = Depends(get_db)):
    if not body.name.strip() or not body.phone.strip() or not body.message.strip():
        raise HTTPException(status_code=400, detail="Заполните все поля")
    q = Question(name=body.name.strip(), phone=body.phone.strip(), message=body.message.strip())
    db.add(q)
    db.commit()
    return {"ok": True}

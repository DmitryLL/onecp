from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Date, ForeignKey, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database import Base


class AdminUser(Base):
    __tablename__ = "admin_users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Customer(Base):
    __tablename__ = "customers"

    id = Column(Integer, primary_key=True, index=True)
    phone = Column(String(20), unique=True, nullable=False, index=True)
    name = Column(String(100), nullable=True)
    password_hash = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    orders = relationship("Order", back_populates="customer")


class SmsCode(Base):
    __tablename__ = "sms_codes"

    id = Column(Integer, primary_key=True, index=True)
    phone = Column(String(20), nullable=False, index=True)
    code = Column(String(6), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    used = Column(Boolean, default=False)


class Dish(Base):
    __tablename__ = "dishes"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    price = Column(Float, nullable=False)
    old_price = Column(Float, nullable=True)  # deprecated, not used
    emoji = Column(String(10), nullable=True)
    image_url = Column(String(500), nullable=True)
    category = Column(String(100), nullable=True, index=True)
    weight = Column(String(50), nullable=True)
    weight_unit = Column(String(10), nullable=True)  # г, мл, л, кг
    proteins = Column(Float, nullable=True)
    fats = Column(Float, nullable=True)
    carbs = Column(Float, nullable=True)
    available = Column(Boolean, default=True)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    order_items = relationship("OrderItem", back_populates="dish")


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    status = Column(String(30), default="new")  # new, confirmed, cooking, ready, delivered, cancelled
    total = Column(Float, nullable=False, default=0)
    comment = Column(Text, nullable=True)
    address = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    customer = relationship("Customer", back_populates="orders")
    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")


class OrderItem(Base):
    __tablename__ = "order_items"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    dish_id = Column(Integer, ForeignKey("dishes.id"), nullable=False)
    quantity = Column(Integer, nullable=False, default=1)
    price = Column(Float, nullable=False)

    order = relationship("Order", back_populates="items")
    dish = relationship("Dish", back_populates="order_items")


class SiteSettings(Base):
    __tablename__ = "site_settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(100), unique=True, nullable=False)
    value = Column(Text, nullable=True)


class DishSet(Base):
    __tablename__ = "dish_sets"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    price = Column(Float, nullable=False)
    image_url = Column(String(500), nullable=True)
    available = Column(Boolean, default=True)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    items = relationship("DishSetItem", back_populates="dish_set", cascade="all, delete-orphan")


class DishSetItem(Base):
    __tablename__ = "dish_set_items"

    id = Column(Integer, primary_key=True, index=True)
    set_id = Column(Integer, ForeignKey("dish_sets.id"), nullable=False)
    dish_id = Column(Integer, ForeignKey("dishes.id"), nullable=False)
    quantity = Column(Integer, nullable=False, default=1)

    dish_set = relationship("DishSet", back_populates="items")
    dish = relationship("Dish")


class CalendarDay(Base):
    __tablename__ = "calendar_days"

    id = Column(Integer, primary_key=True, index=True)
    date = Column(Date, unique=True, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    sets = relationship("CalendarDaySet", back_populates="calendar_day", cascade="all, delete-orphan")


class CalendarDaySet(Base):
    __tablename__ = "calendar_day_sets"

    id = Column(Integer, primary_key=True, index=True)
    calendar_day_id = Column(Integer, ForeignKey("calendar_days.id"), nullable=False)
    set_id = Column(Integer, ForeignKey("dish_sets.id"), nullable=False)

    calendar_day = relationship("CalendarDay", back_populates="sets")
    dish_set = relationship("DishSet")


class Question(Base):
    __tablename__ = "questions"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    phone = Column(String(20), nullable=False)
    message = Column(Text, nullable=False)
    status = Column(String(30), default="new")  # new, processed
    comment = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

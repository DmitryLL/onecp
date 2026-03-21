import io
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders
from datetime import datetime, timezone, timedelta

from openpyxl import Workbook
from sqlalchemy.orm import joinedload

from database import SessionLocal
from models import Order, OrderItem, SiteSettings

logger = logging.getLogger("onecp")

# SMTP settings for popular Russian providers
SMTP_SERVERS = {
    "mail.ru": ("smtp.mail.ru", 465),
    "bk.ru": ("smtp.mail.ru", 465),
    "inbox.ru": ("smtp.mail.ru", 465),
    "list.ru": ("smtp.mail.ru", 465),
    "yandex.ru": ("smtp.yandex.ru", 465),
    "ya.ru": ("smtp.yandex.ru", 465),
    "gmail.com": ("smtp.gmail.com", 465),
}

STATUS_LABELS = {
    "new": "Новый",
    "confirmed": "Подтверждён",
    "cooking": "Готовится",
    "ready": "Готов",
    "delivered": "Доставлен",
    "cancelled": "Отменён",
}


def detect_smtp(email: str) -> tuple[str, int]:
    domain = email.split("@")[-1].lower()
    if domain in SMTP_SERVERS:
        return SMTP_SERVERS[domain]
    return (f"smtp.{domain}", 465)


def get_report_settings() -> dict | None:
    db = SessionLocal()
    try:
        settings = db.query(SiteSettings).filter(
            SiteSettings.key.in_([
                "reportEnabled", "reportSmtpEmail", "reportSmtpPassword",
                "reportRecipient", "reportTime",
            ])
        ).all()
        s = {row.key: row.value for row in settings}
        if s.get("reportEnabled") != "1":
            return None
        if not s.get("reportSmtpEmail") or not s.get("reportSmtpPassword") or not s.get("reportRecipient"):
            return None
        return s
    finally:
        db.close()


def build_orders_excel(date_str: str) -> bytes | None:
    db = SessionLocal()
    try:
        orders = (
            db.query(Order)
            .options(joinedload(Order.items).joinedload(OrderItem.dish), joinedload(Order.customer))
            .filter(Order.created_at >= f"{date_str}T00:00:00", Order.created_at < f"{date_str}T23:59:59.999999")
            .order_by(Order.created_at)
            .all()
        )
        if not orders:
            return None

        wb = Workbook()
        ws = wb.active
        ws.title = "Заказы"
        headers = ["# Заказа", "Дата", "Клиент", "Телефон", "Товар", "Кол-во", "Цена", "Сумма заказа", "Статус", "Комментарий"]
        ws.append(headers)

        for col in range(1, len(headers) + 1):
            ws.cell(row=1, column=col).font = ws.cell(row=1, column=col).font.copy(bold=True)

        for o in orders:
            phone = o.customer.phone if o.customer else "—"
            name = o.customer.name if o.customer and o.customer.name else "—"
            for it in o.items:
                dish_name = it.dish.name if it.dish else "—"
                ws.append([
                    o.id,
                    o.created_at.strftime("%d.%m.%Y %H:%M") if o.created_at else "",
                    name,
                    phone,
                    dish_name,
                    it.quantity,
                    it.price,
                    o.total,
                    STATUS_LABELS.get(o.status, o.status),
                    o.comment or "",
                ])

        # Auto-width
        for col in ws.columns:
            max_len = 0
            col_letter = col[0].column_letter
            for cell in col:
                val = str(cell.value) if cell.value else ""
                max_len = max(max_len, len(val))
            ws.column_dimensions[col_letter].width = min(max_len + 3, 40)

        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()
    finally:
        db.close()


def send_report():
    logger.info("[EMAIL REPORT] Checking if report should be sent...")
    settings = get_report_settings()
    if not settings:
        logger.info("[EMAIL REPORT] Disabled or not configured")
        return

    today = datetime.now(timezone(timedelta(hours=10))).strftime("%Y-%m-%d")  # Vladivostok
    excel_data = build_orders_excel(today)

    smtp_email = settings["reportSmtpEmail"]
    smtp_pass = settings["reportSmtpPassword"]
    recipient = settings["reportRecipient"]
    smtp_host, smtp_port = detect_smtp(smtp_email)

    msg = MIMEMultipart()
    msg["From"] = smtp_email
    msg["To"] = recipient
    msg["Subject"] = f"Заказы за {today}"

    if excel_data:
        msg.attach(MIMEText(f"Отчёт по заказам за {today} во вложении.", "plain", "utf-8"))
        part = MIMEBase("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        part.set_payload(excel_data)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f"attachment; filename=orders_{today}.xlsx")
        msg.attach(part)
    else:
        msg.attach(MIMEText(f"За {today} заказов не было.", "plain", "utf-8"))

    try:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15) as server:
            server.login(smtp_email, smtp_pass)
            server.sendmail(smtp_email, [recipient], msg.as_string())
        logger.info(f"[EMAIL REPORT] Sent to {recipient}")
    except Exception as e:
        logger.error(f"[EMAIL REPORT] Failed: {e}")

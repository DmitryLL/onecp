import io
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders
from datetime import datetime, timezone, timedelta, date as date_type

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from sqlalchemy.orm import joinedload

from database import SessionLocal
from models import Order, OrderItem, SiteSettings, Location

logger = logging.getLogger("onecp")

VLAD_TZ = timezone(timedelta(hours=10))

def get_locations_map(db=None) -> dict[str, str | None]:
    """Returns {address: report_email} from DB. Falls back to empty if no DB."""
    close = False
    if db is None:
        db = SessionLocal()
        close = True
    try:
        locs = db.query(Location).filter(Location.active == True).order_by(Location.sort_order, Location.id).all()
        return {loc.address: loc.report_email for loc in locs}
    finally:
        if close:
            db.close()

MONTHS_RU = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
             'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря']
DAYS_RU = ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье']


def format_date_ru(d):
    return f"{d.day} {MONTHS_RU[d.month - 1]} {d.year}, {DAYS_RU[d.weekday()]}"


# ===== Styles =====
TITLE_FONT = Font(bold=True, size=16, color="3A2A1A")
SUB_FONT = Font(size=11, color="888888")
STATS_FONT = Font(bold=True, size=12, color="2E7D32")
LOC_FONT = Font(bold=True, size=11, color="4A3728")
HEADER_FONT = Font(bold=True, size=11, color="FFFFFF")
HEADER_FILL = PatternFill(start_color="4A3728", end_color="4A3728", fill_type="solid")
HEADER_ALIGN = Alignment(horizontal="center", vertical="center", wrap_text=True)
TOTAL_FONT = Font(bold=True, size=11, color="FFFFFF")
TOTAL_FILL = PatternFill(start_color="4A3728", end_color="4A3728", fill_type="solid")
TOTAL_LABEL_ALIGN = Alignment(horizontal="right", vertical="center")
TOTAL_NUM_ALIGN = Alignment(horizontal="center", vertical="center")
EVEN_FILL = PatternFill(start_color="F8F5F0", end_color="F8F5F0", fill_type="solid")
ODD_FILL = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid")
SEP_FILL = PatternFill(start_color="EDE8E0", end_color="EDE8E0", fill_type="solid")
THIN_BORDER = Border(
    top=Side(style="thin", color="D5CFC7"),
    bottom=Side(style="thin", color="D5CFC7"),
    left=Side(style="thin", color="D5CFC7"),
    right=Side(style="thin", color="D5CFC7"),
)
CENTER_ALIGN = Alignment(horizontal="center", vertical="center")
RIGHT_ALIGN = Alignment(horizontal="right", vertical="center")
LEFT_ALIGN = Alignment(horizontal="left", vertical="center")


def get_delivery_date(created_at):
    """Compute delivery date: before 19:00 Vlad → tomorrow, after → day after tomorrow."""
    vlad_time = created_at.astimezone(VLAD_TZ)
    if vlad_time.hour >= 19:
        return (vlad_time + timedelta(days=2)).date()
    else:
        return (vlad_time + timedelta(days=1)).date()


def get_report_settings() -> dict | None:
    db = SessionLocal()
    try:
        settings = db.query(SiteSettings).filter(
            SiteSettings.key.in_([
                "reportEnabled", "reportSmtpHost", "reportSmtpPort",
                "reportSmtpEmail", "reportSmtpPassword",
                "reportRecipient", "reportTime",
            ])
        ).all()
        s = {row.key: row.value for row in settings}
        if s.get("reportEnabled") != "1":
            return None
        if not s.get("reportSmtpEmail") or not s.get("reportSmtpPassword"):
            return None
        # Need at least one recipient (general or per-location from DB)
        locations_map = get_locations_map()
        has_recipient = bool(s.get("reportRecipient", "").strip())
        if not has_recipient:
            has_recipient = any(v for v in locations_map.values() if v and v.strip())
        if not has_recipient:
            return None
        return s
    finally:
        db.close()


def parse_emails(text: str) -> list[str]:
    """Parse multi-line email field into list of valid emails."""
    if not text:
        return []
    return [e.strip() for e in text.replace(",", "\n").split("\n") if e.strip() and "@" in e.strip()]


def load_orders_for_delivery_date(target_date: date_type):
    """Load all orders whose delivery date matches target_date."""
    db = SessionLocal()
    try:
        # Load recent orders (last 5 days of creation) to find ones delivering on target_date
        cutoff = datetime.combine(target_date - timedelta(days=3), datetime.min.time()).replace(tzinfo=timezone.utc)
        orders = (
            db.query(Order)
            .options(joinedload(Order.items).joinedload(OrderItem.dish), joinedload(Order.customer))
            .filter(Order.created_at >= cutoff)
            .order_by(Order.created_at)
            .all()
        )
        return [o for o in orders if get_delivery_date(o.created_at) == target_date]
    finally:
        db.close()


def _build_guests_sheet(ws, loc_orders, location: str, delivery_date: date_type):
    """Fill a worksheet with guest report data for a specific location."""
    # Title block
    ws.append([f"ONE COFFEE PLACE — Отчёт по гостям — {location}"])
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=9)
    ws["A1"].font = TITLE_FONT

    ws.append([f"Заказ на: {format_date_ru(delivery_date)}"])
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=9)
    ws["A2"].font = SUB_FONT

    ws.append([f"Точка выдачи: {location}"])
    ws.merge_cells(start_row=3, start_column=1, end_row=3, end_column=9)
    ws["A3"].font = Font(bold=True, size=11, color="4A3728")

    total_sum = sum(o.total for o in loc_orders)
    ws.append([f"Всего заказов: {len(loc_orders)}   |   Общая сумма: {total_sum:,.0f} ₽"])
    ws.merge_cells(start_row=4, start_column=1, end_row=4, end_column=9)
    ws["A4"].font = STATS_FONT

    ws.append([])  # empty row

    # Headers
    headers = ["№", "Время", "Клиент", "Телефон", "Блюдо", "Кол-во", "Цена", "Сумма", "Комментарий"]
    ws.append(headers)
    header_row = 6
    for col in range(1, len(headers) + 1):
        cell = ws.cell(row=header_row, column=col)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = HEADER_ALIGN
        cell.border = THIN_BORDER

    # Data
    num_cols = len(headers)
    for idx, o in enumerate(loc_orders):
        if idx > 0:
            ws.append([""] * num_cols)
            sep_row = ws.max_row
            for col in range(1, num_cols + 1):
                c = ws.cell(row=sep_row, column=col)
                c.fill = SEP_FILL
            ws.row_dimensions[sep_row].height = 4
        phone = o.customer.email if o.customer else "—"
        name = o.customer.name if o.customer and o.customer.name else "—"
        created_vlad = o.created_at.astimezone(VLAD_TZ) if o.created_at else None
        time_str = created_vlad.strftime("%H:%M") if created_vlad else "—"
        row_fill = EVEN_FILL if idx % 2 == 1 else ODD_FILL
        for i, it in enumerate(o.items):
            dish_name = it.dish.name if it.dish else "—"
            ws.append([
                o.id if i == 0 else "",
                time_str if i == 0 else "",
                name if i == 0 else "",
                phone if i == 0 else "",
                dish_name,
                it.quantity,
                f"{it.price:.0f} ₽",
                f"{o.total:.0f} ₽" if i == 0 else "",
                (o.comment or "") if i == 0 else "",
            ])
            row_num = ws.max_row
            for col in range(1, num_cols + 1):
                c = ws.cell(row=row_num, column=col)
                c.fill = row_fill
                c.border = THIN_BORDER
                if col in (1, 2, 6):
                    c.alignment = CENTER_ALIGN
                elif col in (7, 8):
                    c.alignment = RIGHT_ALIGN
                else:
                    c.alignment = LEFT_ALIGN

    # Total row
    ws.append([])
    total_row_data = ["", "", "", "", "", "", "ИТОГО:", f"{total_sum:,.0f} ₽", ""]
    ws.append(total_row_data)
    total_row = ws.max_row
    for col in range(1, 10):
        cell = ws.cell(row=total_row, column=col)
        cell.font = TOTAL_FONT
        cell.fill = TOTAL_FILL
        cell.border = THIN_BORDER
        cell.alignment = TOTAL_LABEL_ALIGN if col <= 7 else TOTAL_NUM_ALIGN

    # Column widths
    widths = [8, 10, 22, 16, 30, 10, 12, 14, 24]
    for i, w in enumerate(widths):
        ws.column_dimensions[chr(65 + i)].width = w


def build_guests_excel(orders, location: str, delivery_date: date_type) -> bytes | None:
    """Build 'Отчёт по гостям' for a specific location."""
    loc_orders = [o for o in orders if (o.address or "") == location]
    if not loc_orders:
        return None

    wb = Workbook()
    ws = wb.active
    ws.title = "Отчёт по гостям"
    _build_guests_sheet(ws, loc_orders, location, delivery_date)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_guests_excel_multi(orders, locations: list[str], delivery_date: date_type) -> bytes | None:
    """Build 'Отчёт по гостям' with a sheet per location, including orders from unknown locations."""
    wb = Workbook()
    first = True
    seen_addresses = set()

    for location in locations:
        loc_orders = [o for o in orders if (o.address or "") == location]
        if not loc_orders:
            continue
        seen_addresses.add(location)
        if first:
            ws = wb.active
            ws.title = location[:31]
            first = False
        else:
            ws = wb.create_sheet(title=location[:31])
        _build_guests_sheet(ws, loc_orders, location, delivery_date)

    # Include orders from addresses not in the known locations list
    other_orders = [o for o in orders if (o.address or "") not in seen_addresses]
    if other_orders:
        # Group by address
        addr_groups = {}
        for o in other_orders:
            addr = o.address or "Без адреса"
            addr_groups.setdefault(addr, []).append(o)
        for addr, addr_orders in addr_groups.items():
            if first:
                ws = wb.active
                ws.title = addr[:31]
                first = False
            else:
                ws = wb.create_sheet(title=addr[:31])
            _build_guests_sheet(ws, addr_orders, addr, delivery_date)

    if first:
        return None  # no sheets were created

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_kitchen_excel(orders, delivery_date: date_type, locations_map: dict = None) -> bytes | None:
    """Build 'Отчёт для кухни' — aggregated across all locations with per-location columns."""
    if not orders:
        return None

    wb = Workbook()
    ws = wb.active
    ws.title = "Кухня"

    loc_names = list((locations_map or get_locations_map()).keys())
    num_cols = 3 + len(loc_names)  # №, Блюдо, loc1, loc2, loc3, Итого

    # Aggregate dishes per location and total
    per_loc = {loc: {} for loc in loc_names}
    totals = {}
    total_portions = 0
    for o in orders:
        loc = o.address or ""
        for it in o.items:
            dish_name = it.dish.name if it.dish else "—"
            totals[dish_name] = totals.get(dish_name, 0) + it.quantity
            total_portions += it.quantity
            if loc in per_loc:
                per_loc[loc][dish_name] = per_loc[loc].get(dish_name, 0) + it.quantity

    # Title block
    ws.append(["ONE COFFEE PLACE — Отчёт для кухни — все точки"])
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=num_cols)
    ws["A1"].font = TITLE_FONT

    ws.append([f"Заказ на: {format_date_ru(delivery_date)}"])
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=num_cols)
    ws["A2"].font = SUB_FONT

    ws.append([f"Всего заказов: {len(orders)}   |   Всего порций: {total_portions}"])
    ws.merge_cells(start_row=3, start_column=1, end_row=3, end_column=num_cols)
    ws["A3"].font = STATS_FONT

    ws.append([])  # empty row

    # Headers: №, Блюдо, [locations...], Итого
    headers = ["№", "Блюдо"] + loc_names + ["Итого"]
    ws.append(headers)
    header_row = 5
    for col in range(1, len(headers) + 1):
        cell = ws.cell(row=header_row, column=col)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = HEADER_ALIGN
        cell.border = THIN_BORDER

    # Data
    num = 1
    for dish_name in sorted(totals.keys()):
        row = [num, dish_name]
        for loc in loc_names:
            val = per_loc[loc].get(dish_name, 0)
            row.append(val if val else "")
        row.append(totals[dish_name])
        ws.append(row)
        row_num = ws.max_row
        row_fill = EVEN_FILL if num % 2 == 0 else ODD_FILL
        for col in range(1, len(headers) + 1):
            c = ws.cell(row=row_num, column=col)
            c.fill = row_fill
            c.border = THIN_BORDER
            if col == 1:
                c.alignment = CENTER_ALIGN
            elif col == 2:
                c.alignment = LEFT_ALIGN
            else:
                c.alignment = CENTER_ALIGN
        num += 1

    # Total row
    ws.append([])
    total_row_data = ["", "ИТОГО:"]
    for loc in loc_names:
        total_row_data.append(sum(per_loc[loc].values()))
    total_row_data.append(total_portions)
    ws.append(total_row_data)
    total_row = ws.max_row
    for col in range(1, len(headers) + 1):
        cell = ws.cell(row=total_row, column=col)
        cell.font = TOTAL_FONT
        cell.fill = TOTAL_FILL
        cell.border = THIN_BORDER
        cell.alignment = TOTAL_LABEL_ALIGN if col <= 2 else TOTAL_NUM_ALIGN

    # Column widths
    ws.column_dimensions["A"].width = 6
    ws.column_dimensions["B"].width = 40
    for i, _ in enumerate(loc_names):
        ws.column_dimensions[chr(67 + i)].width = 16
    ws.column_dimensions[chr(67 + len(loc_names))].width = 12

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def get_smtp_connection(settings):
    """Create and return SMTP connection."""
    smtp_email = settings["reportSmtpEmail"]
    smtp_pass = settings["reportSmtpPassword"]
    smtp_host = settings.get("reportSmtpHost", "").strip()
    smtp_port = settings.get("reportSmtpPort", "").strip()

    if smtp_host and ":" in smtp_host:
        parts = smtp_host.split(":")
        smtp_host = parts[0]
        if not smtp_port:
            smtp_port = parts[1]
    if not smtp_host:
        smtp_host, smtp_port = detect_smtp(smtp_email)
    else:
        smtp_port = int(smtp_port) if smtp_port else 465

    server = smtplib.SMTP_SSL(smtp_host, int(smtp_port), timeout=15)
    server.login(smtp_email, smtp_pass)
    return server


def detect_smtp(email: str) -> tuple[str, int]:
    domain = email.split("@")[-1].lower()
    SMTP_SERVERS = {
        "mail.ru": ("smtp.mail.ru", 465),
        "bk.ru": ("smtp.mail.ru", 465),
        "inbox.ru": ("smtp.mail.ru", 465),
        "list.ru": ("smtp.mail.ru", 465),
        "yandex.ru": ("smtp.yandex.ru", 465),
        "ya.ru": ("smtp.yandex.ru", 465),
        "gmail.com": ("smtp.gmail.com", 465),
    }
    if domain in SMTP_SERVERS:
        return SMTP_SERVERS[domain]
    return (f"smtp.{domain}", 465)


def send_email(server, from_email: str, to_emails: list[str], subject: str, body_text: str, attachments: list[tuple[str, bytes]]):
    """Send email with attachments to multiple recipients."""
    if not to_emails:
        return

    msg = MIMEMultipart()
    msg["From"] = from_email
    msg["To"] = ", ".join(to_emails)
    msg["Subject"] = subject
    msg.attach(MIMEText(body_text, "plain", "utf-8"))

    for filename, data in attachments:
        part = MIMEBase("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        part.set_payload(data)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=("utf-8", "", filename))
        msg.attach(part)

    server.sendmail(from_email, to_emails, msg.as_string())
    logger.info(f"[EMAIL REPORT] Sent '{subject}' to {', '.join(to_emails)}")


def _send_html_email(server, from_email: str, to_emails: list[str], subject: str, html_body: str):
    """Send HTML email."""
    if not to_emails:
        return
    msg = MIMEMultipart("alternative")
    msg["From"] = from_email
    msg["To"] = ", ".join(to_emails)
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    server.sendmail(from_email, to_emails, msg.as_string())


def _build_pickup_html(name: str, date_str: str, loc_name: str, loc_address: str, orders) -> str:
    """Build beautiful coffee-themed HTML email for pickup notification."""
    # Build order items table rows
    items_html = ""
    grand_total = 0
    for order in orders:
        for item in order.items:
            dish_name = item.dish.name if item.dish else "—"
            qty = item.quantity
            price = item.price * qty
            grand_total += price
            items_html += f'''
            <tr>
                <td style="padding:10px 16px;border-bottom:1px solid #f0ebe5;color:#4A3728;font-size:14px;">{dish_name}</td>
                <td style="padding:10px 16px;border-bottom:1px solid #f0ebe5;color:#888;font-size:14px;text-align:center;">{qty}</td>
                <td style="padding:10px 16px;border-bottom:1px solid #f0ebe5;color:#4A3728;font-size:14px;text-align:right;font-weight:600;">{price:.0f} ₽</td>
            </tr>'''

    return f'''<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f5f0eb;font-family:'Segoe UI',Tahoma,Geneva,Verdana,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f0eb;padding:32px 0;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(74,55,40,0.08);">

    <!-- Header -->
    <tr>
        <td style="background:linear-gradient(135deg,#4A3728 0%,#6B4F3E 100%);padding:36px 40px;text-align:center;">
            <div style="font-size:32px;margin-bottom:8px;">☕</div>
            <h1 style="margin:0;color:#ffffff;font-size:22px;font-weight:700;letter-spacing:0.5px;">One Coffee Place</h1>
            <p style="margin:6px 0 0;color:rgba(255,255,255,0.7);font-size:13px;letter-spacing:1px;">ГОТОВИМ С ДУШОЙ, ПОДАЁМ С ЛЮБОВЬЮ</p>
        </td>
    </tr>

    <!-- Greeting -->
    <tr>
        <td style="padding:32px 40px 8px;">
            <p style="margin:0;color:#4A3728;font-size:16px;">Здравствуйте, <strong>{name}</strong>!</p>
        </td>
    </tr>

    <!-- Main message -->
    <tr>
        <td style="padding:16px 40px;">
            <div style="background:#f9f6f2;border-radius:12px;padding:24px;border-left:4px solid #c8956c;">
                <p style="margin:0 0 4px;color:#4A3728;font-size:18px;font-weight:700;">Ваш заказ готов к выдаче!</p>
                <p style="margin:0;color:#7a6a5e;font-size:14px;">Заказ на <strong>{date_str}</strong> ждёт вас.</p>
            </div>
        </td>
    </tr>

    <!-- Location -->
    <tr>
        <td style="padding:16px 40px;">
            <table width="100%" cellpadding="0" cellspacing="0" style="background:#faf8f5;border-radius:12px;overflow:hidden;">
                <tr>
                    <td style="padding:20px 24px;">
                        <table cellpadding="0" cellspacing="0">
                            <tr>
                                <td style="vertical-align:top;padding-right:14px;">
                                    <div style="width:40px;height:40px;background:#4A3728;border-radius:10px;text-align:center;line-height:40px;font-size:18px;">📍</div>
                                </td>
                                <td style="vertical-align:top;">
                                    <p style="margin:0;color:#4A3728;font-size:15px;font-weight:700;">{loc_name}</p>
                                    <p style="margin:4px 0 0;color:#9a8a7e;font-size:13px;">{loc_address}</p>
                                </td>
                            </tr>
                        </table>
                    </td>
                </tr>
            </table>
        </td>
    </tr>

    <!-- Order details -->
    <tr>
        <td style="padding:16px 40px 8px;">
            <p style="margin:0 0 12px;color:#4A3728;font-size:14px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;">Ваш заказ</p>
            <table width="100%" cellpadding="0" cellspacing="0" style="border-radius:10px;overflow:hidden;border:1px solid #f0ebe5;">
                <tr style="background:#faf8f5;">
                    <td style="padding:10px 16px;color:#9a8a7e;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;">Блюдо</td>
                    <td style="padding:10px 16px;color:#9a8a7e;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;text-align:center;">Кол-во</td>
                    <td style="padding:10px 16px;color:#9a8a7e;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;text-align:right;">Сумма</td>
                </tr>
                {items_html}
                <tr style="background:#4A3728;">
                    <td colspan="2" style="padding:12px 16px;color:rgba(255,255,255,0.8);font-size:14px;font-weight:600;">Итого</td>
                    <td style="padding:12px 16px;color:#ffffff;font-size:16px;font-weight:700;text-align:right;">{grand_total:.0f} ₽</td>
                </tr>
            </table>
        </td>
    </tr>

    <!-- Divider -->
    <tr>
        <td style="padding:24px 40px 0;">
            <div style="border-top:1px dashed #e0d6cc;"></div>
        </td>
    </tr>

    <!-- Footer -->
    <tr>
        <td style="padding:20px 40px 32px;text-align:center;">
            <p style="margin:0 0 4px;color:#9a8a7e;font-size:13px;">Приятного аппетита! ☕</p>
            <p style="margin:0;color:#c8b8a8;font-size:12px;">One Coffee Place</p>
        </td>
    </tr>

</table>
</td></tr>
</table>
</body>
</html>'''


def send_pickup_notification(location_id: int):
    """Send pickup-ready notification to all customers with today's orders at given location."""
    settings = get_report_settings()
    if not settings:
        raise ValueError("Рассылка не настроена: включите отправку и заполните SMTP поля")

    db = SessionLocal()
    try:
        location = db.query(Location).filter(Location.id == location_id).first()
        if not location:
            raise ValueError("Точка не найдена")

        now_vlad = datetime.now(VLAD_TZ)
        if now_vlad.hour >= 19:
            delivery_date = (now_vlad + timedelta(days=1)).date()
        else:
            delivery_date = now_vlad.date()

        orders = load_orders_for_delivery_date(delivery_date)
        loc_orders = [o for o in orders if (o.address or "") == location.address]
        date_str_short = delivery_date.strftime("%d.%m")
        if not loc_orders:
            raise ValueError(f"Нет заказов на {date_str_short} для точки «{location.name}»")

        # Collect unique customer emails
        customer_ids = list(set(o.customer_id for o in loc_orders))
        from models import Customer
        customers = db.query(Customer).filter(Customer.id.in_(customer_ids)).all()
        emails = [c.email for c in customers if c.email and "@" in c.email]
        if not emails:
            raise ValueError("Нет email-адресов клиентов для отправки")

        # Build customer name map
        customer_map = {c.id: c for c in customers}

        server = get_smtp_connection(settings)
        smtp_email = settings["reportSmtpEmail"]
        try:
            date_str = format_date_ru(delivery_date)
            subject = f"☕ Ваш заказ готов к выдаче — {location.name}"
            sent = 0
            for customer in customers:
                if not customer.email or "@" not in customer.email:
                    continue
                # Get this customer's orders
                cust_orders = [o for o in loc_orders if o.customer_id == customer.id]
                cust_name = customer.name or "Гость"
                html_body = _build_pickup_html(cust_name, date_str, location.name, location.address, cust_orders)
                try:
                    _send_html_email(server, smtp_email, [customer.email], subject, html_body)
                    sent += 1
                except Exception as e:
                    logger.error(f"[NOTIFY] Failed to send to {customer.email}: {e}")
            logger.info(f"[NOTIFY] Sent pickup notification for '{location.name}' to {sent}/{len(emails)} customers")
            return sent
        finally:
            server.quit()
    finally:
        db.close()


def send_report(force=False):
    logger.info("[EMAIL REPORT] Checking if report should be sent...")
    settings = get_report_settings()
    if not settings:
        logger.info("[EMAIL REPORT] Disabled or not configured")
        if force:
            raise ValueError("Отчёт не настроен: включите отправку и заполните все поля")
        return

    # Target delivery date: tomorrow (report covers orders to be delivered tomorrow)
    now_vlad = datetime.now(VLAD_TZ)
    delivery_date = (now_vlad + timedelta(days=1)).date()

    date_str_ru = format_date_ru(delivery_date)
    date_str_file = delivery_date.strftime("%d.%m.%Y")

    orders = load_orders_for_delivery_date(delivery_date)
    smtp_email = settings["reportSmtpEmail"]
    general_emails = parse_emails(settings.get("reportRecipient", ""))

    try:
        server = get_smtp_connection(settings)
    except Exception as e:
        logger.error(f"[EMAIL REPORT] SMTP connection failed: {e}")
        raise

    locations_map = get_locations_map()

    try:
        sent_count = 0

        # 1. Отчёт по гостям — per location
        for location, report_email in locations_map.items():
            loc_emails = parse_emails(report_email or "")
            recipients = list(set(loc_emails + general_emails))
            if not recipients:
                continue

            excel_data = build_guests_excel(orders, location, delivery_date)
            if excel_data:
                subject = f"Отчёт по гостям на {date_str_file} — {location}"
                filename = f"Отчет по гостям на {date_str_file} ({location}).xlsx"
                body = f"Отчёт по гостям за {location} на {date_str_ru} во вложении."
                send_email(server, smtp_email, recipients, subject, body, [(filename, excel_data)])
                sent_count += 1
            else:
                logger.info(f"[EMAIL REPORT] No orders for {location}, skipping")

        # 2. Отчёт для кухни — all locations combined
        all_kitchen_emails = set(general_emails)
        for report_email in locations_map.values():
            all_kitchen_emails.update(parse_emails(report_email or ""))
        all_kitchen_emails = list(all_kitchen_emails)

        if all_kitchen_emails:
            kitchen_data = build_kitchen_excel(orders, delivery_date, locations_map)
            if kitchen_data:
                subject = f"Отчёт для кухни на {date_str_file} — все точки"
                filename = f"Отчет для кухни на {date_str_file} (все точки).xlsx"
                body = f"Сводный отчёт для кухни на {date_str_ru} во вложении."
                send_email(server, smtp_email, all_kitchen_emails, subject, body, [(filename, kitchen_data)])
                sent_count += 1

        if sent_count == 0 and not orders:
            # No orders at all — send notification to general emails
            if general_emails:
                msg = MIMEMultipart()
                msg["From"] = smtp_email
                msg["To"] = ", ".join(general_emails)
                msg["Subject"] = f"Заказы на {date_str_file} — нет заказов"
                msg.attach(MIMEText(f"На {date_str_ru} заказов не поступило.", "plain", "utf-8"))
                server.sendmail(smtp_email, general_emails, msg.as_string())
                logger.info(f"[EMAIL REPORT] Sent 'no orders' notification to {', '.join(general_emails)}")

        logger.info(f"[EMAIL REPORT] Done, sent {sent_count} report(s)")

    finally:
        server.quit()

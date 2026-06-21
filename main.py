from fastapi import FastAPI, Depends, HTTPException, Request, Form, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, date
from typing import Optional
import os

from database import engine, get_db, Base
from models import Room, Booking, BookingLog, ClosedPeriod, WaitlistEntry, WaitlistFillLog

Base.metadata.create_all(bind=engine)

app = FastAPI(title="图书馆研习室预约系统")

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

MAX_DAILY_MINUTES = 240
MIN_BOOKING_MINUTES = 30
MAX_BOOKING_MINUTES = 120
ADMIN_ID = "admin"


def add_log(db: Session, booking_id: int, action: str, details: str = ""):
    log = BookingLog(booking_id=booking_id, action=action, details=details)
    db.add(log)
    db.commit()


def check_time_overlap(db: Session, room_id: int, start_time: datetime, end_time: datetime, exclude_booking_id: Optional[int] = None) -> bool:
    bookings = db.query(Booking).filter(
        Booking.room_id == room_id,
        Booking.status == "active",
    ).all()
    for b in bookings:
        if exclude_booking_id and b.id == exclude_booking_id:
            continue
        if start_time < b.end_time and end_time > b.start_time:
            return True
    return False


def check_room_capacity(db: Session, room_id: int, start_time: datetime, end_time: datetime, exclude_booking_id: Optional[int] = None) -> bool:
    room = db.query(Room).filter(Room.id == room_id).first()
    if not room:
        return False

    check_points = []
    current = start_time
    while current < end_time:
        check_points.append(current)
        current += timedelta(minutes=30)
    check_points.append(end_time - timedelta(seconds=1))

    for point in check_points:
        overlapping_count = 0
        bookings = db.query(Booking).filter(
            Booking.room_id == room_id,
            Booking.status == "active",
        ).all()
        for b in bookings:
            if exclude_booking_id and b.id == exclude_booking_id:
                continue
            if b.start_time <= point < b.end_time:
                overlapping_count += 1
        if overlapping_count >= room.capacity:
            return False
    return True


def check_closed_periods(date_str: str, start_time: datetime, end_time: datetime, db: Session) -> bool:
    closed_periods = db.query(ClosedPeriod).filter(ClosedPeriod.date == date_str).all()
    for cp in closed_periods:
        cp_start = datetime.strptime(f"{date_str} {cp.start_time}", "%Y-%m-%d %H:%M")
        cp_end = datetime.strptime(f"{date_str} {cp.end_time}", "%Y-%m-%d %H:%M")
        if start_time < cp_end and end_time > cp_start:
            return False
    return True


def check_user_time_cross(db: Session, user_id: str, start_time: datetime, end_time: datetime) -> Optional[Booking]:
    date_str = start_time.strftime("%Y-%m-%d")
    start_of_day = datetime.strptime(f"{date_str} 00:00", "%Y-%m-%d %H:%M")
    end_of_day = datetime.strptime(f"{date_str} 23:59", "%Y-%m-%d %H:%M")
    bookings = db.query(Booking).filter(
        Booking.user_id == user_id,
        Booking.status == "active",
        Booking.start_time >= start_of_day,
        Booking.start_time <= end_of_day
    ).all()
    for b in bookings:
        if start_time < b.end_time and end_time > b.start_time:
            return b
    return None


def get_user_daily_minutes(db: Session, user_id: str, date_str: str) -> int:
    start_of_day = datetime.strptime(f"{date_str} 00:00", "%Y-%m-%d %H:%M")
    end_of_day = datetime.strptime(f"{date_str} 23:59", "%Y-%m-%d %H:%M")
    bookings = db.query(Booking).filter(
        Booking.user_id == user_id,
        Booking.status == "active",
        Booking.start_time >= start_of_day,
        Booking.start_time <= end_of_day
    ).all()
    total = 0
    for b in bookings:
        delta = b.end_time - b.start_time
        total += int(delta.total_seconds() / 60)
    return total


def get_waitlist_position(db: Session, entry: WaitlistEntry) -> int:
    return db.query(WaitlistEntry).filter(
        WaitlistEntry.room_id == entry.room_id,
        WaitlistEntry.status == "waiting",
        WaitlistEntry.start_time == entry.start_time,
        WaitlistEntry.end_time == entry.end_time,
        WaitlistEntry.created_at <= entry.created_at,
    ).count()


def is_fully_at_capacity(db: Session, room_id: int, start_time: datetime, end_time: datetime) -> bool:
    room = db.query(Room).filter(Room.id == room_id, Room.is_active == True).first()
    if not room:
        return False

    check_points = []
    current = start_time
    while current < end_time:
        check_points.append(current)
        current += timedelta(minutes=30)
    check_points.append(end_time - timedelta(seconds=1))

    for point in check_points:
        overlapping_count = 0
        bookings = db.query(Booking).filter(
            Booking.room_id == room_id,
            Booking.status == "active",
        ).all()
        for b in bookings:
            if b.start_time <= point < b.end_time:
                overlapping_count += 1
        if overlapping_count < room.capacity:
            return False
    return True


def validate_waitlist_entry(db: Session, user_id: str, room_id: int, start_time: datetime, end_time: datetime, selected_date: str) -> Optional[str]:
    room = db.query(Room).filter(Room.id == room_id, Room.is_active == True).first()
    if not room:
        return "房间不存在或未开放"

    if not check_closed_periods(selected_date, start_time, end_time, db):
        return "所选时段与闭馆时段冲突"

    if not is_fully_at_capacity(db, room_id, start_time, end_time):
        return "所选时段未因容量已满（存在空闲或部分可预约），请直接预约而非加入候补"

    cross_booking = check_user_time_cross(db, user_id, start_time, end_time)
    if cross_booking:
        cb_start = cross_booking.start_time.strftime("%H:%M")
        cb_end = cross_booking.end_time.strftime("%H:%M")
        return f"您在同日存在时间交叉的预约（{cross_booking.room.name} {cb_start}-{cb_end}），与拟候补时段交叉"

    duration_minutes = int((end_time - start_time).total_seconds() / 60)
    current_minutes = get_user_daily_minutes(db, user_id, selected_date)
    if current_minutes + duration_minutes > MAX_DAILY_MINUTES:
        return f"单人每日总时长限制为 {MAX_DAILY_MINUTES} 分钟，当前已用 {current_minutes} 分钟，加入候补后将超出限额"

    now = datetime.now()
    if start_time < now:
        return "不能候补过去的时间"

    return None


def try_auto_fill(db: Session, room_id: int):
    entries = db.query(WaitlistEntry).filter(
        WaitlistEntry.room_id == room_id,
        WaitlistEntry.status == "waiting",
    ).order_by(WaitlistEntry.created_at).all()

    for entry in entries:
        date_str = entry.start_time.strftime("%Y-%m-%d")
        start_time = entry.start_time
        end_time = entry.end_time

        fail_reason = None

        room = db.query(Room).filter(Room.id == room_id, Room.is_active == True).first()
        if not room:
            fail_reason = "房间不存在或未开放"

        if not fail_reason and not check_closed_periods(date_str, start_time, end_time, db):
            fail_reason = "所选时段与闭馆时段冲突"

        if not fail_reason and not check_room_capacity(db, room_id, start_time, end_time):
            fail_reason = "房间容量已满"

        if not fail_reason:
            check_points = []
            cp_current = start_time
            while cp_current < end_time:
                check_points.append(cp_current)
                cp_current += timedelta(minutes=30)
            check_points.append(end_time - timedelta(seconds=1))

            has_any_overlap = False
            for point in check_points:
                active_bookings = db.query(Booking).filter(
                    Booking.room_id == room_id,
                    Booking.status == "active",
                ).all()
                for b in active_bookings:
                    if b.start_time <= point < b.end_time:
                        has_any_overlap = True
                        break
                if has_any_overlap:
                    break
            if not has_any_overlap:
                fail_reason = "候补时段完全空闲（由空闲时段误入的候补），无需补位"

        if not fail_reason:
            cross_booking = check_user_time_cross(db, entry.user_id, start_time, end_time)
            if cross_booking:
                cb_start = cross_booking.start_time.strftime("%H:%M")
                cb_end = cross_booking.end_time.strftime("%H:%M")
                fail_reason = f"本人时间冲突（{cross_booking.room.name} {cb_start}-{cb_end}）"

        if not fail_reason:
            duration_minutes = int((end_time - start_time).total_seconds() / 60)
            current_minutes = get_user_daily_minutes(db, entry.user_id, date_str)
            if current_minutes + duration_minutes > MAX_DAILY_MINUTES:
                fail_reason = f"超出每日时长限额（已用 {current_minutes} 分钟，需 {duration_minutes} 分钟）"

        if not fail_reason:
            now = datetime.now()
            if start_time < now:
                fail_reason = "候补时段已过期"

        if fail_reason:
            fill_log = WaitlistFillLog(
                waitlist_id=entry.id,
                room_id=room_id,
                user_id=entry.user_id,
                user_name=entry.user_name,
                start_time=start_time,
                end_time=end_time,
                success=False,
                reason=fail_reason,
            )
            db.add(fill_log)
            db.commit()
            continue

        booking = Booking(
            room_id=room_id,
            user_id=entry.user_id,
            user_name=entry.user_name,
            start_time=start_time,
            end_time=end_time,
            status="active",
        )
        db.add(booking)
        db.commit()
        db.refresh(booking)

        entry.status = "filled"
        db.commit()

        add_log(db, booking.id, "auto_filled", f"候补自动补位成功：用户{entry.user_name}({entry.user_id})补入房间{room.name}，时段：{start_time}-{end_time}")

        fill_log = WaitlistFillLog(
            waitlist_id=entry.id,
            room_id=room_id,
            user_id=entry.user_id,
            user_name=entry.user_name,
            start_time=start_time,
            end_time=end_time,
            success=True,
            booking_id=booking.id,
        )
        db.add(fill_log)
        db.commit()


def seed_initial_data(db: Session):
    if db.query(Room).count() == 0:
        rooms = [
            Room(name="A101", capacity=4, description="小型讨论室"),
            Room(name="A102", capacity=2, description="双人自习室"),
            Room(name="B201", capacity=6, description="中型研讨室"),
            Room(name="B202", capacity=1, description="单人静音室"),
        ]
        db.add_all(rooms)
        db.commit()

    today = date.today().strftime("%Y-%m-%d")
    if db.query(ClosedPeriod).count() == 0:
        default_closed = ClosedPeriod(
            date=today,
            start_time="12:00",
            end_time="13:00",
            reason="午休闭馆"
        )
        db.add(default_closed)
        db.commit()


@app.on_event("startup")
def startup_event():
    db = next(get_db())
    seed_initial_data(db)
    db.close()


@app.get("/", response_class=HTMLResponse)
async def home(request: Request, user_id: str = "reader1", user_name: str = "读者1", db: Session = Depends(get_db)):
    today = date.today()
    today_str = today.strftime("%Y-%m-%d")
    now = datetime.now()

    active_rooms = db.query(Room).filter(Room.is_active == True).all()
    room_status = []
    for room in active_rooms:
        current_bookings = db.query(Booking).filter(
            Booking.room_id == room.id,
            Booking.status == "active",
            Booking.start_time <= now,
            Booking.end_time > now
        ).count()
        remaining = room.capacity - current_bookings
        total_bookings_today = db.query(Booking).filter(
            Booking.room_id == room.id,
            Booking.status == "active",
            Booking.start_time >= datetime(today.year, today.month, today.day),
            Booking.start_time < datetime(today.year, today.month, today.day) + timedelta(days=1)
        ).count()
        room_status.append({
            "room": room,
            "remaining": remaining,
            "total_today": total_bookings_today,
            "is_open": remaining > 0
        })

    upcoming_bookings = db.query(Booking).filter(
        Booking.status == "active",
        Booking.start_time > now,
        Booking.start_time < now + timedelta(hours=3)
    ).order_by(Booking.start_time).all()

    closed_periods = db.query(ClosedPeriod).filter(ClosedPeriod.date == today_str).all()

    user_minutes = get_user_daily_minutes(db, user_id, today_str)

    return templates.TemplateResponse("index.html", {
        "request": request,
        "room_status": room_status,
        "upcoming_bookings": upcoming_bookings,
        "closed_periods": closed_periods,
        "today_str": today_str,
        "user_id": user_id,
        "user_name": user_name,
        "user_minutes": user_minutes,
        "max_daily_minutes": MAX_DAILY_MINUTES
    })


@app.get("/book", response_class=HTMLResponse)
async def book_form(
    request: Request,
    user_id: str = "reader1",
    user_name: str = "读者1",
    room_id: Optional[int] = None,
    selected_date: Optional[str] = None,
    db: Session = Depends(get_db)
):
    today = date.today()
    if not selected_date:
        selected_date = today.strftime("%Y-%m-%d")

    rooms = db.query(Room).filter(Room.is_active == True).all()
    selected_room = None
    room_slots = []

    if room_id:
        selected_room = db.query(Room).filter(Room.id == room_id).first()
        if selected_room:
            day_start = datetime.strptime(f"{selected_date} 08:00", "%Y-%m-%d %H:%M")
            day_end = datetime.strptime(f"{selected_date} 22:00", "%Y-%m-%d %H:%M")

            slots = []
            current = day_start
            while current < day_end:
                slot_end = current + timedelta(minutes=30)
                slots.append({"start": current, "end": slot_end})
                current = slot_end

            closed_periods = db.query(ClosedPeriod).filter(ClosedPeriod.date == selected_date).all()
            closed_ranges = []
            for cp in closed_periods:
                cp_start = datetime.strptime(f"{selected_date} {cp.start_time}", "%Y-%m-%d %H:%M")
                cp_end = datetime.strptime(f"{selected_date} {cp.end_time}", "%Y-%m-%d %H:%M")
                closed_ranges.append((cp_start, cp_end, cp.reason))

            for slot in slots:
                status = "available"
                closed_reason = ""
                overlap_count = 0

                for cp_start, cp_end, reason in closed_ranges:
                    if slot["start"] < cp_end and slot["end"] > cp_start:
                        status = "closed"
                        closed_reason = reason
                        break

                if status == "available":
                    active_bookings = db.query(Booking).filter(
                        Booking.room_id == room_id,
                        Booking.status == "active",
                    ).all()
                    for b in active_bookings:
                        if slot["start"] < b.end_time and slot["end"] > b.start_time:
                            overlap_count += 1
                    if overlap_count >= selected_room.capacity:
                        status = "full"

                room_slots.append({
                    "start": slot["start"],
                    "end": slot["end"],
                    "time_label": f"{slot['start'].strftime('%H:%M')}-{slot['end'].strftime('%H:%M')}",
                    "status": status,
                    "closed_reason": closed_reason,
                    "overlap_count": overlap_count,
                    "capacity": selected_room.capacity
                })

    closed_periods_list = db.query(ClosedPeriod).filter(ClosedPeriod.date == selected_date).all()
    user_minutes = get_user_daily_minutes(db, user_id, selected_date)

    return templates.TemplateResponse("book.html", {
        "request": request,
        "rooms": rooms,
        "selected_room": selected_room,
        "room_slots": room_slots,
        "selected_date": selected_date,
        "user_id": user_id,
        "user_name": user_name,
        "closed_periods": closed_periods_list,
        "min_booking": MIN_BOOKING_MINUTES,
        "max_booking": MAX_BOOKING_MINUTES,
        "user_minutes": user_minutes,
        "max_daily_minutes": MAX_DAILY_MINUTES
    })


@app.post("/book/create")
async def create_booking(
    user_id: str = Form(...),
    user_name: str = Form(...),
    room_id: int = Form(...),
    selected_date: str = Form(...),
    start_hour: str = Form(...),
    duration_minutes: int = Form(...),
    db: Session = Depends(get_db)
):
    if duration_minutes < MIN_BOOKING_MINUTES or duration_minutes > MAX_BOOKING_MINUTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"预约时长必须在 {MIN_BOOKING_MINUTES}-{MAX_BOOKING_MINUTES} 分钟之间"
        )

    if duration_minutes % 30 != 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="预约时长必须是 30 分钟的整数倍"
        )

    start_time = datetime.strptime(f"{selected_date} {start_hour}", "%Y-%m-%d %H:%M")
    end_time = start_time + timedelta(minutes=duration_minutes)

    now = datetime.now()
    if start_time < now:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不能预约过去的时间"
        )

    day_start = datetime.strptime(f"{selected_date} 08:00", "%Y-%m-%d %H:%M")
    day_end = datetime.strptime(f"{selected_date} 22:00", "%Y-%m-%d %H:%M")
    if start_time < day_start or end_time > day_end:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="预约时间必须在 08:00-22:00 之间"
        )

    room = db.query(Room).filter(Room.id == room_id, Room.is_active == True).first()
    if not room:
        raise HTTPException(status_code=404, detail="房间不存在或未开放")

    if not check_closed_periods(selected_date, start_time, end_time, db):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="所选时段与闭馆时段冲突"
        )

    if check_time_overlap(db, room_id, start_time, end_time):
        if not check_room_capacity(db, room_id, start_time, end_time):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="所选时段房间容量已满"
            )

    cross_booking = check_user_time_cross(db, user_id, start_time, end_time)
    if cross_booking:
        cb_start = cross_booking.start_time.strftime("%H:%M")
        cb_end = cross_booking.end_time.strftime("%H:%M")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"您在同日存在时间交叉的预约（{cross_booking.room.name} {cb_start}-{cb_end}，"
                   f"与拟预约时段交叉（边界相接除外）"
        )

    current_minutes = get_user_daily_minutes(db, user_id, selected_date)
    if current_minutes + duration_minutes > MAX_DAILY_MINUTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"单人每日总时长限制为 {MAX_DAILY_MINUTES} 分钟，当前已用 {current_minutes} 分钟"
        )

    booking = Booking(
        room_id=room_id,
        user_id=user_id,
        user_name=user_name,
        start_time=start_time,
        end_time=end_time,
        status="active"
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)

    add_log(db, booking.id, "created", f"用户{user_name}({user_id})预约了房间{room.name}，时段：{start_time}-{end_time}，时长：{duration_minutes}分钟")

    return RedirectResponse(
        url=f"/history?user_id={user_id}&user_name={user_name}",
        status_code=status.HTTP_303_SEE_OTHER
    )


@app.get("/history", response_class=HTMLResponse)
async def history(
    request: Request,
    user_id: str = "reader1",
    user_name: str = "读者1",
    db: Session = Depends(get_db)
):
    bookings = db.query(Booking).filter(
        Booking.user_id == user_id
    ).order_by(Booking.created_at.desc()).all()

    logs = db.query(BookingLog).join(Booking).filter(
        Booking.user_id == user_id
    ).order_by(BookingLog.timestamp.desc()).all()

    waitlist_entries_raw = db.query(WaitlistEntry).filter(
        WaitlistEntry.user_id == user_id,
    ).order_by(WaitlistEntry.created_at.desc()).all()

    waitlist_entries = []
    for entry in waitlist_entries_raw:
        pos = get_waitlist_position(db, entry) if entry.status == "waiting" else None
        waitlist_entries.append({
            "entry": entry,
            "position": pos,
        })

    return templates.TemplateResponse("history.html", {
        "request": request,
        "bookings": bookings,
        "logs": logs,
        "waitlist_entries": waitlist_entries,
        "user_id": user_id,
        "user_name": user_name
    })


@app.post("/book/cancel/{booking_id}")
async def cancel_booking(
    booking_id: int,
    user_id: str = Form(...),
    user_name: str = Form(...),
    db: Session = Depends(get_db)
):
    booking = db.query(Booking).filter(Booking.id == booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="预约不存在")

    if booking.user_id != user_id:
        raise HTTPException(status_code=403, detail="只能取消自己的预约")

    if booking.status != "active":
        raise HTTPException(status_code=400, detail="该预约已取消或已标记爽约")

    booking.status = "cancelled"
    db.commit()

    add_log(db, booking.id, "cancelled", f"用户{user_name}({user_id})取消了预约")

    try_auto_fill(db, booking.room_id)

    return RedirectResponse(
        url=f"/history?user_id={user_id}&user_name={user_name}",
        status_code=status.HTTP_303_SEE_OTHER
    )


@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(
    request: Request,
    db: Session = Depends(get_db)
):
    rooms = db.query(Room).order_by(Room.id).all()
    closed_periods = db.query(ClosedPeriod).order_by(ClosedPeriod.date, ClosedPeriod.start_time).all()
    today = date.today().strftime("%Y-%m-%d")

    today_start = datetime.strptime(f"{today} 00:00", "%Y-%m-%d %H:%M")
    today_end = today_start + timedelta(days=1)
    today_bookings = db.query(Booking).filter(
        Booking.status == "active",
        Booking.start_time >= today_start,
        Booking.start_time < today_end
    ).order_by(Booking.start_time).all()

    return templates.TemplateResponse("admin.html", {
        "request": request,
        "rooms": rooms,
        "closed_periods": closed_periods,
        "today_bookings": today_bookings,
        "today": today
    })


@app.post("/admin/room/create")
async def create_room(
    name: str = Form(...),
    capacity: int = Form(...),
    description: str = Form(""),
    db: Session = Depends(get_db)
):
    if capacity < 1:
        raise HTTPException(status_code=400, detail="容量必须大于0")

    room = Room(name=name, capacity=capacity, description=description, is_active=True)
    db.add(room)
    db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/room/toggle/{room_id}")
async def toggle_room(room_id: int, db: Session = Depends(get_db)):
    room = db.query(Room).filter(Room.id == room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="房间不存在")
    room.is_active = not room.is_active
    db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/room/update_capacity/{room_id}")
async def update_room_capacity(
    room_id: int,
    capacity: int = Form(...),
    db: Session = Depends(get_db)
):
    if capacity < 1:
        raise HTTPException(status_code=400, detail="容量必须大于0")
    room = db.query(Room).filter(Room.id == room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="房间不存在")
    room.capacity = capacity
    db.commit()

    try_auto_fill(db, room_id)

    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/closed/create")
async def create_closed_period(
    date_str: str = Form(...),
    start_time: str = Form(...),
    end_time: str = Form(...),
    reason: str = Form(""),
    db: Session = Depends(get_db)
):
    cp_start = datetime.strptime(start_time, "%H:%M")
    cp_end = datetime.strptime(end_time, "%H:%M")
    if cp_start >= cp_end:
        raise HTTPException(status_code=400, detail="结束时间必须晚于开始时间")

    closed = ClosedPeriod(
        date=date_str,
        start_time=start_time,
        end_time=end_time,
        reason=reason
    )
    db.add(closed)
    db.commit()

    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/closed/delete/{cp_id}")
async def delete_closed_period(cp_id: int, db: Session = Depends(get_db)):
    cp = db.query(ClosedPeriod).filter(ClosedPeriod.id == cp_id).first()
    if not cp:
        raise HTTPException(status_code=404, detail="闭馆时段不存在")

    room_ids = db.query(WaitlistEntry.room_id).filter(
        WaitlistEntry.status == "waiting"
    ).distinct().all()
    affected_room_ids = [r[0] for r in room_ids]

    db.delete(cp)
    db.commit()

    for rid in affected_room_ids:
        try_auto_fill(db, rid)

    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/booking/no_show/{booking_id}")
async def mark_no_show(booking_id: int, db: Session = Depends(get_db)):
    booking = db.query(Booking).filter(Booking.id == booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="预约不存在")
    if booking.status != "active":
        raise HTTPException(status_code=400, detail="只能标记活跃预约为爽约")

    booking.status = "no_show"
    db.commit()
    add_log(db, booking.id, "no_show", "管理员标记为爽约/未到")

    try_auto_fill(db, booking.room_id)

    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/api/rooms/{room_id}/slots")
async def get_room_slots_api(
    room_id: int,
    date: str,
    db: Session = Depends(get_db)
):
    room = db.query(Room).filter(Room.id == room_id, Room.is_active == True).first()
    if not room:
        return JSONResponse({"error": "房间不存在"}, status_code=404)

    day_start = datetime.strptime(f"{date} 08:00", "%Y-%m-%d %H:%M")
    day_end = datetime.strptime(f"{date} 22:00", "%Y-%m-%d %H:%M")

    closed_periods = db.query(ClosedPeriod).filter(ClosedPeriod.date == date).all()
    closed_ranges = []
    for cp in closed_periods:
        cp_s = datetime.strptime(f"{date} {cp.start_time}", "%Y-%m-%d %H:%M")
        cp_e = datetime.strptime(f"{date} {cp.end_time}", "%Y-%m-%d %H:%M")
        closed_ranges.append((cp_s, cp_e, cp.reason))

    slots = []
    current = day_start
    while current < day_end:
        slot_end = current + timedelta(minutes=30)
        slot_status = "available"
        closed_reason = ""

        for cp_s, cp_e, reason in closed_ranges:
            if current < cp_e and slot_end > cp_s:
                slot_status = "closed"
                closed_reason = reason
                break

        overlap_count = 0
        if slot_status == "available":
            active_bookings = db.query(Booking).filter(
                Booking.room_id == room_id,
                Booking.status == "active",
            ).all()
            for b in active_bookings:
                if current < b.end_time and slot_end > b.start_time:
                    overlap_count += 1
            if overlap_count >= room.capacity:
                slot_status = "full"

        slots.append({
            "start": current.strftime("%H:%M"),
            "end": slot_end.strftime("%H:%M"),
            "status": slot_status,
            "closed_reason": closed_reason,
            "booked": overlap_count,
            "capacity": room.capacity
        })
        current = slot_end

    return JSONResponse({
        "room": {"id": room.id, "name": room.name, "capacity": room.capacity},
        "date": date,
        "slots": slots
    })


@app.get("/api/logs")
async def get_all_logs(
    limit: int = 100,
    db: Session = Depends(get_db)
):
    logs = db.query(BookingLog).order_by(BookingLog.timestamp.desc()).limit(limit).all()
    result = []
    for log in logs:
        result.append({
            "id": log.id,
            "booking_id": log.booking_id,
            "action": log.action,
            "details": log.details,
            "timestamp": log.timestamp.isoformat() if log.timestamp else None
        })
    return JSONResponse({"logs": result})


@app.post("/waitlist/join")
async def join_waitlist(
    user_id: str = Form(...),
    user_name: str = Form(...),
    room_id: int = Form(...),
    selected_date: str = Form(...),
    start_hour: str = Form(...),
    duration_minutes: int = Form(...),
    db: Session = Depends(get_db)
):
    if duration_minutes < MIN_BOOKING_MINUTES or duration_minutes > MAX_BOOKING_MINUTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"候补时长必须在 {MIN_BOOKING_MINUTES}-{MAX_BOOKING_MINUTES} 分钟之间"
        )

    if duration_minutes % 30 != 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="候补时长必须是 30 分钟的整数倍"
        )

    start_time = datetime.strptime(f"{selected_date} {start_hour}", "%Y-%m-%d %H:%M")
    end_time = start_time + timedelta(minutes=duration_minutes)

    now = datetime.now()
    if start_time < now:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不能候补过去的时间"
        )

    day_start = datetime.strptime(f"{selected_date} 08:00", "%Y-%m-%d %H:%M")
    day_end = datetime.strptime(f"{selected_date} 22:00", "%Y-%m-%d %H:%M")
    if start_time < day_start or end_time > day_end:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="候补时间必须在 08:00-22:00 之间"
        )

    validation_error = validate_waitlist_entry(db, user_id, room_id, start_time, end_time, selected_date)
    if validation_error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=validation_error
        )

    duplicate = db.query(WaitlistEntry).filter(
        WaitlistEntry.room_id == room_id,
        WaitlistEntry.user_id == user_id,
        WaitlistEntry.start_time == start_time,
        WaitlistEntry.end_time == end_time,
        WaitlistEntry.status == "waiting",
    ).first()
    if duplicate:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="您已在该时段的候补队列中"
        )

    entry = WaitlistEntry(
        room_id=room_id,
        user_id=user_id,
        user_name=user_name,
        start_time=start_time,
        end_time=end_time,
        status="waiting",
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)

    position = get_waitlist_position(db, entry)

    return RedirectResponse(
        url=f"/history?user_id={user_id}&user_name={user_name}&waitlist_msg=已加入候补，排队位置：第{position}位",
        status_code=status.HTTP_303_SEE_OTHER
    )


@app.post("/waitlist/cancel/{entry_id}")
async def cancel_waitlist(
    entry_id: int,
    user_id: str = Form(...),
    user_name: str = Form(...),
    db: Session = Depends(get_db)
):
    entry = db.query(WaitlistEntry).filter(WaitlistEntry.id == entry_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="候补记录不存在")

    if entry.user_id != user_id:
        raise HTTPException(status_code=403, detail="只能取消自己的候补")

    if entry.status != "waiting":
        raise HTTPException(status_code=400, detail="该候补已处理（已补位/已取消/已过期）")

    entry.status = "cancelled"
    db.commit()

    return RedirectResponse(
        url=f"/history?user_id={user_id}&user_name={user_name}&waitlist_msg=候补已取消",
        status_code=status.HTTP_303_SEE_OTHER
    )


@app.get("/waitlist/my", response_class=HTMLResponse)
async def my_waitlist(
    request: Request,
    user_id: str = "reader1",
    user_name: str = "读者1",
    db: Session = Depends(get_db)
):
    entries = db.query(WaitlistEntry).filter(
        WaitlistEntry.user_id == user_id,
    ).order_by(WaitlistEntry.created_at.desc()).all()

    result = []
    for entry in entries:
        pos = get_waitlist_position(db, entry) if entry.status == "waiting" else None
        result.append({
            "entry": entry,
            "position": pos,
        })

    return templates.TemplateResponse("history.html", {
        "request": request,
        "waitlist_entries": result,
        "user_id": user_id,
        "user_name": user_name,
        "show_waitlist_tab": True,
    })


@app.get("/admin/waitlist", response_class=HTMLResponse)
async def admin_waitlist(
    request: Request,
    room_id: Optional[int] = None,
    db: Session = Depends(get_db)
):
    rooms = db.query(Room).order_by(Room.id).all()

    query = db.query(WaitlistEntry).filter(WaitlistEntry.status == "waiting")
    if room_id:
        query = query.filter(WaitlistEntry.room_id == room_id)
    entries = query.order_by(WaitlistEntry.room_id, WaitlistEntry.start_time, WaitlistEntry.created_at).all()

    result = []
    for entry in entries:
        pos = get_waitlist_position(db, entry)
        result.append({
            "entry": entry,
            "position": pos,
        })

    return templates.TemplateResponse("admin.html", {
        "request": request,
        "rooms": rooms,
        "waitlist_entries": result,
        "filter_room_id": room_id,
        "show_waitlist_tab": True,
        "today": date.today().strftime("%Y-%m-%d"),
    })


@app.get("/admin/fill-logs", response_class=HTMLResponse)
async def admin_fill_logs(
    request: Request,
    db: Session = Depends(get_db)
):
    logs = db.query(WaitlistFillLog).order_by(WaitlistFillLog.created_at.desc()).limit(200).all()

    return templates.TemplateResponse("admin.html", {
        "request": request,
        "fill_logs": logs,
        "show_fill_logs_tab": True,
        "today": date.today().strftime("%Y-%m-%d"),
    })


@app.get("/api/waitlist/{room_id}")
async def get_waitlist_api(
    room_id: int,
    db: Session = Depends(get_db)
):
    entries = db.query(WaitlistEntry).filter(
        WaitlistEntry.room_id == room_id,
        WaitlistEntry.status == "waiting",
    ).order_by(WaitlistEntry.created_at).all()

    result = []
    for entry in entries:
        pos = get_waitlist_position(db, entry)
        result.append({
            "id": entry.id,
            "user_id": entry.user_id,
            "user_name": entry.user_name,
            "start_time": entry.start_time.isoformat(),
            "end_time": entry.end_time.isoformat(),
            "position": pos,
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
        })
    return JSONResponse({"waitlist": result})

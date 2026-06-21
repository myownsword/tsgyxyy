from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey, Text, Float
from sqlalchemy.orm import relationship
from datetime import datetime
from database import Base


class Room(Base):
    __tablename__ = "rooms"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    capacity = Column(Integer, nullable=False, default=1)
    is_active = Column(Boolean, default=True)
    description = Column(String(500), nullable=True)

    bookings = relationship("Booking", back_populates="room")
    waitlist_entries = relationship("WaitlistEntry", back_populates="room", order_by="WaitlistEntry.created_at")


class Booking(Base):
    __tablename__ = "bookings"

    id = Column(Integer, primary_key=True, index=True)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False)
    user_id = Column(String(100), nullable=False, index=True)
    user_name = Column(String(100), nullable=False)
    start_time = Column(DateTime, nullable=False, index=True)
    end_time = Column(DateTime, nullable=False, index=True)
    status = Column(String(20), nullable=False, default="active")
    created_at = Column(DateTime, default=datetime.now)

    room = relationship("Room", back_populates="bookings")
    logs = relationship("BookingLog", back_populates="booking")


class BookingLog(Base):
    __tablename__ = "booking_logs"

    id = Column(Integer, primary_key=True, index=True)
    booking_id = Column(Integer, ForeignKey("bookings.id"), nullable=False)
    action = Column(String(50), nullable=False)
    details = Column(Text, nullable=True)
    timestamp = Column(DateTime, default=datetime.now)

    booking = relationship("Booking", back_populates="logs")


class ClosedPeriod(Base):
    __tablename__ = "closed_periods"

    id = Column(Integer, primary_key=True, index=True)
    date = Column(String(10), nullable=False, index=True)
    start_time = Column(String(5), nullable=False)
    end_time = Column(String(5), nullable=False)
    reason = Column(String(200), nullable=True)


class WaitlistEntry(Base):
    __tablename__ = "waitlist_entries"

    id = Column(Integer, primary_key=True, index=True)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False)
    user_id = Column(String(100), nullable=False, index=True)
    user_name = Column(String(100), nullable=False)
    start_time = Column(DateTime, nullable=False, index=True)
    end_time = Column(DateTime, nullable=False, index=True)
    status = Column(String(20), nullable=False, default="waiting")
    created_at = Column(DateTime, default=datetime.now)

    room = relationship("Room", back_populates="waitlist_entries")
    fill_logs = relationship("WaitlistFillLog", back_populates="waitlist_entry")


class WaitlistFillLog(Base):
    __tablename__ = "waitlist_fill_logs"

    id = Column(Integer, primary_key=True, index=True)
    waitlist_id = Column(Integer, ForeignKey("waitlist_entries.id"), nullable=False)
    room_id = Column(Integer, nullable=False)
    user_id = Column(String(100), nullable=False)
    user_name = Column(String(100), nullable=False)
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=False)
    success = Column(Boolean, nullable=False, default=False)
    reason = Column(Text, nullable=True)
    booking_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.now)

    waitlist_entry = relationship("WaitlistEntry", back_populates="fill_logs")

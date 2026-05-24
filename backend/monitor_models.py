from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime, JSON, ForeignKey, Text
from db import Base


class WatchItem(Base):
    __tablename__ = "watch_items"

    id                 = Column(Integer, primary_key=True, index=True)
    trademark_name     = Column(String, nullable=False)
    holder_name        = Column(String, default="")
    nice_classes       = Column(JSON, default=list)   # ["1","2","35"]
    offices            = Column(JSON, default=list)   # ["RO","EM"]
    notification_email = Column(String, nullable=False)
    frequency          = Column(String, default="weekly")  # daily / weekly / monthly
    active             = Column(Boolean, default=True)
    created_at         = Column(DateTime, default=datetime.utcnow)
    last_checked_at    = Column(DateTime, nullable=True)


class SeenTrademark(Base):
    __tablename__ = "seen_trademarks"

    id               = Column(Integer, primary_key=True, index=True)
    watch_item_id    = Column(Integer, ForeignKey("watch_items.id", ondelete="CASCADE"))
    st13             = Column(String, nullable=False)
    tm_name          = Column(String, default="")
    tm_office        = Column(String, default="")
    similarity_level = Column(String, default="")   # conflict / similar
    application_date = Column(String, default="")
    first_seen_at    = Column(DateTime, default=datetime.utcnow)


class AlertLog(Base):
    __tablename__ = "alert_logs"

    id             = Column(Integer, primary_key=True, index=True)
    watch_item_id  = Column(Integer, ForeignKey("watch_items.id", ondelete="CASCADE"))
    sent_at        = Column(DateTime, default=datetime.utcnow)
    num_new_marks  = Column(Integer, default=0)
    email_to       = Column(String, default="")
    status         = Column(String, default="sent")   # sent / error / skipped
    error_msg      = Column(Text, default="")

from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime, JSON, ForeignKey, Text, LargeBinary, UniqueConstraint
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
    reference_image    = Column(String, nullable=True)  # nume fișier logo de referință (import Excel), pentru comparație vizuală
    image_hash         = Column(String, nullable=True)  # sha256 al imaginii de referință — identitate vizuală, pentru deduplicare la import
    application_number = Column(String, nullable=True)  # (210) Nr. Depozit / Nr. Cerere
    registration_number = Column(String, nullable=True)  # (111) Nr. Înregistrare
    filing_date        = Column(String, nullable=True)  # (220) Data depunerii, "YYYY-MM-DD"
    publication_date   = Column(String, nullable=True)  # (442) Data publicării, "YYYY-MM-DD"
    representative_name = Column(String, nullable=True)  # (740) Reprezentant — doar numele
    active             = Column(Boolean, default=True)
    created_at         = Column(DateTime, default=datetime.utcnow)
    last_checked_at    = Column(DateTime, nullable=True)


class BulletinMark(Base):
    """O marcă publicată într-un buletin OSIM/EUIPO, cu TOATE informațiile aduse despre ea
    (câmpurile din buletin + detaliile din TMview/API-ul EUIPO), păstrate în baza de date ca să
    poată fi consultate ulterior, fără să mai descărcăm buletinul. Coloanele de sus sunt doar
    pentru căutare/sortare; marca completă e în `data`."""
    __tablename__ = "bulletin_marks"
    __table_args__ = (UniqueConstraint("source", "application_number", "bulletin_date", name="uq_bulletin_mark"),)

    id                 = Column(Integer, primary_key=True, index=True)
    source             = Column(String, nullable=False, index=True)       # "osim" / "euipo"
    bulletin_date      = Column(String, nullable=False, index=True)       # "YYYY-MM-DD" — data buletinului
    application_number = Column(String, nullable=False, index=True)      # (210)
    position           = Column(Integer, default=0)                       # ordinea în buletin
    trademark_name     = Column(String, default="", index=True)           # (541)
    applicant          = Column(String, default="")                       # (731)
    representative     = Column(String, default="")                       # (740)
    nice_classes       = Column(JSON, default=list)                       # (511)
    status             = Column(String, default="")
    application_date   = Column(String, default="")                       # (220)
    publication_date   = Column(String, default="")                       # (442)
    detail_enriched    = Column(Boolean, default=False)                   # are detaliile complete (produse/servicii etc.)
    data               = Column(JSON, default=dict)                       # marca completă, cu toate câmpurile
    first_saved_at     = Column(DateTime, default=datetime.utcnow)
    updated_at         = Column(DateTime, default=datetime.utcnow)


class SavedCheck(Base):
    """O verificare de disponibilitate (denumire + clase + teritorii) cu rezultatele ei complete,
    salvată automat ca s-o putem redeschide fără să repetăm căutarea, și ștearsă când nu mai e
    nevoie de ea. La re-verificarea aceleiași combinații se actualizează înregistrarea existentă."""
    __tablename__ = "saved_checks"

    id             = Column(Integer, primary_key=True, index=True)
    check_key      = Column(String, unique=True, index=True, nullable=False)  # DENUMIRE|clase|teritorii
    trademark_name = Column(String, nullable=False, index=True)
    nice_classes   = Column(JSON, default=list)
    offices        = Column(JSON, default=list)
    include_expired = Column(Boolean, default=True)
    total_found    = Column(Integer, default=0)
    risky_count    = Column(Integer, default=0)      # risc ridicat / foarte ridicat
    similar_count  = Column(Integer, default=0)
    ended_count    = Column(Integer, default=0)      # încheiate / anulate / respinse
    expired_count  = Column(Integer, default=0)
    source         = Column(String, default="")
    note           = Column(String, default="")      # notă liberă (client, dosar etc.)
    check_count    = Column(Integer, default=1)      # de câte ori a fost verificată combinația
    data           = Column(JSON, default=dict)      # rezultatul complet, exact cum îl afișează pagina
    created_at     = Column(DateTime, default=datetime.utcnow)
    updated_at     = Column(DateTime, default=datetime.utcnow)


class BulletinImage(Base):
    """Imaginea unei mărci din buletin — pentru OSIM fișierul extras din PDF dispare odată cu
    containerul, iar pentru EUIPO imaginea se cere altfel live din API."""
    __tablename__ = "bulletin_images"

    source             = Column(String, primary_key=True)
    application_number = Column(String, primary_key=True)
    content_type       = Column(String, default="image/png")
    data               = Column(LargeBinary, nullable=False)
    saved_at           = Column(DateTime, default=datetime.utcnow)


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

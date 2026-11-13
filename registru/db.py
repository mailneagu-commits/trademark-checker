"""
Baza de date a registrului public — PostgreSQL pe Railway (DATABASE_URL), SQLite local.
Separată de baza aplicației interne de monitorizare: site-ul public nu are acces la
portofoliul clienților.
"""
import os
import re
import unicodedata
from datetime import datetime

from sqlalchemy import (Boolean, Column, DateTime, Integer, JSON, LargeBinary, String, Text,
                        create_engine, text)
from sqlalchemy.orm import declarative_base, sessionmaker

_url = os.environ.get("DATABASE_URL", "")
if _url.startswith("postgres://"):
    _url = _url.replace("postgres://", "postgresql://", 1)
if not _url:
    _url = "sqlite:///" + os.environ.get("REGISTRU_DB_PATH",
                                         os.path.join(os.path.dirname(__file__), "registru.db"))

IS_PG = _url.startswith("postgresql")
engine = create_engine(_url, pool_pre_ping=True,
                       **({} if IS_PG else {"connect_args": {"check_same_thread": False}}))
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def norm(s) -> str:
    """Text comparabil pentru căutare: fără diacritice, litere mici, spații simple."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    # Denumirea rămâne întreagă; doar forma de căutare (index B-tree în PostgreSQL, max ≈2,7 kB
    # pe intrare) e limitată, ca un bloc PDF stricat să nu blocheze salvarea mărcii.
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9&@+ ]+", " ", s)).strip()[:1000]


class Mark(Base):
    """O marcă OSIM, consolidată din toate aparițiile ei în BOPI (cerere publicată, admisă,
    înregistrată, respinsă). Cheia e nr. de depozit compact: „M202609829”."""
    __tablename__ = "marks"

    app_number       = Column(String, primary_key=True)
    name             = Column(Text, default="")
    name_norm        = Column(Text, default="", index=True)
    mark_type        = Column(String, default="")       # Verbală / Figurativă / Combinată
    status           = Column(String, default="", index=True)
    status_date      = Column(String, default="")       # YYYY-MM-DD — buletinul care a dat starea
    filing_date      = Column(String, default="", index=True)
    publication_date = Column(String, default="", index=True)
    reg_number       = Column(String, default="", index=True)
    reg_date         = Column(String, default="", index=True)
    expiry_date      = Column(String, default="", index=True)   # calculată: depozit + 10 ani / ultima reînnoire
    tmview_checked   = Column(String, default="")              # data căutării denumirii în TMview (mărci reînnoite vechi)
    tm_status        = Column(String, default="")              # starea curentă din TMview (cereri în curs): „Application opposed”…
    tm_status_date   = Column(String, default="")
    tm_status_checked = Column(String, default="")             # data ultimei verificări a stării în TMview
    holder           = Column(Text, default="")
    holder_norm      = Column(Text, default="", index=True)
    holder_address   = Column(Text, default="")
    representative   = Column(Text, default="")
    rep_norm         = Column(Text, default="", index=True)
    representative_address = Column(Text, default="")
    classes          = Column(JSON, default=list)
    goods            = Column(JSON, default=dict)       # {"35": "Publicitate, ..."}
    vienna           = Column(Text, default="")
    colors           = Column(Text, default="")
    disclaimer       = Column(Text, default="")       # declarația de neinvocare a unui drept exclusiv
    has_image        = Column(Boolean, default=False)
    events           = Column(JSON, default=list)       # [{"date", "bopi", "chapter"}]
    updated_at       = Column(DateTime, default=datetime.utcnow)


class MarkImage(Base):
    __tablename__ = "mark_images"

    app_number   = Column(String, primary_key=True)
    content_type = Column(String, default="image/webp")
    data         = Column(LargeBinary, nullable=False)


class MarkImageHQ(Base):
    """Logo-ul original din TMview (fără fundalul / modelele din paginile BOPI). Tabel separat: încărcarea
    din buletine nu-l atinge; /api/image îl preferă când există."""
    __tablename__ = "mark_images_hq"

    app_number   = Column(String, primary_key=True)
    content_type = Column(String, default="image/webp")
    data         = Column(LargeBinary, nullable=True)       # None = TMview nu are logo / încercare eșuată
    status       = Column(String, default="")               # ok / none / fail
    fetched_at   = Column(DateTime, default=datetime.utcnow)


class CourtCase(Base):
    """Un dosar din Portalul instanțelor (portal.just.ro) cu obiect „marcă” — vezi courts.py."""
    __tablename__ = "court_cases"

    id          = Column(String, primary_key=True)         # „TribunalulBUCURESTI|1350/3/2022”
    numar       = Column(String, default="")
    institutie  = Column(String, default="")
    data        = Column(String, default="")
    departament = Column(String, default="")
    stadiu      = Column(String, default="")
    obiect      = Column(Text, default="")
    modificat   = Column(String, default="")
    parti       = Column(JSON, default=list)
    sedinte     = Column(JSON, default=list)
    cai_atac    = Column(JSON, default=list)
    mark_regs   = Column(JSON, default=list)               # nr. de marcă din obiect
    mark_apps   = Column(JSON, default=list)               # nr. de depozit din obiect
    updated_at  = Column(DateTime, default=datetime.utcnow)


class CourtLink(Base):
    __tablename__ = "court_links"

    case_id    = Column(String, primary_key=True)
    app_number = Column(String, primary_key=True, index=True)
    kind       = Column(String, default="")                # „nr” (sigur) / „titular” (de verificat)


class SourceFile(Base):
    """Un PDF BOPI procesat (zilnic / lunar / cereri admise) — ca reluarea să nu-l repete."""
    __tablename__ = "source_files"

    url           = Column(String, primary_key=True)
    kind          = Column(String, default="")          # daily / monthly / admise
    bulletin_date = Column(String, default="", index=True)
    label         = Column(String, default="")
    status        = Column(String, default="")          # ok / missing / error
    marks         = Column(Integer, default=0)
    error         = Column(Text, default="")
    processed_at  = Column(DateTime, default=datetime.utcnow)


class MarkChange(Base):
    """Un rând din tabelele buletinului lunar (transfer, reînnoire, radiere, licență, limitare,
    modificarea reprezentării). Se păstrează și pentru mărcile care nu sunt încă în bază (mai vechi
    de 2022), ca să poată fi aplicate când va fi adăugată arhiva."""
    __tablename__ = "mark_changes"

    id            = Column(Integer, primary_key=True)
    kind          = Column(String, default="", index=True)
    app_number    = Column(String, default="", index=True)
    reg_number    = Column(String, default="", index=True)
    old_value     = Column(Text, default="")
    new_value     = Column(Text, default="")
    expiry        = Column(String, default="")
    title         = Column(String, default="")
    bulletin_date = Column(String, default="", index=True)
    label         = Column(String, default="")
    applied       = Column(Boolean, default=False)
    name          = Column(Text, default="")            # „Denumire marcă” din tabel, când există
    representative = Column(Text, default="")           # (740) mandatarul, unde tabelul îl dă (reînnoiri)
    renewal_no    = Column(Integer, default=0)          # a câta reînnoire („008649(5R)” → 5), 0 = netipărit


class Lead(Base):
    """O solicitare trimisă de un vizitator: informații despre mărci bifate / cercetare."""
    __tablename__ = "leads"

    id         = Column(Integer, primary_key=True)
    kind       = Column(String, default="info")         # info / cercetare
    name       = Column(String, default="")
    email      = Column(String, default="")
    phone      = Column(String, default="")
    message    = Column(Text, default="")
    marks      = Column(JSON, default=list)             # nr. de depozit bifate
    created_at = Column(DateTime, default=datetime.utcnow)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _fix_disclaimers():
    """Corectează mărcile încărcate înainte ca declarația art. 22 să fie separată de denumire."""
    from bopi_parser import split_disclaimer
    db = SessionLocal()
    try:
        n = 0
        from sqlalchemy import or_
        pats = ("%conformitate%", "%Conform%", "%Potrivit%", "%La data de%", "%prevederil%", "%Prin Hot%",
                "%S-A R%", "%aplicabile%", "%La depunerea%", "%Prin adresa%", "%Prin scris%", "%Prin răsp%",
                "%Solicitantul%", "%Titularul%", "%prioritatea%", "%cazul dumneav%")
        q = db.query(Mark).filter(or_(*[Mark.name.like(p) for p in pats]))
        for m in q:
            name, disc = split_disclaimer(m.name)
            if disc:
                m.name, m.name_norm = name, norm(name)
                if disc not in (m.disclaimer or ""):
                    m.disclaimer = (m.disclaimer + " " + disc).strip() if m.disclaimer else disc
                m.mark_type = ("Combinată" if m.has_image and m.name else
                               "Figurativă" if m.has_image else "Verbală")
                n += 1
        db.commit()
        if n:
            print(f"[DB] Declarații separate de denumire: {n}")
    finally:
        db.close()


def _ensure_columns():
    from sqlalchemy import inspect
    have_mc = {c["name"] for c in inspect(engine).get_columns("mark_changes")}
    if "name" not in have_mc:
        with engine.begin() as c:
            c.execute(text("ALTER TABLE mark_changes ADD COLUMN name TEXT DEFAULT ''"))
        print("[DB] Coloana mark_changes.name adăugată")
    if "representative" not in have_mc:
        with engine.begin() as c:
            c.execute(text("ALTER TABLE mark_changes ADD COLUMN representative TEXT DEFAULT ''"))
        print("[DB] Coloana mark_changes.representative adăugată")
    if "renewal_no" not in have_mc:
        with engine.begin() as c:
            c.execute(text("ALTER TABLE mark_changes ADD COLUMN renewal_no INTEGER DEFAULT 0"))
        print("[DB] Coloana mark_changes.renewal_no adăugată")
    have = {c["name"] for c in inspect(engine).get_columns("marks")}
    for col in ("tm_status", "tm_status_date", "tm_status_checked"):
        if col not in have:
            with engine.begin() as c:
                c.execute(text(f"ALTER TABLE marks ADD COLUMN {col} VARCHAR DEFAULT ''"))
            print(f"[DB] Coloana marks.{col} adăugată")
    if "tmview_checked" not in have:
        with engine.begin() as c:
            c.execute(text("ALTER TABLE marks ADD COLUMN tmview_checked VARCHAR DEFAULT ''"))
    if "expiry_date" not in have:
        with engine.begin() as c:
            c.execute(text("ALTER TABLE marks ADD COLUMN expiry_date VARCHAR DEFAULT ''"))
            c.execute(text("CREATE INDEX IF NOT EXISTS ix_marks_expiry_date ON marks (expiry_date)"))
        from ingest import expiry_of
        db = SessionLocal()
        try:
            for m in db.query(Mark).filter(Mark.status == "Înregistrată").yield_per(1000):
                m.expiry_date = expiry_of(m)
            db.commit()
        finally:
            db.close()
        print("[DB] Coloana marks.expiry_date adăugată și completată")


def _reprocess_old_changes():
    """Reînnoirile citite înainte să păstrăm clasele (old_value gol) — tabelele lunare respective
    se reprocesează o dată, ca mărcile reînnoite fără marcă în bază să intre cu clasele lor."""
    db = SessionLocal()
    try:
        stale = db.query(MarkChange.label).filter(
            ((MarkChange.kind == "renewal") & (MarkChange.old_value == "")) |
            ((MarkChange.kind == "limitation") & (MarkChange.new_value == ""))).distinct().all()
        labels = [l for (l,) in stale]
        if labels:
            n = db.query(SourceFile).filter(SourceFile.kind == "changes", SourceFile.label.in_(labels))\
                .delete(synchronize_session=False)
            db.commit()
            print(f"[DB] Tabele de modificări de reprocesat (clase la reînnoiri): {n}")
    finally:
        db.close()


def _reprocess_for_history():
    """O singură dată: buletinele citite înainte ca evenimentele să rețină titularul, denumirea și
    clasele de la data lor se recitesc, ca istoricul mărcilor să fie complet."""
    marker = "migration:history-v1"
    db = SessionLocal()
    try:
        if db.get(SourceFile, marker):
            return
        n = db.query(SourceFile).filter(SourceFile.kind.in_(("daily", "admise", "monthly", "changes")),
                                        SourceFile.status == "ok").delete(synchronize_session=False)
        db.add(SourceFile(url=marker, kind="migration", status="ok", label="istoric v1"))
        db.commit()
        print(f"[DB] Buletine de recitit pentru istoric: {n}")
    finally:
        db.close()


def _reprocess_for_names():
    """O singură dată: buletinele cu fișe (zilnice, cereri admise, lunare) se recitesc cu parserul
    corectat — denumiri întregi (fără limita de 400), rândurile dintr-o literă / din cifre păstrate
    („1”, „G”, „BAG BANII 2082”), fișele de pe paginile cu „Conform Anexa” nemaisărite, notificările
    OSIM scoase din denumire. Tabelele de modificări nu se recitesc. Denumirile tăiate la 400 de
    caractere se golesc, ca buletinul recitit să le completeze."""
    marker = "migration:names-v2"
    db = SessionLocal()
    try:
        if db.get(SourceFile, marker):
            return
        from sqlalchemy import func
        n = 0
        for m in db.query(Mark).filter(func.length(Mark.name) >= 400):
            m.name, m.name_norm = "", ""
            n += 1
        k = db.query(SourceFile).filter(SourceFile.kind.in_(("daily", "admise", "monthly")),
                                        SourceFile.status == "ok").delete(synchronize_session=False)
        db.add(SourceFile(url=marker, kind="migration", status="ok", label="denumiri v2"))
        db.commit()
        print(f"[DB] Denumiri tăiate golite: {n}; buletine de recitit: {k}")
    finally:
        db.close()


def _reprocess_changes_for_representatives():
    """O singură dată: tabelele de modificări se recitesc, ca mandatarul din lista de reînnoiri (740)
    să ajungă în fișa și istoricul mărcilor care n-aveau reprezentant la depunere."""
    marker = "migration:change-rep-v1"
    db = SessionLocal()
    try:
        if db.get(SourceFile, marker):
            return
        k = db.query(SourceFile).filter(SourceFile.kind == "changes", SourceFile.status == "ok")\
            .delete(synchronize_session=False)
        db.add(SourceFile(url=marker, kind="migration", status="ok", label="mandatari din reînnoiri v1"))
        db.commit()
        print(f"[DB] Tabele de modificări de recitit (mandatari): {k}")
    finally:
        db.close()


def _flag_limited_goods():
    """O singură dată: limitările recitite (migrarea mandatarilor) au suprascris lista finală de produse
    din fișa „Mărci înregistrate”. Mărcile cu clase fără produse și o limitare primesc „|gsneed”:
    produsele actuale se iau din TMview (job-ul tmview_goods)."""
    marker = "migration:limited-goods-v1"
    db = SessionLocal()
    try:
        if db.get(SourceFile, marker):
            return
        n = 0
        for m in db.query(Mark).filter(~Mark.app_number.like("R%")).yield_per(2000):
            if any((e.get("chapter") or "").startswith("Limitare") for e in (m.events or [])) and \
                    any(not (m.goods or {}).get(str(c)) for c in (m.classes or [])):
                tc = (m.tmview_checked or "").replace("|gs2", "")
                m.tmview_checked = tc + "|gsneed"
                n += 1
        db.add(SourceFile(url=marker, kind="migration", status="ok", label="produse după limitare v1"))
        db.commit()
        print(f"[DB] Mărci cu produse de refăcut din TMview (limitări): {n}")
    finally:
        db.close()


def _fix_old_filing_expiry():
    """O singură dată: expirarea mărcilor înregistrate cu depozit mai vechi de 10 ani față de
    înregistrare (mărci UE transformate), rămasă goală de vechea regulă de calcul."""
    marker = "migration:old-filing-expiry-v1"
    db = SessionLocal()
    try:
        if db.get(SourceFile, marker):
            return
        from ingest import expiry_of
        n = 0
        for m in db.query(Mark).filter(Mark.status == "Înregistrată", Mark.expiry_date == "").all():
            e = expiry_of(m)
            if e:
                m.expiry_date, n = e, n + 1
        db.add(SourceFile(url=marker, kind="migration", status="ok", label="expirare mărci UE transformate v1"))
        db.commit()
        print(f"[DB] Expirări completate (depozit vechi): {n}")
    finally:
        db.close()


def _flag_missing_publication():
    """O singură dată: mărcile publicate înainte de 2022 (intrate în bază la admitere / înregistrare)
    n-au data publicării — o luăm din TMview („|pubneed”), până ajunge citirea arhivei la ele."""
    marker = "migration:missing-publication-v1"
    db = SessionLocal()
    try:
        if db.get(SourceFile, marker):
            return
        n = 0
        for m in db.query(Mark).filter(~Mark.app_number.like("R%"), Mark.publication_date == "").all():
            if "|pubneed" not in (m.tmview_checked or ""):
                m.tmview_checked, n = (m.tmview_checked or "").replace("|gs3", "") + "|pubneed", n + 1
        db.add(SourceFile(url=marker, kind="migration", status="ok", label="data publicării din TMview v1"))
        db.commit()
        print(f"[DB] Mărci fără data publicării (de completat din TMview): {n}")
    finally:
        db.close()


def _flag_archive_publication():
    """O singură dată, după citirea arhivei: mărcile din arhivă (doar „Mărci înregistrate” în
    buletinele lunare) primesc din TMview data publicării și data exactă a înregistrării."""
    marker = "migration:archive-publication-v2"
    db = SessionLocal()
    try:
        if db.get(SourceFile, marker):
            return
        n = 0
        for m in db.query(Mark).filter(~Mark.app_number.like("R%"), Mark.publication_date == "").yield_per(5000):
            tc = (m.tmview_checked or "").replace("|gs3", "").replace("|gsf", "")
            if "|pubneed" not in tc:
                tc += "|pubneed"
            if tc != m.tmview_checked:
                m.tmview_checked, n = tc, n + 1
        db.add(SourceFile(url=marker, kind="migration", status="ok", label="date publicare arhivă din TMview v2"))
        db.commit()
        print(f"[DB] Mărci din arhivă de completat din TMview (publicare / înregistrare): {n}")
    finally:
        db.close()


def _fix_legacy_diacritics():
    """O singură dată: diacriticele fonturilor vechi BOPI („Õi”, „b|uturi”, „protecÛie”) refăcute în
    toate mărcile deja citite; separatorul „˜˜˜˜˜˜˜” scos."""
    marker = "migration:legacy-diacritics-v3"
    db = SessionLocal()
    try:
        if db.get(SourceFile, marker):
            return
        from bopi_parser import fix_cp1252_goods, fix_record, has_legacy_diacritics, _strings
        fields = ("name", "holder", "holder_address", "representative", "representative_address",
                  "disclaimer", "colors")
        n = 0
        for m in db.query(Mark).yield_per(2000):
            vals = {f: getattr(m, f) or "" for f in fields}
            texts = list(vals.values()) + list(_strings(m.goods or {})) + list(_strings(m.events or []))
            if not (has_legacy_diacritics(*texts) or any("˜" in t for t in texts) or
                    any(fix_cp1252_goods(t) != t for t in (m.goods or {}).values())):
                continue
            legacy = has_legacy_diacritics(*texts)
            for f, v in vals.items():
                setattr(m, f, fix_record(v, legacy))
            m.goods = {c: fix_cp1252_goods(t) for c, t in fix_record(dict(m.goods or {}), legacy).items()}
            m.events = fix_record(list(m.events or []), legacy)
            m.name_norm, m.holder_norm, m.rep_norm = norm(m.name), norm(m.holder), norm(m.representative)
            n += 1
            if n % 2000 == 0:
                db.commit()
        db.add(SourceFile(url=marker, kind="migration", status="ok", label="diacritice fonturi vechi v1"))
        db.commit()
        print(f"[DB] Mărci cu diacritice refăcute: {n}")
    finally:
        db.close()


def _archive_needfix():
    """O singură dată (12.11.2026): utilizatorul renunță la data publicării cererii la mărcile din arhivă.
    Din coada TMview rămân doar mărcile cu alte câmpuri lipsă („|needfix”)."""
    marker = "migration:archive-needfix-v1"
    db = SessionLocal()
    try:
        if db.get(SourceFile, marker):
            return
        n = 0
        for m in db.query(Mark).filter(~Mark.app_number.like("R%")).yield_per(5000):
            tc = (m.tmview_checked or "").replace("|pubneed", "")
            if "|gs3" not in tc and (
                    any(not (m.goods or {}).get(str(c)) for c in (m.classes or [])) or
                    not m.holder_address or m.holder_address == m.holder or not m.mark_type or
                    (m.status == "Înregistrată" and not m.reg_date)):
                if "|needfix" not in tc:
                    tc += "|needfix"
                n += 1
            if tc != (m.tmview_checked or ""):
                m.tmview_checked = tc
        db.add(SourceFile(url=marker, kind="migration", status="ok", label="arhivă: doar câmpuri lipsă v1"))
        db.commit()
        print(f"[DB] Mărci din arhivă cu câmpuri de completat din TMview: {n}")
    finally:
        db.close()


def _retry_renewed_logos():
    """O singură dată: mărcile reînnoite vechi fără logo, încercate în timpul blocajelor TMview, se
    reîncearcă (ELECTRA 3R29570 avea logo în TMview, dar apărea „Verbală”, fără imagine)."""
    marker = "migration:retry-renewed-logos-v1"
    db = SessionLocal()
    try:
        if db.get(SourceFile, marker):
            return
        n = 0
        for m in db.query(Mark).filter(Mark.app_number.like("R%"), Mark.has_image == False,  # noqa: E712
                                       Mark.tmview_checked.like("%|img%")).all():
            m.tmview_checked = m.tmview_checked.replace("|img", "")
            n += 1
        db.add(SourceFile(url=marker, kind="migration", status="ok", label="logo mărci reînnoite v1"))
        db.commit()
        print(f"[DB] Mărci reînnoite fără logo, de reîncercat în TMview: {n}")
    finally:
        db.close()


def _recompute_reps():
    """O singură dată: reprezentantul actual al fiecărei mărci, din cel mai recent document BOPI
    (reînnoirile pierdeau mandatarul la unirea cu fișa din arhivă — ex. Beauty Mania)."""
    marker = "migration:current-rep-v1"
    db = SessionLocal()
    try:
        if db.get(SourceFile, marker):
            return
        from ingest import _current_rep
        n = 0
        for m in db.query(Mark).yield_per(2000):
            before = m.representative
            _current_rep(m)
            if m.representative != before:
                n += 1
        db.add(SourceFile(url=marker, kind="migration", status="ok", label="reprezentant actual v1"))
        db.commit()
        print(f"[DB] Mărci cu reprezentantul actualizat: {n}")
    finally:
        db.close()


def _fix_old_filing():
    """Mărcile vechi importate din TMview: data depozitului dedusă fără reînnoiri (008472 ISPH)."""
    import ingest
    marker = "migration:old-filing-v1"
    db = SessionLocal()
    try:
        if db.get(SourceFile, marker):
            return
        for m in db.query(Mark).filter(Mark.tmview_checked.like("%|old%")):
            if m.expiry_date and m.reg_date:
                m.filing_date = ingest.old_filing(m.expiry_date, m.reg_date)
        db.add(SourceFile(url=marker, kind="migration", status="ok", label="depozit mărci vechi v1"))
        db.commit()
    finally:
        db.close()


def _fix_read_errors_v2():
    """A doua trecere: coduri INID cu spațiu („SOBYC (511 )Produse…”) și mărci fără titular / fără clase,
    trimise la completarea din TMview (fișa are titularul și produsele)."""
    marker = "migration:read-errors-v2"
    db = SessionLocal()
    try:
        if db.get(SourceFile, marker):
            return
        import re as _re
        from sqlalchemy import Text, cast, or_
        tail = _re.compile(r"\s*\(\s*5[0-9]{2}\s*\)\s*(Clasificare|Produse|Culori|Mențiuni|Men).*$", _re.S)
        n = 0
        for m in db.query(Mark).filter(Mark.name.op("~")(r"\(\s*5[0-9]{2}\s*\)") if IS_PG else Mark.name.like("%5%)%")):
            fixed = tail.sub("", m.name or "").strip()
            if fixed and fixed != m.name:
                m.name, m.name_norm = fixed, norm(fixed)
                n += 1
        for m in db.query(Mark).filter(Mark.app_number.like("M%"), or_(
                Mark.holder == "", Mark.holder.is_(None), Mark.classes.is_(None),
                cast(Mark.classes, Text).in_(["[]", "null"]))):
            if "|needfix" not in (m.tmview_checked or ""):
                m.tmview_checked = (m.tmview_checked or "").replace("|gs3", "") + "|needfix"
        db.add(SourceFile(url=marker, kind="migration", status="ok", label="erori de citire v2"))
        db.commit()
        print(f"[DB] Denumiri curățate (v2): {n}")
    finally:
        db.close()


def _fix_garbled_names():
    """Denumiri tipărite cu font cu codificare proprie („!"#$%&’"#()*…”, ex. M 2001 04258): se golesc și
    se completează din TMview."""
    marker = "migration:garbled-names-v1"
    db = SessionLocal()
    try:
        if db.get(SourceFile, marker):
            return
        from bopi_parser import garbled_name
        n = 0
        for m in db.query(Mark).filter(Mark.name.op("~")(r'^[!"#$%&]') if IS_PG else Mark.name.like("!%")):
            if garbled_name(m.name):
                m.name, m.name_norm = "", ""
                m.tmview_checked = (m.tmview_checked or "").replace("|nm", "") + "|gname"
                m.events = [dict(e, name="") if garbled_name(e.get("name") or "") else e for e in (m.events or [])]
                n += 1
        db.add(SourceFile(url=marker, kind="migration", status="ok", label="denumiri cu font codificat v1"))
        db.commit()
        print(f"[DB] Denumiri cu font codificat golite (se iau din TMview): {n}")
    finally:
        db.close()


def _fix_party_names():
    """Titular / mandatar tăiat la prima virgulă („BRASOVEANU” în loc de „BRASOVEANU, CERNAT SI
    ASOCIATII”, „APPLE” în loc de „APPLE, INC.”): se refac din adresa completă."""
    marker = "migration:party-names-v1"
    db = SessionLocal()
    try:
        if db.get(SourceFile, marker):
            return
        from sqlalchemy import or_
        from bopi_parser import party_name
        from ingest import _recompute_holder

        def longer(short, full):
            pn = party_name(full)
            return pn if short and pn != short and pn.startswith(short + ",") else ""
        n = 0
        # Cheile întâi, apoi pe bucăți — commit-ul în timpul unui cursor de pe server l-ar închide.
        keys = [k for (k,) in db.query(Mark.app_number).filter(
            or_(Mark.holder_address.like("%,%,%"), Mark.representative_address.like("%,%,%")))]
        for i in range(0, len(keys), 2000):
            for m in db.query(Mark).filter(Mark.app_number.in_(keys[i:i + 2000])):
                evs, changed = [], False
                rep = longer(m.representative, m.representative_address)
                for e in m.events or []:
                    e2 = dict(e)
                    h = longer(e.get("holder") or "", e.get("holder_address") or "")
                    if h:
                        e2["holder"] = h
                    if rep and e.get("representative") == m.representative:
                        e2["representative"] = rep
                    changed |= e2 != e
                    evs.append(e2)
                if changed:
                    m.events = evs
                if rep:
                    m.representative, m.rep_norm = rep, norm(rep)
                h = longer(m.holder, m.holder_address)
                if h:
                    m.holder, m.holder_norm = h, norm(h)
                if changed:
                    _recompute_holder(m)
                if changed or rep or h:
                    n += 1
            db.commit()
        db.add(SourceFile(url=marker, kind="migration", status="ok", label="nume titular/mandatar cu virgulă v1"))
        db.commit()
        print(f"[DB] Titulari / mandatari refăcuți (nume cu virgulă): {n}")
    finally:
        db.close()


def _fix_date_reps():
    """Liste de reînnoiri cu coloane decalate: data reînnoirii citită drept mandatar („17/01/2022”,
    REFLEKTOR TV) — se scoate, iar expirarea se prelungește cu 10 ani când data = vechea expirare."""
    marker = "migration:date-reps-v1"
    db = SessionLocal()
    try:
        if db.get(SourceFile, marker):
            return
        from ingest import _DATE_LIKE, _renewal_from, _current_rep, expiry_of
        keys = [k for (k,) in db.query(Mark.app_number).filter(Mark.status == "Înregistrată")]
        n = 0
        for i in range(0, len(keys), 2000):
            for m in db.query(Mark).filter(Mark.app_number.in_(keys[i:i + 2000])):
                bad = [e for e in (m.events or []) if _DATE_LIKE.match(e.get("representative") or "")]
                if not bad and not _DATE_LIKE.match(m.representative or ""):
                    continue
                evs = []
                for e in m.events or []:
                    if _DATE_LIKE.match(e.get("representative") or ""):
                        e = dict(e)
                        iso = _renewal_from(e["representative"], m.expiry_date)
                        if iso and e.get("chapter") == "Reînnoire" and not e.get("expiry"):
                            e["expiry"] = iso
                            e["detail"] = f"valabilă până la {iso[8:10]}.{iso[5:7]}.{iso[:4]}"
                        e.pop("representative")
                    evs.append(e)
                m.events = evs
                if _DATE_LIKE.match(m.representative or ""):
                    m.representative = m.rep_norm = m.representative_address = ""
                _current_rep(m)
                m.expiry_date = expiry_of(m)
                n += 1
            db.commit()
        db.add(SourceFile(url=marker, kind="migration", status="ok", label="mandatar = dată v1"))
        db.commit()
        print(f"[DB] Mandatari citiți ca dată, corectați: {n}")
    finally:
        db.close()


def _retry_failed_sources():
    """Buletine marcate „citite” deși descărcarea a eșuat (eroare de conexiune la osim.ro, 0 mărci):
    se recitesc la următoarea rulare."""
    marker = "migration:retry-failed-sources-v1"
    db = SessionLocal()
    try:
        if db.get(SourceFile, marker):
            return
        n = 0
        for s in db.query(SourceFile).filter(SourceFile.status == "ok", SourceFile.error != "",
                                             SourceFile.kind.in_(["monthly", "changes", "daily", "admise"])):
            if not s.marks:
                s.status = "error"
                n += 1
        db.add(SourceFile(url=marker, kind="migration", status="ok", label="buletine eșuate v1"))
        db.commit()
        print(f"[DB] Buletine de recitit: {n}")
    finally:
        db.close()


def _flag_unreadable_sources():
    """Buletinele lunare „citite” cu 0 mărci (fonturi fără tabel de caractere) — marcate necitibile."""
    marker = "migration:unreadable-sources-v1"
    db = SessionLocal()
    try:
        if db.get(SourceFile, marker):
            return
        for s in db.query(SourceFile).filter(SourceFile.status == "ok", SourceFile.marks == 0,
                                             SourceFile.kind.in_(["monthly", "daily", "admise"])):
            s.status = "unreadable"
        db.add(SourceFile(url=marker, kind="migration", status="ok", label="buletine necitibile v1"))
        db.commit()
    finally:
        db.close()


def _fix_read_errors():
    """O singură dată: erori de citire rămase în denumiri (coduri INID lipite: „( 531) Clasificare Viena…”,
    capitolul „Produse și/sau servicii…” luat drept denumire) și mărci înregistrate fără dată de expirare
    / de depozit (de completat din TMview)."""
    marker = "migration:read-errors-v1"
    db = SessionLocal()
    try:
        if db.get(SourceFile, marker):
            return
        import re as _re
        from ingest import expiry_of
        bad = _re.compile(r"^\s*Produse (și|şi|si)/sau servicii|\(\s*5[0-9]{2}\)|\b5[0-9]{2}\)\s*(Clasificare|Produse|Culori)")
        tail = _re.compile(r"\s*\(?\s*(531|511|591|526)\s*\)\s*(Clasificare Viena|Produse|Culori|Mențiuni|Men).*$", _re.S)
        n_name = n_exp = 0
        for m in db.query(Mark).yield_per(2000):
            name = m.name or ""
            if name and bad.search(name):
                fixed = tail.sub("", name).strip()
                if not fixed or bad.search(fixed):
                    # Denumirea dintr-un alt document al mărcii (alt buletin), dacă e curată.
                    alt = [e.get("name") for e in sorted(m.events or [], key=lambda e: e.get("date") or "")
                           if e.get("name") and not bad.search(e["name"])]
                    fixed = alt[-1] if alt else ""
                if fixed != name:
                    m.name, m.name_norm = fixed, norm(fixed)
                    n_name += 1
                    if not fixed and "|needfix" not in (m.tmview_checked or ""):
                        m.tmview_checked = (m.tmview_checked or "").replace("|gs3", "") + "|needfix"
            if m.status == "Înregistrată" and (not m.expiry_date or not m.filing_date):
                exp = expiry_of(m)
                if exp and exp != m.expiry_date:
                    m.expiry_date = exp
                    n_exp += 1
                elif "|needfix" not in (m.tmview_checked or ""):
                    m.tmview_checked = (m.tmview_checked or "").replace("|gs3", "") + "|needfix"
        db.add(SourceFile(url=marker, kind="migration", status="ok", label="erori de citire v1"))
        db.commit()
        print(f"[DB] Denumiri curățate: {n_name}; expirări completate: {n_exp}")
    finally:
        db.close()


def _reprocess_changes_for_renewal_no():
    """O singură dată: tabelele de modificări se recitesc, ca reînnoirile să rețină numărul tipărit
    de OSIM („008649(5R)”) — prefixul R / 2R / 5R al mărcii nu se mai deduce din date."""
    marker = "migration:renewal-no-v1"
    db = SessionLocal()
    try:
        if db.get(SourceFile, marker):
            return
        k = db.query(SourceFile).filter(SourceFile.kind == "changes", SourceFile.status == "ok")\
            .delete(synchronize_session=False)
        db.add(SourceFile(url=marker, kind="migration", status="ok", label="număr reînnoire v1"))
        db.commit()
        print(f"[DB] Tabele de modificări de recitit (nr. reînnoire): {k}")
    finally:
        db.close()


def _link_known_deposits():
    """Mărci reînnoite al căror nr. de depozit TMview nu-l are legat de nr. de înregistrare
    (căutarea după înregistrare nu le găsește) — legătura dată de utilizator."""
    known = {"R135640": "M201407160"}
    marker = "migration:known-deposits-v1"
    db = SessionLocal()
    try:
        if db.get(SourceFile, marker):
            return
        for app, dep in known.items():
            m = db.get(Mark, app)
            if m is not None:
                m.events = list(m.events or []) + [{"date": "2026-11-11", "bopi": "TMview", "chapter": "Nr. depozit",
                                                    "detail": dep, "app": dep, "st13": "RO50000" + dep}]
                m.tmview_checked = None
        db.add(SourceFile(url=marker, kind="migration", status="ok", label="depozite cunoscute v1"))
        db.commit()
    finally:
        db.close()


def _repair_stubs_and_holders():
    """O singură dată: modificările publicate înainte de apariția mărcii reînnoite „R…” se aplică acum,
    iar titularul tuturor mărcilor se recalculează cu noua ordine (cesiune > schimbare de nume >
    schimbare de adresă, la aceeași dată)."""
    marker = "migration:stubs-holders-v1"
    db = SessionLocal()
    try:
        if db.get(SourceFile, marker):
            return
        from ingest import _apply_pending, _recompute_holder
        n = 0
        for m in db.query(Mark).filter(Mark.app_number.like("R%")).all():
            _apply_pending(db, m)
            n += 1
        db.commit()
        for m in db.query(Mark).yield_per(2000):
            _recompute_holder(m)
        db.add(SourceFile(url=marker, kind="migration", status="ok", label="modificări + titulari v1"))
        db.commit()
        print(f"[DB] Mărci reînnoite verificate pentru modificări în așteptare: {n}")
    finally:
        db.close()


def _reprocess_dailies_for_representatives():
    """O singură dată: buletinele zilnice și de cereri admise (rapide) se recitesc, ca fiecare eveniment
    să rețină reprezentantul de la data lui — așa se vede când a fost înscris un mandatar ulterior."""
    marker = "migration:daily-rep-v1"
    db = SessionLocal()
    try:
        if db.get(SourceFile, marker):
            return
        k = db.query(SourceFile).filter(SourceFile.kind.in_(("daily", "admise")), SourceFile.status == "ok")\
            .delete(synchronize_session=False)
        db.add(SourceFile(url=marker, kind="migration", status="ok", label="reprezentanți în istoric v1"))
        db.commit()
        print(f"[DB] Buletine zilnice de recitit (reprezentanți): {k}")
    finally:
        db.close()


def _reprocess_changes_for_names():
    """O singură dată: tabelele de modificări se recitesc, ca denumirea („Denumire marcă”) să ajungă
    la mărcile reînnoite vechi, păstrate doar după nr. de marcă (R<nr.>)."""
    marker = "migration:change-names-v1"
    db = SessionLocal()
    try:
        if db.get(SourceFile, marker):
            return
        k = db.query(SourceFile).filter(SourceFile.kind == "changes", SourceFile.status == "ok")\
            .delete(synchronize_session=False)
        db.add(SourceFile(url=marker, kind="migration", status="ok", label="denumiri din tabele v1"))
        db.commit()
        print(f"[DB] Tabele de modificări de recitit (denumiri): {k}")
    finally:
        db.close()


def _fix_reg_zeros():
    """Nr. de marcă citite cu zerouri în față (arhiva: „065304”) — normalizate ca în tabelele de
    reînnoiri, apoi unite cu marca reînnoită păstrată pe nr. de marcă și cu modificările în așteptare."""
    db = SessionLocal()
    try:
        ms = db.query(Mark).filter(Mark.reg_number.like("0%")).all()
        if not ms:
            return
        from ingest import _apply_pending, _merge_stub, expiry_of
        for m in ms:
            m.reg_number = m.reg_number.lstrip("0")
            db.flush()
            _merge_stub(db, m)
            _apply_pending(db, m)
            m.expiry_date = expiry_of(m)
        db.commit()
        print(f"[DB] Nr. de marcă normalizate (zerouri în față): {len(ms)}")
    finally:
        db.close()


def _fill_stub_filing_dates():
    """Mărcile reînnoite vechi (R<nr.>) denumite din tabele: data de depozit din nr. de depozit + expirare."""
    from ingest import _stub_filing_date
    db = SessionLocal()
    try:
        n = 0
        for m in db.query(Mark).filter(Mark.app_number.like("R%"), Mark.expiry_date != "",
                                       (Mark.filing_date == "") | (Mark.filing_date == None)):  # noqa: E711
            _stub_filing_date(m)
            n += bool(m.filing_date)
        db.commit()
        if n:
            print(f"[DB] Date de depozit completate la mărci reînnoite: {n}")
    finally:
        db.close()


def _drop_part_duplicates():
    """O singură dată: buletinele 09–12/2022 au fost citite și întregi, și pe părți („partea I/II”) —
    evenimentele și modificările dublate de pe părți se șterg (cele din fișierul întreg rămân)."""
    marker = "migration:part-dupes-v1"
    db = SessionLocal()
    try:
        if db.get(SourceFile, marker):
            return
        from sqlalchemy import String, cast
        n = 0
        for m in db.query(Mark).filter(cast(Mark.events, String).like("%partea%")):
            evs = m.events or []
            keys = {(e.get("date"), e.get("bopi"), e.get("chapter")) for e in evs}
            keep = []
            for e in evs:
                b = e.get("bopi") or ""
                if " partea" in b and (e.get("date"), b.split(" partea")[0], e.get("chapter")) in keys:
                    continue
                keep.append(e)
            if len(keep) != len(evs):
                m.events = keep
                n += 1
        whole = {l for (l,) in db.query(MarkChange.label).filter(~MarkChange.label.like("% partea%")).distinct()}
        k = 0
        for (l,) in db.query(MarkChange.label).filter(MarkChange.label.like("% partea%")).distinct().all():
            if l.split(" partea")[0] in whole:
                k += db.query(MarkChange).filter(MarkChange.label == l).delete(synchronize_session=False)
        db.add(SourceFile(url=marker, kind="migration", status="ok", label="dubluri părți 2022"))
        db.commit()
        print(f"[DB] Dubluri „partea”: {n} mărci curățate, {k} modificări șterse")
    finally:
        db.close()


def _retry_nameless_logos():
    """O singură dată: mărcile reînnoite găsite în TMview fără denumire (figurative), fără logo — logo-ul
    se mai caută o dată (enrich_logos le ia pe cele fără „|img”)."""
    marker = "migration:nameless-logos-v1"
    db = SessionLocal()
    try:
        if db.get(SourceFile, marker):
            return
        n = 0
        for m in db.query(Mark).filter(Mark.app_number.like("R%"), Mark.name == "", Mark.has_image == False,  # noqa: E712
                                       Mark.tmview_checked.like("%img")):
            if any(e.get("bopi") == "TMview" for e in (m.events or [])):
                m.tmview_checked = m.tmview_checked.replace("|img", "")
                n += 1
        db.add(SourceFile(url=marker, kind="migration", status="ok", label="logo-uri figurative v1"))
        db.commit()
        print(f"[DB] Logo-uri de recăutat (mărci figurative din TMview): {n}")
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(engine)
    _ensure_columns()
    _fix_disclaimers()
    _reprocess_old_changes()
    _reprocess_for_history()
    _reprocess_for_names()
    _reprocess_changes_for_names()
    _reprocess_changes_for_representatives()
    _reprocess_dailies_for_representatives()
    _flag_limited_goods()
    _fix_old_filing_expiry()
    _flag_missing_publication()
    _reprocess_changes_for_renewal_no()
    _repair_stubs_and_holders()
    _link_known_deposits()
    _flag_archive_publication()
    # Trece prin toate mărcile (minute întregi) — în fundal, ca site-ul să pornească imediat.
    import threading
    threading.Thread(target=lambda: (_fix_legacy_diacritics(), _archive_needfix(), _retry_renewed_logos(),
                                     _recompute_reps(), _fix_read_errors(), _fix_old_filing(), _fix_read_errors_v2(), _fix_garbled_names(), _retry_failed_sources(), _flag_unreadable_sources(),
                                     _fix_party_names(), _fix_date_reps()),
                     daemon=True).start()
    _fix_reg_zeros()
    _fill_stub_filing_dates()
    _drop_part_duplicates()
    _retry_nameless_logos()
    if IS_PG:
        # Căutare „conține” rapidă pe sute de mii de rânduri: index trigram.
        with engine.begin() as c:
            c.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            for col in ("name_norm", "holder_norm", "rep_norm"):
                c.execute(text(f"CREATE INDEX IF NOT EXISTS ix_marks_{col}_trgm "
                               f"ON marks USING gin ({col} gin_trgm_ops)"))

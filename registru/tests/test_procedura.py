"""Etapele procedurii OSIM pe mărci reale (datele verificate în PDF-urile BOPI / „Cereri admise” de pe
osim.ro) și cazurile-limită ale termenului de 2 luni. Rulare: python3 -m pytest registru/tests -q"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import procedura as p  # noqa: E402

TODAY = "2026-11-17"
PUB, ADM, REG = p.PUBLISHED, p.ADMITTED, p.REGISTERED


def mark(status, pub, events, reg_date="", filed=""):
    return SimpleNamespace(status=status, publication_date=pub, reg_date=reg_date, events=events,
                           proc_status="", proc_manual="", app_number="X", filing_date=filed)


def test_depusa_dupa_legea_112_2020():
    # M 2020 05000 — depusă 17.07.2020 (după 12.07.2020): procedura nouă, cu „Cereri admise”.
    r = p.compute(mark("Înregistrată", "2020-07-24",
                       [{"date": "2020-07-24", "bopi": "BOPI 24.07.2020", "chapter": PUB},
                        {"date": "2021-02-28", "bopi": "BOPI 02/2021", "chapter": ADM},
                        {"date": "2021-06-30", "bopi": "BOPI 06/2021", "chapter": REG}],
                       filed="2020-07-17"), TODAY)
    assert r["obs_end"] == "2020-09-24" and r["admise_date"] == "2021-02-28" and r["proc_status"] == p.REG


def test_depusa_inainte_de_legea_112_2020():
    # M 2020 03000 — depusă 08.05.2020: procedura veche, fără etape (rămâne istoricul mărcii).
    r = p.compute(mark("Înregistrată", "2020-05-15",
                       [{"date": "2020-05-15", "bopi": "BOPI 15.05.2020", "chapter": PUB},
                        {"date": "2021-03-31", "bopi": "BOPI 03/2021", "chapter": REG}],
                       filed="2020-05-08"), TODAY)
    assert not r["proc_status"] and not r["obs_end"]


def test_observatii_velora():
    # M 2026 10963 VELORA — BOPI 16.11.2026; 16.01.2027 e sâmbătă → luni 18.01.2027.
    r = p.compute(mark("Cerere publicată", "2026-11-16",
                       [{"date": "2026-11-16", "bopi": "BOPI 16.11.2026", "chapter": PUB}]), TODAY)
    assert r["obs_end"] == "2027-01-18" and r["proc_status"] == p.OBS and not r["opp_end"]


def test_examinare_seafood_boil():
    # M 2026 07751 Seafood Boil — BOPI 17.08.2026, observații până la 17.10.2026 (sâmbătă → 19.10),
    # fără „Cereri admise” până azi: în examinare, perioada de opoziție nu se calculează.
    r = p.compute(mark("Cerere publicată", "2026-08-17",
                       [{"date": "2026-08-17", "bopi": "BOPI 17.08.2026", "chapter": PUB}]), TODAY)
    assert r["obs_end"] == "2026-10-19" and r["proc_status"] == p.EXAM and r["opp_end"] == ""


def test_opozitie_m202606170():
    # M 2026 06170 — BOPI 28.07.2026, „Cereri admise 16.11.2026” → opoziție până la 18.01.2027.
    r = p.compute(mark("Admisă la înregistrare", "2026-07-28", [
        {"date": "2026-07-28", "bopi": "BOPI 28.07.2026", "chapter": PUB},
        {"date": "2026-11-16", "bopi": "Cereri admise 16.11.2026", "chapter": ADM}]), TODAY)
    assert r["admise_date"] == "2026-11-16" and r["opp_end"] == "2027-01-18" and r["proc_status"] == p.OPP


def test_opozitie_inchisa_m202511837():
    # M 2025 11837 — BOPI 13.01.2026, „Cereri admise 15.04.2026” → opoziție până la 15.06.2026;
    # neînregistrată nici azi: opoziție închisă, de peste 3 luni → semnalată (posibilă opoziție).
    m = mark("Admisă la înregistrare", "2026-01-13", [
        {"date": "2026-01-13", "bopi": "BOPI 13.01.2026", "chapter": PUB},
        {"date": "2026-04-15", "bopi": "Cereri admise 15.04.2026", "chapter": ADM}])
    r = p.compute(m, TODAY)
    assert r["opp_end"] == "2026-06-15" and r["proc_status"] == p.OPP_CLOSED
    assert any("posibilă opoziție" in a for a in p.anomalies(m, r, TODAY))


def test_inregistrata_cronica_de_hunedoara():
    # M 2026 03051 Cronica de Hunedoara — BOPI 20.04.2026, „Cereri admise 31.07.2026” (opoziție până la
    # 30.09.2026 — 31.09 nu există), înregistrată nr. 217874 în BOPI 10/2026.
    m = mark("Înregistrată", "2026-04-20", [
        {"date": "2026-04-20", "bopi": "BOPI 20.04.2026", "chapter": PUB},
        {"date": "2026-07-31", "bopi": "Cereri admise 31.07.2026", "chapter": ADM},
        {"date": "2026-10-31", "bopi": "BOPI 10/2026", "chapter": REG}], reg_date="2026-10-31")
    r = p.compute(m, TODAY)
    assert r["opp_end"] == "2026-09-30" and r["reg_pub_date"] == "2026-10-31" and r["proc_status"] == p.REG
    assert p.anomalies(m, r, TODAY) == []


def test_admisa_inainte_de_sfarsitul_observatiilor():
    # M 2026 08315 — BOPI 02.09.2026, dar deja în „Cereri admise 30.10.2026” (observații până la 02.11).
    m = mark("Admisă la înregistrare", "2026-09-02", [
        {"date": "2026-09-02", "bopi": "BOPI 02.09.2026", "chapter": PUB},
        {"date": "2026-10-30", "bopi": "Cereri admise 30.10.2026", "chapter": ADM}])
    r = p.compute(m, TODAY)
    assert r["opp_end"] == "2026-12-30" and r["proc_status"] == p.OPP
    assert "admisă înainte de sfârșitul observațiilor" in p.anomalies(m, r, TODAY)


def test_termen_cazuri_limita():
    assert p.term_end("2026-12-31") == "2027-03-01"     # 28.02.2027 duminică
    assert p.term_end("2026-08-31") == "2026-11-02"     # 31.10 sâmbătă, 01.11 duminică
    assert p.term_end("2026-10-31") == "2026-12-31"
    assert p.term_end("2027-10-30") == "2027-12-30"     # joi, zi lucrătoare
    assert p.term_end("2026-09-30") == "2026-12-02"     # 30.11 Sf. Andrei, 01.12 Ziua Națională


def test_sarbatori():
    from datetime import date
    assert p._orthodox_easter(2026) == date(2026, 4, 12)
    assert p._orthodox_easter(2027) == date(2027, 5, 2)
    assert not p.is_working_day(date(2026, 12, 1)) and not p.is_working_day(date(2026, 4, 10))  # 1 Dec, Vinerea Mare
    assert p.term_end("2026-10-01") == "2026-12-02"     # 1 decembrie → 2 decembrie

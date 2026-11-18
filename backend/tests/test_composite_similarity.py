"""Testele obligatorii pentru scorul compus din ProMonitor (rulare: python -m pytest backend/tests
sau direct: python backend/tests/test_composite_similarity.py)."""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import composite_similarity as cs  # noqa: E402
from daily_monitor import DEFAULT_MONITOR_LIKE, compute_conflicts  # noqa: E402


def _watch(name, classes=("35",), wid=1):
    return SimpleNamespace(id=wid, trademark_name=name, nice_classes=list(classes), monitor_like=None,
                           offices=["RO", "EM", "WO"], application_number="", holder_name="",
                           portfolio="", origin_office="OSIM", reference_image="")


def _mark(name, classes=("35",), app="M202600001"):
    return {"applicationNumber": app, "tmName": name, "niceClass": [int(c) for c in classes],
            "status": "Cerere publicată", "office": "RO"}


def _like(watch_name, mark_name):
    rows = compute_conflicts([_mark(mark_name)], [_watch(watch_name)])
    return rows[0] if rows else None


def test_bioplafar_plafaria_medium_or_more():
    r = _like("PLAFARIA", "BioPlafar")
    assert r and r["like"] >= 0.70, r
    assert cs.grade(r["like"]) in ("mediu", "ridicat")
    r = _like("PLAFARIA", "BioPlafar.ro")
    assert r and r["like"] >= 0.70, r


def test_ecovital_vitalia_conflict():
    assert _like("VITALIA", "ECOVITAL")
    assert _like("ECOVITAL", "VITALIA")


def test_promedic_medicul_conflict():
    assert _like("MEDICUL", "PROMEDIC")
    assert _like("PROMEDIC", "MEDICUL")


def test_identical_100():
    r = _like("PLAFARIA", "PLAFARIA")
    assert r and r["like"] == 1.0


def test_plafaria_telefonia_not_conflict():
    assert _like("PLAFARIA", "TELEFONIA") is None
    assert cs.composite(cs.prepare("PLAFARIA"), cs.prepare("TELEFONIA"))[0] < DEFAULT_MONITOR_LIKE


def test_biofarm_biotech_not_conflict():
    assert _like("BIOFARM", "BIOTECH") is None
    assert _like("BIOTECH", "BIOFARM") is None


def test_normalization():
    assert cs.normalize("Ștefănești-Țară® ™ Î.Â") == "stefanestitaraia"


if __name__ == "__main__":
    fails = 0
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            try:
                f()
                print("OK  ", n)
            except AssertionError as e:
                fails += 1
                print("FAIL", n, e)
    for a, b in [("PLAFARIA", "BioPlafar"), ("VITALIA", "ECOVITAL"), ("MEDICUL", "PROMEDIC"),
                 ("PLAFARIA", "PLAFARIA"), ("PLAFARIA", "TELEFONIA"), ("BIOFARM", "BIOTECH")]:
        r = _like(a, b)
        print(f"  {b:>12} ↔ {a:<10}", f"{r['like']:.0%} {cs.grade(r['like'])} ({r['watch']['method']})" if r else "— nu apare")
    sys.exit(1 if fails else 0)

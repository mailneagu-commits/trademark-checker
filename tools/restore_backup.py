#!/usr/bin/env python3
"""Restaurează local un backup descărcat din aplicație (zip cu monitor.db + watch_images/).

Utilizare, din folderul proiectului:
    python3 tools/restore_backup.py ~/Downloads/backup-marci-2026-10-01_0930.zip

Pune baza de date în ./monitor.db și logo-urile în ./data/watch_images/ — locurile implicite
folosite de aplicația pornită local (./start.sh). Baza de date locală existentă este păstrată
ca monitor.db.bak-<data>.
"""
import os
import shutil
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    if len(sys.argv) != 2 or not os.path.isfile(sys.argv[1]):
        print(__doc__)
        return 1
    with zipfile.ZipFile(sys.argv[1]) as z:
        names = z.namelist()
        if "monitor.db" not in names:
            print("Arhiva nu conține monitor.db — nu e un backup valid.")
            return 1
        db_path = os.path.join(ROOT, "monitor.db")
        if os.path.exists(db_path):
            bak = f"{db_path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
            shutil.copy2(db_path, bak)
            print(f"Baza locală existentă a fost salvată în {os.path.basename(bak)}")
        with z.open("monitor.db") as src, open(db_path, "wb") as dst:
            shutil.copyfileobj(src, dst)
        img_dir = os.path.join(ROOT, "data", "watch_images")
        os.makedirs(img_dir, exist_ok=True)
        n = 0
        for name in names:
            if name.startswith("watch_images/") and not name.endswith("/"):
                with z.open(name) as src, open(os.path.join(img_dir, os.path.basename(name)), "wb") as dst:
                    shutil.copyfileobj(src, dst)
                n += 1
        if "manifest.json" in names:
            print(z.read("manifest.json").decode())
    print(f"Gata: monitor.db restaurat, {n} logo-uri. Pornește aplicația cu ./start.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())

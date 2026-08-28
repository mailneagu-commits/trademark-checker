"""
Directorul de date persistent — DB SQLite, cache buletine OSIM/EUIPO, imagini de
referință ale mărcilor monitorizate.

Implicit (DATA_DIR nesetat), toate aceste fișiere stau pe filesystem-ul efemer al
containerului Railway și se pierd la fiecare deploy. Dacă e atașat un Volume
Railway (Settings → Volumes) și variabila de mediu DATA_DIR e setată la calea de
montare (ex. "/data"), toate supraviețuiesc repornirilor/redeploy-urilor.
"""
import os

DATA_DIR = os.environ.get("DATA_DIR", "").rstrip("/")

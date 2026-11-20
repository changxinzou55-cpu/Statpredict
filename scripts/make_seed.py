"""Write seed/statpredict.db.gz from the live database, for hosts that start with an empty disk."""
import gzip
import os
import shutil
import sqlite3

from app import db

tmp = db.DB_PATH + ".seed-tmp"
if os.path.exists(tmp):
    os.remove(tmp)
c = sqlite3.connect(f"file:{db.DB_PATH}?mode=ro", uri=True)
c.execute("VACUUM INTO ?", (tmp,))
c.close()
os.makedirs(os.path.dirname(db.SEED_PATH), exist_ok=True)
with open(tmp, "rb") as src, gzip.open(db.SEED_PATH, "wb", compresslevel=9) as dst:
    shutil.copyfileobj(src, dst)
os.remove(tmp)
print(f"{db.SEED_PATH}: {os.path.getsize(db.SEED_PATH) / 1e6:.1f} MB")

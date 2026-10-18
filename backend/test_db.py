from sqlalchemy import text
from app.database import engine

with engine.connect() as conn:
    hasil = conn.execute(
        text("SELECT current_database(), PostGIS_Full_Version()")
    ).one()

    print("Database:", hasil[0])
    print("PostGIS:", hasil[1])
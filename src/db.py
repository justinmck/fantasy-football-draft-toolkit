from sqlalchemy import create_engine
from src.settings import DB_URL
engine = create_engine(DB_URL, future=True)

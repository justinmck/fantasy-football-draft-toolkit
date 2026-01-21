import os
DB_URL = os.getenv("DATABASE_URL", "sqlite:///data/fantasy_data.db")
API_ORIGINS = os.getenv("API_ORIGINS", "http://localhost:5173,http://localhost:3000").split(",")
TEAMS = int(os.getenv("LEAGUE_TEAMS", "14"))

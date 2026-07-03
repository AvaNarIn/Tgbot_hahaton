import os

from dotenv import load_dotenv


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DGIS_API_KEY = os.getenv("DGIS_API_KEY")
DATABASE_PATH = os.getenv("DATABASE_PATH", "trips.db")

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is not set. Create a .env file and add BOT_TOKEN=your_token_here"
    )

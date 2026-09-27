from sqlalchemy import *
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    DB_URL: str
    ALGORITHM: str
    KEY: str
    EXPIRE: int
    BUNNY_STORAGE_KEY: str
    STORAGE_ZONE_NAME : str
    PULL_ZONE_URL:str
    BUNNY_REGION_URL : str
    STREAM_API_KEY : str
    STREAM_LIBRARY_ID : int
    PRIVATE_STORAGE_KEY : str
    BUNNY_TOKEN_SECURITY_KEY : str
    PRIVATE_STORAGE_ZONE : str
    STREAM_CDN_HOSTNAME : str
    BUNNY_CDN_HOSTNAME : str
    OPENAI_API_KEY : str
    GROQ_API_KEY : str
    RESEND_API_KEY: str
    FIREBASE_BASE64_KEY: str
    UNSPLASH_ACCESS_KEY: str
    REVENUECAT_WEBHOOK_SECRET: str
    REFRESH_EXPIRE_DAYS: str
    AGENT_ROUTER_API_KEY: str = ""
    AI_PROVIDER: str = "agentrouter"
    YOUTUBE_API_KEY: str = ""
    PLATFORM_SUPER_ADMINS: str = ""
    ONESIGNAL_APP_ID: str = ""
    ONESIGNAL_REST_API_KEY: str = ""
    ONE_SIGNAL_APP_ID: str = ""
    ONE_SIGNAL_REST_API_KEY: str = ""
    ONESIGNAL_API_KEY: str = ""
    ONE_SIGNAL_API_KEY: str = ""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def get_onesignal_app_id(self) -> str:
        import os
        return self.ONESIGNAL_APP_ID or self.ONE_SIGNAL_APP_ID or os.getenv("ONESIGNAL_APP_ID") or os.getenv("ONE_SIGNAL_APP_ID") or ""

    def get_onesignal_rest_api_key(self) -> str:
        import os
        return self.ONESIGNAL_REST_API_KEY or self.ONE_SIGNAL_REST_API_KEY or self.ONESIGNAL_API_KEY or self.ONE_SIGNAL_API_KEY or os.getenv("ONESIGNAL_REST_API_KEY") or os.getenv("ONE_SIGNAL_REST_API_KEY") or os.getenv("ONESIGNAL_API_KEY") or ""
    
Url= Settings().DB_URL
engine = create_engine(
    Url,
    pool_pre_ping=True,      # <-- THE MAGIC FIX: Checks connection health before querying
    pool_recycle=300,        # <-- Forces SQLAlchemy to refresh connections every 5 minutes
    pool_size=5,             # Keep the pool small so you don't exhaust Neon's limits
    max_overflow=10)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
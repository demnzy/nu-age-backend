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
    model_config = SettingsConfigDict(env_file=".env")
    
Url= Settings().DB_URL
print(Url)
engine = create_engine(Url)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
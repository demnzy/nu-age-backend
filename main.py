import asyncio
import sys
from fastapi import *
from routers import enrollments, media, users,courses,categories, organisations, curriculum,chat,certificate,study,subscriptions,network
from models import Base
from database import engine
Base.metadata.create_all(bind=engine)
from fastapi.middleware.cors import CORSMiddleware

tags_metadata = [
    {
        "name": "Users",
        "description": "Operations involving **login**, registration, and profile management.",
    },
    {
        "name": "Courses",
        "description": "Create and manage courses. **Admin only**.",
    },
    {
        "name": "Curriculum Management",
        "description": "Manage course curricula. **Admin only**.",
    },
    {
        "name": "Certificates",
        "description": "Generate and manage certificates.",
    },
    {
        "name": "Subscription Management",
        "description": "Manage user subscriptions and plans.",
    }

]
app = FastAPI(openapi_tags=tags_metadata)
app.include_router(users.router, tags=["Users"])
app.include_router(courses.router, tags=["Courses"])
app.include_router(categories.router, tags=["Categories"])
app.include_router(enrollments.router,tags=["enrollments"])
app.include_router(organisations.router,tags=["organisations"])
app.include_router(media.router, tags=["Media Handling"])
app.include_router(curriculum.router, tags=["Curriculum Management"])
app.include_router(chat.router, tags=["Chat"])
app.include_router(certificate.router, tags=["Certificates"])
app.include_router(study.router, tags=["Self Study"])
app.include_router(subscriptions.router, tags=["Subscription Management"])
app.include_router(network.router, tags=["Friends Management"])

# Add this right after you declare: app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"], # Allows your Reflex frontend
    allow_credentials=True,
    allow_methods=["*"], 
    allow_headers=["*"], 
)
 

import sys
from fastapi.routing import APIRoute
from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import text
from database import get_db # Ensure this matches your import path

@app.get("/health", tags=["System"])
async def health_check(db: Session = Depends(get_db)):
    """
    A lightweight endpoint to keep the server awake and verify database connectivity.
    """
    try:
        # The ultimate lightweight query: ask PostgreSQL to literally just return the number 1
        db.execute(text("SELECT 1"))
        
        return {
            "status": "active",
            "backend": "online",
            "database": "connected"
        }
        
    except Exception as e:
        # If the database is unreachable, we fail loudly with a 503 Service Unavailable
        print(f"Health Check Failed: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Backend is running, but database connection failed."
        )
    
@app.on_event("startup")
def print_routes():
    print("\n" + "="*50)
    print("  AVAILABLE ROUTES (Use these URLs!)")
    print("="*50)
    found_any = False
    for route in app.routes:
        if isinstance(route, APIRoute):
            found_any = True
            print(f"METHOD: {route.methods}  |  PATH: {route.path}")
    
    if not found_any:
        print(">> NO ROUTES FOUND! check app.include_router() lines.")
    print("="*50 + "\n")
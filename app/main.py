from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.database import Base, engine
from app.config import settings
from app.routers import auth, users, courses, study_spaces, materials, ai_tutor
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="IMOWE API",
    description="A smarter way to learn. Foundation stage: auth, academic profile, Study Spaces, materials.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(ai_tutor.router)
app.include_router(users.router)
app.include_router(courses.router)
app.include_router(study_spaces.router)
app.include_router(materials.router)


@app.get("/health")
def health_check():
    return {"status": "ok", "service": "imowe-api"}
@app.get("/")
def root():
    return {
        "message": "IMOWE API is running.",
        "docs": "/docs",
        "health": "/health",
    }

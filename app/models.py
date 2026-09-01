import uuid
import enum
from datetime import datetime

from sqlalchemy import (
    Column, String, Integer, Float, ForeignKey, DateTime, Enum, Text
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


def gen_uuid():
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    full_name = Column(String(150), nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    academic_profile = relationship(
        "AcademicProfile", back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    study_spaces = relationship("StudySpace", back_populates="user", cascade="all, delete-orphan")


class AcademicProfile(Base):
    __tablename__ = "academic_profiles"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), unique=True, nullable=False)

    institution = Column(String(200), nullable=True)
    programme = Column(String(200), nullable=True)
    level = Column(Integer, default=1)
    xp = Column(Integer, default=0)
    avatar_url = Column(String(500), nullable=True)

    user = relationship("User", back_populates="academic_profile")


class Course(Base):
    __tablename__ = "courses"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)

    title = Column(String(200), nullable=False)
    subtitle = Column(String(200), nullable=True)
    cover_color = Column(String(20), nullable=True)
    cover_image_url = Column(String(500), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    study_spaces = relationship("StudySpace", back_populates="course", cascade="all, delete-orphan")


class StudySpaceStatus(str, enum.Enum):
    current = "current"
    completed = "completed"
    archived = "archived"


class StudySpace(Base):
    __tablename__ = "study_spaces"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    course_id = Column(UUID(as_uuid=False), ForeignKey("courses.id"), nullable=False)

    status = Column(Enum(StudySpaceStatus), default=StudySpaceStatus.current)
    progress_percent = Column(Float, default=0.0)
    next_up = Column(String(200), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="study_spaces")
    course = relationship("Course", back_populates="study_spaces")
    materials = relationship("Material", back_populates="study_space", cascade="all, delete-orphan")


class MaterialStatus(str, enum.Enum):
    uploaded = "uploaded"
    processing = "processing"
    processed = "processed"
    failed = "failed"


class MaterialType(str, enum.Enum):
    document = "document"
    recording = "recording"


class Material(Base):
    __tablename__ = "materials"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    study_space_id = Column(UUID(as_uuid=False), ForeignKey("study_spaces.id"), nullable=False)
    linked_material_id = Column(UUID(as_uuid=False), ForeignKey("materials.id"), nullable=True)

    filename = Column(String(300), nullable=False)
    file_path = Column(String(500), nullable=True)
    content_type = Column(String(100), nullable=True)
    material_type = Column(Enum(MaterialType), default=MaterialType.document)
    status = Column(Enum(MaterialStatus), default=MaterialStatus.uploaded)
    extracted_topics = Column(Text, nullable=True)
    extracted_text = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    study_space = relationship("StudySpace", back_populates="materials")
    linked_material = relationship("Material", remote_side=[id])


class ChatThread(Base):
    """
    One conversation thread (ChatGPT-style history). A material or a course
    can have many threads; each holds its own AIInteraction messages.
    """
    __tablename__ = "chat_threads"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    study_space_id = Column(UUID(as_uuid=False), ForeignKey("study_spaces.id"), nullable=True)
    material_id = Column(UUID(as_uuid=False), ForeignKey("materials.id"), nullable=True)

    title = Column(String(200), nullable=False, default="New chat")

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AIInteraction(Base):
    """A single message in an AI Tutor conversation, belonging to one ChatThread."""
    __tablename__ = "ai_interactions"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    thread_id = Column(UUID(as_uuid=False), ForeignKey("chat_threads.id"), nullable=True)
    study_space_id = Column(UUID(as_uuid=False), ForeignKey("study_spaces.id"), nullable=True)
    material_id = Column(UUID(as_uuid=False), ForeignKey("materials.id"), nullable=True)

    role = Column(String(20), nullable=False)  # "user" or "assistant"
    content = Column(Text, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)


class QuizQuestion(Base):
    __tablename__ = "quiz_questions"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    study_space_id = Column(UUID(as_uuid=False), ForeignKey("study_spaces.id"), nullable=False)

    question_text = Column(Text, nullable=False)
    options = Column(Text, nullable=False)
    correct_index = Column(Integer, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)


class QuizAttempt(Base):
    __tablename__ = "quiz_attempts"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    study_space_id = Column(UUID(as_uuid=False), ForeignKey("study_spaces.id"), nullable=False)

    score = Column(Integer, nullable=False)
    total = Column(Integer, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)

import uuid
import enum
from datetime import datetime

from sqlalchemy import (
    Column, String, Integer, Float, Boolean, ForeignKey, DateTime, Date, Enum, Text, UniqueConstraint
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
    __tablename__ = "chat_threads"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    study_space_id = Column(UUID(as_uuid=False), ForeignKey("study_spaces.id"), nullable=True)
    material_id = Column(UUID(as_uuid=False), ForeignKey("materials.id"), nullable=True)

    title = Column(String(200), nullable=False, default="New chat")

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AIInteraction(Base):
    __tablename__ = "ai_interactions"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    thread_id = Column(UUID(as_uuid=False), ForeignKey("chat_threads.id"), nullable=True)
    study_space_id = Column(UUID(as_uuid=False), ForeignKey("study_spaces.id"), nullable=True)
    material_id = Column(UUID(as_uuid=False), ForeignKey("materials.id"), nullable=True)

    role = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)


class QuizQuestion(Base):
    __tablename__ = "quiz_questions"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    study_space_id = Column(UUID(as_uuid=False), ForeignKey("study_spaces.id"), nullable=False)

    question_text = Column(Text, nullable=False)
    options = Column(Text, nullable=False)
    correct_index = Column(Integer, nullable=False)
    topic = Column(String(150), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)


class QuizAttempt(Base):
    __tablename__ = "quiz_attempts"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    study_space_id = Column(UUID(as_uuid=False), ForeignKey("study_spaces.id"), nullable=False)

    score = Column(Integer, nullable=False)
    total = Column(Integer, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)


class QuizAttemptAnswer(Base):
    """One graded question within a QuizAttempt - what makes a real,
    per-topic Strengths/Needs Improvement breakdown possible, instead of
    just an aggregate score per attempt."""
    __tablename__ = "quiz_attempt_answers"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    attempt_id = Column(UUID(as_uuid=False), ForeignKey("quiz_attempts.id"), nullable=False)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    study_space_id = Column(UUID(as_uuid=False), ForeignKey("study_spaces.id"), nullable=False)

    topic = Column(String(150), nullable=True)
    is_correct = Column(Boolean, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)


class StudyEvent(Base):
    """
    Lightweight activity log - one row per meaningful interaction (quiz
    attempt, material opened, tutor message sent, narration played, lecture
    recorded). This is what Weeks Active, study streaks, and approximate
    study time are computed from - see app/routers/analytics.py.
    """
    __tablename__ = "study_events"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    study_space_id = Column(UUID(as_uuid=False), ForeignKey("study_spaces.id"), nullable=True)
    event_type = Column(String(50), nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)


class MasteryRecord(Base):
    """
    One row per (user, study space, topic). Updated after every quiz submit
    by app/mastery.py. mastery_percent is a weighted blend of past and
    latest performance, so it moves as the student improves or slips.
    """
    __tablename__ = "mastery_records"
    __table_args__ = (
        UniqueConstraint("user_id", "study_space_id", "topic", name="uq_mastery_user_space_topic"),
    )

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    study_space_id = Column(UUID(as_uuid=False), ForeignKey("study_spaces.id"), nullable=False)
    topic = Column(String(150), nullable=False)

    attempts = Column(Integer, default=0)
    questions_seen = Column(Integer, default=0)
    questions_correct = Column(Integer, default=0)
    mastery_percent = Column(Float, default=0.0)
    last_seen = Column(DateTime, default=datetime.utcnow)


class MaterialChunk(Base):
    """
    A slice of a material's text plus its embedding vector (stored as JSON
    text so no pgvector extension is needed). Powers Course Memory: the AI
    Tutor retrieves only the chunks relevant to a question - see app/rag.py.
    """
    __tablename__ = "material_chunks"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    material_id = Column(UUID(as_uuid=False), ForeignKey("materials.id"), nullable=False, index=True)
    study_space_id = Column(UUID(as_uuid=False), ForeignKey("study_spaces.id"), nullable=False, index=True)

    chunk_index = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    embedding = Column(Text, nullable=False)  # JSON list of floats

    created_at = Column(DateTime, default=datetime.utcnow)


class MessageFeedback(Base):
    """Like / dislike on a tutor reply. One row per (user, message)."""
    __tablename__ = "message_feedback"
    __table_args__ = (
        UniqueConstraint("user_id", "message_id", name="uq_feedback_user_message"),
    )

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    message_id = Column(UUID(as_uuid=False), ForeignKey("ai_interactions.id"), nullable=False, index=True)

    rating = Column(String(10), nullable=False)  # "like" | "dislike"
    reason = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)


class PasswordResetToken(Base):
    """
    One-time password-reset link. Only a SHA-256 hash of the token is stored,
    so a database leak can't be used to reset anyone's password.
    """
    __tablename__ = "password_reset_tokens"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False, index=True)
    token_hash = Column(String(64), unique=True, index=True, nullable=False)

    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class CourseSummary(Base):
    """AI study summary for a whole course, saved so it isn't regenerated on every visit."""
    __tablename__ = "course_summaries"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    study_space_id = Column(UUID(as_uuid=False), ForeignKey("study_spaces.id"), unique=True, nullable=False, index=True)

    content = Column(Text, nullable=False)       # JSON: overview, key_points, sections, key_terms
    source_hash = Column(String(64), nullable=False)  # fingerprint of the materials it was built from

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Flashcard(Base):
    __tablename__ = "flashcards"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    study_space_id = Column(UUID(as_uuid=False), ForeignKey("study_spaces.id"), nullable=False, index=True)

    front = Column(Text, nullable=False)
    back = Column(Text, nullable=False)
    topic = Column(String(150), nullable=True)

    times_known = Column(Integer, default=0)
    times_missed = Column(Integer, default=0)
    last_known = Column(Boolean, nullable=True)
    last_reviewed = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)


class Note(Base):
    """A student's own note for a course - typed by hand or saved from a tutor answer."""
    __tablename__ = "notes"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    study_space_id = Column(UUID(as_uuid=False), ForeignKey("study_spaces.id"), nullable=False, index=True)

    title = Column(String(200), nullable=False)
    content = Column(Text, nullable=False)
    source = Column(String(20), default="manual")        # "manual" | "tutor"
    source_message_id = Column(String(36), nullable=True)  # plain id, deliberately not a foreign key

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class VideoLesson(Base):
    """A 'slides + voice' lesson made from one tutor answer. Saved so reopening it costs nothing.
    message_id is a plain string (no foreign key) so deleting chats or courses never gets blocked by it."""
    __tablename__ = "video_lessons"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False, index=True)
    message_id = Column(String(36), nullable=True, index=True)
    title = Column(String(200), nullable=False)
    content = Column(Text, nullable=False)  # JSON list of scenes
    created_at = Column(DateTime, default=datetime.utcnow)


class Meeting(Base):
    """A live audio study meeting. Joined with a short code; capped at max_participants."""
    __tablename__ = "meetings"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    code = Column(String(8), unique=True, nullable=False, index=True)
    host_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False, index=True)
    title = Column(String(120), nullable=False)
    study_space_id = Column(String(36), nullable=True)  # plain string: never blocks deleting a course
    max_participants = Column(Integer, nullable=False, default=8)
    status = Column(String(12), nullable=False, default="active")  # active | ended
    created_at = Column(DateTime, default=datetime.utcnow)
    ended_at = Column(DateTime, nullable=True)


class MeetingParticipant(Base):
    __tablename__ = "meeting_participants"
    __table_args__ = (UniqueConstraint("meeting_id", "user_id", name="uq_meeting_user"),)

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    meeting_id = Column(UUID(as_uuid=False), ForeignKey("meetings.id"), nullable=False, index=True)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False, index=True)
    joined_at = Column(DateTime, default=datetime.utcnow)
    last_seen = Column(DateTime, default=datetime.utcnow)   # refreshed by the app every ~20 seconds
    left_at = Column(DateTime, nullable=True)


class Deadline(Base):
    """An assignment, test or exam with a due date. study_space_id is a plain string (no foreign key),
    so deleting a course keeps the deadline and just unlinks it."""
    __tablename__ = "deadlines"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False, index=True)
    study_space_id = Column(String(36), nullable=True, index=True)
    title = Column(String(200), nullable=False)
    kind = Column(String(20), nullable=False, default="assignment")  # assignment | test | exam | other
    due_date = Column(Date, nullable=False, index=True)
    due_time = Column(String(5), nullable=True)                     # "HH:MM", local time, optional
    notes = Column(Text, nullable=True)
    done = Column(Boolean, nullable=False, default=False)
    done_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class MaterialChapterSummary(Base):
    """Chapter-by-chapter summary of one long material. Plain-string ids (no foreign keys)
    so deleting a material or course is never blocked by it."""
    __tablename__ = "material_chapter_summaries"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    material_id = Column(String(36), unique=True, nullable=False, index=True)
    study_space_id = Column(String(36), nullable=False, index=True)
    content = Column(Text, nullable=False)     # JSON list of {title, summary, key_points}
    source_len = Column(Integer, nullable=False)  # length of the text it was made from, to spot changes
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

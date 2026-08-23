from datetime import datetime
from typing import Optional
from pydantic import BaseModel, EmailStr, ConfigDict


class SignUpRequest(BaseModel):
    full_name: str
    email: EmailStr
    password: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class AcademicProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    institution: Optional[str] = None
    programme: Optional[str] = None
    level: int
    xp: int
    avatar_url: Optional[str] = None


class AcademicProfileUpdate(BaseModel):
    institution: Optional[str] = None
    programme: Optional[str] = None
    avatar_url: Optional[str] = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    full_name: str
    email: EmailStr
    created_at: datetime
    academic_profile: Optional[AcademicProfileOut] = None


class CourseCreate(BaseModel):
    title: str
    subtitle: Optional[str] = None
    cover_color: Optional[str] = None
    cover_image_url: Optional[str] = None


class CourseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    subtitle: Optional[str] = None
    cover_image_url: Optional[str] = None


class StudySpaceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    progress_percent: float
    next_up: Optional[str] = None
    course: CourseOut


class StudySpaceProgressUpdate(BaseModel):
    progress_percent: float
    next_up: Optional[str] = None


class MaterialOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    filename: str
    content_type: Optional[str] = None
    status: str
    extracted_topics: Optional[str] = None  # JSON-encoded list of strings
    created_at: datetime
# ---------- AI Tutor ----------

class ChatMessageIn(BaseModel):
    message: str
    study_space_id: Optional[str] = None


class ChatMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    role: str
    content: str
    created_at: datetime
# ---------- Quiz ----------

class QuizGenerateRequest(BaseModel):
    num_questions: int = 5


class QuizQuestionOut(BaseModel):
    """Sent before submission - correct_index is deliberately left out."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    question_text: str
    options: list[str]


class QuizAnswerIn(BaseModel):
    question_id: str
    selected_index: int


class QuizSubmitRequest(BaseModel):
    answers: list[QuizAnswerIn]


class QuizReviewItem(BaseModel):
    question_id: str
    question_text: str
    options: list[str]
    correct_index: int
    selected_index: Optional[int] = None
    is_correct: bool


class QuizSubmitResult(BaseModel):
    score: int
    total: int
    review: list[QuizReviewItem]

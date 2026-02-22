from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from enum import Enum
import datetime

from extensions import db
bcrypt = Bcrypt()

class UserRole(Enum):
    STUDENT = 'student'
    COUNSELOR = 'counselor'
    ADMIN = 'admin'
    PEER_VOLUNTEER = 'peer_volunteer'
    MODERATOR = 'moderator'

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    role = db.Column(db.Enum(UserRole), default=UserRole.STUDENT, nullable=False)
    email_hash = db.Column(db.String(128), unique=True, nullable=True)
    is_blocked = db.Column(db.Boolean, default=False)

    def set_password(self, password):
        self.password_hash = bcrypt.generate_password_hash(password).decode('utf-8')

    def check_password(self, password):
        return bcrypt.check_password_hash(self.password_hash, password)

# --- MODELS FOR DASHBOARD FUNCTIONALITY ---

class MoodCheckin(db.Model):
    __tablename__ = 'mood_checkin'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    mood = db.Column(db.String(50), nullable=False)
    timestamp = db.Column(db.DateTime, nullable=False, default=datetime.datetime.utcnow)
    
    user = db.relationship('User', backref=db.backref('mood_checkins', lazy=True))

class JournalEntry(db.Model):
    __tablename__ = 'journal_entry'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    content = db.Column(db.Text, nullable=False)
    entry_type = db.Column(db.String(50), default='reflection')
    timestamp = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    
    user = db.relationship('User', backref=db.backref('journal_entries', lazy=True))

class UserActivityLog(db.Model):
    __tablename__ = 'user_activity_log'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    resource_id = db.Column(db.Integer, db.ForeignKey('resource.id'), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    
    user = db.relationship('User', backref=db.backref('activity_logs', lazy=True))
    resource = db.relationship('Resource', backref=db.backref('activity_logs', lazy=True))
    
# -------------------------------------------

class ChatHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    conversation_id = db.Column(db.String(36), nullable=False, index=True)
    sender = db.Column(db.String(10), nullable=False) # 'user' or 'bot'
    message = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    
    # Analytics Fields
    intent = db.Column(db.String(100), nullable=True)
    emotion = db.Column(db.String(50), nullable=True)
    sentiment_score = db.Column(db.Float, nullable=True)
    is_crisis = db.Column(db.Boolean, default=False)

    user = db.relationship('User', backref=db.backref('chat_history', lazy=True))

class ChatSession(db.Model):
    __tablename__ = 'chat_session'
    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.String(36), unique=True, nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    start_time = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    end_time = db.Column(db.DateTime, nullable=True)
    is_completed = db.Column(db.Boolean, default=False)
    
    # Impact Metrics
    feedback_score = db.Column(db.Integer, nullable=True) # e.g., 1 for positive, 0 for negative
    feedback_text = db.Column(db.Text, nullable=True)
    primary_emotion = db.Column(db.String(50), nullable=True)
    
    user = db.relationship('User', backref=db.backref('chat_sessions', lazy=True))


class ConfidentialData(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, unique=True)
    user = db.relationship('User', backref=db.backref('confidential_data', uselist=False))
    name = db.Column(db.String(150), nullable=False)
    phone_number = db.Column(db.String(20), nullable=False)
    parent_name = db.Column(db.String(150), nullable=False)
    parent_phone_number = db.Column(db.String(20), nullable=False)

class VerificationCode(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), nullable=False, index=True)
    code_hash = db.Column(db.String(128), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)

class CounselorProfile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    user = db.relationship('User', backref=db.backref('counselor_profile', uselist=False))
    specialization = db.Column(db.String(150))
    availability = db.Column(db.JSON)
    meeting_location = db.Column(db.String(255), nullable=True)


class Appointment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    counselor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    appointment_time = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(20), default='pending', nullable=False)
    notes = db.Column(db.Text, nullable=True)
    # UPDATED: Changed default and comment to reflect frontend options
    mode = db.Column(db.String(20), nullable=False, default='video_call') # Modes: 'video_call', 'voice_call', 'message', 'in_person'
    meeting_link = db.Column(db.String(255), nullable=True) 
    student = db.relationship('User', foreign_keys=[student_id])
    counselor = db.relationship('User', foreign_keys=[counselor_id])
    session_started_at = db.Column(db.DateTime, nullable=True)
    session_ended_at = db.Column(db.DateTime, nullable=True)

class Resource(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text, nullable=False)
    type = db.Column(db.String(50), nullable=False) # video, audio, article
    language = db.Column(db.String(50), default='English')
    url = db.Column(db.String(255), nullable=True) # Optional for articles
    content = db.Column(db.Text, nullable=True) # For articles
    status = db.Column(db.String(20), default='pending') # pending, licensed, rejected
    author_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True) # Counselor who created it
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    
    author = db.relationship('User', backref=db.backref('resources', lazy=True))

class ChatMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    receiver_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    content = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    is_read = db.Column(db.Boolean, default=False)
    
    sender = db.relationship('User', foreign_keys=[sender_id], backref='sent_messages')
    receiver = db.relationship('User', foreign_keys=[receiver_id], backref='received_messages')

class ClientNote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    counselor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    note = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    
    counselor = db.relationship('User', foreign_keys=[counselor_id])
    student = db.relationship('User', foreign_keys=[student_id])
    
class Community(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    description = db.Column(db.Text, nullable=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    
    creator = db.relationship('User', foreign_keys=[created_by_id])
    posts = db.relationship('ForumPost', backref='community', lazy='dynamic', cascade="all, delete-orphan")
    members = db.relationship('CommunityMember', backref='community', lazy='dynamic', cascade="all, delete-orphan")

class CommunityMember(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    joined_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    
    user = db.relationship('User', backref='community_memberships')

class ForumPost(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), nullable=False)
    author_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, nullable=False)
    media_url = db.Column(db.String(255), nullable=True)
    likes_count = db.Column(db.Integer, default=0)
    timestamp = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    author = db.relationship('User', backref='forum_posts')
    replies = db.relationship('ForumReply', backref='post', lazy='dynamic', cascade="all, delete-orphan")

class ForumReply(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey('forum_post.id'), nullable=False)
    author_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    content = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    author = db.relationship('User', backref='forum_replies')

class Notification(db.Model):
    __tablename__ = 'notification'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    message = db.Column(db.String(255), nullable=False)
    link = db.Column(db.String(255), nullable=True)
    is_read = db.Column(db.Boolean, default=False, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.datetime.utcnow)

    user = db.relationship('User', backref=db.backref('notifications', lazy=True))

class AssessmentResult(db.Model):
    __tablename__ = 'assessment_result'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    test_type = db.Column(db.String(20), nullable=False) # PHQ-9, GAD-7, GHQ-12
    score = db.Column(db.Integer, nullable=False)
    interpretation = db.Column(db.String(100), nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    
    user = db.relationship('User', backref=db.backref('assessment_results', lazy=True))
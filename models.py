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

class User(db.Model):
    """
    User model for storing anonymous login credentials.
    The username is system-generated and unique.
    Email is NOT stored here to maintain anonymity.
    """
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    role = db.Column(db.Enum(UserRole), default=UserRole.STUDENT, nullable=False)
    
    def set_password(self, password):
        self.password_hash = bcrypt.generate_password_hash(password).decode('utf-8')

    def check_password(self, password):
        return bcrypt.check_password_hash(self.password_hash, password)
class ChatHistory(db.Model):
    """
    Stores a record of a single turn in a conversation between a user and the chatbot.
    """
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    conversation_id = db.Column(db.String(100), nullable=False, index=True)
    
    # Store the raw, complete JSON payload from Botpress for future analysis
    botpress_payload = db.Column(db.JSON, nullable=True) 
    
    # For quick access, you can also store specific parts of the conversation
    user_message = db.Column(db.Text, nullable=True)
    bot_response = db.Column(db.Text, nullable=True)
    
    timestamp = db.Column(db.DateTime, default=datetime.datetime.utcnow)

    # Establish the relationship to the User model
    user = db.relationship('User', backref=db.backref('chat_history', lazy=True))
class ConfidentialData(db.Model):
    """
    Stores confidential user details in a separate table,
    linked one-to-one with the User model.
    """
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, unique=True)
    user = db.relationship('User', backref=db.backref('confidential_data', uselist=False))
    name = db.Column(db.String(150), nullable=False)
    phone_number = db.Column(db.String(20), nullable=False)
    parent_name = db.Column(db.String(150), nullable=False)
    parent_phone_number = db.Column(db.String(20), nullable=False)

class VerificationCode(db.Model):
    """
    Temporarily stores verification codes for the registration process.
    """
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), nullable=False, index=True)
    code_hash = db.Column(db.String(128), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)

class CounselorProfile(db.Model):
    """Profile for counselors, linked to a User."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    user = db.relationship('User', backref=db.backref('counselor_profile', uselist=False))
    specialization = db.Column(db.String(150))
    availability = db.Column(db.JSON)

class Appointment(db.Model):
    """Model for booking appointments."""
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    counselor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    appointment_time = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(20), default='booked', nullable=False)
    notes = db.Column(db.Text, nullable=True)

    student = db.relationship('User', foreign_keys=[student_id])
    counselor = db.relationship('User', foreign_keys=[counselor_id])

class Resource(db.Model):
    """Model for psychoeducational resources."""
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text, nullable=False)
    type = db.Column(db.String(50), nullable=False)
    language = db.Column(db.String(50), default='English')
    url = db.Column(db.String(255), nullable=False)
    
class ForumPost(db.Model):
    """Model for posts in the peer support forum."""
    id = db.Column(db.Integer, primary_key=True)
    author_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    
    author = db.relationship('User', backref='forum_posts')
    replies = db.relationship('ForumReply', backref='post', lazy='dynamic', cascade="all, delete-orphan")

class ForumReply(db.Model):
    """Model for replies to a forum post."""
    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey('forum_post.id'), nullable=False)
    author_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    content = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    
    author = db.relationship('User', backref='forum_replies')


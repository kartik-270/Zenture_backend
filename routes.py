from flask import Blueprint, request, jsonify
# Corrected: Import ALL necessary models from the models file
from models import (
    db, User, UserRole, VerificationCode, ConfidentialData, 
    Resource, CounselorProfile, Appointment, ForumPost, ForumReply, bcrypt,ChatHistory
)
from extensions import mail

from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity, get_jwt
from functools import wraps
import datetime
from datetime import timedelta, datetime as dt
import re
import random
from flask_mail import Message

# Create a Blueprint for the API
api_bp = Blueprint('api', __name__)

# --- Helper Functions ---
def send_verification_email(email, code):
    """
    Sends a verification email to the user with the provided OTP code.
    """
    try:
        subject = "Your Verification Code for Mental Health Platform"
        msg = Message(subject, recipients=[email])
        msg.body = f"""
Hello,

Thank you for registering. Your verification code is: {code}

This code will expire in 10 minutes.

If you did not request this, please ignore this email.
"""
        mail.send(msg)
        return True
    except Exception as e:
        print(f"Error sending email: {e}")
        return False

def generate_unique_username():
    """Generates a username that doesn't already exist."""
    while True:
        adjectives = ['Quiet', 'Bright', 'Silent', 'Happy', 'Blue', 'Green', 'Red']
        nouns = ['River', 'Sun', 'Moon', 'Star', 'Tree', 'Sky', 'Sea']
        username = random.choice(adjectives) + random.choice(nouns) + str(random.randint(100, 999))
        if not User.query.filter_by(username=username).first():
            return username

# --- Authorization Decorators ---
def roles_required(*roles):
    def wrapper(fn):
        @wraps(fn)
        @jwt_required()
        def decorator(*args, **kwargs):
            claims = get_jwt()
            user_role_str = claims.get('role')
            # Convert string role from token back to Enum member
            user_role = UserRole(user_role_str)
            
            # Convert required roles from string to Enum members
            required_roles = [UserRole(r) for r in roles]
            
            if user_role not in required_roles:
                return jsonify(msg="Access forbidden: insufficient permissions"), 403
            return fn(*args, **kwargs)
        return decorator
    return wrapper

# --- Authentication & Registration Endpoints ---

@api_bp.route('/register/start', methods=['POST'])
def register_start():
    data = request.get_json()
    email = data.get('email')

    if not email:
        return jsonify(msg="Email is required"), 400

    email_regex = r"^[a-zA-Z0-9._%+-]+@akgec\.ac\.in$"
    if not re.match(email_regex, email):
        return jsonify(msg="Please use a valid AKGEC college email ID."), 400
    
    otp = str(random.randint(100000, 999999))
    code_hash = bcrypt.generate_password_hash(otp).decode('utf-8')
    expires_at = datetime.datetime.utcnow() + timedelta(minutes=10)

    VerificationCode.query.filter_by(email=email).delete()

    new_code = VerificationCode(email=email, code_hash=code_hash, expires_at=expires_at)
    db.session.add(new_code)
    db.session.commit()

    if not send_verification_email(email, otp):
        return jsonify(msg="Could not send verification email. Please try again later."), 500

    return jsonify(msg="Verification code sent to your email. It will expire in 10 minutes."), 200

@api_bp.route('/register/verify-and-create', methods=['POST'])
def register_verify_and_create():
    data = request.get_json()
    email = data.get('email')
    otp = data.get('otp')
    password = data.get('password')

    if not all([email, otp, password]):
        return jsonify(msg="Email, OTP, and password are required."), 400

    verification = VerificationCode.query.filter_by(email=email).order_by(VerificationCode.expires_at.desc()).first()

    if not verification:
        return jsonify(msg="Invalid email or code has expired."), 404
    
    if datetime.datetime.utcnow() > verification.expires_at:
        db.session.delete(verification)
        db.session.commit()
        return jsonify(msg="Verification code has expired."), 400

    if not bcrypt.check_password_hash(verification.code_hash, otp):
        return jsonify(msg="Invalid verification code."), 401

    unique_username = generate_unique_username()
    new_user = User(username=unique_username, role=UserRole.STUDENT)
    new_user.set_password(password)
    
    db.session.add(new_user)
    db.session.delete(verification)
    db.session.commit()
    
    return jsonify(msg="Account created successfully. Please log in with your unique username.", username=unique_username), 201

@api_bp.route('/register/complete-profile', methods=['POST'])
@jwt_required()
def complete_profile():
    data = request.get_json()
    user_id = get_jwt_identity()

    if not data.get('consent'):
        return jsonify(msg="Consent is required to store personal information."), 403
        
    required_fields = ['name', 'phone_number', 'parent_name', 'parent_phone_number']
    if not all(field in data for field in required_fields):
        return jsonify(msg="All personal detail fields are required."), 400
        
    if ConfidentialData.query.filter_by(user_id=user_id).first():
        return jsonify(msg="Profile already completed."), 409

    confidential_info = ConfidentialData(
        user_id=user_id,
        name=data['name'],
        phone_number=data['phone_number'],
        parent_name=data['parent_name'],
        parent_phone_number=data['parent_phone_number']
    )
    db.session.add(confidential_info)
    db.session.commit()

    return jsonify(msg="Your profile has been securely saved."), 201

# ... (rest of the file remains the same)

@api_bp.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    user = User.query.filter_by(username=data.get('username')).first()

    if user and user.check_password(data.get('password')):
        additional_claims = {"role": user.role.value}
        # Cast user.id to a string to satisfy PyJWT's subject validation
        access_token = create_access_token(
            identity=str(user.id), 
            additional_claims=additional_claims
        )
        return jsonify(access_token=access_token)

    return jsonify(msg="Bad username or password"), 401
@api_bp.route('/botpress/webhook/save-chat', methods=['POST'])
@jwt_required()
def save_botpress_chat():
    """
    Webhook to receive and save chat history from Botpress for the logged-in user.
    The user is identified by the JWT sent in the Authorization header.
    """
    try:
        # 1. Get the user's ID from their login token. This is secure.
        user_id_from_token = get_jwt_identity()
        
        # Check if the user exists in the database
        user = User.query.get(user_id_from_token)
        if not user:
            return jsonify(msg="User not found."), 404

        # 2. Get the chat data payload sent from Botpress
        data = request.get_json()
        if not data:
            return jsonify(msg="Missing JSON payload in request"), 400

        # 3. Extract relevant information from the Botpress payload
        #    (You may need to adjust these keys based on what your bot sends)
        conversation_id = data.get('conversationId')
        user_message = data.get('userMessage')
        bot_response = data.get('botResponse')

        if not conversation_id:
            return jsonify(msg="Payload must include a 'conversationId'"), 400

        # 4. Create the new chat history record, linking it to the logged-in user
        new_chat_entry = ChatHistory(
            user_id=user.id,  # Assign the chat record to the authenticated user
            conversation_id=conversation_id,
            user_message=user_message,
            bot_response=bot_response,
            botpress_payload=data  # Store the full original payload for auditing
        )

        # 5. Save the record to the database
        db.session.add(new_chat_entry)
        db.session.commit()

        return jsonify(msg=f"Chat log saved for user {user.username}"), 200

    except Exception as e:
        db.session.rollback()
        print(f"ERROR in /botpress/webhook/save-chat: {e}") # For debugging
        return jsonify(msg="An internal server error occurred."), 500

# ... (rest of the file remains the same)
# --- Resource Hub Endpoints ---

@api_bp.route('/resources', methods=['GET'])
@jwt_required()
def get_resources():
    resources = Resource.query.all()
    return jsonify([{
        "id": r.id,
        "title": r.title,
        "description": r.description,
        "type": r.type,
        "language": r.language,
        "url": r.url
    } for r in resources])

@api_bp.route('/resources', methods=['POST'])
@roles_required('admin', 'counselor')
def add_resource():
    data = request.get_json()
    new_resource = Resource(
        title=data['title'],
        description=data['description'],
        type=data['type'],
        language=data.get('language', 'English'),
        url=data['url']
    )
    db.session.add(new_resource)
    db.session.commit()
    return jsonify(msg="Resource added successfully"), 201

# --- Counselor and Appointment Endpoints ---

@api_bp.route('/counselors', methods=['GET'])
@jwt_required()
def get_counselors():
    counselors = User.query.filter_by(role=UserRole.COUNSELOR).all()
    counselor_list = []
    for c in counselors:
        profile = CounselorProfile.query.filter_by(user_id=c.id).first()
        counselor_list.append({
            "id": c.id,
            "username": c.username, # Anonymous username
            "specialization": profile.specialization if profile else "N/A",
            "availability": profile.availability if profile else "N/A"
        })
    return jsonify(counselor_list)

@api_bp.route('/appointments', methods=['POST'])
@roles_required('student')
def book_appointment():
    data = request.get_json()
    student_id = get_jwt_identity()
    
    appointment_time_str = data.get('appointment_time')
    appointment_time = dt.fromisoformat(appointment_time_str)

    new_appointment = Appointment(
        student_id=student_id,
        counselor_id=data['counselor_id'],
        appointment_time=appointment_time,
        notes=data.get('notes')
    )
    db.session.add(new_appointment)
    db.session.commit()
    return jsonify(msg="Appointment booked successfully."), 201

@api_bp.route('/appointments', methods=['GET'])
@jwt_required()
def get_appointments():
    user_id = get_jwt_identity()
    claims = get_jwt()
    user_role = claims.get('role')

    if user_role == 'student':
        appointments = Appointment.query.filter_by(student_id=user_id).all()
    elif user_role == 'counselor':
        appointments = Appointment.query.filter_by(counselor_id=user_id).all()
    else:
        return jsonify([]) # Admins/others don't see personal appointments this way

    return jsonify([{
        "id": a.id,
        "student_id": a.student_id,
        "counselor_id": a.counselor_id,
        "appointment_time": a.appointment_time.isoformat(),
        "status": a.status
    } for a in appointments])

# --- Forum Endpoints ---

@api_bp.route('/forum/posts', methods=['GET'])
@jwt_required()
def get_forum_posts():
    posts = ForumPost.query.order_by(ForumPost.timestamp.desc()).all()
    return jsonify([{
        "id": p.id,
        "author_username": p.author.username,
        "title": p.title,
        "content": p.content,
        "timestamp": p.timestamp.isoformat()
    } for p in posts])

@api_bp.route('/forum/posts', methods=['POST'])
@jwt_required()
def create_forum_post():
    data = request.get_json()
    author_id = get_jwt_identity()
    post = ForumPost(
        author_id=author_id,
        title=data['title'],
        content=data['content']
    )
    db.session.add(post)
    db.session.commit()
    return jsonify(msg="Post created successfully"), 201

@api_bp.route('/forum/posts/<int:post_id>', methods=['GET'])
@jwt_required()
def get_single_post(post_id):
    post = ForumPost.query.get_or_404(post_id)
    replies = [{
        "id": r.id,
        "author_username": r.author.username,
        "content": r.content,
        "timestamp": r.timestamp.isoformat()
    } for r in post.replies]
    
    return jsonify({
        "id": post.id,
        "author_username": post.author.username,
        "title": post.title,
        "content": post.content,
        "timestamp": post.timestamp.isoformat(),
        "replies": replies
    })

@api_bp.route('/forum/posts/<int:post_id>/reply', methods=['POST'])
@jwt_required()
def reply_to_post(post_id):
    post = ForumPost.query.get_or_404(post_id)
    data = request.get_json()
    author_id = get_jwt_identity()
    
    reply = ForumReply(
        post_id=post.id,
        author_id=author_id,
        content=data['content']
    )
    db.session.add(reply)
    db.session.commit()
    return jsonify(msg="Reply posted successfully"), 201


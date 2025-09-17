from flask import Blueprint, request, jsonify
from models import (
    db, User, UserRole, VerificationCode, ConfidentialData, 
    Resource, CounselorProfile, Appointment, ForumPost, ForumReply, bcrypt, ChatHistory, MoodCheckin, JournalEntry,Notification, UserActivityLog
)
from extensions import mail

from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity, get_jwt
from functools import wraps
import datetime
from datetime import timedelta, datetime as dt
import re
import random
import uuid # Import uuid for generating unique links
from flask_mail import Message

api_bp = Blueprint('api', __name__)

@api_bp.route('/admin/register', methods=['POST'])
def register_admin():
    data = request.get_json()
    username = data.get('username')
    password = data.get('password')

    if not username or not password:
        return jsonify(msg="Username and password are required"), 400

    if User.query.filter_by(username=username).first():
        return jsonify(msg="Username already exists"), 409

    new_admin = User(username=username, role=UserRole.ADMIN)
    new_admin.set_password(password)

    db.session.add(new_admin)
    db.session.commit()

    return jsonify(msg="Admin registered successfully"), 201

def send_verification_email(email, code):
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
    while True:
        adjectives = ['Quiet', 'Bright', 'Silent', 'Happy', 'Blue', 'Green', 'Red']
        nouns = ['River', 'Sun', 'Moon', 'Star', 'Tree', 'Sky', 'Sea']
        username = random.choice(adjectives) + random.choice(nouns) + str(random.randint(100, 999))
        if not User.query.filter_by(username=username).first():
            return username
            
def generate_random_slot(date_str):
    try:
        date_obj = dt.strptime(date_str, "%Y-%m-%d")
        slots = ["09:00", "10:30", "12:00", "14:00", "15:30", "17:00"]
        time_str = random.choice(slots)
        hour, minute = map(int, time_str.split(":"))
        return dt(date_obj.year, date_obj.month, date_obj.day, hour, minute)
    except ValueError:
        return None

# --- Authorization Decorators ---
def roles_required(*roles):
    def wrapper(fn):
        @wraps(fn)
        @jwt_required()
        def decorator(*args, **kwargs):
            claims = get_jwt()
            user_role_str = claims.get('role')
            user_role = UserRole(user_role_str)
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

    for u in User.query.all():
        if u.email_hash and bcrypt.check_password_hash(u.email_hash, email):
            return jsonify(msg="An account with this email already exists."), 409

    unique_username = generate_unique_username()
    email_hash = bcrypt.generate_password_hash(email).decode('utf-8')

    new_user = User(username=unique_username, email_hash=email_hash, role=UserRole.STUDENT)
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

@api_bp.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    user = User.query.filter_by(username=data.get('username')).first()

    if user and user.check_password(data.get('password')):
        additional_claims = {"role": user.role.value}
        access_token = create_access_token(
            identity=str(user.id), 
            additional_claims=additional_claims
        )
        return jsonify(access_token=access_token,username=user.username)

    return jsonify(msg="Bad username or password"), 401
    
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
@api_bp.route('/admin/dashboard', methods=['GET'])
@roles_required('admin')
def admin_dashboard_data():
    """
    An endpoint exclusively for admins. 
    Provides basic statistics for the admin dashboard.
    """
    user_count = User.query.count()
    appointment_count = Appointment.query.count()
    
    return jsonify({
        "message": "Welcome, Admin!",
        "stats": {
            "totalUsers": user_count,
            "totalAppointments": appointment_count
        }
    }), 200
@api_bp.route("/counselors", methods=["GET"])
@jwt_required()
def get_counselors():
    counselors = (
        db.session.query(CounselorProfile)
        .join(User)
        .filter(User.role == UserRole.COUNSELOR)
        .all()
    )

    result = []
    for c in counselors:
        dummy_reviews = 5 
        dummy_image = f"https://i.pravatar.cc/150?u={c.user.id}"
        result.append({
            "id": c.id, 
            "user_id": c.user.id, 
            "name": c.user.username,
            "specialty": c.specialization,
            "reviews": dummy_reviews, 
            "image": dummy_image
        })
    return jsonify(result)

@api_bp.route('/appointments', methods=['POST'])
@jwt_required()
def book_appointment():
    data = request.json
    student_id = get_jwt_identity()
    
    counselor_id = data.get('counselor_id')
    appointment_date = data.get('appointment_date')
    mode = data.get('mode') # Get mode from frontend (e.g., 'video_call', 'in_person')
    description = data.get('description')

    if not all([counselor_id, appointment_date, mode]):
        return jsonify({"error": "Counselor ID, date, and mode are required"}), 400
    
    try:
        counselor = User.query.get(int(counselor_id))
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid counselor ID format."}), 400
    
    if not counselor or counselor.role != UserRole.COUNSELOR:
        return jsonify({"error": "Counselor not found or invalid"}), 404

    appointment_time = generate_random_slot(appointment_date)
    if not appointment_time:
        return jsonify({"error": "Invalid date format or value."}), 400

    # Rectified: Generate a meeting link only for 'video_call' mode
    meeting_link = None
    if mode == 'video_call':
        meeting_link = f"/session/{uuid.uuid4()}"

    new_appointment = Appointment(
        student_id=student_id,
        counselor_id=counselor.id,
        appointment_time=appointment_time,
        status="booked",
        notes=description,
        mode=mode, # Save the mode from the frontend
        meeting_link=meeting_link
    )
    db.session.add(new_appointment)
    student_msg = f"Your appointment with {counselor.username} is confirmed for {appointment_time.strftime('%b %d, %Y at %I:%M %p')}."
    counselor_msg = f"You have a new appointment with a student for {appointment_time.strftime('%b %d, %Y at %I:%M %p')}."

    student_notification = Notification(user_id=student_id, message=student_msg, link=meeting_link)
    counselor_notification = Notification(user_id=counselor.id, message=counselor_msg, link=meeting_link)
    
    db.session.add(student_notification)
    db.session.add(counselor_notification)
    db.session.commit()
    
    return jsonify({
        "message": "Appointment confirmed!",
        "appointment": {
            "counselor": counselor.username,
            "date": appointment_time.strftime("%Y-%m-%d"),
            "time": appointment_time.strftime("%H:%M"),
            "mode": mode,
            "description": description,
            "status": new_appointment.status,
            "meeting_link": new_appointment.meeting_link
        }
    }), 201
@api_bp.route('/notifications', methods=['GET'])
@jwt_required()
def get_notifications():
    user_id = get_jwt_identity()
    notifications = Notification.query.filter_by(user_id=user_id, is_read=False).order_by(Notification.timestamp.desc()).all()
    
    return jsonify([{
        "id": n.id,
        "message": n.message,
        "link": n.link,
        "timestamp": n.timestamp.isoformat()
    } for n in notifications])
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
        return jsonify([])

    return jsonify([{
        "id": a.id,
        "student_id": a.student_id,
        "counselor_id": a.counselor_id,
        "appointment_time": a.appointment_time.isoformat(),
        "status": a.status,
        "mode": a.mode,
        "meeting_link": a.meeting_link
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

# --- Dashboard Endpoints ---

@api_bp.route('/mood-checkin/today-status', methods=['GET']) # <-- RENAMED ENDPOINT
@jwt_required()
def get_today_mood_checkin():
    """Checks if the current user has already submitted a mood today."""
    user_id = get_jwt_identity()
    today_start = datetime.datetime.combine(datetime.date.today(), datetime.time.min)
    today_end = datetime.datetime.combine(datetime.date.today(), datetime.time.max)

    checkin = MoodCheckin.query.filter(
        MoodCheckin.user_id == user_id,
        MoodCheckin.timestamp >= today_start,
        MoodCheckin.timestamp <= today_end
    ).first()

    if checkin:
        return jsonify({"hasCheckedIn": True, "mood": checkin.mood})
    else:
        return jsonify({"hasCheckedIn": False})

@api_bp.route('/mood-checkin', methods=['POST'])
@jwt_required()
def add_mood_checkin():
    """Adds a mood check-in for the current user, if one for today doesn't exist."""
    user_id = get_jwt_identity()
    
    today_start = datetime.datetime.combine(datetime.date.today(), datetime.time.min)
    today_end = datetime.datetime.combine(datetime.date.today(), datetime.time.max)
    existing_checkin = MoodCheckin.query.filter(
        MoodCheckin.user_id == user_id,
        MoodCheckin.timestamp >= today_start,
        MoodCheckin.timestamp <= today_end
    ).first()

    if existing_checkin:
        return jsonify(msg="You have already checked in today."), 409

    data = request.get_json()
    mood = data.get('mood')
    
    if not mood:
        return jsonify(msg="Mood is required"), 400

    new_checkin = MoodCheckin(user_id=user_id, mood=mood)
    db.session.add(new_checkin)
    db.session.commit()
    
    return jsonify(msg="Mood saved successfully"), 201

@api_bp.route('/mood-history', methods=['GET'])
@jwt_required()
def get_mood_history():
    user_id = get_jwt_identity()
    days_str = request.args.get('days', '7')
    
    try:
        days = int(days_str)
    except ValueError:
        return jsonify(msg="Invalid 'days' parameter"), 400

    start_date = dt.utcnow() - timedelta(days=days)
    
    checkins = MoodCheckin.query.filter(
        MoodCheckin.user_id == user_id,
        MoodCheckin.timestamp >= start_date
    ).order_by(MoodCheckin.timestamp.asc()).all()
    
    history = [{
        "mood": c.mood,
        "date": c.timestamp.isoformat()
    } for c in checkins]
    
    return jsonify(history)

@api_bp.route('/dashboard/activity-summary', methods=['GET'])
@jwt_required()
def get_activity_summary():
    user_id = get_jwt_identity()

    seven_days_ago = dt.utcnow() - timedelta(days=7)
    journal_count = JournalEntry.query.filter(
        JournalEntry.user_id == user_id,
        JournalEntry.timestamp >= seven_days_ago
    ).count()

    assessment_count = 1 

    return jsonify({
        "journalEntriesThisWeek": journal_count,
        "assessmentsCompleted": assessment_count
    })
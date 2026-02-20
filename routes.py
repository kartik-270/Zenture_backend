from flask import Blueprint, request, jsonify, current_app
from flask_cors import CORS
from sqlalchemy import func
import re
import os 
from werkzeug.utils import secure_filename
from models import (
    db, User, UserRole, VerificationCode, ConfidentialData, 
    Resource, CounselorProfile, Appointment, ForumPost, ForumReply, bcrypt, ChatHistory, MoodCheckin, JournalEntry,Notification, UserActivityLog,
    ChatMessage, ClientNote, ChatSession
)
from extensions import mail
from followupquestions import FOLLOW_UP_QUESTIONS
from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity, get_jwt, get_jwt_header
from functools import wraps
import datetime
from datetime import datetime as dt, timedelta
import re
import random
import uuid
import os
import shutil
import base64
from flask_mail import Message
import resend

api_bp = Blueprint('api', __name__)
CORS(api_bp, resources={r"/api/*": {"origins": "*"}}, supports_credentials=True) # Enable CORS for all routes in this blueprint

MODELS = {
    "LISTENER": None,
    "RESPONDER": None,
    "EMOTION": None,
    "SENTIMENT": None
}

def get_chatbot_models():
    """Lazy-loads models only when needed."""
    if MODELS["LISTENER"] is None:
        from transformers import pipeline, AutoModelForCausalLM, AutoTokenizer
        from peft import PeftModel
        
        print("Loading models into memory...")
        # Load Listener
        MODELS["LISTENER"] = pipeline("text-classification", model="./listener_model", tokenizer="./listener_model")
        
        # Load Responder (LoRA)
        base_model = AutoModelForCausalLM.from_pretrained("microsoft/DialoGPT-medium")
        model = PeftModel.from_pretrained(base_model, "./responder_model")
        tokenizer = AutoTokenizer.from_pretrained("./responder_model")
        MODELS["RESPONDER"] = pipeline("text-generation", model=model, tokenizer=tokenizer)
        
        # Load Analytics
        MODELS["EMOTION"] = pipeline("text-classification", model="bhadresh-savani/bert-base-uncased-emotion", return_all_scores=True)
        MODELS["SENTIMENT"] = pipeline("text-classification", model="distilbert-base-uncased-finetuned-sst-2-english")
        
    return MODELS

CRISIS_RESPONSE = "I'm so sorry you're going through this. Please know that help is available. You can connect with someone immediately by calling 988 in the US or finding a local crisis hotline. Your life is important, and support is available."

@api_bp.route('/test-email', methods=['GET'])
def test_email_config():
    """Diagnostic endpoint to check email configuration and connectivity."""
    config_info = {
        "MAIL_SERVER": current_app.config.get('MAIL_SERVER'),
        "MAIL_PORT": current_app.config.get('MAIL_PORT'),
        "MAIL_USE_TLS": current_app.config.get('MAIL_USE_TLS'),
        "MAIL_USE_SSL": current_app.config.get('MAIL_USE_SSL'),
        "RESEND_KEY_SET": bool(current_app.config.get('RESEND_API_KEY')),
    }
    
    # Check if we should use Resend (for production)
    if current_app.config.get('RESEND_API_KEY'):
        try:
            resend.api_key = current_app.config.get('RESEND_API_KEY')
            params = {
                "from": "Zenture <onboarding@resend.dev>",
                "to": current_app.config.get('MAIL_USERNAME') or "recipient@example.com",
                "subject": "Zenture Email Diagnostic (Resend)",
                "text": "This email was sent via the Resend HTTP API to bypass SMTP blocks."
            }
            resend.Emails.send(params)
            return jsonify({"status": "success", "method": "Resend API", "config": config_info}), 200
        except Exception as e:
            return jsonify({"status": "error", "method": "Resend API", "error": str(e), "config": config_info}), 500

    # Fallback to SMTP (for local)
    import eventlet
    try:
        with eventlet.Timeout(10):
            subject = "Zenture Email Diagnostic (SMTP)"
            msg = Message(subject, recipients=[current_app.config.get('MAIL_USERNAME')])
            msg.body = "This email was sent via the standard SMTP fallback."
            mail.send(msg)
            return jsonify({"status": "success", "method": "SMTP", "config": config_info}), 200
    except Exception as e:
        return jsonify({"status": "error", "method": "SMTP", "error": str(e), "config": config_info}), 500

def send_with_resend(to_email, subject, body_text):
    """Helper to send email via Resend API."""
    try:
        if not current_app.config.get('RESEND_API_KEY'):
            return False
        resend.api_key = current_app.config.get('RESEND_API_KEY')
        params = {
            "from": "Zenture <onboarding@resend.dev>",
            "to": to_email,
            "subject": subject,
            "text": body_text,
        }
        resend.Emails.send(params)
        return True
    except Exception as e:
        print(f"Resend error: {e}")
        return False


@api_bp.route('/chatbot', methods=['POST'])
def chatbot_endpoint():
    # 1. Initialize models using lazy loader (prevents Port Not Found error)
    models = get_chatbot_models()
    
    # 2. Safety check: Ensure models are ready
    if not models["LISTENER"] or not models["RESPONDER"]:
        return jsonify(
            response="The chatbot models are currently initializing. Please try again in a few seconds.", 
            followUps=[], 
            conversation_id=None
        ), 503

    # 3. Identify user from JWT (if provided)
    user_id = None
    try:
        if get_jwt_header():
            user_id = get_jwt_identity()
    except Exception:
        pass

    data = request.get_json() or {}
    user_input = data.get('message', '')
    # Generate or retrieve conversation ID
    conversation_id = data.get('conversation_id') or str(uuid.uuid4())

    if not user_input:
        return jsonify(response="Please provide a message.", followUps=[], conversation_id=conversation_id), 400

    # 4. Analytics: Emotion & Sentiment Detection
    emotion_label = 'neutral'
    sentiment_score = 0.5
    try:
        if models["EMOTION"]:
            raw_emotions = models["EMOTION"](user_input)
            emotions = raw_emotions[0] if isinstance(raw_emotions[0], list) else raw_emotions
            emotion_label = max(emotions, key=lambda x: x['score'])['label']
        
        if models["SENTIMENT"]:
            sentiment_res = models["SENTIMENT"](user_input)[0]
            sentiment_score = 1.0 if sentiment_res['label'] == 'POSITIVE' else 0.0
    except Exception as e:
        print(f"Analytics model error: {e}")

    # 5. Track or Start Session in Database
    try:
        session = ChatSession.query.filter_by(conversation_id=conversation_id).first()
        if not session:
            session = ChatSession(conversation_id=conversation_id, user_id=user_id)
            db.session.add(session)
            db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"Session tracking error: {e}")

    # 6. Step 1: Listener Model (Classification)
    try:
        analysis_result = models["LISTENER"](user_input)[0]
        predicted_label = analysis_result['label']
        confidence_score = analysis_result['score']
    except Exception as e:
        print(f"Listener model error: {e}")
        return jsonify(response="I'm having trouble understanding. Could you rephrase?", followUps=[], conversation_id=conversation_id), 500

    # 7. Risk Detection / Crisis Handling
    is_crisis = predicted_label == "Suicidal" and confidence_score > 0.8
    if is_crisis:
        bot_response = CRISIS_RESPONSE
        if user_id:
            try:
                user_entry = ChatHistory(
                    user_id=user_id, conversation_id=conversation_id, sender='user', 
                    message=user_input, emotion=emotion_label, sentiment_score=sentiment_score,
                    intent=predicted_label, is_crisis=is_crisis
                )
                bot_entry = ChatHistory(user_id=user_id, conversation_id=conversation_id, sender='bot', message=bot_response)
                db.session.add_all([user_entry, bot_entry])
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                print(f"Failed to save crisis history: {e}")
        return jsonify(response=bot_response, followUps=[], conversation_id=conversation_id), 200

    # 8. Retrieve Conversation History for Context
    prompt_history = ""
    if user_id:
        conversation_history = ChatHistory.query.filter_by(
            user_id=user_id,
            conversation_id=conversation_id
        ).order_by(ChatHistory.timestamp.asc()).all()
        
        lookback_window = 6
        for turn in conversation_history[-lookback_window:]:
             prompt_history += f"<{turn.sender}> {turn.message} "

    # 9. Step 2: Generate Response with Responder Model
    try:
        tone_instruction = "(Be warm and professional)"
        if sentiment_score < 0.3:
            tone_instruction = "(Be empathetic and supportive)"
        elif sentiment_score > 0.7:
            tone_instruction = "(Be encouraging and positive)"
            
        few_shot_examples = (
            "User: I'm feeling really down today.\nCounselor: I'm sorry to hear that. I'm here to listen.\n"
            "User: I'm having a panic attack.\nCounselor: Take a deep breath. You are safe.\n"
        )
        
        full_prompt = (
            f"Instruction: You are a compassionate mental health counselor. {tone_instruction}\n"
            f"{few_shot_examples}\n"
            f"User's status: {predicted_label}.\n"
            f"{prompt_history}User: {user_input}\nCounselor:"
        )

        response = models["RESPONDER"](
            full_prompt, 
            max_new_tokens=60, 
            do_sample=True, 
            top_k=50, 
            top_p=0.9,
            temperature=0.7,
            repetition_penalty=1.2,
            pad_token_id=models["RESPONDER"].tokenizer.eos_token_id,
            eos_token_id=models["RESPONDER"].tokenizer.eos_token_id
        )

        generated_text = response[0]['generated_text']
        
        # Extract response after the last 'Counselor:' tag
        if "Counselor:" in generated_text:
            bot_response = generated_text.split("Counselor:")[-1].strip()
        else:
            bot_response = generated_text.replace(full_prompt, "").strip()

        # Clean hallucinations/artifacts
        stop_markers = ["User:", "System:", "<user>", "<bot>", "\n", "Counselor:"]
        for marker in stop_markers:
            bot_response = bot_response.split(marker)[0].strip()
        
        if not bot_response:
            bot_response = "I'm here for you. I'm listening."

    except Exception as e:
        print(f"Responder model error: {e}")
        return jsonify(response="I'm not able to generate a response right now.", followUps=[], conversation_id=conversation_id), 500

    # 10. Final Step: Save History & Return Response
    all_follow_ups = FOLLOW_UP_QUESTIONS.get(predicted_label, ["How can I help you today?"])
    follow_ups = random.sample(all_follow_ups, min(len(all_follow_ups), 2))

    if user_id:
        try:
            user_entry = ChatHistory(
                user_id=user_id, conversation_id=conversation_id, sender='user', 
                message=user_input, emotion=emotion_label, sentiment_score=sentiment_score,
                intent=predicted_label, is_crisis=False
            )
            bot_entry = ChatHistory(user_id=user_id, conversation_id=conversation_id, sender='bot', message=bot_response)
            db.session.add_all([user_entry, bot_entry])
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(f"Failed to save chat history: {e}")

    return jsonify(response=bot_response, followUps=follow_ups, conversation_id=conversation_id), 200


    # ... save to history and return ...

@api_bp.route('/chatbot/feedback', methods=['POST'])
@jwt_required()
def save_chatbot_feedback():
    data = request.get_json()
    conversation_id = data.get('conversation_id')
    score = data.get('score')
    text = data.get('text')
    
    if not conversation_id:
        return jsonify(msg="Conversation ID is required"), 400
        
    session = ChatSession.query.filter_by(conversation_id=conversation_id).first()
    if not session:
        return jsonify(msg="Session not found"), 404
        
    session.feedback_score = score
    session.feedback_text = text
    db.session.commit()
    return jsonify(msg="Feedback saved successfully"), 200

@api_bp.route('/chatbot/session/end', methods=['POST'])
@jwt_required()
def end_chatbot_session():
    data = request.get_json()
    conversation_id = data.get('conversation_id')
    
    if not conversation_id:
        return jsonify(msg="Conversation ID is required"), 400
        
    session = ChatSession.query.filter_by(conversation_id=conversation_id).first()
    if not session:
        return jsonify(msg="Session not found"), 404
        
    session.end_time = datetime.datetime.utcnow()
    session.is_completed = True
    
    # Calculate primary emotion for the session
    history = ChatHistory.query.filter_by(conversation_id=conversation_id, sender='user').all()
    if history:
        emotions = [h.emotion for h in history if h.emotion]
        if emotions:
            session.primary_emotion = max(set(emotions), key=emotions.count)
            
    db.session.commit()
    return jsonify(msg="Session ended successfully"), 200
def send_username_email(email, username):
    """Sends username via Resend API (production) or SMTP (local fallback)."""
    subject = "Your Username for Mental Health Platform"
    body = f"""
Hello,

You requested your username. Your username is: {username}

If you did not request this, please ignore this email.
"""
    # 1. Try Resend First (Production)
    if send_with_resend(email, subject, body):
        return True
        
    # 2. Fallback to SMTP (Local)
    try:
        msg = Message(subject, recipients=[email])
        msg.body = body
        mail.send(msg)
        return True
    except Exception as e:
        print(f"Error sending email via SMTP: {e}")
        return False

@api_bp.route('/forgot-username', methods=['POST'])
def forgot_username():
    data = request.get_json()
    email = data.get('email')
    
    if not email:
        return jsonify(msg="Email is required."), 400
    
    # Hash the provided email to check against the stored hash
    user = None
    for u in User.query.all():
        if u.email_hash and bcrypt.check_password_hash(u.email_hash, email):
            user = u
            break
            
    if not user:
        # For security, we give a generic success message even if the email isn't found
        # to prevent malicious users from discovering valid emails.
        return jsonify(msg="If an account with that email exists, the username has been sent."), 200
        
    if not send_username_email(email, user.username):
        return jsonify(msg="Could not send username email. Please try again later."), 500
    
    return jsonify(msg="If an account with that email exists, the username has been sent."), 200



# --- Forgot Password Endpoints ---

@api_bp.route('/forgot-password/request', methods=['POST'])
def forgot_password_request():
    data = request.get_json()
    email = data.get('email')

    if not email:
        return jsonify(msg="Email is required"), 400

    # 1. Check if user exists with this email
    user = None
    for u in User.query.all():
        if u.email_hash and bcrypt.check_password_hash(u.email_hash, email):
            user = u
            break
    
    if not user:
        # Security: Don't reveal if user exists
        return jsonify(msg="If an account with that email exists, an OTP has been sent."), 200

    # 2. Generate and Store OTP
    otp = str(random.randint(100000, 999999))
    code_hash = bcrypt.generate_password_hash(otp).decode('utf-8')
    expires_at = dt.utcnow() + timedelta(minutes=10)

    # Remove any existing codes for this email
    VerificationCode.query.filter_by(email=email).delete()

    new_code = VerificationCode(email=email, code_hash=code_hash, expires_at=expires_at)
    db.session.add(new_code)
    db.session.commit()

    # 3. Send OTP
    if not send_verification_email(email, otp):
        return jsonify(msg="Could not send OTP. Please try again later."), 500

    return jsonify(msg="If an account with that email exists, an OTP has been sent."), 200

@api_bp.route('/forgot-password/verify', methods=['POST'])
def forgot_password_verify():
    data = request.get_json()
    email = data.get('email')
    otp = data.get('otp')

    if not email or not otp:
        return jsonify(msg="Email and OTP are required"), 400

    verification = VerificationCode.query.filter_by(email=email).order_by(VerificationCode.expires_at.desc()).first()

    if not verification:
        return jsonify(msg="Invalid or expired OTP."), 400
    
    if dt.utcnow() > verification.expires_at:
        db.session.delete(verification)
        db.session.commit()
        return jsonify(msg="OTP has expired."), 400

    if not bcrypt.check_password_hash(verification.code_hash, otp):
        return jsonify(msg="Invalid OTP."), 400

    return jsonify(msg="OTP verified successfully."), 200

@api_bp.route('/forgot-password/reset', methods=['POST'])
def forgot_password_reset():
    data = request.get_json()
    email = data.get('email')
    otp = data.get('otp')
    new_password = data.get('new_password')

    if not all([email, otp, new_password]):
        return jsonify(msg="Email, OTP, and new password are required"), 400

    # 1. Verify OTP again (crucial for security)
    verification = VerificationCode.query.filter_by(email=email).order_by(VerificationCode.expires_at.desc()).first()
    
    if not verification:
        return jsonify(msg="Invalid or expired OTP."), 400
        
    if dt.utcnow() > verification.expires_at:
        db.session.delete(verification)
        db.session.commit()
        return jsonify(msg="OTP has expired."), 400

    if not bcrypt.check_password_hash(verification.code_hash, otp):
        return jsonify(msg="Invalid OTP."), 400

    # 2. Find User and Update Password
    user = None
    for u in User.query.all():
        if u.email_hash and bcrypt.check_password_hash(u.email_hash, email):
            user = u
            break
            
    if not user:
         return jsonify(msg="User not found."), 404

    user.set_password(new_password)
    
    # 3. Cleanup OTP
    db.session.delete(verification)
    db.session.commit()

    return jsonify(msg="Password reset successfully. You can now login."), 200

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

@api_bp.route('/admin/change-password', methods=['POST'])
@jwt_required()
def admin_change_password():
    data = request.get_json()
    new_password = data.get('newPassword')
    
    if not new_password:
        return jsonify(msg="New password is required"), 400
        
    user_id = get_jwt_identity()
    user = User.query.get(user_id)
    
    if not user:
        return jsonify(msg="User not found"), 404
        
    user.set_password(new_password)
    db.session.commit()
    
    return jsonify(msg="Password updated successfully"), 200

@api_bp.route('/upload', methods=['POST'])
@jwt_required()
def upload_file():
    if 'file' not in request.files:
        return jsonify({"msg": "No file part"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"msg": "No selected file"}), 400
        
    if file:
        filename = secure_filename(file.filename)
        unique_filename = f"{uuid.uuid4().hex}_{filename}"
        filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], unique_filename)
        file.save(filepath)
        
        file_url = f"/uploads/{unique_filename}" 
        return jsonify({"url": file_url, "filename": unique_filename}), 201

# --- PUBLIC RESOURCES (HUB) ---

@api_bp.route('/resources', methods=['GET'])
def get_public_resources():
    rtype = request.args.get('type')
    query = Resource.query.filter_by(status='approved')
    
    if rtype:
        query = query.filter_by(type=rtype)
        
    resources = query.order_by(Resource.created_at.desc()).all()
    
    return jsonify([{
        "id": r.id,
        "title": r.title,
        "description": r.description,
        "type": r.type,
        "url": r.url,
        "content": r.content,
        "author": r.author.username if r.author else "Zenture Team",
        "date": r.created_at.strftime("%b %d, %Y")
    } for r in resources]), 200

# --- ADMIN RESOURCE MANAGEMENT ---

@api_bp.route('/admin/resources', methods=['GET'])
@jwt_required()
def get_all_resources_admin():
    resources = Resource.query.order_by(Resource.status.desc(), Resource.created_at.desc()).all()
    return jsonify([{
        "id": r.id,
        "title": r.title,
        "type": r.type,
        "status": r.status,
        "author": r.author.username if r.author else "System",
        "date": r.created_at.strftime("%Y-%m-%d")
    } for r in resources]), 200

@api_bp.route('/admin/resource/<int:resource_id>/status', methods=['PUT'])
@jwt_required()
def update_resource_status(resource_id):
    data = request.get_json()
    new_status = data.get('status')
    
    resource = Resource.query.get(resource_id)
    if not resource:
        return jsonify({"msg": "Resource not found"}), 404
        
    resource.status = new_status
    db.session.commit()
    return jsonify({"msg": f"Resource {new_status}"}), 200
@api_bp.route('/counsellor/register', methods=['POST'])
def register_counsellor():
    data = request.get_json()
    username = data.get('username')
    password = data.get('password')

    if not username or not password:
        return jsonify(msg="Username and password are required"), 400

    if User.query.filter_by(username=username).first():
        return jsonify(msg="Username already exists"), 409

    new_counsellor = User(username=username, role=UserRole.COUNSELOR)
    new_counsellor.set_password(password)

    db.session.add(new_counsellor)
    db.session.commit()

    return jsonify(msg="Counsellor registered successfully"), 201

def send_verification_email(email, code):
    try:
        subject = "Your Verification Code for Mental Health Platform"
        msg = Message(subject, recipients=[email])
        '''msg.body = f"""
Hello,

Thank you for registering. Your verification code is: {code}

This code will expire in 10 minutes.

If you did not request this, please ignore this email.
"""'''
        body = f"""
Hello,

Thank you for registering. Your verification code is: {code}

This code will expire in 10 minutes.

If you did not request this, please ignore this email.
"""
        #mail.send(msg)
        if not send_with_resend(email, subject, body):
            return "daily limit exceeded"
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

    # email_regex = r"^[a-zA-Z0-9._%+-]+@akgec\.ac\.in$"
    # if not re.match(email_regex, email):
    #     return jsonify(msg="Please use a valid AKGEC college email ID."), 400
    
    otp = str(random.randint(100000, 999999))
    code_hash = bcrypt.generate_password_hash(otp).decode('utf-8')
    expires_at = dt.utcnow() + timedelta(minutes=10)

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
    
    if dt.utcnow() > verification.expires_at:
        db.session.delete(verification)
        db.session.commit()
        return jsonify(msg="Verification code has expired."), 400

    if not bcrypt.check_password_hash(verification.code_hash, otp):
        return jsonify(msg="Invalid verification code."), 401

    for u in User.query.all():
        if u.email_hash:
            try:
                if bcrypt.check_password_hash(u.email_hash, email):
                    return jsonify(msg="An account with this email already exists."), 409
            except ValueError:
                continue

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

# NEW ENDPOINT: Fetch upcoming appointments for admin dashboard
@api_bp.route('/admin/upcoming-appointments', methods=['GET'])
@roles_required('admin')
def get_upcoming_appointments():
    """
    Fetches a list of upcoming appointments for the admin dashboard.
    """
    try:
        # Query for upcoming appointments that are booked and in the future
        upcoming_appointments = (
            db.session.query(Appointment, User)
            .join(User, Appointment.student_id == User.id)
            .filter(
                Appointment.status == 'booked',
                Appointment.appointment_time >= dt.datetime.utcnow()
            )
            .order_by(Appointment.appointment_time.asc())
            .limit(5)
            .all()
        )

        appointments_list = [
            {
                "student_username": user.username,
                "appointment_time": appointment.appointment_time.isoformat()
            }
            for appointment, user in upcoming_appointments
        ]

        return jsonify(appointments_list), 200
    except Exception as e:
        print(f"Error fetching upcoming appointments: {e}")
        return jsonify({"error": "An internal error occurred."}), 500

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
            "image": dummy_image,
            "availability": c.availability,
            "contact": c.user.email_hash # In a real app this would be decoded or a separate field
        })
    return jsonify(result)

@api_bp.route("/admin/counselors", methods=["POST"])
@roles_required('admin')
def register_counselor():
    data = request.get_json()
    
    # 1. Create User
    if User.query.filter_by(username=data['username']).first():
        return jsonify(msg="Username already exists"), 409
        
    new_user = User(
        username=data['username'],
        email_hash=data.get('email', 'placeholder@email.com'), # simplified for demo
        role=UserRole.COUNSELOR
    )
    new_user.set_password(data['password'])
    db.session.add(new_user)
    db.session.flush() # Get ID
    
    # 2. Create Profile
    new_profile = CounselorProfile(
        user_id=new_user.id,
        specialization=data.get('specialization', 'General'),
        availability=data.get('availability', {})
    )
    db.session.add(new_profile)
    db.session.commit()
    
    return jsonify(msg="Counselor registered successfully", id=new_user.id), 201

@api_bp.route("/admin/counselors/<int:counselor_id>", methods=["PUT"])
@roles_required('admin')
def update_counselor(counselor_id):
    data = request.get_json()
    
    # query the CounselorProfile
    profile = CounselorProfile.query.get(counselor_id)
    if not profile:
        return jsonify(msg="Counselor profile not found"), 404
        
    user = User.query.get(profile.user_id)
    if not user:
         return jsonify(msg="Counselor user not found"), 404

    # Update User fields (if provided)
    if 'username' in data and data['username'] != user.username:
        if User.query.filter_by(username=data['username']).first():
            return jsonify(msg="Username already exists"), 409
        user.username = data['username']
        
    if 'email' in data:
        # In a real app, validate email format
        user.email_hash = data['email'] # simplified reuse of field
        
    if 'password' in data and data['password']:
        user.set_password(data['password'])
        
    # Update Profile fields
    if 'specialization' in data:
        profile.specialization = data['specialization']
        
    if 'availability' in data:
        # Expecting structure: { "days": ["Mon", "Wed"], "timeRange": "09:00-17:00" }
        profile.availability = data['availability']
        
    db.session.commit()
    return jsonify(msg="Counselor updated successfully"), 200

# --- RESOURCE MANAGEMENT ENDPOINTS (Admin) ---

@api_bp.route('/admin/resources/<int:resource_id>', methods=['PUT'])
@roles_required('admin')
def update_resource(resource_id):
    data = request.get_json()
    resource = Resource.query.get(resource_id)
    if not resource:
        return jsonify(msg="Resource not found"), 404
        
    if 'title' in data: resource.title = data['title']
    if 'description' in data: resource.description = data['description']
    if 'content' in data: resource.content = data['content']
    if 'url' in data: resource.url = data['url']
    if 'type' in data: resource.type = data['type']
    if 'status' in data: resource.status = data['status']
    
    db.session.commit()
    return jsonify(msg="Resource updated successfully"), 200

@api_bp.route('/admin/resources/<int:resource_id>', methods=['DELETE'])
@roles_required('admin')
def delete_resource(resource_id):
    resource = Resource.query.get(resource_id)
    if not resource:
        return jsonify(msg="Resource not found"), 404
        
    db.session.delete(resource)
    db.session.commit()
    return jsonify(msg="Resource deleted successfully"), 200

# --- ANALYTICS ENDPOINTS ---

@api_bp.route('/admin/analytics/overview', methods=['GET'])
@roles_required('admin')
def get_analytics_overview():
    # 1. Total Users
    total_users = User.query.count()
    
    # 2. Active Users (Users with activity in last 30 days)
    thirty_days_ago = datetime.datetime.utcnow() - datetime.timedelta(days=30)
    active_users = User.query.join(UserActivityLog).filter(UserActivityLog.timestamp >= thirty_days_ago).distinct().count()
    
    # 3. Total Sessions (Appointments with status 'completed' - assuming logic, or just all booked)
    total_sessions = Appointment.query.filter_by(status='completed').count()
    
    # 4. Avg Session Duration (Placeholder - existing model doesn't strictly track duration in minutes usually)
    # We'll return a static or calculated proxy
    avg_duration = "45 min" 
    
    return jsonify({
        "totalUsers": total_users,
        "activeUsers": active_users,
        "totalSessions": total_sessions,
        "avgSessionDuration": avg_duration
    })

@api_bp.route('/admin/students', methods=['GET'])
@roles_required('admin')
def get_students():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 10, type=int)
    
    students_query = User.query.filter_by(role=UserRole.STUDENT).order_by(User.id.desc())
    pagination = students_query.paginate(page=page, per_page=per_page, error_out=False)
    
    students_data = []
    today = datetime.datetime.utcnow().date()
    
    for student in pagination.items:
        # Count mood check-ins today
        mood_count = MoodCheckin.query.filter(
            MoodCheckin.user_id == student.id,
            func.date(MoodCheckin.timestamp) == today
        ).count()
        
        # Count resource activities today
        resource_count = UserActivityLog.query.filter(
            UserActivityLog.user_id == student.id,
            func.date(UserActivityLog.timestamp) == today
        ).count()
        
        students_data.append({
            "id": student.id,
            "username": student.username,
            "stats": {
                "mood_checkins": mood_count,
                "resources_viewed": resource_count
            }
        })
        
    return jsonify({
        "students": students_data,
        "total": pagination.total,
        "pages": pagination.pages,
        "current_page": page
    }), 200

@api_bp.route('/admin/analytics/chatbot', methods=['GET'])
@roles_required('admin')
def get_chatbot_analytics():
    # 1. Emotional Distribution
    emotions_data = db.session.query(
        ChatHistory.emotion, func.count(ChatHistory.id)
    ).filter(ChatHistory.sender == 'user').group_by(ChatHistory.emotion).all()
    
    emotion_dist = {emotion if emotion else 'neutral': count for emotion, count in emotions_data}
    
    # 2. Intent Distribution (Clusters)
    intents_data = db.session.query(
        ChatHistory.intent, func.count(ChatHistory.id)
    ).filter(ChatHistory.sender == 'user').group_by(ChatHistory.intent).all()
    
    intent_dist = {intent if intent else 'general': count for intent, count in intents_data}
    
    # 3. Completion Rate
    total_sessions = ChatSession.query.count()
    completed_sessions = ChatSession.query.filter_by(is_completed=True).count()
    completion_rate = (completed_sessions / total_sessions * 100) if total_sessions > 0 else 0
    
    # 4. Crisis Count
    crisis_count = ChatHistory.query.filter_by(is_crisis=True).count()
    
    # 5. Emotional Trends (Last 7 days)
    seven_days_ago = datetime.datetime.utcnow() - datetime.timedelta(days=7)
    trend_data = db.session.query(
        func.date(ChatHistory.timestamp), ChatHistory.emotion, func.count(ChatHistory.id)
    ).filter(
        ChatHistory.sender == 'user',
        ChatHistory.timestamp >= seven_days_ago
    ).group_by(func.date(ChatHistory.timestamp), ChatHistory.emotion).all()
    
    trends = {}
    for date, emotion, count in trend_data:
        date_str = date.strftime("%Y-%m-%d")
        if date_str not in trends:
            trends[date_str] = {}
        trends[date_str][emotion if emotion else 'neutral'] = count
        
    # 6. Avg Feedback Score
    avg_feedback = db.session.query(func.avg(ChatSession.feedback_score)).scalar() or 0
    
    return jsonify({
        "emotionDistribution": emotion_dist,
        "intentDistribution": intent_dist,
        "completionRate": round(completion_rate, 2),
        "crisisCount": crisis_count,
        "trends": trends,
        "avgFeedbackScore": round(float(avg_feedback), 2)
    }), 200

@api_bp.route('/appointments', methods=['POST'])
@jwt_required()
def book_appointment():
    data = request.json
    student_id = get_jwt_identity()
    
    counselor_id = data.get('counselor_id')
    appointment_date_str = data.get('appointment_date')
    appointment_time_str = data.get('appointment_time')
    mode = data.get('mode') # Get mode from frontend (e.g., 'video_call', 'in_person')
    description = data.get('description')

    if not all([counselor_id, appointment_date_str, appointment_time_str, mode]):
        return jsonify({"error": "Counselor ID, date, time, and mode are required"}), 400
    
    # Rectified: Generate a meeting link only for 'video_call' mode
    meeting_link = None
    if mode == 'video_call':
        meeting_link = f"/session/{uuid.uuid4()}"

    try:
        counselor = User.query.get(int(counselor_id))
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid counselor ID format."}), 400
    
    if not counselor or counselor.role != UserRole.COUNSELOR:
        return jsonify({"error": "Counselor not found or invalid"}), 404

    try:
        # Combine date and time strings and parse into a single datetime object
        combined_dt_str = f"{appointment_date_str} {appointment_time_str}"
        appointment_time = dt.strptime(combined_dt_str, "%Y-%m-%d %H:%M")
    except ValueError:
        return jsonify({"error": "Invalid date or time format."}), 400

    # Check if the slot is already booked
    existing_appointment = Appointment.query.filter(
        Appointment.counselor_id == counselor_id,
        Appointment.appointment_time == appointment_time
    ).first()

    if existing_appointment:
        return jsonify({"error": "This time slot is already booked. Please choose another one."}), 409

    new_appointment = Appointment(
        student_id=student_id,
        counselor_id=counselor.id,
        appointment_time=appointment_time,
        status="pending",
        notes=description,
        mode=mode, 
        meeting_link=meeting_link # Link generated if video_call
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

# --- ANALYTICS ENDPOINTS ---
@api_bp.route('/admin/analytics/engagement', methods=['GET'])
@roles_required('admin')
def get_engagement_stats():
    # Last 7 days
    today = datetime.datetime.utcnow().date()
    days = [(today - datetime.timedelta(days=i)).strftime('%a') for i in range(6, -1, -1)]
    
    # Placeholder: In real app, query User and UserActivityLog tables with group_by date
    # Mocking data structure for frontend chart
    import random
    new_users = [{"label": day, "value": random.randint(5, 50)} for day in days]
    active_sessions = [{"label": day, "value": random.randint(20, 100)} for day in days]
    
    return jsonify({
        "newUsers": new_users,
        "activeSessions": active_sessions
    })

@api_bp.route('/admin/analytics/mood', methods=['GET'])
@roles_required('admin')
def get_mood_analytics():
    # Last 7 days mood checkins
    today = datetime.datetime.utcnow().date()
    start_date = today - datetime.timedelta(days=7)
    
    # Query MoodCheckin
    checkins = db.session.query(
        func.date(MoodCheckin.timestamp), MoodCheckin.mood, func.count(MoodCheckin.id)
    ).filter(MoodCheckin.timestamp >= start_date).group_by(func.date(MoodCheckin.timestamp), MoodCheckin.mood).all()
    
    # Process into format for AnxietyAnalysisChart
    # Simplified mapping: Happy/Calm -> Low, Neutral -> Medium, Sad/Angry/Anxious -> High
    mood_map = {
        'happy': 'low', 'calm': 'low', 
        'neutral': 'medium', 
        'sad': 'high', 'angry': 'high', 'anxious': 'high'
    }
    
    data_map = {}
    days = [(today - datetime.timedelta(days=i)).strftime('%Y-%m-%d') for i in range(6, -1, -1)]
    
    for day in days:
        data_map[day] = {'low': 0, 'medium': 0, 'high': 0, 'day': datetime.datetime.strptime(day, '%Y-%m-%d').strftime('%a')}
        
    for date, mood, count in checkins:
        date_str = date.strftime('%Y-%m-%d')
        if date_str in data_map:
            severity = mood_map.get(mood.lower(), 'medium')
            data_map[date_str][severity] += count
            
    return jsonify(list(data_map.values()))

@api_bp.route('/admin/analytics/resources', methods=['GET'])
@roles_required('admin')
def get_resource_analytics():
    # Top 5 most active resources
    top_resources = db.session.query(
        Resource.title, Resource.type, func.count(UserActivityLog.id)
    ).join(UserActivityLog).group_by(Resource.id).order_by(func.count(UserActivityLog.id).desc()).limit(5).all()
    
    return jsonify([
        {"title": r[0], "type": r[1], "views": r[2]}
        for r in top_resources
    ])

@api_bp.route('/admin/analytics/chat', methods=['GET'])
@roles_required('admin')
def get_chat_analytics():
    # 1. Sentiment Arc (Real Logic)
    # Fetch chats from the last 24 hours
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=24)
    recent_chats = ChatHistory.query.filter(ChatHistory.timestamp >= cutoff).order_by(ChatHistory.timestamp.asc()).all()

    sentiment_data = []
    # Bucket sentiments by hour for the trend line
    sentiment_buckets = {}
    
    from textblob import TextBlob
    from collections import Counter

    all_text = ""
    
    for chat in recent_chats:
        blob = TextBlob(chat.user_message)
        score = blob.sentiment.polarity
        
        # Round to nearest hour for the chart
        hour_key = chat.timestamp.strftime("%H:00")
        if hour_key not in sentiment_buckets:
            sentiment_buckets[hour_key] = []
        sentiment_buckets[hour_key].append(score)
        
        all_text += " " + chat.user_message

    # Calculate average sentiment per hour
    for hour, scores in sentiment_buckets.items():
        avg_score = sum(scores) / len(scores)
        sentiment_data.append({"timestamp": hour, "sentimentScore": round(avg_score, 2)})

    # Sort by time
    sentiment_data.sort(key=lambda x: x['timestamp'])
    
    # If no data, provide a placeholder so chart isn't empty
    if not sentiment_data:
        sentiment_data = [{"timestamp": "No Data", "sentimentScore": 0}]

    # 2. Topic Modeling (Simple Keyword Frequency relying on Noun Phrases)
    blob_all = TextBlob(all_text)
    # Filter for words > 3 chars to avoid stopwords roughly
    words = [w.lower() for w in blob_all.words if len(w) > 3 and w.isalpha()]
    
    # Common stopwords to exclude (basic list)
    stopwords = {'this', 'that', 'have', 'from', 'what', 'your', 'with', 'about', 'want', 'feel', 'like', 'just', 'know', 'make', 'time', 'really', 'would', 'could'}
    filtered_words = [w for w in words if w not in stopwords]
    
    word_counts = Counter(filtered_words).most_common(5)
    
    topics = []
    for word, count in word_counts:
        topics.append({
            "topic": word.capitalize(),
            "volume": count,
            "keywords": [word] # In a real LDA, this would be a cluster of related words
        })
    
    if not topics:
         topics = [{"topic": "No Chats Yet", "volume": 0, "keywords": []}]

    # 3. Risk Prediction (Keyword based for now)
    risk_keywords = ['die', 'suicide', 'kill', 'end it', 'hopeless', 'pain', 'hurt']
    at_risk_users = {} # user_id -> score

    for chat in recent_chats:
        msg_lower = chat.user_message.lower()
        for kw in risk_keywords:
            if kw in msg_lower:
                if chat.user_id not in at_risk_users:
                    at_risk_users[chat.user_id] = {"score": 0, "factors": set()}
                
                at_risk_users[chat.user_id]["score"] += 20
                at_risk_users[chat.user_id]["factors"].add(f"Keyword: '{kw}'")
    
    risks = []
    for uid, data in at_risk_users.items():
        # Get student pseudonym/ID
        risks.append({
            "studentId": f"Student_{uid}", 
            "riskScore": min(data["score"], 100),
            "riskFactors": list(data["factors"])
        })

    return jsonify({
        "sentiment": sentiment_data,
        "topics": topics,
        "risks": risks
    })


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

    for a in appointments:
        if a.status == 'booked' and a.mode != 'in_person' and not a.meeting_link:
            a.meeting_link = f"/session/{uuid.uuid4()}"
            db.session.add(a)
    
    if appointments:
        db.session.commit()

    return jsonify([{
        "id": a.id,
        "student_id": a.student_id,
        "counselor_id": a.counselor_id,
        "appointment_time": a.appointment_time.isoformat(),
        "status": a.status,
        "mode": a.mode,
        "meeting_link": a.meeting_link
    } for a in appointments])

# --- FORUM ENDPOINTS ---
# (Existing forum routes)

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
    today_start = dt.combine(datetime.date.today(), datetime.time.min)
    today_end = dt.combine(datetime.date.today(), datetime.time.max)

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
    
    today_start = dt.combine(datetime.date.today(), datetime.time.min)
    today_end = dt.combine(datetime.date.today(), datetime.time.max)
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

@api_bp.route('/journals', methods=['GET'])
@jwt_required()
def get_student_journals():
    user_id = get_jwt_identity()
    journals = JournalEntry.query.filter_by(user_id=user_id).order_by(JournalEntry.timestamp.desc()).all()
    return jsonify([{
        "id": j.id,
        "title": j.entry_type.capitalize() if j.entry_type else "Journal Entry", 
        "date": j.timestamp.isoformat(),
        "snippet": j.content[:100] + "..." if len(j.content) > 100 else j.content
    } for j in journals])
# Add this new endpoint to routes.py for the student dashboard
# @api_bp.route("/counselor/profile/<int:user_id>", methods=["GET"])
# @jwt_required()
# def get_counselor_profile(user_id):
#     date_str = request.args.get("date")
#     if not date_str:
#         return jsonify({"error": "Missing required query parameter: date"}), 400

#     try:
#         date_obj = dt.strptime(date_str, "%Y-%m-%d").date()
#     except ValueError:
#         return jsonify({"error": "Invalid date format. Use YYYY-MM-DD."}), 400

#     counselor = CounselorProfile.query.filter_by(id=user_id).first()
#     if not counselor:
#         return jsonify({"error": "Counselor not found"}), 404

#     # Define working hours (dummy slots, you can replace with dynamic logic)
#     available_slots = ["10:00", "11:00", "14:00", "15:00", "16:00","14:30","15:30","16:30",]

#     # Fetch booked appointments for this counselor on given date
#     booked = Appointment.query.filter_by(
#         counselor_id=user_id,
#         appointment_date=date_obj
#     ).all()

#     booked_times = [appt.appointment_time.strftime("%H:%M") for appt in booked]
#     free_slots = [slot for slot in available_slots if slot not in booked_times]

#     return jsonify({
#         "counselor_id": counselor.user_id,
#         "name": counselor.user.username,
#         "specialization": counselor.specialization,
#         "available_slots": [
#             {
#                 "date": date_str,
#                 "slots": free_slots
#             }
#         ]
#     })
@api_bp.route("/counselor/profile/<int:profile_id>", methods=["GET"])
@jwt_required()
def get_counselor_profile(profile_id):
    date_str = request.args.get("date")
    if not date_str:
        return jsonify({"error": "Missing required query parameter: date"}), 400

    try:
        date_obj = dt.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "Invalid date format. Use YYYY-MM-DD."}), 400

    counselor_profile = CounselorProfile.query.filter_by(id=profile_id).first()
    if not counselor_profile:
        return jsonify({"error": "Counselor not found"}), 404

    # Calculate start and end of the day for the query
    start_of_day = dt.combine(date_obj, datetime.time.min)
    end_of_day = dt.combine(date_obj, datetime.time.max)
    
    # Fetch existing appointments (booked or pending)
    booked_appointments = Appointment.query.filter(
        Appointment.counselor_id == counselor_profile.user_id, 
        Appointment.appointment_time.between(start_of_day, end_of_day),
        Appointment.status.in_(['booked', 'pending'])
    ).all()

    # Set of booked times strings
    booked_times = {appt.appointment_time.strftime("%H:%M") for appt in booked_appointments}

    # Generate 30-minute slots from 09:00 to 18:00
    slots = []
    current_time = dt.strptime("09:00", "%H:%M")
    end_time_limit = dt.strptime("18:00", "%H:%M")

    while current_time <= end_time_limit:
        time_str = current_time.strftime("%H:%M")
        slots.append({
            "time": time_str,
            "available": time_str not in booked_times
        })
        current_time += timedelta(minutes=30)

    return jsonify({
        "counselor_id": counselor_profile.user_id,
        "name": counselor_profile.user.username,
        "specialization": counselor_profile.specialization,
        "available_slots": slots
    })
@api_bp.route('/student/dashboard-data', methods=['GET'])
@roles_required('student')
def get_student_dashboard_data():
    try:
        student_id = get_jwt_identity()
        student = User.query.get(student_id)

        if not student:
            return jsonify({"msg": "Student not found"}), 404

        now = dt.utcnow()
        session_window_start = now - timedelta(minutes=50)

        upcoming_appointments_query = (
            db.session.query(Appointment, User)
            .join(User, Appointment.counselor_id == User.id)
            .filter(
                Appointment.student_id == student_id,
                Appointment.appointment_time >= session_window_start 
            )
            .order_by(Appointment.appointment_time.asc())
            .limit(5)
            .all()
        )
        
        appointments_data = []
        for appt, counsellor in upcoming_appointments_query:
            if appt.status == 'booked' and appt.mode != 'in_person' and not appt.meeting_link:
                appt.meeting_link = f"/session/{uuid.uuid4()}"
                db.session.add(appt)
            
            appointments_data.append({
                'id': appt.id,
                'counsellorName': counsellor.username,
                'date': appt.appointment_time.isoformat(),
                'mode': appt.mode,
                'status': appt.status,
                'meeting_link': appt.meeting_link
            })
        
        if upcoming_appointments_query:
            db.session.commit()
        
        dashboard_data = {
            'studentName': student.username,
            'upcomingAppointments': appointments_data
        }
        
        return jsonify(dashboard_data), 200

    except Exception as e:
        print(f"Error fetching student dashboard data: {e}")
        return jsonify({"msg": "An error occurred while fetching student data"}), 500

# --- NEW COUNSELOR DASHBOARD ENDPOINTS ---
# This endpoint fetches all appointments for the logged-in counselor.
# Add this new, consolidated endpoint to routes.py

# In routes.py, REPLACE the entire get_counsellor_dashboard_data function with this corrected version

@api_bp.route('/counsellor/dashboard-data', methods=['GET'])
@roles_required('counselor')
def get_counsellor_dashboard_data():
    try:
        counsellor_id = get_jwt_identity()
        counsellor = User.query.get(counsellor_id)

        if not counsellor:
            return jsonify({"msg": "Counselor not found"}), 404

        appointments_query = (
            db.session.query(Appointment, User)
            .join(User, Appointment.student_id == User.id)
            .filter(Appointment.counselor_id == counsellor_id)
            .order_by(Appointment.appointment_time.asc())
            .all()
        )
        
        appointments_data = []
        for appt, student in appointments_query:
            if appt.status == 'booked' and appt.mode != 'in_person' and not appt.meeting_link:
                appt.meeting_link = f"/session/{uuid.uuid4()}"
                db.session.add(appt)

            appointments_data.append({
                'id': appt.id,
                'studentName': student.username,
                'date': appt.appointment_time.isoformat(),
                'mode': appt.mode,
                'status': appt.status,
                'meeting_link': appt.meeting_link,
            })
        
        if appointments_query:
            db.session.commit()

        distinct_student_ids = db.session.query(Appointment.student_id)\
            .filter_by(counselor_id=counsellor_id).distinct()
        
        client_users = User.query.filter(User.id.in_(distinct_student_ids)).all()

        clients_data = [{'name': user.username, 'status': 'Active', 'rating': 4.5} for user in client_users]

        dashboard_data = {
            'counsellorName': counsellor.username,
            'appointments': appointments_data,
            'clients': clients_data
        }
        
        return jsonify(dashboard_data), 200

    except Exception as e:
        print(f"Error fetching counsellor dashboard data: {e}")
        return jsonify({"msg": "An error occurred while fetching dashboard data"}), 500

# Add this new route in routes.py under the --- Counselor and Appointment Endpoints --- section

@api_bp.route('/counsellor/create-profile', methods=['POST'])
@roles_required('counselor') # Protects the route, ensuring only a logged-in counsellor can create a profile
def create_counsellor_profile():
    user_id = get_jwt_identity()
    data = request.get_json()
    specialization = data.get('specialization')

    if not specialization:
        return jsonify(msg="Specialization is required"), 400

    # Check if a profile already exists for this user
    if CounselorProfile.query.filter_by(user_id=user_id).first():
        return jsonify(msg="Profile already exists for this counsellor"), 409

    # Create the new counsellor profile
    new_profile = CounselorProfile(
        user_id=user_id,
        specialization=specialization
    )
# Add this new endpoint to routes.py for the student dashboard
# @api_bp.route("/counselor/profile/<int:user_id>", methods=["GET"])
# @jwt_required()
# def get_counselor_profile(user_id):
#     date_str = request.args.get("date")
#     if not date_str:
#         return jsonify({"error": "Missing required query parameter: date"}), 400

#     try:
#         date_obj = dt.strptime(date_str, "%Y-%m-%d").date()
#     except ValueError:
#         return jsonify({"error": "Invalid date format. Use YYYY-MM-DD."}), 400

#     counselor = CounselorProfile.query.filter_by(id=user_id).first()
#     if not counselor:
#         return jsonify({"error": "Counselor not found"}), 404

#     # Define working hours (dummy slots, you can replace with dynamic logic)
#     available_slots = ["10:00", "11:00", "14:00", "15:00", "16:00","14:30","15:30","16:30",]

#     # Fetch booked appointments for this counselor on given date
#     booked = Appointment.query.filter_by(
#         counselor_id=user_id,
#         appointment_date=date_obj
#     ).all()

#     booked_times = [appt.appointment_time.strftime("%H:%M") for appt in booked]
#     free_slots = [slot for slot in available_slots if slot not in booked_times]

#     return jsonify({
#         "counselor_id": counselor.user_id,
#         "name": counselor.user.username,
#         "specialization": counselor.specialization,
#         "available_slots": [
#             {
#                 "date": date_str,
#                 "slots": free_slots
#             }
#         ]
#     })



@api_bp.route('/appointments/<int:appointment_id>/status', methods=['PUT'])
@roles_required('counselor')
def update_appointment_status(appointment_id):
    data = request.json
    new_status = data.get('status')
    counselor_id = get_jwt_identity()

    if new_status not in ['booked', 'rejected', 'canceled']:
        return jsonify({"error": "Invalid status."}), 400

    appointment = Appointment.query.get(appointment_id)
    if not appointment:
        return jsonify({"error": "Appointment not found."}), 404

    if appointment.counselor_id != int(counselor_id):
        return jsonify({"error": "Unauthorized to update this appointment."}), 403

    appointment.status = new_status
    
    # Generate meeting link ONLY if accepted (booked) and it's a video call
    if new_status == 'booked' and appointment.mode == 'video_call' and not appointment.meeting_link:
        appointment.meeting_link = f"/session/{uuid.uuid4()}"
        
        # Notify Student
        msg = f"Your appointment has been accepted! Join via the video link."
        notif = Notification(user_id=appointment.student_id, message=msg, link=appointment.meeting_link)
        db.session.add(notif)
    elif new_status == 'rejected':
         msg = f"Your appointment request was declined."
         notif = Notification(user_id=appointment.student_id, message=msg, link=None)
         db.session.add(notif)

    db.session.commit()

    return jsonify({
        "message": f"Appointment {new_status} successfully.",
        "meeting_link": appointment.meeting_link
    })

@api_bp.route('/session/verify/<string:session_id>', methods=['GET'])
@jwt_required()
def verify_session_access(session_id):
    user_id = get_jwt_identity()
    user = User.query.get(user_id)
    
    # Check if a valid appointment exists with this link
    # The stored link is likely "/session/uuid", so we match that
    link_path = f"/session/{session_id}"
    print(f"DEBUG: Verifying session access. UserID: {user_id}, SessionID: {session_id}, LinkPath: {link_path}")
    
    appointment = Appointment.query.filter(
        Appointment.meeting_link == link_path,
        (Appointment.student_id == user_id) | (Appointment.counselor_id == user_id)
    ).first()
    
    if not appointment:
        return jsonify({"allowed": False, "error": "Invalid session or unauthorized."}), 403
    
    # Time Limit Check
    now = dt.now()
    appointment_time = appointment.appointment_time
    
    # If appointment_time is string (some DB adapters), parse it. 
    # SQLAlchemy usually returns datetime object.
    
    # Allow joining 10 mins before
    start_window = appointment_time - timedelta(minutes=10)
    # Session duration 30 mins (allow up to 30 mins after start)
    end_window = appointment_time + timedelta(minutes=30)
    
    if now < start_window:
        wait_time = (start_window - now).total_seconds() / 60
        return jsonify({"allowed": False, "error": f"Session starts in {int(wait_time)} minutes."}), 403
        
    if now > end_window:
         return jsonify({"allowed": False, "error": "Session has expired."}), 403
         
    return jsonify({
        "allowed": True, 
        "mode": appointment.mode,
        "startTime": appointment_time.isoformat(),
        "user": {
            "id": user.id,
            "name": user.username,
            "role": user.role.value
        }
    })

# --- COUNSELOR DASHBOARD: CLIENTS ---

@api_bp.route('/counsellor/clients', methods=['GET'])
@jwt_required()
def get_counsellor_clients():
    current_user_id = get_jwt_identity()
    user = User.query.get(current_user_id)
    if user.role != UserRole.COUNSELOR:
        return jsonify({"msg": "Unauthorized"}), 403
        
    # Find students who have appointments with this counselor
    # Use distinct to avoid duplicates
    stmt = db.session.query(Appointment.student_id).filter_by(counselor_id=current_user_id).distinct()
    student_ids = [row[0] for row in stmt.all()]
    
    clients = []
    for sid in student_ids:
        student = User.query.get(sid)
        if student:
            # Get latest note
            latest_note = ClientNote.query.filter_by(counselor_id=current_user_id, student_id=sid).order_by(ClientNote.timestamp.desc()).first()
            clients.append({
                "id": student.id,
                "name": student.username,
                "email": student.email_hash, # Privacy consideration needed
                "latest_note": latest_note.note if latest_note else None
            })
            
    return jsonify(clients), 200

@api_bp.route('/counsellor/client/<int:student_id>/note', methods=['POST'])
@jwt_required()
def add_client_note(student_id):
    current_user_id = get_jwt_identity()
    data = request.get_json()
    note_content = data.get('note')
    
    if not note_content:
        return jsonify({"msg": "Note content required"}), 400
        
    note = ClientNote(counselor_id=current_user_id, student_id=student_id, note=note_content)
    db.session.add(note)
    db.session.commit()
    
    return jsonify({"msg": "Note added successfully"}), 201



# --- MESSAGING SYSTEM ---

@api_bp.route('/messages/conversations', methods=['GET'])
@jwt_required()
def get_conversations():
    current_user_id = get_jwt_identity()
    
    # Get distinct users communicated with
    sent_to = db.session.query(ChatMessage.receiver_id).filter_by(sender_id=current_user_id)
    received_from = db.session.query(ChatMessage.sender_id).filter_by(receiver_id=current_user_id)
    
    contact_ids = set([r[0] for r in sent_to.all()] + [r[0] for r in received_from.all()])
    
    conversations = []
    for uid in contact_ids:
        contact = User.query.get(uid)
        if contact:
            # Get last message
            last_msg = ChatMessage.query.filter(
                ((ChatMessage.sender_id == current_user_id) & (ChatMessage.receiver_id == uid)) |
                ((ChatMessage.sender_id == uid) & (ChatMessage.receiver_id == current_user_id))
            ).order_by(ChatMessage.timestamp.desc()).first()
            
            unread_count = ChatMessage.query.filter_by(sender_id=uid, receiver_id=current_user_id, is_read=False).count()
            
            conversations.append({
                "user": {"id": contact.id, "name": contact.username, "role": contact.role.value},
                "last_message": last_msg.content[:50] + "..." if last_msg else "",
                "timestamp": last_msg.timestamp.isoformat() if last_msg else None,
                "unread": unread_count
            })
            
    # Sort by timestamp desc
    conversations.sort(key=lambda x: x['timestamp'] or "", reverse=True)
    return jsonify(conversations), 200

@api_bp.route('/messages/<int:user_id>', methods=['GET'])
@jwt_required()
def get_messages(user_id):
    current_user_id = get_jwt_identity()
    
    messages = ChatMessage.query.filter(
        ((ChatMessage.sender_id == current_user_id) & (ChatMessage.receiver_id == user_id)) |
        ((ChatMessage.sender_id == user_id) & (ChatMessage.receiver_id == current_user_id))
    ).order_by(ChatMessage.timestamp.asc()).all()
    
    return jsonify([
        {
            "id": m.id,
            "sender_id": m.sender_id,
            "receiver_id": m.receiver_id,
            "content": m.content,
            "timestamp": m.timestamp.isoformat(),
            "is_read": m.is_read
        } for m in messages
    ]), 200

@api_bp.route('/messages', methods=['POST'])
@jwt_required()
def send_message():
    current_user_id = get_jwt_identity()
    data = request.get_json()
    receiver_id = data.get('receiver_id')
    content = data.get('content')
    
    if not receiver_id or not content:
        return jsonify({"msg": "Receiver and content required"}), 400
        
    msg = ChatMessage(sender_id=current_user_id, receiver_id=receiver_id, content=content)
    db.session.add(msg)
    
    # Create Notification
    sender = User.query.get(current_user_id)
    notif = Notification(
        user_id=receiver_id,
        message=f"New message from {sender.username}",
        type="message"
    )
    db.session.add(notif)
    
    db.session.commit()
    
    return jsonify({"msg": "Sent", "id": msg.id, "timestamp": msg.timestamp.isoformat()}), 201

@api_bp.route('/messages/read/<int:sender_id>', methods=['PUT'])
@jwt_required()
def mark_messages_read(sender_id):
    current_user_id = get_jwt_identity()
    ChatMessage.query.filter_by(sender_id=sender_id, receiver_id=current_user_id, is_read=False)\
        .update({ChatMessage.is_read: True})
    db.session.commit()
    return jsonify({"msg": "Marked read"}), 200

# --- RESOURCES MANAGEMENT ---

@api_bp.route('/counsellor/resources', methods=['GET'])
@jwt_required()
def get_counselor_resources():
    current_user_id = get_jwt_identity()
    resources = Resource.query.filter_by(author_id=current_user_id).order_by(Resource.created_at.desc()).all()
    
    return jsonify([{
        "id": r.id,
        "title": r.title,
        "type": r.type,
        "status": r.status,
        "created_at": r.created_at.isoformat() if r.created_at else None
    } for r in resources]), 200

@api_bp.route('/counsellor/resources', methods=['POST'])
@jwt_required()
def create_resource():
    current_user_id = get_jwt_identity()
    data = request.get_json()
    
    new_res = Resource(
        title=data.get('title'),
        description=data.get('description'),
        type=data.get('type'), # video, audio, article
        url=data.get('url'),
        content=data.get('content'),
        author_id=current_user_id,
        status='pending'
    )
    db.session.add(new_res)
    db.session.commit()
    return jsonify({"msg": "Resource submitted for review", "id": new_res.id}), 201

# --- SETTINGS / PROFILE ---

@api_bp.route('/counsellor/settings', methods=['GET'])
@jwt_required()
def get_cownsellor_settings():
    current_user_id = get_jwt_identity()
    profile = CounselorProfile.query.filter_by(user_id=current_user_id).first()
    user = User.query.get(current_user_id)
    
    if not profile:
        profile = CounselorProfile(user_id=current_user_id)
        db.session.add(profile)
        db.session.commit()
        
    return jsonify({
        "username": user.username,
        "specialization": profile.specialization,
        "availability": profile.availability
    }), 200

@api_bp.route('/counsellor/settings', methods=['PUT'])
@jwt_required()
def update_counselor_settings():
    current_user_id = get_jwt_identity()
    data = request.get_json()
    
    profile = CounselorProfile.query.filter_by(user_id=current_user_id).first()
    if not profile:
        profile = CounselorProfile(user_id=current_user_id)
        db.session.add(profile)
        
    if 'specialization' in data:
        profile.specialization = data['specialization']
    if 'availability' in data:
        profile.availability = data['availability']
        
    db.session.commit()
    return jsonify(msg="Settings updated"), 200

@api_bp.route('/counsellor/client/<int:student_id>', methods=['GET'])
@jwt_required()
def get_client_details(student_id):
    current_user_id = get_jwt_identity()
    student = User.query.get(student_id)
    if not student:
        return jsonify({"msg": "Student not found"}), 404
        
    notes = ClientNote.query.filter_by(counselor_id=current_user_id, student_id=student_id).order_by(ClientNote.timestamp.desc()).all()
    appointments = Appointment.query.filter_by(counselor_id=current_user_id, student_id=student_id).order_by(Appointment.appointment_time.desc()).all()
    
    return jsonify({
        "student": {
            "id": student.id,
            "name": student.username,
            "email": student.email_hash
        },
        "notes": [{"id": n.id, "content": n.note, "timestamp": n.timestamp.isoformat()} for n in notes],
        "appointments": [{"id": a.id, "date": a.appointment_time.isoformat(), "status": a.status, "mode": a.mode} for a in appointments]
    }), 200



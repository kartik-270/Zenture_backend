from flask import Blueprint, request, jsonify, current_app
from flask_cors import CORS
from sqlalchemy import func
import re
import os 
from werkzeug.utils import secure_filename
from models import (
    db, User, UserRole, VerificationCode, ConfidentialData, 
    Resource, CounselorProfile, Appointment, ForumPost, ForumReply, bcrypt, ChatHistory, MoodCheckin, JournalEntry,Notification, UserActivityLog,
    ChatMessage, ClientNote, ChatSession, AssessmentResult
)
from extensions import mail, socketio
from followupquestions import FOLLOW_UP_QUESTIONS
from inference.safety import is_crisis as check_crisis, CRISIS_RESPONSE
import uuid
import datetime
from datetime import datetime as dt, timedelta
import json
import requests
from flask import Response
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
import requests
import threading
from flask import Response
import json

api_bp = Blueprint('api', __name__)
CORS(api_bp, resources={r"/api/*": {"origins": "*"}}, supports_credentials=True) # Enable CORS for all routes in this blueprint

# AI Models are now handled by the standalone inference_server.py on port 5001
_inference_base = os.getenv("INFERENCE_API_URL", "http://localhost:5001")
if _inference_base.endswith("/generate"):
    INFERENCE_API_URL = _inference_base
else:
    # Append /generate if it's just the base URL
    INFERENCE_API_URL = f"{_inference_base.rstrip('/')}/generate"

def get_chatbot_models():
    """No longer loads models locally to save memory."""
    return None

# CRISIS_RESPONSE is now imported from inference.safety

@api_bp.route('/test-email', methods=['GET'])
def test_email_config():
    """Diagnostic endpoint to check email configuration and connectivity."""
    config_info = {
        "MAIL_SERVER": current_app.config.get('MAIL_SERVER'),
        "MAIL_PORT": current_app.config.get('MAIL_PORT'),
        "MAIL_USERNAME": current_app.config.get('MAIL_USERNAME'),
        "MAILEROO_KEY_SET": bool(current_app.config.get('MAILEROO_API_KEY')),
    }
    
    # Check if we should use Maileroo (for production)
    if current_app.config.get('MAILEROO_API_KEY'):
        try:
            api_key = current_app.config.get('MAILEROO_API_KEY')
            url = "https://smtp.maileroo.com/api/v2/emails"
            headers = {
                "X-Api-Key": api_key,
                "Content-Type": "application/json"
            }
            payload = {
                "from": {"address": current_app.config.get('MAIL_DEFAULT_SENDER') or "no-reply@zenture.com", "name": "Zenture"},
                "to": [{"address": current_app.config.get('MAIL_USERNAME') or "recipient@example.com"}],
                "subject": "Zenture Email Diagnostic (Maileroo)",
                "plain": "This email was sent via the Maileroo HTTP API to bypass SMTP blocks."
            }
            response = requests.post(url, json=payload, headers=headers)
            response.raise_for_status()

            return jsonify({"status": "success", "method": "Maileroo API", "config": config_info}), 200
        except requests.exceptions.HTTPError as e:
            error_msg = e.response.text if hasattr(e, 'response') and e.response else str(e)
            return jsonify({"status": "error", "method": "Maileroo API", "error": error_msg, "config": config_info}), 500
        except Exception as e:
            return jsonify({"status": "error", "method": "Maileroo API", "error": str(e), "config": config_info}), 500

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

def send_with_maileroo(to_email, subject, body_text):
    """Helper to send email via Maileroo API."""
    try:
        api_key = current_app.config.get('MAILEROO_API_KEY')
        if not api_key:
            print("Missing MAILEROO_API_KEY")
            return False
        
        url = "https://smtp.maileroo.com/api/v2/emails"
        
        headers = {
            "X-API-Key": api_key,
            "Content-Type": "application/json"
        }

        payload = {
            "from": {
                "address": "zenture@53d5a76f1add5fd6.maileroo.org",
                "name": "Zenture"
            },
            "to": [
                {
                    "address": to_email
                }
            ],
            "subject": subject,
            "plain": body_text
        }

        response = requests.post(url, json=payload, headers=headers)

        print(response.status_code)
        print(response.text)

        response.raise_for_status()
        return True

    except requests.exceptions.HTTPError as e:
        print("Maileroo HTTP Error:", e.response.text)
        return False
    except Exception as e:
        print("Maileroo error:", str(e))
        return False

def save_to_chat_history(user_id, conversation_id, user_msg, bot_msg, is_crisis=False, intent="General", sentiment=0.5, emotion="neutral"):
    """Helper to save chat interaction as a single row per exchange."""
    if not user_id:
        return
    try:
        entry = ChatHistory(
            user_id=user_id,
            conversation_id=conversation_id,
            user_message=user_msg,
            bot_response=bot_msg,
            emotion=emotion,
            sentiment_score=sentiment,
            intent=intent,
            is_crisis=is_crisis
        )
        db.session.add(entry)
        db.session.commit()

        if is_crisis:
            try:
                _alert_user = User.query.get(int(user_id))
                _username = _alert_user.username if _alert_user else f"User #{user_id}"
            except Exception:
                _username = f"User #{user_id}"
            _alert_payload = {
                "user_id": user_id,
                "username": _username,
                "message": user_msg,
                "emotion": emotion or "unknown",
                "timestamp": dt.utcnow().isoformat() + "Z",
                "type": "crisis"
            }
            socketio.emit('high-risk-alert', _alert_payload, room='admin')
            socketio.emit('high-risk-alert', _alert_payload, room='counselor')
    except Exception as e:
        db.session.rollback()
        print(f"Failed to save history: {e}")


@api_bp.route('/chatbot', methods=['POST'])
def chatbot_endpoint():
    # Model check is now done by attempting to connect to the inference server

    # 3. Identify user from JWT (if provided)
    user_id = None
    try:
        from flask_jwt_extended import decode_token
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]
            if token and token != 'null':
                decoded = decode_token(token)
                user_id = decoded.get('sub')
    except Exception:
        pass

    data = request.get_json() or {}
    user_input = data.get('message', '')
    # Generate or retrieve conversation ID
    conversation_id = data.get('conversation_id') or str(uuid.uuid4())

    if not user_input:
        return jsonify(response="Please provide a message.", followUps=[], conversation_id=conversation_id), 400

    # Analytics are now handled by the inference server
    emotion_label = 'neutral'
    sentiment_score = 0.5

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

    # Risk Detection / Crisis Handling requires simple check first
    # We will let the inference server handle the detailed classification
    predicted_label = "General"
    confidence_score = 1.0

    # 7. Risk Detection / Crisis Handling
    is_crisis = check_crisis(user_input, predicted_label, confidence_score)
    if is_crisis:
        bot_response = CRISIS_RESPONSE
        if user_id:
            try:
                crisis_entry = ChatHistory(
                    user_id=user_id, conversation_id=conversation_id,
                    user_message=user_input, bot_response=CRISIS_RESPONSE,
                    emotion=emotion_label, sentiment_score=sentiment_score,
                    intent=predicted_label, is_crisis=True
                )
                db.session.add(crisis_entry)
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                print(f"Failed to save crisis history: {e}")
        
        # Emit real-time alert (include username for the admin modal)
        try:
            _alert_user = User.query.get(int(user_id)) if user_id else None
            _username = _alert_user.username if _alert_user else f"User #{user_id or 'anonymous'}"
        except Exception:
            _username = f"User #{user_id}"
        _crisis_payload = {
            "user_id": user_id,
            "username": _username,
            "message": user_input,
            "emotion": emotion_label or "unknown",
            "timestamp": dt.utcnow().isoformat() + "Z",
            "type": "crisis"
        }
        socketio.emit('high-risk-alert', _crisis_payload, room='admin')
        socketio.emit('high-risk-alert', _crisis_payload, room='counselor')

        # Return as SSE stream so the frontend's stream reader can display it correctly
        crisis_follow_ups = [
            "Book a Counselor Session",
            "Tell me more about what you're feeling",
            "What kind of support would help right now?"
        ]


        def crisis_stream():
            # Send the full message as a single chunk so it renders immediately
            yield f"data: {json.dumps({'chunk': bot_response})}\n\n"
            # Send the final signal with follow-ups so the frontend knows streaming is done
            yield f"data: {json.dumps({'final': True, 'full_response': bot_response, 'followUps': crisis_follow_ups, 'conversation_id': conversation_id, 'is_crisis': True})}\n\n"

        crisis_response = Response(crisis_stream(), mimetype='text/event-stream')
        crisis_response.headers['Cache-Control'] = 'no-cache'
        crisis_response.headers['X-Accel-Buffering'] = 'no'
        return crisis_response

    # 9. Step 2: Generate Response with Responder Model (Llama-3.2)
    try:
        # Detect if user is specifically asking for help/tips vs sharing a feeling
        help_keywords = ["help", "advice", "tip", "strategy", "cope", "how", "what", "steps", "method", "guide"]
        is_request = any(kw in user_input.lower() for kw in help_keywords)
        
        # Dynamic Prompting based on Intent
        if predicted_label in ["Stress", "Anxiety", "Sadness"]:
            if is_request:
                objective = (
                    "You are a solution-focused assistant. "
                    "Provide DIRECT, PRACTICAL advice immediately. "
                    "Start with a helpful opening and a numbered list of strategies."
                )
                force_start = "I'd be happy to share some strategies with you. Here are some practical steps:\n1. "
            else:
                objective = (
                    "You are an empathetic counselor. Validate the user's emotions warmth and empathy. "
                    "After validating, offer a few gentle, practical tips to help them feel better."
                )
                force_start = "I'm sorry to hear you're feeling that way. It's completely valid to feel this way. To help you manage this, here are a few things you can try:\n1. "
        else:
            objective = "You are a professional and empathetic counselor. Respond warmly and validate emotions."
            force_start = ""

        sys_prompt = (
            f"You are Zenture, a professional and empathetic mental health counselor. {objective} "
            "STRICT PERSONA RULES: 1. You are an AI assistant, not a human. Never invent personal life stories, family, or pets (e.g., do not say 'my dog died' or 'I have a kids'). 2. Focus entirely on the user's feelings. "
            "LANGUAGE RULES: Auto-detect the language of the user's message and ALWAYS respond in that EXACT same language (Hindi, Tamil, Spanish, French, Telugu, etc.). "
            "Never switch back to English unless the user does. Maintain quality and empathy in all languages. "
            "FORMATTING: Keep responses structured, use a friendly tone with emojis."
        )
        
        # Format conversation history into Llama-3 Instruct blocks
        formatted_prompt = f"<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n\n{sys_prompt}<|eot_id|>"
        
        if user_id:
            conversation_history = ChatHistory.query.filter_by(
                user_id=user_id,
                conversation_id=conversation_id
            ).order_by(ChatHistory.timestamp.asc()).all()

            lookback_window = 15
            for turn in conversation_history[-lookback_window:]:
                # Each row has user_message + bot_response (single row per exchange)
                if turn.user_message:
                    formatted_prompt += f"<|start_header_id|>user<|end_header_id|>\n\n{turn.user_message}<|eot_id|>"
                if turn.bot_response:
                    formatted_prompt += f"<|start_header_id|>assistant<|end_header_id|>\n\n{turn.bot_response}<|eot_id|>"

        
        # Add current user input and assistant start
        formatted_prompt += f"<|start_header_id|>user<|end_header_id|>\n\n{user_input}<|eot_id|><|start_header_id|>assistant<|end_header_id|>\n\n"

        # Prepare request for standalone inference server
        payload = {
            "prompt": user_input,
            "sys_prompt": sys_prompt,
            "history_prompt": formatted_prompt.replace(f"<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n\n{sys_prompt}<|eot_id|>", "")
        }

        # Capture the real app object NOW (while request context is active).
        # current_app is a proxy and cannot be used inside a streaming generator
        # that runs outside the request context (eventlet async).
        flask_app = current_app._get_current_object()

        def generate_proxy_stream():
            full_text_accumulator = ""  # Accumulate all chunks as fallback
            history_saved = False       # Flag to avoid double-saving
            try:
                print(f"Proxying to Inference Server: {INFERENCE_API_URL}", flush=True)
                with requests.post(INFERENCE_API_URL, json=payload, stream=True, timeout=120) as r:
                    for line in r.iter_lines():
                        if line:
                            decoded_line = line.decode('utf-8')
                            yield f"{decoded_line}\n\n"

                            if decoded_line.startswith("data: {"):
                                try:
                                    json_data = json.loads(decoded_line[6:])

                                    # Accumulate chunk text for fallback saving
                                    if json_data.get('chunk'):
                                        full_text_accumulator += json_data['chunk']

                                    # Primary save path: when inference server sends final flag
                                    if json_data.get('final') and not history_saved:
                                        f_response = json_data.get('full_response') or full_text_accumulator
                                        p_label = json_data.get('predicted_label', 'General')
                                        s_score = json_data.get('sentiment_score', 0.5)
                                        e_label = json_data.get('emotion_label', 'neutral')
                                        # Use actual confidence score — NOT hardcoded 1.0
                                        # so borderline model classifications don't trigger crisis
                                        p_confidence = json_data.get('confidence_score', 0.5)
                                        i_crisis = check_crisis(user_input, p_label, p_confidence)

                                        # Use the captured real app object (not the proxy)
                                        with flask_app.app_context():
                                            save_to_chat_history(user_id, conversation_id, user_input, f_response, i_crisis, p_label, s_score, e_label)
                                        history_saved = True
                                        print(f"[Chat History] Saved for user_id={user_id}, conv={conversation_id}", flush=True)
                                except Exception as e:
                                    print(f"Error parsing final chunk in proxy: {e}", flush=True)

            except Exception as e:
                print(f"Inference server connection error: {e}", flush=True)
                yield f"data: {json.dumps({'chunk': 'I am having trouble connecting to my brain service. Please ensure the inference server is running.'})}\n\n"

            # Fallback save: if generation ended but 'final' flag was never received
            if not history_saved and full_text_accumulator and user_id:
                try:
                    print(f"[Chat History] Fallback save for user_id={user_id}", flush=True)
                    with flask_app.app_context():
                        save_to_chat_history(user_id, conversation_id, user_input, full_text_accumulator)
                except Exception as e:
                    print(f"[Chat History] Fallback save failed: {e}", flush=True)


        response = Response(generate_proxy_stream(), mimetype='text/event-stream')
        response.headers['Cache-Control'] = 'no-cache'
        response.headers['X-Accel-Buffering'] = 'no'
        return response

    except Exception as e:
        print(f"Responder model error: {e}")
        return jsonify(response="I'm not able to generate a response right now.", followUps=[], conversation_id=conversation_id), 500

    # 10. Final Step: Save History & Return Response
    all_follow_ups = FOLLOW_UP_QUESTIONS.get(predicted_label, ["How can I help you today?"])
    random_follow_ups = random.sample(all_follow_ups, min(len(all_follow_ups), 2))
    
    # Merge sentiment referrals if any
    final_follow_ups = list(set(follow_ups + random_follow_ups))

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
            print(f"Failed to save history: {e}")

    return jsonify(response=bot_response, followUps=final_follow_ups, conversation_id=conversation_id), 200


    # ... save to history and return ...

@api_bp.route('/chatbot/feedback', methods=['POST'])
def save_chatbot_feedback():
    # Attempt to get user identity if token exists, but don't require it
    user_id = None
    try:
        from flask_jwt_extended import decode_token
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]
            if token and token != 'null':
                decoded = decode_token(token)
                user_id = decoded.get('sub')
    except Exception:
        pass

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
def end_chatbot_session():
    # Attempt to get user identity if token exists, but don't require it
    user_id = None
    try:
        from flask_jwt_extended import decode_token
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]
            if token and token != 'null':
                decoded = decode_token(token)
                user_id = decoded.get('sub')
    except Exception:
        pass

    data = request.get_json()
    conversation_id = data.get('conversation_id')
    
    if not conversation_id:
        return jsonify(msg="Conversation ID is required"), 400
        
    session = ChatSession.query.filter_by(conversation_id=conversation_id).first()
    if not session:
        return jsonify(msg="Session not found"), 404
        
    session.end_time = dt.utcnow()
    session.is_completed = True
    
    # Calculate primary emotion for the session
    history = ChatHistory.query.filter_by(conversation_id=conversation_id, sender='user').all()
    if history:
        emotions = [h.emotion for h in history if h.emotion]
        if emotions:
            session.primary_emotion = max(set(emotions), key=emotions.count)
            
    db.session.commit()
    return jsonify(msg="Session ended successfully"), 200

@api_bp.route('/chatbot/facial-analysis', methods=['POST'])
@jwt_required()
def chatbot_facial_analysis():
    """
    Proxy route: Accepts a base64 image from the frontend, forwards it to the
    inference server for emotion detection, logs the result to the DB, and
    returns the emotion/stress data back to the frontend.
    This keeps the inference server hidden from direct frontend access.
    """
    import requests as http_requests
    import os

    user_id = get_jwt_identity()
    data = request.get_json()
    image_b64 = data.get('image')

    if not image_b64:
        return jsonify({'error': 'No image provided'}), 400

    inference_url = os.environ.get('INFERENCE_SERVER_URL', 'http://localhost:5001')

    try:
        # Forward the image to the inference server
        inference_res = http_requests.post(
            f"{inference_url}/facial-stress",
            json={'image': image_b64},
            timeout=15
        )
        inference_res.raise_for_status()
        result = inference_res.json()

        stress_level = result.get('stress_level')
        emotion = result.get('emotion')

        # Log to MoodCheckin table
        if stress_level is not None:
            new_checkin = MoodCheckin(
                user_id=user_id,
                mood=f"Facial: {emotion.capitalize()}" if emotion else "Facial Analysis",
                intensity=max(1, min(10, int(round(stress_level)))),
                facial_stress_score=float(stress_level),
                analysis_report=f"Chatbot facial scan — emotion: {emotion}, stress: {stress_level}/10"
            )
            db.session.add(new_checkin)
            db.session.commit()

        return jsonify(result), 200

    except http_requests.exceptions.Timeout:
        return jsonify({'error': 'Inference server timed out'}), 504
    except Exception as e:
        print(f"Facial analysis proxy error: {e}")
        return jsonify({'error': 'Facial analysis failed'}), 500

def send_username_email(email, username):
    """Sends username via Maileroo API (production) or SMTP (local fallback)."""
    subject = "Your Username for Mental Health Platform"
    body = f"""
Hello,

You requested your username. Your username is: {username}

If you did not request this, please ignore this email.
"""
    # 1. Try Maileroo First (Production)
    # if send_with_maileroo(email, subject, body):
    #     return True
        
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
        if u.email_hash:
            try:
                if bcrypt.check_password_hash(u.email_hash, email):
                    user = u
                    break
            except ValueError:
                # If email_hash is not a valid hash, skip it
                continue
    
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
        if u.email_hash:
            try:
                if bcrypt.check_password_hash(u.email_hash, email):
                    user = u
                    break
            except ValueError:
                # If email_hash is not a valid hash, skip it
                continue
            
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
        "date": r.created_at.strftime("%b %d, %Y"),
        "views": r.views
    } for r in resources]), 200

@api_bp.route('/resources/<int:resource_id>', methods=['GET'])
def get_single_resource(resource_id):
    resource = Resource.query.get_or_404(resource_id)
    
    # Track view if user is logged in
    try:
        from flask_jwt_extended import get_jwt_identity, verify_jwt_in_request
        verify_jwt_in_request(optional=True)
        user_id = get_jwt_identity()
        if user_id:
            # Only record unique log for this user/resource
            existing_log = UserActivityLog.query.filter_by(user_id=user_id, resource_id=resource_id).first()
            if not existing_log:
                new_log = UserActivityLog(user_id=user_id, resource_id=resource_id)
                db.session.add(new_log)
                resource.views += 1
                db.session.commit()
    except Exception as e:
        print(f"Error logging resource view: {e}")

    return jsonify({
        "id": resource.id,
        "title": resource.title,
        "description": resource.description,
        "type": resource.type,
        "url": resource.url,
        "content": resource.content,
        "views": resource.views,
        "author": resource.author.username if resource.author else "Zenture Team",
        "date": resource.created_at.strftime("%b %d, %Y")
    }), 200
    resource = Resource.query.get_or_404(resource_id)
    return jsonify({
        "id": resource.id,
        "title": resource.title,
        "description": resource.description,
        "type": resource.type,
        "url": resource.url,
        "content": resource.content,
        "author": resource.author.username if resource.author else "Zenture Team",
        "date": resource.created_at.strftime("%b %d, %Y")
    }), 200

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
    subject = "Your Verification Code for Zenture Wellness"
    body = f"""
Hello,

Thank you for registering. Your verification code is: {code}

This code will expire in 10 minutes.

If you did not request this, please ignore this email.
"""
    
    # 1. Try Maileroo First
    # if send_with_maileroo(email, subject, body):
    #     return True
        
    # 2. Fallback to SMTP
    try:
        msg = Message(subject, recipients=[email])
        msg.body = body
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


import cloudinary
import cloudinary.uploader

@api_bp.route('/upload', methods=['POST'])
@jwt_required()
def upload_media():
    if 'file' not in request.files:
        return jsonify({"msg": "No file part"}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({"msg": "No selected file"}), 400

    try:
        # Initialize Cloudinary configuration
        cloudinary.config(
            cloud_name=current_app.config['CLOUDINARY_CLOUD_NAME'],
            api_key=current_app.config['CLOUDINARY_API_KEY'],
            api_secret=current_app.config['CLOUDINARY_API_SECRET'],
            secure=True
        )

        # Upload the file to Cloudinary
        upload_result = cloudinary.uploader.upload(file, resource_type="auto")
        
        return jsonify({
            "msg": "File uploaded successfully",
            "url": upload_result.get("secure_url")
        }), 200
    except Exception as e:
        print(f"Cloudinary upload error: {e}")
        return jsonify({"msg": f"Failed to upload media: {str(e)}"}), 500


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

@api_bp.route('/admin/alerts/high-risk', methods=['GET'])
@roles_required('admin')
def get_high_risk_alerts():
    """Returns crisis chat history entries for the admin review dashboard."""
    try:
        crisis_entries = (
            ChatHistory.query
            .filter_by(is_crisis=True)
            .order_by(ChatHistory.timestamp.desc())
            .limit(50)
            .all()
        )

        result = []
        for entry in crisis_entries:
            user = User.query.get(entry.user_id)
            result.append({
                "id": entry.id,
                "user_id": entry.user_id,
                "username": user.username if user else f"User #{entry.user_id}",
                "message": entry.user_message or "",
                "bot_response": entry.bot_response or "",
                "emotion": entry.emotion or "unknown",
                "intent": entry.intent or "unknown",
                "timestamp": entry.timestamp.isoformat() + "Z" if entry.timestamp else None,
                "conversation_id": entry.conversation_id,
                "is_resolved": entry.is_resolved or False,
                "type": "crisis"
            })

        return jsonify(result), 200
    except Exception as e:
        return jsonify(msg=str(e)), 500


@api_bp.route('/admin/alerts/high-risk/<int:alert_id>/resolve', methods=['PUT'])
@roles_required('admin')
def resolve_high_risk_alert_admin(alert_id):
    """Admin marks a crisis alert as resolved."""
    entry = ChatHistory.query.get_or_404(alert_id)
    entry.is_resolved = True
    db.session.commit()
    return jsonify({"msg": "Marked as resolved"}), 200


@api_bp.route('/counselor/alerts/high-risk/<int:alert_id>/resolve', methods=['PUT'])
@roles_required('counselor')
def resolve_high_risk_alert_counselor(alert_id):
    """Counselor marks a crisis alert as resolved after outreach."""
    entry = ChatHistory.query.get_or_404(alert_id)
    entry.is_resolved = True
    db.session.commit()
    return jsonify({"msg": "Marked as resolved"}), 200




@api_bp.route('/admin/analytics/assessments', methods=['GET'])
@roles_required('admin')
def get_assessment_analytics():
    try:
        # Get count per test type
        counts = db.session.query(
            AssessmentResult.test_type,
            db.func.count(AssessmentResult.id)
        ).group_by(AssessmentResult.test_type).all()
        
        # Get high-risk distribution (scores indicating severe issues)
        # For simplicity, we'll just return counts of each test type for now
        # and maybe some basic breakdown if we have a bigger dataset.
        
        data = []
        for test, count in counts:
            data.append({"test": test, "count": count})
            
        return jsonify(data), 200
    except Exception as e:
        return jsonify(msg=str(e)), 500

# Safety Check / Health
@api_bp.route('/health', methods=['GET'])
def health_check():
    return jsonify(status="ok"), 200

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

@api_bp.route('/assessments', methods=['POST'])
@jwt_required()
def save_assessment():
    current_user_id = get_jwt_identity()
    data = request.get_json()
    
    if not data or 'test_type' not in data or 'score' not in data:
        return jsonify(msg="Missing required fields"), 400
        
    result = AssessmentResult(
        user_id=current_user_id,
        test_type=data.get('test_type'),
        score=data.get('score'),
        interpretation=data.get('interpretation')
    )
    
    try:
        db.session.add(result)
        db.session.commit()
        return jsonify(msg="Assessment result saved successfully"), 201
    except Exception as e:
        db.session.rollback()
        return jsonify(msg=f"Error saving assessment: {str(e)}"), 500

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
    
    # Get resource statistics (unique views and total views)
    resources = Resource.query.filter_by(status='approved').order_by(Resource.views.desc()).all()
    resource_stats = [
        {
            "id": r.id,
            "title": r.title,
            "type": r.type,
            "views": r.views
        } for r in resources
    ]
    
    return jsonify({
        "message": "Welcome, Admin!",
        "stats": {
            "totalUsers": user_count,
            "totalAppointments": appointment_count,
            "resources": resource_stats
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
                Appointment.appointment_time >= dt.utcnow()
            )
            .order_by(Appointment.appointment_time.asc())
            .limit(5)
            .all()
        )

        appointments_list = [
            {
                "student_username": user.username,
                "appointment_time": (appointment.appointment_time + timedelta(hours=5, minutes=30)).isoformat()
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
            "meeting_location": c.meeting_location,
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
        availability=data.get('availability', {}),
        meeting_location=data.get('meeting_location')
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
        
    if 'meeting_location' in data:
        profile.meeting_location = data['meeting_location']
        
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
    if 'language' in data: resource.language = data['language']
    
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
    
    # 2. Active Users — users with activity log in last 30 days, fallback to ChatHistory
    thirty_days_ago = dt.utcnow() - timedelta(days=30)
    active_users = User.query.join(UserActivityLog).filter(
        UserActivityLog.timestamp >= thirty_days_ago
    ).distinct().count()
    
    # Fallback: if activity log is sparse, count users with any chat in last 30 days
    if active_users == 0:
        active_users = db.session.query(ChatHistory.user_id).filter(
            ChatHistory.user_id.isnot(None),
            ChatHistory.timestamp >= thirty_days_ago
        ).distinct().count()
    
    # 3. Total Sessions — count of AI chatbot sessions (ChatSession)
    total_sessions = ChatSession.query.count()
    
    # 4. Avg Session Duration — compute from actual ChatSession start/end times
    completed_sessions = ChatSession.query.filter(
        ChatSession.end_time.isnot(None),
        ChatSession.start_time.isnot(None)
    ).all()
    
    if completed_sessions:
        total_seconds = sum(
            (s.end_time - s.start_time).total_seconds()
            for s in completed_sessions
            if s.end_time > s.start_time
        )
        avg_seconds = total_seconds / len(completed_sessions)
        minutes = int(avg_seconds // 60)
        seconds = int(avg_seconds % 60)
        avg_duration = f"{minutes}m {seconds}s"
    else:
        avg_duration = "N/A"
    
    # 5. Urgent Action Required (Unacknowledged Crisis)
    unacknowledged_alerts = ChatHistory.query.filter_by(is_crisis=True).count()
    
    return jsonify({
        "totalUsers": total_users,
        "activeUsers": active_users,
        "totalSessions": total_sessions,
        "avgSessionDuration": avg_duration,
        "unacknowledgedAlerts": unacknowledged_alerts
    })




@api_bp.route('/admin/analytics/counselors-status', methods=['GET'])
@roles_required('admin')
def get_counselors_status():
    from flask import jsonify
    
    total_counselors = CounselorProfile.query.count()
    # Let's consider counselors with availability configured as at least active. 
    # For a real app, you'd check a last_active timestamp or WebSocket connection.
    # We will simulate "Online" based on total counselors for now
    online = total_counselors
    
    # Available now: Check how many don't have an active ongoing appointment
    now = dt.utcnow()
    # Active appointments are those where status is 'booked' and time is around now
    busy_counselors = Appointment.query.filter(
        Appointment.status == 'booked',
        Appointment.appointment_time <= now,
        Appointment.appointment_time >= now - timedelta(hours=1)
    ).distinct(Appointment.counselor_id).count()
    
    available = max(0, total_counselors - busy_counselors)
    
    return jsonify({
        "online": online,
        "available": available,
        "avgWaitTime": "~2 mins" 
    })

@api_bp.route('/admin/analytics/forum-activity', methods=['GET'])
@roles_required('admin')
def get_forum_activity():
    twenty_four_hours_ago = dt.utcnow() - timedelta(hours=24)
    
    new_posts = ForumPost.query.filter(ForumPost.timestamp >= twenty_four_hours_ago).count()
    
    # Assuming all posts are unmoderated initially, but since we don't have a moderated flag, 
    # we'll return 0 or the total new posts for the demo.
    unmoderated_posts = ForumPost.query.count() // 2 # Mocked logic if no flag exists
    
    active_threads = ForumPost.query.join(ForumReply).distinct(ForumPost.id).count()
    
    return jsonify({
        "newPosts24h": new_posts,
        "unmoderatedPosts": unmoderated_posts,
        "activeThreads": active_threads
    })

@api_bp.route('/admin/students', methods=['GET'])
@roles_required('admin')
def get_students():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 10, type=int)
    
    students_query = User.query.filter_by(role=UserRole.STUDENT).order_by(User.id.desc())
    pagination = students_query.paginate(page=page, per_page=per_page, error_out=False)
    
    students_data = []
    today = dt.utcnow().date()
    
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

@api_bp.route('/admin/moderators', methods=['GET'])
@roles_required('admin')
def get_moderators():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 10, type=int)
    
    moderators_query = User.query.filter_by(role=UserRole.MODERATOR).order_by(User.id.desc())
    pagination = moderators_query.paginate(page=page, per_page=per_page, error_out=False)
    
    moderators_data = []
    today = dt.utcnow().date()
    
    for mod in pagination.items:
        # Same stats as students
        mood_count = MoodCheckin.query.filter(
            MoodCheckin.user_id == mod.id,
            func.date(MoodCheckin.timestamp) == today
        ).count()
        
        resource_count = UserActivityLog.query.filter(
            UserActivityLog.user_id == mod.id,
            func.date(UserActivityLog.timestamp) == today
        ).count()
        
        moderators_data.append({
            "id": mod.id,
            "username": mod.username,
            "stats": {
                "mood_checkins": mood_count,
                "resources_viewed": resource_count
            }
        })
        
    return jsonify({
        "moderators": moderators_data,
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
    seven_days_ago = dt.utcnow() - timedelta(days=7)
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
        # Parse as IST (local) and convert to UTC
        local_time = dt.strptime(combined_dt_str, "%Y-%m-%d %H:%M")
        # Assuming the app is used in IST (+5:30)
        appointment_time = local_time - datetime.timedelta(hours=5, minutes=30)
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
            "date": local_time.strftime("%Y-%m-%d"),
            "time": local_time.strftime("%H:%M"),
            "mode": mode,
            "description": description,
            "status": new_appointment.status,
            "meeting_link": new_appointment.meeting_link
        }
    }), 201

# --- MUTUAL FEEDBACK ENDPOINTS ---

@api_bp.route('/appointments/<int:appointment_id>/feedback', methods=['POST'])
@jwt_required()
def submit_appointment_feedback(appointment_id):
    """Allows both counselors and students to submit feedback for an appointment."""
    user_id = get_jwt_identity()
    claims = get_jwt()
    role = claims.get('role')
    data = request.json
    
    appointment = Appointment.query.get_or_404(appointment_id)
    
    if role == 'counselor':
        if appointment.counselor_id != int(user_id):
            return jsonify({"error": "Unauthorized"}), 403
        appointment.counselor_feedback = data.get('feedback')
    elif role == 'student':
        if appointment.student_id != int(user_id):
            return jsonify({"error": "Unauthorized"}), 403
        appointment.student_emotional_state = data.get('emotional_state')
        appointment.session_helpfulness = data.get('helpfulness')
        appointment.student_feedback = data.get('feedback')
        appointment.counselor_rating = data.get('rating')
    else:
        return jsonify({"error": "Invalid role"}), 403
        
    db.session.commit()
    return jsonify({"message": "Feedback submitted successfully"}), 200

# --- FACIAL STRESS LOGGING ---

@api_bp.route('/mood-checkin/facial-analysis', methods=['POST'])
@jwt_required()
def log_facial_analysis():
    """Logs facial stress analysis results into a mood check-in or specific log."""
    user_id = get_jwt_identity()
    data = request.json
    stress_score = data.get('stress_level')
    
    if stress_score is None:
        return jsonify({"error": "Stress level is required"}), 400
        
    # Create a new mood check-in with 'facial_analysis' type
    new_checkin = MoodCheckin(
        user_id=user_id,
        mood="Facial Analysis",
        intensity=int(float(stress_score)), # Mapping 1-10 to intensity
        facial_stress_score=float(stress_score),
        analysis_report=f"Stress detected via facial analysis: {stress_score}/10"
    )
    db.session.add(new_checkin)
    db.session.commit()
    
    return jsonify({"message": "Facial analysis logged", "checkin_id": new_checkin.id}), 201

# --- ANALYTICS ENDPOINTS ---
@api_bp.route('/admin/analytics/engagement', methods=['GET'])
@roles_required('admin')
def get_engagement_stats():
    # Last 7 days
    today = dt.utcnow().date()
    days = [(today - timedelta(days=i)).strftime('%a') for i in range(6, -1, -1)]
    
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
    today = dt.utcnow().date()
    start_date = today - timedelta(days=7)
    
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
    days = [(today - timedelta(days=i)).strftime('%Y-%m-%d') for i in range(6, -1, -1)]
    
    for day in days:
        data_map[day] = {'low': 0, 'medium': 0, 'high': 0, 'day': dt.strptime(day, '%Y-%m-%d').strftime('%a')}
        
    for date, mood, count in checkins:
        date_str = date.strftime('%Y-%m-%d')
        if date_str in data_map:
            severity = mood_map.get(mood.lower(), 'medium')
            data_map[date_str][severity] += count
            
    return jsonify(list(data_map.values()))

@api_bp.route('/admin/analytics/resources', methods=['GET'])
@roles_required('admin')
def get_resource_analytics():
    try:
        # LEFT JOIN so resources with 0 activity log entries still appear
        top_resources = db.session.query(
            Resource.title,
            Resource.type,
            func.count(UserActivityLog.id).label('views')
        ).outerjoin(
            UserActivityLog, UserActivityLog.resource_id == Resource.id
        ).filter(
            Resource.status == 'licensed'  # only show approved resources
        ).group_by(
            Resource.id, Resource.title, Resource.type
        ).order_by(
            func.count(UserActivityLog.id).desc()
        ).limit(5).all()

        # If no licensed resources, return all resources regardless of status
        if not top_resources:
            top_resources = db.session.query(
                Resource.title,
                Resource.type,
                func.count(UserActivityLog.id).label('views')
            ).outerjoin(
                UserActivityLog, UserActivityLog.resource_id == Resource.id
            ).group_by(
                Resource.id, Resource.title, Resource.type
            ).order_by(
                func.count(UserActivityLog.id).desc()
            ).limit(5).all()

        return jsonify([
            {"title": r[0], "type": r[1], "views": r[2]}
            for r in top_resources
        ])
    except Exception as e:
        print(f"Resource analytics error: {e}")
        return jsonify([]), 200


@api_bp.route('/admin/analytics/chat', methods=['GET'])
@roles_required('admin')
def get_chat_analytics():
    # 1. Sentiment Arc (Real Logic)
    # Fetch chats from the last 24 hours
    cutoff = dt.utcnow() - timedelta(hours=24)
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

@api_bp.route('/admin/analytics/holistic', methods=['GET'])
@roles_required('admin')
def get_holistic_analytics():
    """Provides aggregated holistic insights including causes of stress and wellness index."""
    from textblob import TextBlob
    from collections import Counter

    # 1. Causes of Stress (Logic: Keyword extraction from ChatHistory and MoodCheckin)
    stress_categories = {
        "Academic": ["exam", "study", "grades", "assignment", "test", "professor", "college"],
        "Social": ["friend", "relationship", "peer", "lonely", "social", "party", "talk"],
        "Personal": ["family", "home", "health", "sleep", "money", "future"],
        "Emotional": ["sad", "angry", "anxious", "depressed", "hopeless"]
    }

    category_counts = {cat: 0 for cat in stress_categories}
    
    # Analyze recent ChatHistory (last 30 days)
    cutoff = dt.utcnow() - timedelta(days=30)
    chats = ChatHistory.query.filter(ChatHistory.timestamp >= cutoff, ChatHistory.sender == 'user').all()
    
    for chat in chats:
        msg = chat.message.lower()
        for cat, keywords in stress_categories.items():
            if any(kw in msg for kw in keywords):
                category_counts[cat] += 1

    # 2. Wellness Index (Based on mood, sentiment, and completion rates)
    # Simple algorithm: average of normalized metrics
    avg_sentiment = db.session.query(func.avg(ChatHistory.sentiment_score)).filter(ChatHistory.timestamp >= cutoff).scalar() or 0
    avg_mood = db.session.query(func.avg(MoodCheckin.intensity)).filter(MoodCheckin.timestamp >= cutoff).scalar() or 5
    
    # Normalize: sentiment (-1 to 1 -> 0 to 100), mood (1 to 10 -> 0 to 100)
    norm_sentiment = (avg_sentiment + 1) * 50
    norm_mood = avg_mood * 10
    wellness_index = (norm_sentiment + norm_mood) / 2

    # 3. Actionable Insights
    top_cat = max(category_counts, key=category_counts.get)
    insights = [
        {
            "id": 1,
            "title": f"High {top_cat} Stress Detected",
            "description": f"Students are frequently mentioning {top_cat.lower()}-related concerns. Consider organizing a focus group or sharing resources.",
            "type": "warning"
        },
        {
            "id": 2,
            "title": "Positive Engagement Trend",
            "description": "Chatbot completion rates have increased by 15% this week.",
            "type": "success"
        }
    ]

    return jsonify({
        "causesOfStress": [{"subject": k, "A": v, "fullMark": max(category_counts.values()) or 10} for k, v in category_counts.items()],
        "wellnessIndex": round(wellness_index, 1),
        "insights": insights
    })



@api_bp.route('/admin/student/<int:user_id>/confidential', methods=['GET'])
@roles_required('admin')
def get_student_confidential_admin(user_id):
    """Admin route to fetch student personal details."""
    conf = ConfidentialData.query.filter_by(user_id=user_id).first()
    if not conf:
        return jsonify({"error": "Confidential data not found"}), 404
    
    user = User.query.get(user_id)
    return jsonify({
        "username": user.username,
        "name": conf.name,
        "phone": conf.phone_number,
        "parent_name": conf.parent_name,
        "parent_phone": conf.parent_phone_number
    })

@api_bp.route('/counselor/student/<int:user_id>/confidential', methods=['GET'])
@roles_required('counselor')
def get_student_confidential_counselor(user_id):
    """Counselor route to fetch student personal details for outreach."""
    # In a real app, check if the counselor is assigned to this student
    conf = ConfidentialData.query.filter_by(user_id=user_id).first()
    if not conf:
        return jsonify({"error": "Confidential data not found"}), 404
    
    user = User.query.get(user_id)
    return jsonify({
        "username": user.username,
        "name": conf.name,
        "phone": conf.phone_number
    })

@api_bp.route('/admin/assign-counselor', methods=['POST'])
@roles_required('admin')
def assign_counselor_manual():
    """Allows admin to manually assign a counselor to a student for an urgent session."""
    data = request.json
    student_id = data.get('student_id')
    counselor_id = data.get('counselor_id')
    
    if not student_id or not counselor_id:
        return jsonify({"error": "Missing student or counselor ID"}), 400
    
    # Create an urgent appointment
    appointment_time = dt.utcnow() + timedelta(minutes=30) # Suggest in 30 mins
    
    new_appointment = Appointment(
        student_id=student_id,
        counselor_id=counselor_id,
        appointment_time=appointment_time,
        status="booked", # Pre-confirmed by admin
        notes="URGENT: Assigned by Admin for crisis management.",
        mode="video_call",
        meeting_link=f"/session/{uuid.uuid4()}"
    )
    db.session.add(new_appointment)
    
    # Notify both
    notif_student = Notification(
        user_id=student_id, 
        message=f"Admin has assigned an urgent session for you with Counselor ID {counselor_id} in 30 minutes.",
        link=new_appointment.meeting_link
    )
    notif_counselor = Notification(
        user_id=counselor_id, 
        message=f"URGENT: Admin has assigned you a crisis case (Student ID {student_id}). Please join in 30 minutes.",
        link=new_appointment.meeting_link
    )
    db.session.add(notif_student)
    db.session.add(notif_counselor)
    
    db.session.commit()
    return jsonify({"msg": "Counselor assigned successfully and notifications sent.", "meeting_link": new_appointment.meeting_link})


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
        "appointment_time": (a.appointment_time + timedelta(hours=5, minutes=30)).isoformat(),
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

@api_bp.route('/mood-checkin/today-status', methods=['GET'])
@jwt_required()
def get_today_mood_checkin():
    """Returns all mood check-ins for today, using IST-aware date."""
    user_id = get_jwt_identity()
    user = User.query.get(user_id)
    # Use IST date to match the user's local "today" (UTC+5:30)
    now_ist = datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
    today_ist = now_ist.date()

    # IST midnight boundaries for today
    today_ist_start = datetime.datetime(today_ist.year, today_ist.month, today_ist.day) - datetime.timedelta(hours=5, minutes=30)
    today_ist_end = today_ist_start + datetime.timedelta(days=1)

    # Use the User's last_checkin_date: true ONLY if they checked in today (IST)
    has_checked_in = (user.last_checkin_date == today_ist)

    # Fetch checkins within today's IST window only
    checkins = MoodCheckin.query.filter(
        MoodCheckin.user_id == user_id,
        MoodCheckin.timestamp >= today_ist_start,
        MoodCheckin.timestamp < today_ist_end
    ).order_by(MoodCheckin.timestamp.asc()).all()

    if checkins:
        latest = checkins[-1]
        history = [{
            "mood": c.mood,
            "intensity": c.intensity,
            "timestamp": c.timestamp.isoformat() + "Z"
        } for c in checkins]

        return jsonify({
            "hasCheckedIn": has_checked_in,
            "count": len(checkins),
            "latest": {
                "mood": latest.mood,
                "intensity": latest.intensity,
                "timestamp": latest.timestamp.isoformat() + "Z"
            },
            "allCheckins": history
        })
    else:
        return jsonify({
            "hasCheckedIn": has_checked_in,
            "count": 0,
            "allCheckins": []
        })


@api_bp.route('/mood-checkin', methods=['POST'])
@jwt_required()
def add_mood_checkin():
    """Adds a detailed mood check-in for the current user."""
    user_id = get_jwt_identity()
    data = request.get_json()
    
    mood = data.get('mood')
    intensity = data.get('intensity', 5)
    sleep = data.get('sleep', 'Good')
    social = data.get('social', False)
    energy = data.get('energy', 'Medium')
    
    if not mood:
        return jsonify(msg="Mood is required"), 400

    # 1. Wellness Index (0-10)
    mood_w = {'Happy': 10, 'Calm': 10, 'Stressed': 4, 'Sad': 3, 'Anxious': 3, 'Angry': 2}
    mood_b = mood_w.get(mood, 5)
    int_f = (intensity/10.0) if mood in ['Happy', 'Calm'] else ((11-intensity)/10.0)
    s_l = {'Excellent': 10, 'Good': 8, 'Fair': 5, 'Poor': 2}.get(sleep, 8)
    e_l = {'High': 10, 'Medium': 7, 'Low': 3}.get(energy, 7)
    wellness_score = round(((mood_b*0.4)+(int_f*10*0.2)+(s_l*0.15)+(e_l*0.15)+(10 if social else 0)*0.1), 1)

    # 2. Insights Generator
    insights = []
    if mood in ['Sad', 'Angry', 'Stressed', 'Anxious']:
        if intensity > 7: insights.append("Intensity is high right now; remember deep breathing.")
        if e_l < 5 and s_l < 5: insights.append("Fatigue is often linked to heighten stress or low mood; prioritize rest tonight.")
        if not social: insights.append("When we struggle, we often withdraw. Try to reach out to one person today.")
    elif mood in ['Happy', 'Calm']:
        if e_l > 7: insights.append("You have strong momentum today—consider working on a creative project.")
        if social: insights.append("Social connection is clearly boosting your wellbeing!")

    if wellness_score < 4.5: insights.append("Your wellness index is a bit low today. Consider a counselor chat.")
    if not insights: insights.append("Consistency helps you understand your wellness patterns.")

    analysis_report = " ".join(insights[:3])

    new_checkin = MoodCheckin(
        user_id=user_id, 
        mood=mood,
        intensity=intensity,
        sleep_quality=sleep,
        social_interaction=social,
        energy_level=energy,
        wellness_score=wellness_score,
        analysis_report=analysis_report
    )
    db.session.add(new_checkin)

    # Streak Logic
    user = User.query.get(user_id)
    # Use IST date so daily reset works correctly for IST users (UTC+5:30)
    today = (datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)).date()
    
    # Initialize streak_count if it's None
    if user.streak_count is None:
        user.streak_count = 0
        
    print(f"DEBUG: Streak logic for user {user_id}. last_checkin: {user.last_checkin_date}, today (IST): {today}")
    
    if user.last_checkin_date is None:
        user.streak_count = 1
        user.last_checkin_date = today
        print(f"DEBUG: First checkin. Streak set to 1")
    elif user.last_checkin_date < today:
        if user.last_checkin_date == today - datetime.timedelta(days=1):
            user.streak_count += 1
            print(f"DEBUG: Yesterday checkin found. Streak incremented to {user.streak_count}")
        else:
            user.streak_count = 1
            print(f"DEBUG: Gap in checkin. Streak reset to 1")
        user.last_checkin_date = today
    else:
        print(f"DEBUG: Already checked in today (IST). Streak remains {user.streak_count}")
    
    db.session.commit()
    
    return jsonify({
        "msg": "Mood saved successfully",
        "analysis": analysis_report,
        "streak": user.streak_count
    }), 201

@api_bp.route('/user/streak', methods=['GET'])
@jwt_required()
def get_user_streak():
    user_id = get_jwt_identity()
    user = User.query.get(user_id)
    
    # Check if streak should be reset (if they missed yesterday)
    # Use IST date so streak resets correctly for users in UTC+5:30
    today = (datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)).date()
    print(f"DEBUG: get_user_streak for {user_id}. last_checkin: {user.last_checkin_date}, today (IST): {today}")
    if user.last_checkin_date and user.last_checkin_date < today - datetime.timedelta(days=1):
        print(f"DEBUG: Streak reset for {user_id}. last_checkin was {user.last_checkin_date}")
        user.streak_count = 0
        db.session.commit()
        
    return jsonify({
        "streak": user.streak_count,
        "last_checkin": user.last_checkin_date.isoformat() if user.last_checkin_date else None
    })

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
        "intensity": c.intensity,
        "sleep": c.sleep_quality,
        "social": c.social_interaction,
        "energy": c.energy_level,
        "wellness_score": c.wellness_score, # Include the new score
        "analysis": c.analysis_report,
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

    # Calculate start and end of the day for the query (Convert IST bounds to UTC)
    start_of_day_utc = dt.combine(date_obj, datetime.time.min) - timedelta(hours=5, minutes=30)
    end_of_day_utc = dt.combine(date_obj, datetime.time.max) - timedelta(hours=5, minutes=30)
    
    # Fetch existing appointments (booked or pending)
    booked_appointments = Appointment.query.filter(
        Appointment.counselor_id == counselor_profile.user_id, 
        Appointment.appointment_time.between(start_of_day_utc, end_of_day_utc),
        Appointment.status.in_(['booked', 'pending'])
    ).all()

    # Set of booked times strings (Convert UTC back to IST for local matching)
    booked_times = {(appt.appointment_time + timedelta(hours=5, minutes=30)).strftime("%H:%M") for appt in booked_appointments}

    # Custom Availability Logic
    avail = counselor_profile.availability or {}
    allowed_days = avail.get("days", ["Mon", "Tue", "Wed", "Thu", "Fri"])
    time_range = avail.get("timeRange", "09:00-18:00")
    
    current_day_str = date_obj.strftime("%a") # "Mon", "Tue", etc.
    
    slots = []
    if current_day_str in allowed_days:
        try:
            start_str, end_str = time_range.split("-")
            current_time = dt.strptime(start_str, "%H:%M")
            end_time_limit = dt.strptime(end_str, "%H:%M")
        except:
            # Fallback
            current_time = dt.strptime("09:00", "%H:%M")
            end_time_limit = dt.strptime("18:00", "%H:%M")

        while current_time <= end_time_limit:
            time_str = current_time.strftime("%H:%M")
            slots.append({
                "time": time_str,
                "available": time_str not in booked_times
            })
            current_time += timedelta(minutes=15)

    return jsonify({
        "counselor_id": counselor_profile.user_id,
        "name": counselor_profile.user.username,
        "specialization": counselor_profile.specialization,
        "available_slots": slots,
        "meeting_location": counselor_profile.meeting_location
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
                'date': (appt.appointment_time + timedelta(hours=5, minutes=30)).isoformat(),
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
                'date': (appt.appointment_time + timedelta(hours=5, minutes=30)).isoformat(),
                'mode': appt.mode,
                'status': appt.status,
                'meeting_link': appt.meeting_link,
            })
        
        if appointments_query:
            db.session.commit()

        # Filter clients to only those who have approved/booked appointments or specific messaging permission
        permitted_student_ids = db.session.query(Appointment.student_id)\
            .filter(
                Appointment.counselor_id == counsellor_id,
                (Appointment.status == 'booked') | (Appointment.allow_messaging == True)
            ).distinct().all()
        
        permitted_student_ids = [s[0] for s in permitted_student_ids]
        client_users = User.query.filter(User.id.in_(permitted_student_ids)).all()

        clients_data = [{'name': user.username, 'status': 'Active', 'id': user.id} for user in client_users]

        # Calculate Average Session Duration
        completed_sessions = Appointment.query.filter(
            Appointment.counselor_id == counsellor_id,
            Appointment.status == 'completed',
            Appointment.session_started_at.isnot(None),
            Appointment.session_ended_at.isnot(None)
        ).all()

        total_duration = 0
        count = 0
        for s in completed_sessions:
            duration = (s.session_ended_at - s.session_started_at).total_seconds() / 60.0
            if duration > 0:
                total_duration += duration
                count += 1
        
        avg_duration = round(total_duration / count, 1) if count > 0 else "N/A"

        dashboard_data = {
            'counsellorName': counsellor.username,
            'appointments': appointments_data,
            'clients': clients_data,
            'stats': {
                'totalSessions': len(completed_sessions),
                'averageSessionDuration': avg_duration
            }
        }
        
        return jsonify(dashboard_data), 200

    except Exception as e:
        print(f"Error fetching counsellor dashboard data: {e}")
        return jsonify({"msg": "An error occurred while fetching dashboard data"}), 500

@api_bp.route('/pending-feedbacks', methods=['GET'])
@jwt_required()
def get_pending_feedbacks():
    user_id = get_jwt_identity()
    user = User.query.get(user_id)
    
    if not user:
        return jsonify(msg="User not found"), 404
    
    pending = []
    if user.role == UserRole.STUDENT:
        # Sessions completed > 15 mins ago (approx) where student feedback is empty
        appointments = Appointment.query.filter(
            Appointment.student_id == user_id,
            Appointment.status == 'completed',
            Appointment.student_feedback.is_(None)
        ).all()
        for a in appointments:
            pending.append({
                "id": a.id,
                "role": "student",
                "counselor_name": a.counselor.username,
                "time": a.appointment_time.isoformat()
            })
    elif user.role == UserRole.COUNSELOR:
        appointments = Appointment.query.filter(
            Appointment.counselor_id == user_id,
            Appointment.status == 'completed',
            Appointment.counselor_feedback.is_(None)
        ).all()
        for a in appointments:
            pending.append({
                "id": a.id,
                "role": "counselor",
                "student_name": a.student.username,
                "time": a.appointment_time.isoformat()
            })
            
    return jsonify(pending), 200

@api_bp.route('/appointments/<int:appointment_id>/start-session', methods=['PUT'])
@jwt_required()
def start_session(appointment_id):
    appt = Appointment.query.get_or_404(appointment_id)
    if not appt.session_started_at:
        appt.session_started_at = datetime.datetime.utcnow()
        db.session.commit()
    return jsonify(msg="Session started"), 200

@api_bp.route('/appointments/<int:appointment_id>/end-session', methods=['PUT'])
@jwt_required()
def end_session(appointment_id):
    appt = Appointment.query.get_or_404(appointment_id)
    appt.session_ended_at = datetime.datetime.utcnow()
    appt.status = 'completed' # Set to completed when session ends properly
    db.session.commit()
    return jsonify(msg="Session ended"), 200

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
    now = dt.utcnow()
    appointment_time = appointment.appointment_time
    
    # If appointment_time is string (some DB adapters), parse it. 
    # SQLAlchemy usually returns datetime object.
    
    # If the session is more than 45 minutes past its scheduled time, it's expired.
    # (30 mins duration + 15 mins buffer)
    if now > appointment_time + timedelta(minutes=45):
        if appointment.status == 'pending':
            appointment.status = 'rejected'
            db.session.commit()
        return jsonify({"allowed": False, "error": "This session has already expired."}), 403

    # Allow joining 10 mins before
    start_window = appointment_time - timedelta(minutes=10)
    # Allow up to 45 mins after start
    end_window = appointment_time + timedelta(minutes=45)
    
    if now < start_window:
        wait_time = (start_window - now).total_seconds() / 60
        return jsonify({"allowed": False, "error": f"Session starts in {int(wait_time)} minutes."}), 403
        
    if now > end_window:
         return jsonify({"allowed": False, "error": "Session has expired."}), 403
         
    return jsonify({
        "allowed": True, 
        "mode": appointment.mode,
        "appointment_id": appointment.id,
        "startTime": appointment_time.isoformat() + 'Z',
        "user": {
            "id": user.id,
            "name": user.username,
            "role": user.role.value
        }
    })

@api_bp.route('/appointments/<int:appt_id>/messaging-permission', methods=['PUT'])
@jwt_required()
def set_messaging_permission(appt_id):
    user_id = get_jwt_identity()
    appointment = Appointment.query.get(appt_id)
    
    if not appointment or appointment.student_id != int(user_id):
        return jsonify({"msg": "Unauthorized"}), 403
        
    data = request.json
    appointment.allow_messaging = data.get('allow_messaging', True)
    db.session.commit()
    return jsonify({"msg": "Permission updated"}), 200

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
    
    # Also include counselors if they have an appointment with allow_messaging=True
    permitted_appointments = Appointment.query.filter(
        (Appointment.student_id == current_user_id) & (Appointment.allow_messaging == True)
    ).all()
    for appt in permitted_appointments:
        contact_ids.add(appt.counselor_id)
    
    # Also include students for the counselor side
    permitted_as_counselor = Appointment.query.filter(
        (Appointment.counselor_id == current_user_id) & (Appointment.allow_messaging == True)
    ).all()
    for appt in permitted_as_counselor:
        contact_ids.add(appt.student_id)
    
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
                "unread_count": unread_count
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
            "is_read": m.is_read,
            "is_sender": m.sender_id == current_user_id
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
        language=data.get('language', 'English'),
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
        "availability": profile.availability,
        "meeting_location": profile.meeting_location
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
    if 'meeting_location' in data:
        profile.meeting_location = data['meeting_location']
        
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
        "appointments": [{"id": a.id, "date": a.appointment_time.isoformat(), "status": a.status, "mode": a.mode, "notes": getattr(a, 'notes', '')} for a in appointments]
    }), 200


# --- Video & Network Traversal ---
import os
import requests

from flask_cors import cross_origin

@api_bp.route('/session/turn-credentials', methods=['GET'])
@cross_origin()
@jwt_required()
def get_turn_credentials():
    """Generates short-lived TURN credentials using Metered API."""
    metered_domain = os.environ.get('METERED_DOMAIN')
    metered_secret = os.environ.get('METERED_SECRET_KEY')
    
    if not metered_domain or not metered_secret:
        return jsonify({
            "iceServers": [
                { "urls": "stun:stun.l.google.com:19302" },
                { "urls": "stun:global.stun.twilio.com:3478" }
            ]
        })

    try:
        response = requests.get(f"https://{metered_domain}/api/v1/turn/credentials?apiKey={metered_secret}")
        data = response.json()
        
        # Metered returns an array directly, or sometimes an object
        if isinstance(data, list):
            return jsonify({ "iceServers": data })
        elif "iceServers" in data:
            return jsonify({ "iceServers": data["iceServers"] })
        else:
            return jsonify({ "error": "Unknown format" }), 500
    except Exception as e:
        print(f"Metered TURN Error: {e}", flush=True)
        return jsonify({"error": str(e)}), 500

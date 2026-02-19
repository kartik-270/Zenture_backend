import eventlet
eventlet.monkey_patch()
print("--- Application Starting ---", flush=True)

from flask import Flask, send_from_directory
from flask_cors import CORS

from config import Config
from extensions import db, bcrypt, migrate, jwt, mail
from routes import api_bp

from flask_socketio import SocketIO, emit, join_room, leave_room
import os

socketio = SocketIO()

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)
    bcrypt.init_app(app)
    migrate.init_app(app, db)
    jwt.init_app(app)
    mail.init_app(app)
    CORS(app)  

    socketio.init_app(app, cors_allowed_origins="*", async_mode='eventlet')

    app.register_blueprint(api_bp, url_prefix='/api')

    @app.route('/')
    def index():
        return "Welcome to the Mental Health Platform API!"
        
    # Ensure tables exist
    

    return app

try:
    app = create_app()
    print("APP CREATED SUCCESSFULLY", flush=True)
except Exception as e:
    print("APP FAILED TO START:", e, flush=True)
    raise


# --- WebRTC Signaling Events ---

@socketio.on('join-room')
def handle_join_room(data):
    room = data.get('roomId')
    user_id = data.get('userId')
    join_room(room)
    print(f"User {user_id} joined room {room}")
    # Notify others in the room
    emit('user-connected', user_id, room=room, include_self=False)

@socketio.on('offer')
def handle_offer(data):
    # Forward offer to the specific room (broadcasting to others)
    emit('offer', data, room=data.get('roomId'), include_self=False)

@socketio.on('answer')
def handle_answer(data):
    emit('answer', data, room=data.get('roomId'), include_self=False)

@socketio.on('ice-candidate')
def handle_ice_candidate(data):
    emit('ice-candidate', data, room=data.get('roomId'), include_self=False)

@socketio.on('toggle-media')
def handle_media_toggle(data):
    # data: { roomId, userId, kind: 'audio'|'video', enabled: bool }
    emit('media-state-changed', data, room=data.get('roomId'), include_self=False)

@socketio.on('chat-message')
def handle_chat_message(data):
    # data: { roomId, userId, message, timestamp }
    emit('receive-message', data, room=data.get('roomId'), include_self=False)


# Upload Configuration
UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB max size

@app.route('/uploads/<path:filename>')
def serve_uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

if __name__ == '__main__':
    socketio.run(app, debug=True, port=int(os.environ.get("PORT", 5000)))

from flask import Blueprint, request, jsonify
from models import db, User, UserRole, Community, CommunityMember, ForumPost, ForumReply, ChatMessage
from flask_jwt_extended import jwt_required, get_jwt_identity
from routes import roles_required
import datetime

community_bp = Blueprint('community', __name__)

# --- Community Management ---

@community_bp.route('/communities', methods=['GET'])
def get_communities():
    communities = Community.query.all()
    user_id = None
    try:
        from flask_jwt_extended import verify_jwt_in_request
        verify_jwt_in_request(optional=True)
        user_id = get_jwt_identity()
    except Exception:
        pass

    result = []
    for c in communities:
        is_member = False
        if user_id:
            member = CommunityMember.query.filter_by(community_id=c.id, user_id=user_id).first()
            if member:
                is_member = True
        
        result.append({
            "id": c.id,
            "name": c.name,
            "description": c.description,
            "member_count": c.members.count(),
            "is_member": is_member
        })
    return jsonify(result), 200

@community_bp.route('/communities', methods=['POST'])
@roles_required('admin', 'moderator')
def create_community():
    data = request.get_json()
    name = data.get('name')
    description = data.get('description', '')
    user_id = get_jwt_identity()

    if not name:
        return jsonify({"msg": "Community name is required"}), 400

    existing = Community.query.filter_by(name=name).first()
    if existing:
        return jsonify({"msg": "Community already exists"}), 409

    new_community = Community(name=name, description=description, created_by_id=user_id)
    db.session.add(new_community)
    db.session.commit()

    # Automatically add creator as member
    member = CommunityMember(community_id=new_community.id, user_id=user_id)
    db.session.add(member)
    db.session.commit()

    return jsonify({"msg": "Community created successfully", "id": new_community.id}), 201

@community_bp.route('/communities/<int:community_id>/join', methods=['POST'])
@jwt_required()
def join_community(community_id):
    user_id = get_jwt_identity()
    
    community = Community.query.get(community_id)
    if not community:
        return jsonify({"msg": "Community not found"}), 404

    existing = CommunityMember.query.filter_by(community_id=community_id, user_id=user_id).first()
    if existing:
        return jsonify({"msg": "Already a member"}), 400

    member = CommunityMember(community_id=community_id, user_id=user_id)
    db.session.add(member)
    db.session.commit()

    return jsonify({"msg": "Joined community successfully"}), 200

# --- Community Posts ---

@community_bp.route('/communities/<int:community_id>/posts', methods=['GET'])
def get_community_posts(community_id):
    community = Community.query.get(community_id)
    if not community:
        return jsonify({"msg": "Community not found"}), 404

    posts = ForumPost.query.filter_by(community_id=community_id).order_by(ForumPost.timestamp.desc()).all()
    
    result = []
    for p in posts:
        result.append({
            "id": p.id,
            "title": p.title,
            "content": p.content,
            "likes_count": p.likes_count,
            "reply_count": p.replies.count(),
            "timestamp": p.timestamp.isoformat(),
            "author": {
                "id": p.author.id,
                "username": p.author.username,
                "role": p.author.role.value
            }
        })
    return jsonify(result), 200

@community_bp.route('/communities/<int:community_id>/posts', methods=['POST'])
@jwt_required()
def create_community_post(community_id):
    user_id = get_jwt_identity()
    user = User.query.get(user_id)
    if getattr(user, 'is_blocked', False):
         return jsonify({"msg": "You are blocked from posting"}), 403

    data = request.get_json()
    title = data.get('title')
    content = data.get('content')

    if not title or not content:
        return jsonify({"msg": "Title and content are required"}), 400

    community = Community.query.get(community_id)
    if not community:
        return jsonify({"msg": "Community not found"}), 404

    new_post = ForumPost(title=title, content=content, author_id=user_id, community_id=community_id)
    db.session.add(new_post)
    db.session.commit()

    return jsonify({"msg": "Post created successfully", "id": new_post.id}), 201

# --- Post Replies ---

@community_bp.route('/posts/<int:post_id>/replies', methods=['GET'])
def get_post_replies(post_id):
    post = ForumPost.query.get(post_id)
    if not post:
        return jsonify({"msg": "Post not found"}), 404

    replies = ForumReply.query.filter_by(post_id=post_id).order_by(ForumReply.timestamp.asc()).all()
    result = []
    for r in replies:
        result.append({
            "id": r.id,
            "content": r.content,
            "timestamp": r.timestamp.isoformat(),
            "author": {
                "id": r.author.id,
                "username": r.author.username,
                "role": r.author.role.value
            }
        })
    return jsonify(result), 200

@community_bp.route('/posts/<int:post_id>/replies', methods=['POST'])
@jwt_required()
def add_post_reply(post_id):
    user_id = get_jwt_identity()
    user = User.query.get(user_id)
    if getattr(user, 'is_blocked', False):
         return jsonify({"msg": "You are blocked from posting"}), 403

    data = request.get_json()
    content = data.get('content')

    if not content:
        return jsonify({"msg": "Content is required"}), 400

    post = ForumPost.query.get(post_id)
    if not post:
        return jsonify({"msg": "Post not found"}), 404

    new_reply = ForumReply(content=content, author_id=user_id, post_id=post_id)
    db.session.add(new_reply)
    db.session.commit()

    return jsonify({"msg": "Reply added successfully"}), 201

# --- Direct Messaging ---

@community_bp.route('/messages/direct/<int:other_user_id>', methods=['GET'])
@jwt_required()
def get_direct_messages(other_user_id):
    user_id = int(get_jwt_identity())
    
    messages = ChatMessage.query.filter(
        db.or_(
            db.and_(ChatMessage.sender_id == user_id, ChatMessage.receiver_id == other_user_id),
            db.and_(ChatMessage.sender_id == other_user_id, ChatMessage.receiver_id == user_id)
        )
    ).order_by(ChatMessage.timestamp.asc()).all()

    result = []
    for m in messages:
        result.append({
            "id": m.id,
            "sender_id": m.sender_id,
            "receiver_id": m.receiver_id,
            "content": m.content,
            "timestamp": m.timestamp.isoformat()
        })
    return jsonify(result), 200

@community_bp.route('/messages/direct/<int:other_user_id>', methods=['POST'])
@jwt_required()
def send_direct_message(other_user_id):
    user_id = int(get_jwt_identity())
    user = User.query.get(user_id)
    if getattr(user, 'is_blocked', False):
         return jsonify({"msg": "You are blocked from messaging"}), 403

    data = request.get_json()
    content = data.get('content')

    if not content:
        return jsonify({"msg": "Content is required"}), 400

    other_user = User.query.get(other_user_id)
    if not other_user:
        return jsonify({"msg": "User not found"}), 404

    new_message = ChatMessage(sender_id=user_id, receiver_id=other_user_id, content=content)
    db.session.add(new_message)
    db.session.commit()

    return jsonify({"msg": "Message sent"}), 201

@community_bp.route('/communities/<int:community_id>', methods=['DELETE'])
@roles_required('admin')
def delete_community(community_id):
    community = Community.query.get(community_id)
    if not community:
        return jsonify({"msg": "Community not found"}), 404

    db.session.delete(community)
    db.session.commit()
    return jsonify({"msg": "Community deleted successfully"}), 200

# --- Moderation ---

@community_bp.route('/posts/<int:post_id>', methods=['DELETE'])
@roles_required('admin', 'moderator')
def delete_post(post_id):
    post = ForumPost.query.get(post_id)
    if not post:
        return jsonify({"msg": "Post not found"}), 404

    db.session.delete(post)
    db.session.commit()
    return jsonify({"msg": "Post deleted"}), 200

@community_bp.route('/users/<int:target_user_id>/block', methods=['POST'])
@roles_required('admin', 'moderator')
def block_user(target_user_id):
    user = User.query.get(target_user_id)
    if not user:
        return jsonify({"msg": "User not found"}), 404
        
    user.is_blocked = True
    db.session.commit()
    return jsonify({"msg": "User has been blocked"}), 200

@community_bp.route('/admin/assign_moderator', methods=['POST'])
@roles_required('admin')
def assign_moderator():
    data = request.get_json()
    username = data.get('username')

    user = User.query.filter_by(username=username).first()
    if not user:
        return jsonify({"msg": "User not found"}), 404
        
    user.role = UserRole.MODERATOR
    db.session.commit()
    return jsonify({"msg": f"{username} is now a moderator"}), 200

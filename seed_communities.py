from app import app, db
from models import Community, User

with app.app_context():
    admin = User.query.filter_by(username='admin').first() or User.query.first()
    if admin:
        communities = [
            {"name": "Exam Stress", "description": "A place to discuss and share tips for managing academic pressure and performance anxiety."},
            {"name": "Transition Phase", "description": "Support for students adjusting to new environments, college life, or entering the workforce."},
            {"name": "Anxiety Support", "description": "General discussions and peer-support strategies for dealing with daily anxiety."},
            {"name": "General Wellbeing", "description": "Share your thoughts, experiences, and positivity here!"}
        ]
        
        for c_data in communities:
            if not Community.query.filter_by(name=c_data["name"]).first():
                c = Community(name=c_data["name"], description=c_data["description"], created_by_id=admin.id)
                db.session.add(c)
                print(f"Added community: {c.name}")
        
        db.session.commit()
    else:
        print("No users found to set as creator. Please register a user first.")

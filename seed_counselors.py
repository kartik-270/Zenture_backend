from app import app, db
from models import User, CounselorProfile, UserRole
import random

def create_mock_counselors():
    with app.app_context():
        print("Creating mock counselors...")

        # list of mock counselors
        mock_data = [
            {"username": "Dr. Sarah Johnson", "specialization": "Anxiety & Depression", "password": "password123"},
            {"username": "Dr. Michael Chen", "specialization": "Academic Stress", "password": "password123"},
            {"username": "Emily Davis, LMFT", "specialization": "Relationships & Family", "password": "password123"},
            {"username": "Dr. Robert Wilson", "specialization": "Trauma & PTSD", "password": "password123"},
            {"username": "Jessica Brown", "specialization": "Mindfulness & CBT", "password": "password123"}
        ]

        for data in mock_data:
            # Check if user already exists
            if User.query.filter_by(username=data["username"]).first():
                print(f"User {data['username']} already exists. Skipping.")
                continue

            # Create User
            new_user = User(
                username=data["username"],
                role=UserRole.COUNSELOR
            )
            new_user.set_password(data["password"])
            db.session.add(new_user)
            db.session.flush() # Flush to get the new_user.id before commit

            # Create Counselor Profile
            new_profile = CounselorProfile(
                user_id=new_user.id,
                specialization=data["specialization"],
                availability={"days": ["Mon", "Tue", "Wed", "Thu", "Fri"], "hours": "9am-5pm"} # Dummy availability
            )
            db.session.add(new_profile)
            
            print(f"Created counselor: {data['username']} - {data['specialization']}")

        try:
            db.session.commit()
            print("Successfully committed changes to database.")
        except Exception as e:
            db.session.rollback()
            print(f"Error creating counselors: {e}")

if __name__ == "__main__":
    create_mock_counselors()

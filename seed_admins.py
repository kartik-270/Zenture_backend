import eventlet
eventlet.monkey_patch()
from app import app
from extensions import db, bcrypt
from models import User, UserRole

def seed_admins():
    with app.app_context():
        admins = [
            {"username": "admin1", "password": "admin1password", "email": "admin1@zenture.com"},
            {"username": "admin2", "password": "admin2password", "email": "admin2@zenture.com"}
        ]

        print("Seeding admins...")
        for admin_data in admins:
            user = User.query.filter_by(username=admin_data["username"]).first()
            if not user:
                new_admin = User(
                    username=admin_data["username"],
                    role=UserRole.ADMIN,
                    email_hash=bcrypt.generate_password_hash(admin_data["email"]).decode('utf-8')
                )
                new_admin.set_password(admin_data["password"])
                db.session.add(new_admin)
                print(f"Created admin: {admin_data['username']}")
            else:
                print(f"Admin already exists: {admin_data['username']}")
        
        db.session.commit()
        print("Admin seeding completed.")

if __name__ == "__main__":
    seed_admins()

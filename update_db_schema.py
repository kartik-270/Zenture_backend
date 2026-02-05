from app import app, db
from sqlalchemy import text

with app.app_context():
    print("Creating new tables...")
    db.create_all()
    
    # Manually add columns to Resource if they don't exist (SQLite workaround)
    with db.engine.connect() as conn:
        # Check Resource columns
        try:
            conn.execute(text("ALTER TABLE resource ADD COLUMN content TEXT"))
            print("Added content column")
        except Exception: pass

        try:
            conn.execute(text("ALTER TABLE resource ADD COLUMN status VARCHAR(20) DEFAULT 'pending'"))
            print("Added status column")
        except Exception: pass

        try:
            conn.execute(text("ALTER TABLE resource ADD COLUMN author_id INTEGER REFERENCES user(id)"))
            print("Added author_id column")
        except Exception: pass
            
        try:
            conn.execute(text("ALTER TABLE resource ADD COLUMN created_at DATETIME"))
            print("Added created_at column")
        except Exception: pass

    print("Database updated successfully.")

from app import app, db
from sqlalchemy import text

with app.app_context():
    print("Creating new tables...")
    db.create_all()
    
    # Manually add columns to models if they don't exist
    with db.engine.connect() as conn:
        # ChatHistory updates
        columns_to_add = [
            ("chathistory", "sender", "VARCHAR(10)"),
            ("chathistory", "message", "TEXT"),
            ("chathistory", "intent", "VARCHAR(100)"),
            ("chathistory", "emotion", "VARCHAR(50)"),
            ("chathistory", "sentiment_score", "FLOAT"),
            ("chathistory", "is_crisis", "BOOLEAN DEFAULT 0")
        ]
        
        for table, col, col_type in columns_to_add:
            try:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))
                print(f"Added {col} column to {table}")
            except Exception: pass

        # Resource updates
        resource_cols = [
            ("resource", "content", "TEXT"),
            ("resource", "status", "VARCHAR(20) DEFAULT 'pending'"),
            ("resource", "author_id", "INTEGER REFERENCES user(id)"),
            ("resource", "created_at", "DATETIME")
        ]
        for table, col, col_type in resource_cols:
            try:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))
                print(f"Added {col} column to {table}")
            except Exception: pass

    # Ensure ChatSession table exists
    db.create_all()
    print("Database updated successfully.")

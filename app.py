from flask import Flask
from flask_cors import CORS

from config import Config
# Corrected: Import all extension instances from the central extensions.py file
from extensions import db, bcrypt, migrate, jwt, mail
# Import the API blueprint
from routes import api_bp

def create_app():
    """
    Creates and configures an instance of the Flask application.
    This is the application factory.
    """
    app = Flask(__name__)
    app.config.from_object(Config)

    # Corrected: Initialize all extensions with the app instance
    db.init_app(app)
    bcrypt.init_app(app)
    migrate.init_app(app, db)
    jwt.init_app(app)
    mail.init_app(app)
    CORS(app)  


    # Register the API blueprint with the application
    app.register_blueprint(api_bp, url_prefix='/api')

    # A simple route to test if the server is running
    @app.route('/')
    def index():
        return "Welcome to the Mental Health Platform API!"

    return app

# Create the app instance using the factory.
# This allows 'flask run' and 'flask db' commands to work.
app = create_app()

if __name__ == '__main__':
    # This block runs the app with the development server
    # when you execute 'python app.py' directly.
    app.run(host='0.0.0.0',debug=True,port=5000)


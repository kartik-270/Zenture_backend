from flask import Flask
from flask_cors import CORS

from config import Config
from extensions import db, bcrypt, migrate, jwt, mail
from routes import api_bp

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)
    bcrypt.init_app(app)
    migrate.init_app(app, db)
    jwt.init_app(app)
    mail.init_app(app)
    CORS(app)  


    app.register_blueprint(api_bp, url_prefix='/api')

    @app.route('/')
    def index():
        return "Welcome to the Mental Health Platform API!"

    return app

app = create_app()

if __name__ == '__main__':
    app.run(host='0.0.0.0',debug=True,port=5000)


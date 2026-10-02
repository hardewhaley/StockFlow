from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager

app = Flask(__name__)

app.config['SECRET_KEY'] = 'stockflow-secret-key'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///stockflow.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db= SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access this page'
login_manager.login_message_category = 'info'

from app.models import User

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

from app import routes
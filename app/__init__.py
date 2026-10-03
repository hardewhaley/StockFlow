import os
from flask import Flask, request, redirect, url_for, flash
from dotenv import load_dotenv
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_mail import Mail

load_dotenv(
    os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        '.env'
    )
)

app = Flask(__name__)

app.config['SECRET_KEY'] = os.getenv('SECRET_KEY')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///stockflow.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.getenv('MAIL_USERNAME')

db= SQLAlchemy(app)
login_manager = LoginManager(app)


@login_manager.unauthorized_handler
def unauthorized():
    flash('Please log in to access this page', 'info')

    if request.path.startswith('/customer'):
        return redirect(url_for('customer_login'))
    
    if request.path.startswith('/supplier'):
        return redirect(url_for('supplier_login'))

    if request.path.startswith('/staff'):
        return redirect(url_for('staff_login'))
    
    return redirect(url_for('login'))


mail = Mail(app)

from app.models import User, Customer, Supplier

@login_manager.user_loader
def load_user(user_id):
    kind, _, raw_id = user_id.partition(':')
    try:
        if kind == 'customer':
            return db.session.get(Customer, int(raw_id))
        if kind == 'supplier':
            return db.session.get(Supplier, int(raw_id))
        if kind == 'user':
            return db.session.get(User, int(raw_id))
        return db.session.get(User, int(user_id))
    except ValueError:
        return None

from app import routes


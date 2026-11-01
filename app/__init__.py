
import os
import requests

from flask import Flask, request, redirect, url_for, flash
from dotenv import load_dotenv
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFProtect, generate_csrf


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

# Brevo configuration
app.config['BREVO_API_KEY'] = os.getenv('BREVO_API_KEY')
app.config['BREVO_SENDER_EMAIL'] = os.getenv('BREVO_SENDER_EMAIL')
app.config['BREVO_SENDER_NAME'] = 'StockFlow'

app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024


db = SQLAlchemy(app)
migrate = Migrate(app, db, render_as_batch=True)
login_manager = LoginManager(app)

csrf = CSRFProtect(app)
app.jinja_env.globals['csrf_token'] = generate_csrf


def send_email(subject, recipients, text_content, reply_to=None):
    """
    Send transactional emails through Brevo API.
    """

    api_key = app.config['BREVO_API_KEY']
    sender_email = app.config['BREVO_SENDER_EMAIL']

    if not api_key or not sender_email:
        raise RuntimeError(
            "Brevo API key or sender email is not configured."
        )

    payload = {
        'sender': {
            'name': app.config['BREVO_SENDER_NAME'],
            'email': sender_email
        },
        'to': [
            {'email': email}
            for email in recipients
        ],
        'subject': subject,
        'textContent': text_content
    }

    if reply_to:
        payload['replyTo'] = {
            'email': reply_to
        }

    headers = {
        'accept': 'application/json',
        'api-key': api_key,
        'content-type': 'application/json'
    }

    response = requests.post(
        'https://api.brevo.com/v3/smtp/email',
        headers=headers,
        json=payload,
        timeout=20
    )

    response.raise_for_status()

    return response.json()


@login_manager.unauthorized_handler
def unauthorized():
    flash('Please log in to access this page', 'info')

    if request.path.startswith('/customer'):
        return redirect(url_for('customer_login'))

    if request.path.startswith('/staff'):
        return redirect(url_for('staff_login'))

    return redirect(url_for('login'))


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

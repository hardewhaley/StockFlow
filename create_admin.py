from getpass import getpass
from werkzeug.security import generate_password_hash

from app import app, db
from app.models import User

with app.app_context():
    full_name = input("Enter admin full name: ").strip()
    email = input("Enter admin email: ").strip().lower()
    password = getpass("Enter admin password: ")

    existing_user = User.query.filter_by(email=email).first()

    if existing_user:
        print("A user with this email already exists.")

    else:
        admin = User(
            full_name = full_name,
            email = email,
            password = generate_password_hash(password),
            role = 'admin'
        )
        db.session.add(admin)
        db.session.commit()
        print("Admin account created successfully!")
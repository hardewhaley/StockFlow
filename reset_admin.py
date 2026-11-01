
import os

from werkzeug.security import generate_password_hash

from app import app, db
from app.models import User


with app.app_context():
    full_name = os.getenv('ADMIN_NAME')
    email = os.getenv('ADMIN_EMAIL')
    password = os.getenv('ADMIN_PASSWORD')

    if not all([full_name, email, password]):
        raise RuntimeError("Admin environment variables are missing.")

    email = email.strip().lower()

    if len(password) < 8:
        raise ValueError("Admin password must be at least 8 characters.")

    admin = User.query.filter_by(email=email).first()

    if admin:
        admin.full_name = full_name
        admin.password = generate_password_hash(password)
        admin.role = 'admin'
        print("Existing admin account updated successfully!")

    else:
        admin = User(
            full_name=full_name,
            email=email,
            password=generate_password_hash(password),
            role='admin'
        )

        db.session.add(admin)
        print("New admin account created successfully!")

    db.session.commit()

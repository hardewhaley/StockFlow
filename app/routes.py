import secrets
import time

from flask import render_template, redirect, flash, url_for, session
from flask_login import login_user, logout_user, login_required, current_user
from flask_mail import Message
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from app import app, db, mail
from app.forms import LoginForm, StaffForm, CustomerRegistrationForm, CustomerOTPForm, SupplierRegistrationForm, SupplierOTPForm
from app.models import User, Customer, Product, Supplier

OTP_LIFETIME_SECONDS = 600
OTP_MAX_ATTEMPTS = 5


def dashboard_for(user):
    if user.role == 'admin':
        return url_for('admin_dashboard')
    
    if user.role == 'staff':
        return url_for('staff_dashboard')
    
    if user.role == 'supplier':
        return url_for('supplier_dashboard')
    
    return url_for('home')

# customer
@app.route('/')
def home():
    products = Product.query.all()
    return render_template('customer/dashboard.html', products=products)


@app.route('/customer/dashboard')
def customer_dashboard():
    products = Product.query.all()
    return render_template('customer/dashboard.html', products=products)


@app.route('/customer/register', methods=['GET', 'POST'])
def customer_register():
    if current_user.is_authenticated:
        return redirect(dashboard_for(current_user))

    form = CustomerRegistrationForm()
    if form.validate_on_submit():
        full_name = form.full_name.data.strip()
        email = form.email.data.strip().lower()
        phone = form.phone.data.strip()

        if Customer.query.filter_by(email=email).first():
            flash("An account with this email already exists.", "error")
            return redirect(url_for('customer_register'))

        if Customer.query.filter_by(phone=phone).first():
            flash("An account with this phone number already exists.", "error")
            return redirect(url_for('customer_register'))

        otp = str(secrets.randbelow(900000) + 100000)

        message = Message(
            subject='StockFlow - Verify Your Email',
            sender=app.config['MAIL_USERNAME'],
            recipients=[email]
        )
        message.body = (
            f"Hello {full_name},\n\n"
            "Welcome to StockFlow!\n"
            f"Your email verification OTP is: {otp}\n"
            "This OTP will expire in 10 minutes.\n\n"
            "If you did not create a StockFlow account, you can ignore this email.\n\n"
            "StockFlow"
        )

        try:
            mail.send(message)
        except Exception:
            app.logger.exception("OTP email failed")
            flash("We couldn't send the verification email. Please try again.", "error")
            return redirect(url_for('customer_register'))

        session['customer_registration'] = {
            'full_name': full_name,
            'email': email,
            'phone': phone,
            'password': generate_password_hash(form.password.data),
            'otp_hash': generate_password_hash(otp),
            'expires': time.time() + OTP_LIFETIME_SECONDS,
            'attempts': 0,
        }

        flash("A verification code has been sent to your email.", "success")
        return redirect(url_for('verify_customer_otp'))

    return render_template('customer/register.html', form=form)


@app.route('/customer/verify_otp', methods=['GET', 'POST'])
def verify_customer_otp():
    registration_data = session.get('customer_registration')

    if not registration_data:
        flash("Your registration session has expired. Please register again.", "error")
        return redirect(url_for('customer_register'))

    if time.time() > registration_data['expires']:
        session.pop('customer_registration', None)
        flash("Your code has expired. Please register again.", "error")
        return redirect(url_for('customer_register'))

    form = CustomerOTPForm()
    if form.validate_on_submit():
        attempts = registration_data.get('attempts', 0)
        if attempts >= OTP_MAX_ATTEMPTS:
            session.pop('customer_registration', None)
            flash("Too many incorrect attempts. Please register again.", "error")
            return redirect(url_for('customer_register'))

        entered_otp = form.otp.data.strip()

        if check_password_hash(registration_data['otp_hash'], entered_otp):
            customer = Customer(
                full_name=registration_data['full_name'],
                email=registration_data['email'],
                phone=registration_data['phone'],
                password=registration_data['password']
            )
            try:
                db.session.add(customer)
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                session.pop('customer_registration', None)
                flash("An account with this email or phone number already exists.", "error")
                return redirect(url_for('customer_register'))

            session.pop('customer_registration', None)
            flash("Your account has been created successfully. Please log in.", "success")
            return redirect(url_for('customer_login'))

        registration_data['attempts'] = attempts + 1
        session['customer_registration'] = registration_data
        flash("Invalid verification code. Please try again.", "error")

    return render_template('customer/verify_otp.html', form=form)


@app.route('/customer/login', methods=['GET', 'POST'])
def customer_login():
    if current_user.is_authenticated:
        return redirect(dashboard_for(current_user))

    form = LoginForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        password = form.password.data

        customer = Customer.query.filter_by(email=email).first()
        if customer and check_password_hash(customer.password, password):
            login_user(customer)
            flash("Welcome to StockFlow!", "success")
            return redirect(url_for('home'))
        flash("Invalid email or password.", "error")

    return render_template('customer/login.html', form=form)


@app.route('/customer/logout')
@login_required
def customer_logout():
    logout_user()
    flash("You have been logged out successfully.", "success")
    return redirect(url_for('customer_login'))

# admin login
@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(dashboard_for(current_user))

    form = LoginForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        password = form.password.data

        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password, password):
            if user.role != 'admin':
                flash("You are not authorized to access the admin area", "error")
                return redirect(url_for('login'))

            login_user(user)
            flash("Welcome to StockFlow, Admin!", "success")
            return redirect(url_for('admin_dashboard'))
        flash("Invalid email or password.", "error")

    return render_template('login.html', form=form)

# staff login
@app.route('/staff/login', methods=['GET', 'POST'])
def staff_login():
    if current_user.is_authenticated:
        return redirect(dashboard_for(current_user))

    form = LoginForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        password = form.password.data

        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password, password):
            if user.role != 'staff':
                flash("This login is only for staff accounts.", "error")
                return redirect(url_for('staff_login'))

            login_user(user)
            flash("Welcome to StockFlow!", "success")
            return redirect(url_for('staff_dashboard'))
        flash("Invalid email or password.", "error")

    return render_template('staff_login.html', form=form)

# staff dashboard
@app.route('/staff/dashboard')
@login_required
def staff_dashboard():
    if current_user.role != 'staff':
        flash("You are not authorized to access the staff dashboard.", "error")
        return redirect(dashboard_for(current_user))
    return render_template('staff/staff_dashboard.html')

# admin dashboard
@app.route('/admin_dashboard')
@login_required
def admin_dashboard():
    if current_user.role != 'admin':
        flash("You are not authorized to access the admin area", "error")
        return redirect(dashboard_for(current_user))
    return render_template('admin/dashboard.html')


@app.route('/admin/staff/create', methods=['GET', 'POST'])
@login_required
def create_staff():
    if current_user.role != 'admin':
        flash("You are not authorized to access this page.", "error")
        return redirect(dashboard_for(current_user))

    form = StaffForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()

        if User.query.filter_by(email=email).first():
            flash("A user with this email already exists.", "error")
            return redirect(url_for('create_staff'))

        staff = User(
            full_name=form.full_name.data.strip(),
            email=email,
            password=generate_password_hash(form.password.data),
            role='staff'
        )
        try:
            db.session.add(staff)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("A user with this email already exists.", "error")
            return redirect(url_for('create_staff'))

        flash("Staff account has been created successfully.", "success")
        return redirect(url_for('create_staff'))

    return render_template('admin/create_staff.html', form=form)

# supplier
@app.route('/supplier/register', methods=['GET', 'POST'])
def supplier_register():
    if current_user.is_authenticated:
        return redirect(dashboard_for(current_user))

    form = SupplierRegistrationForm()
    if form.validate_on_submit():
        full_name = form.full_name.data.strip()
        email = form.email.data.strip().lower()
        phone = form.phone.data.strip()
        address = form.address.data.strip()

        if Supplier.query.filter_by(email=email).first():
            flash("A supplier with this email already exists.", "error")
            return redirect(url_for('supplier_register'))

        if Supplier.query.filter_by(phone=phone).first():
            flash("A supplier with this phone number already exists.", "error")
            return redirect(url_for('supplier_register'))

        otp = str(secrets.randbelow(900000) + 100000)

        message = Message(
            subject='StockFlow - Verify Your Supplier Account',
            sender=app.config['MAIL_USERNAME'],
            recipients=[email]
        )

        message.body = (
            f"Hello {full_name},\n\n"
            "Welcome to StockFlow!\n"
            "You are registering as a supplier.\n\n"
            f"Your email verification OTP is: {otp}\n"
            "This OTP will expire in 10 minutes.\n\n"
            "If you did not create this account, you can ignore this email.\n\n"
            "StockFlow"
        )

        try:
            mail.send(message)

        except Exception:
            app.logger.exception("Supplier OTP email failed")
            flash("We couldn't send the verification email. Please try again.", "error")
            return redirect(url_for('supplier_register'))

        session['supplier_registration'] = {
            'full_name': full_name,
            'email': email,
            'phone': phone,
            'address': address,
            'password': generate_password_hash(form.password.data),
            'otp_hash': generate_password_hash(otp),
            'expires': time.time() + OTP_LIFETIME_SECONDS,
            'attempts': 0
        }

        flash("A verification code has been sent to your email.","success")
        return redirect(url_for('verify_supplier_otp'))
    return render_template('supplier/register.html', form=form)

@app.route('/supplier/verify_otp', methods=['GET', 'POST'])
def verify_supplier_otp():
    registration_data = session.get('supplier_registration')

    if not registration_data:
        flash(
            "Your registration session has expired. Please register again.",
            "error"
        )
        return redirect(url_for('supplier_register'))

    if time.time() > registration_data['expires']:
        session.pop('supplier_registration', None)

        flash("Your verification code has expired. Please register again.", "error")
        return redirect(url_for('supplier_register'))

    form = SupplierOTPForm()

    if form.validate_on_submit():
        attempts = registration_data.get('attempts', 0)

        if attempts >= OTP_MAX_ATTEMPTS:
            session.pop('supplier_registration', None)

            flash("Too many incorrect attempts. Please register again.","error")
            return redirect(url_for('supplier_register'))

        entered_otp = form.otp.data.strip()

        if check_password_hash(registration_data['otp_hash'], entered_otp):

            supplier = Supplier(
                full_name=registration_data['full_name'],
                email=registration_data['email'],
                phone=registration_data['phone'],
                address=registration_data['address'],
                password=registration_data['password']
            )

            try:
                db.session.add(supplier)
                db.session.commit()

            except IntegrityError:
                db.session.rollback()
                session.pop('supplier_registration', None)

                flash("A supplier with this email or phone number already exists.", "error")
                return redirect(url_for('supplier_register'))

            session.pop('supplier_registration', None)

            flash("Your supplier account has been created successfully. Please log in.", "success")
            return redirect(url_for('supplier_login'))

        registration_data['attempts'] = attempts + 1
        session['supplier_registration'] = registration_data

        flash("Invalid verification code. Please try again.", "error")
    return render_template('supplier/verify_otp.html', form=form)

@app.route('/supplier/login', methods=['GET', 'POST'])
def supplier_login():
    if current_user.is_authenticated:
        return redirect(dashboard_for(current_user))

    form = LoginForm()

    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        password = form.password.data

        supplier = Supplier.query.filter_by(email=email).first()

        if supplier and check_password_hash(supplier.password, password):
            login_user(supplier)

            flash("Welcome to StockFlow!", "success")
            return redirect(url_for('supplier_dashboard'))

        flash("Invalid email or password.", "error")

    return render_template('supplier/login.html', form=form)

@app.route('/supplier/dashboard')
@login_required
def supplier_dashboard():
    if current_user.role != 'supplier':
        flash("You are not authorized to access the supplier dashboard","error")
        return redirect(dashboard_for(current_user))
    return render_template('supplier/dashboard.html')

@app.route('/supplier/logout')
@login_required
def supplier_logout():
    if current_user.role != 'supplier':
        flash("You are not authorized to access this page.", "error")
        return redirect(dashboard_for(current_user))

    logout_user()

    flash("You have been logged out successfully.", "success")
    return redirect(url_for('supplier_login'))

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash("You have been logged out successfully.", "success")
    return redirect(url_for('login'))
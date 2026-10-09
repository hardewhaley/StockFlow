import secrets
import string
import time
from decimal import Decimal, InvalidOperation

from flask import render_template, redirect, flash, url_for, session, request
from flask_login import login_user, logout_user, login_required, current_user
from flask_mail import Message
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from app import app, db, mail
from app.forms import LoginForm, StaffForm, CustomerRegistrationForm, CustomerOTPForm, StaffLoginForm
from app.models import User, Customer, Product, Supplier, Category


OTP_LIFETIME_SECONDS = 600
OTP_MAX_ATTEMPTS = 5
MAX_PRICE = Decimal('99999999.99')


def has_role(*roles):
    return getattr(current_user, 'role', None) in roles


def dashboard_for(user):
    role = getattr(user, 'role', None)

    if role == 'admin':
        return url_for('admin_dashboard')

    if role == 'staff':
        return url_for('staff_dashboard')

    return url_for('home')


def parse_price(value, label, errors):
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError):
        errors.append(f'Enter a valid {label}.')
        return None

    if not amount.is_finite():
        errors.append(f'Enter a valid {label}.')
        return None

    if amount < 0:
        errors.append(f'{label.capitalize()} cannot be negative.')
        return None

    if amount > MAX_PRICE:
        errors.append(f'{label.capitalize()} is too large.')
        return None

    return amount.quantize(Decimal('0.01'))


def load_choice(model, raw_id, current_id=None):
    try:
        obj = db.session.get(model, int(raw_id))
    except (ValueError, TypeError):
        return None

    if obj and (obj.is_active or obj.id == current_id):
        return obj

    return None


def generate_staff_code(length=6):
    characters = string.ascii_uppercase + string.digits
    return "STF-" + ''.join(secrets.choice(characters) for _ in range(length))


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

    form = StaffLoginForm()

    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        user = User.query.filter_by(email=email).first()

        if user and user.role == 'staff' and check_password_hash(user.password, form.staff_code.data.strip()):
            login_user(user)
            flash("Welcome to StockFlow!", "success")
            return redirect(url_for('staff_dashboard'))

        flash("Invalid email or staff code.", "error")

    return render_template('staff_login.html', form=form)


# dashboards
@app.route('/staff/dashboard')
@login_required
def staff_dashboard():
    if not has_role('staff'):
        flash("You are not authorized to access the staff dashboard.", "error")
        return redirect(dashboard_for(current_user))
    return render_template('staff/staff_dashboard.html')


@app.route('/admin_dashboard')
@login_required
def admin_dashboard():
    if not has_role('admin'):
        flash("You are not authorized to access the admin area", "error")
        return redirect(dashboard_for(current_user))
    return render_template('admin/dashboard.html')


# staff management
@app.route('/admin/staff/create', methods=['GET', 'POST'])
@login_required
def create_staff():
    if not has_role('admin'):
        flash("You are not authorized to access this page.", "error")
        return redirect(dashboard_for(current_user))

    form = StaffForm()

    if form.validate_on_submit():
        full_name = form.full_name.data.strip()
        email = form.email.data.strip().lower()

        if User.query.filter_by(email=email).first():
            flash("A user with this email already exists.", "error")
            return redirect(url_for('create_staff'))

        staff_code = generate_staff_code()

        staff = User(
            full_name=full_name,
            email=email,
            password=generate_password_hash(staff_code),
            role='staff'
        )

        try:
            db.session.add(staff)
            db.session.flush()

            message = Message(
                subject='Your StockFlow Staff Account',
                sender=app.config['MAIL_USERNAME'],
                recipients=[email]
            )
            message.body = f"""Hello {full_name},

Your StockFlow staff account has been created.

Login Email: {email}
Staff Code: {staff_code}

Please use these details to log in to StockFlow.
Keep your staff code private.

Regards,
StockFlow Team
"""
            mail.send(message)
            db.session.commit()

        except IntegrityError:
            db.session.rollback()
            flash("A user with this email already exists.", "error")
            return redirect(url_for('create_staff'))

        except Exception:
            db.session.rollback()
            app.logger.exception("Staff creation failed")
            flash("Staff account could not be created because the email could not be sent.", "error")
            return redirect(url_for('create_staff'))

        flash("Staff account has been created successfully.", "success")
        return redirect(url_for('create_staff'))

    return render_template('admin/create_staff.html', form=form)


# categories
@app.route('/categories')
@login_required
def categories():
    if not has_role('admin', 'staff'):
        flash('You are not authorized to access this page.', 'error')
        return redirect(dashboard_for(current_user))

    category_list = Category.query.order_by(Category.name.asc()).all()
    return render_template('categories/index.html', categories=category_list)


@app.route('/categories/add', methods=['GET', 'POST'])
@login_required
def add_category():
    if not has_role('admin', 'staff'):
        flash('You are not authorized to access this page.', 'error')
        return redirect(dashboard_for(current_user))

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()

        if not name:
            flash("Category name is required.", "error")
            return render_template('categories/add.html')

        existing_category = Category.query.filter(
            func.lower(Category.name) == name.lower()
        ).first()

        if existing_category:
            flash("A category with this name already exists.", "error")
            return render_template('categories/add.html')

        category = Category(name=name, description=description or None)
        db.session.add(category)
        db.session.commit()

        flash("Category added successfully!", "success")
        return redirect(url_for('categories'))

    return render_template('categories/add.html')


@app.route('/categories/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit_category(id):
    if not has_role('admin', 'staff'):
        flash("You are not authorized to access this page.", "error")
        return redirect(dashboard_for(current_user))

    category = db.get_or_404(Category, id)

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()

        if not name:
            flash("Category name is required.", "error")
            return render_template('categories/edit.html', category=category)

        existing_category = Category.query.filter(
            func.lower(Category.name) == name.lower(),
            Category.id != category.id
        ).first()

        if existing_category:
            flash("A category with this name already exists.", "error")
            return render_template('categories/edit.html', category=category)

        category.name = name
        category.description = description or None
        db.session.commit()

        flash('Category updated successfully!', 'success')
        return redirect(url_for('categories'))

    return render_template('categories/edit.html', category=category)


@app.route('/categories/<int:id>/toggle-status', methods=['POST'])
@login_required
def toggle_category_status(id):
    if not has_role('admin', 'staff'):
        flash("You are not authorized to perform this action.", "error")
        return redirect(dashboard_for(current_user))

    category = db.get_or_404(Category, id)
    category.is_active = not category.is_active
    db.session.commit()

    state = "enabled" if category.is_active else "disabled"
    flash(f"Category {state} successfully!", "success")
    return redirect(url_for('categories'))


# products
def read_product_form(current=None):
    errors = []
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()

    if not name:
        errors.append('Product name is required.')
    elif len(name) > 100:
        errors.append('Product name must be 100 characters or fewer.')
    else:
        duplicate = Product.query.filter(func.lower(Product.name) == name.lower())
        if current is not None:
            duplicate = duplicate.filter(Product.id != current.id)
        if duplicate.first():
            errors.append('A product with this name already exists.')

    if len(description) > 300:
        errors.append('Description must be 300 characters or fewer.')

    category = load_choice(
        Category, request.form.get('category_id'),
        current.category_id if current else None
    )
    if category is None:
        errors.append('Please select an enabled category.')

    supplier = load_choice(
        Supplier, request.form.get('supplier_id'),
        current.supplier_id if current else None
    )
    if supplier is None:
        errors.append('Please select an active supplier.')

    purchase_price = parse_price(request.form.get('purchase_price', '').strip(), 'purchase price', errors)
    selling_price = parse_price(request.form.get('selling_price', '').strip(), 'selling price', errors)

    quantity = None
    try:
        quantity = int(request.form.get('quantity', '').strip())
        if quantity < 0:
            errors.append('Quantity cannot be negative.')
    except ValueError:
        errors.append('Enter a valid whole-number quantity.')

    data = {
        'name': name,
        'description': description or None,
        'category_id': category.id if category else None,
        'supplier_id': supplier.id if supplier else None,
        'purchase_price': purchase_price,
        'selling_price': selling_price,
        'quantity': quantity,
    }
    return data, errors


def product_form_choices(product=None):
    category_filter = Category.is_active.is_(True)
    supplier_filter = Supplier.is_active.is_(True)

    if product is not None:
        category_filter = or_(category_filter, Category.id == product.category_id)
        supplier_filter = or_(supplier_filter, Supplier.id == product.supplier_id)

    category_list = Category.query.filter(category_filter).order_by(Category.name.asc()).all()
    supplier_list = Supplier.query.filter(supplier_filter).order_by(Supplier.full_name.asc()).all()
    return category_list, supplier_list


@app.route('/products')
@login_required
def products():
    if not has_role('admin', 'staff'):
        flash("You are not authorized to access products.", "error")
        return redirect(dashboard_for(current_user))

    product_list = Product.query.order_by(Product.name.asc()).all()
    return render_template('products/index.html', products=product_list)


@app.route('/products/add', methods=['GET', 'POST'])
@login_required
def add_product():
    if not has_role('admin', 'staff'):
        flash("You are not authorized to add products.", "error")
        return redirect(dashboard_for(current_user))

    category_list, supplier_list = product_form_choices()

    if request.method == 'POST':
        data, errors = read_product_form()

        if errors:
            for error in errors:
                flash(error, 'error')
            return render_template('products/add.html', categories=category_list, suppliers=supplier_list)

        try:
            db.session.add(Product(**data))
            db.session.commit()
            flash('Product added successfully.', 'success')
            return redirect(url_for('products'))
        except Exception:
            db.session.rollback()
            app.logger.exception("Failed to save product")
            flash('Unable to save the product. Please try again.', 'error')

    return render_template('products/add.html', categories=category_list, suppliers=supplier_list)


@app.route('/products/<int:product_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_product(product_id):
    if not has_role('admin', 'staff'):
        flash("You are not authorized to edit products.", "error")
        return redirect(dashboard_for(current_user))

    product = db.session.get(Product, product_id)
    if product is None:
        flash("Product not found.", "error")
        return redirect(url_for('products'))

    category_list, supplier_list = product_form_choices(product)

    if request.method == 'POST':
        data, errors = read_product_form(current=product)

        if errors:
            for error in errors:
                flash(error, 'error')
            return render_template('products/edit.html', product=product,
                                   categories=category_list, suppliers=supplier_list)

        for key, value in data.items():
            setattr(product, key, value)

        try:
            db.session.commit()
            flash('Product updated successfully.', 'success')
            return redirect(url_for('products'))
        except Exception:
            db.session.rollback()
            app.logger.exception("Failed to update product")
            flash('Unable to update the product. Please try again.', 'error')

    return render_template('products/edit.html', product=product,
                           categories=category_list, suppliers=supplier_list)


@app.route('/products/<int:product_id>/delete', methods=['POST'])
@login_required
def delete_product(product_id):
    if not has_role('admin', 'staff'):
        flash("You are not authorized to delete products.", "error")
        return redirect(dashboard_for(current_user))

    product = db.session.get(Product, product_id)
    if product is None:
        flash("Product not found.", "error")
        return redirect(url_for('products'))

    try:
        db.session.delete(product)
        db.session.commit()
        flash("Product deleted.", "success")
    except Exception:
        db.session.rollback()
        app.logger.exception("Failed to delete product")
        flash("Unable to delete this product.", "error")

    return redirect(url_for('products'))

# suppliers
@app.route('/suppliers')
@login_required
def suppliers():
    if current_user.role != 'admin':
        flash("You are not authorized to manage suppliers.", "error")
        return redirect(dashboard_for(current_user))

    supplier_list = Supplier.query.order_by(Supplier.full_name.asc()).all()
    return render_template('suppliers/index.html', suppliers=supplier_list)

import re

EMAIL_PATTERN = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def clean_phone(value):
    return re.sub(r'[\s\-()]', '', value)

@app.route('/suppliers/add', methods=['GET', 'POST'])
@login_required
def add_supplier():
    if not has_role('admin'):
        flash("You are not authorized to add suppliers.", "error")
        return redirect(dashboard_for(current_user))

    if request.method == 'POST':
        data, errors = read_supplier_form()

        if errors:
            for error in errors:
                flash(error, 'error')
            return render_template('suppliers/add.html')

        try:
            db.session.add(Supplier(**data, is_active=True))
            db.session.commit()
            flash('Supplier added successfully.', 'success')
            return redirect(url_for('suppliers'))
        except IntegrityError:
            db.session.rollback()
            flash('A supplier with this email or phone number already exists.', 'error')
        except Exception:
            db.session.rollback()
            app.logger.exception("Failed to add supplier")
            flash('Unable to save the supplier. Please try again.', 'error')

    return render_template('suppliers/add.html')

def read_supplier_form(current=None):
    errors = []
    full_name = request.form.get('full_name', '').strip()
    email = request.form.get('email', '').strip().lower()
    phone = clean_phone(request.form.get('phone', ''))
    address = request.form.get('address', '').strip()

    if not full_name:
        errors.append('Supplier name is required.')
    elif len(full_name) > 100:
        errors.append('Supplier name must be 100 characters or fewer.')

    if not email:
        errors.append('Email address is required.')
    elif len(email) > 120 or not EMAIL_PATTERN.match(email):
        errors.append('Enter a valid email address.')
    else:
        duplicate = Supplier.query.filter_by(email=email)
        if current is not None:
            duplicate = duplicate.filter(Supplier.id != current.id)
        if duplicate.first():
            errors.append('A supplier with this email already exists.')

    if not phone:
        errors.append('Phone number is required.')
    elif len(phone) > 20:
        errors.append('Phone number must be 20 characters or fewer.')
    else:
        duplicate = Supplier.query.filter_by(phone=phone)
        if current is not None:
            duplicate = duplicate.filter(Supplier.id != current.id)
        if duplicate.first():
            errors.append('A supplier with this phone number already exists.')

    if not address:
        errors.append('Address is required.')
    elif len(address) > 255:
        errors.append('Address must be 255 characters or fewer.')

    data = {'full_name': full_name, 'email': email, 'phone': phone, 'address': address}
    return data, errors


@app.route('/suppliers/<int:supplier_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_supplier(supplier_id):
    if not has_role('admin'):
        flash("You are not authorized to edit suppliers.", "error")
        return redirect(dashboard_for(current_user))

    supplier = db.session.get(Supplier, supplier_id)
    if supplier is None:
        flash("Supplier not found.", "error")
        return redirect(url_for('suppliers'))

    if request.method == 'POST':
        data, errors = read_supplier_form(current=supplier)

        if errors:
            for error in errors:
                flash(error, 'error')
            return render_template('suppliers/edit.html', supplier=supplier)

        for key, value in data.items():
            setattr(supplier, key, value)

        try:
            db.session.commit()
            flash('Supplier updated successfully.', 'success')
            return redirect(url_for('suppliers'))
        except IntegrityError:
            db.session.rollback()
            flash('A supplier with this email or phone number already exists.', 'error')
        except Exception:
            db.session.rollback()
            app.logger.exception("Failed to update supplier")
            flash('Unable to update the supplier. Please try again.', 'error')

    return render_template('suppliers/edit.html', supplier=supplier)

@app.route('/suppliers/<int:supplier_id>/toggle-status', methods=['POST'])
@login_required
def toggle_supplier_status(supplier_id):
    if not has_role('admin'):
        flash("You are not authorized to perform this action.", "error")
        return redirect(dashboard_for(current_user))

    supplier = db.session.get(Supplier, supplier_id)
    if supplier is None:
        flash("Supplier not found.", "error")
        return redirect(url_for('suppliers'))

    supplier.is_active = not supplier.is_active
    db.session.commit()

    state = "activated" if supplier.is_active else "deactivated"
    flash(f"Supplier {state} successfully!", "success")
    return redirect(url_for('suppliers'))

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash("You have been logged out successfully.", "success")
    return redirect(url_for('login'))
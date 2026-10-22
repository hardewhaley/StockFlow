import re
import secrets
import string
import time
from decimal import Decimal, InvalidOperation

from flask import render_template, redirect, flash, url_for, session, request, abort
from flask_login import login_user, logout_user, login_required, current_user
from flask_mail import Message
from sqlalchemy import func, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload, selectinload
from werkzeug.security import check_password_hash, generate_password_hash

from app import app, db, mail
from app.forms import LoginForm, StaffForm, CustomerRegistrationForm, CustomerOTPForm, StaffLoginForm
from app.models import User, Customer, Product, Supplier, Category, StockMovement, Sale, SaleItem


OTP_LIFETIME_SECONDS = 600
OTP_MAX_ATTEMPTS = 5
MAX_PRICE = Decimal('99999999.99')
REASON_MAX_LENGTH = 255
EMAIL_PATTERN = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


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

EMAIL_PATTERN = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

PAYMENT_METHODS = [
    {
        'value': 'pay_on_delivery',
        'label': 'Pay on Delivery',
        'description': 'Pay in cash when your order arrives.',
        'icon': 'fa-money-bill-wave'
    },
    {
        'value': 'bank_transfer',
        'label': 'Bank Transfer',
        'description': 'Transfer the total to our account after ordering.',
        'icon': 'fa-building-columns'
    },
    {
        'value': 'card',
        'label': 'Card Payment',
        'description': 'Pay with your debit or credit card.',
        'icon': 'fa-credit-card'
    }
]

PAYMENT_LABELS = {method['value']: method['label'] for method in PAYMENT_METHODS}


def get_cart_items():
    saved_cart = session.get('cart', {})
    cart_items = []
    cleaned_cart = {}
    total = Decimal('0.00')
    adjusted = False

    for product_id, quantity in saved_cart.items():
        try:
            product_id = int(product_id)
            quantity = int(quantity)
        except (ValueError, TypeError):
            adjusted = True
            continue

        product = db.session.get(Product, product_id)

        if (
            product is None
            or quantity <= 0
            or product.quantity <= 0
            or product.selling_price is None
        ):
            adjusted = True
            continue

        if quantity > product.quantity:
            quantity = product.quantity
            adjusted = True

        cleaned_cart[str(product_id)] = quantity

        price = product.selling_price
        subtotal = price * quantity
        total += subtotal

        cart_items.append({
            'product': product,
            'quantity': quantity,
            'price': price,
            'subtotal': subtotal
        })

    session['cart'] = cleaned_cart
    return cart_items, total, adjusted
ORDER_TABS = [
    ('all', 'All'),
    ('pending', 'Pending'),
    ('confirmed', 'Confirmed'),
    ('delivered', 'Delivered'),
    ('cancelled', 'Cancelled'),
]

ORDER_STATUSES = [key for key, label in ORDER_TABS if key != 'all']


def get_customer_order(order_id):
    sale = db.session.get(Sale, order_id)

    if (
        sale is None
        or sale.order_number is None
        or sale.customer_id != current_user.id
    ):
        abort(404)

    return sale



# customer
@app.route('/')
def home():
    products = Product.query.all()
    return render_template('customer/dashboard.html', products=products)


@app.route('/shop')
def shop():
    products = Product.query.order_by(Product.name.asc()).all()
    return render_template('customer/shop.html', products=products)

@app.route('/product/<int:product_id>')
def product_detail(product_id):
    product = db.session.get(Product, product_id)

    if product is None:
        flash("Product not found.", "error")
        return redirect(url_for('shop'))
    return render_template('customer/product_detail.html', product=product)

@app.route('/cart/add/<int:product_id>', methods=['POST'])
def add_to_cart(product_id):
    if not has_role('customer'):
        flash("Please log in with a customer account to shop.", "error")
        return redirect(url_for('shop'))
    product = db.session.get(Product, product_id)

    if product is None:
        flash("Product not found.", "error")
        return redirect(url_for('shop'))

    if product.quantity <= 0:
        flash("This product is currently out of stock.", "error")
        return redirect(url_for('product_detail', product_id=product.id))

    try:
        quantity = int(request.form.get('quantity', 1))
    except (ValueError, TypeError):
        quantity = 0

    if quantity <= 0:
        flash("Please enter a valid quantity.", "error")
        return redirect(url_for('product_detail', product_id=product.id))

    cart = session.get('cart', {})
    product_key = str(product.id)
    current_quantity = cart.get(product_key, 0)

    if current_quantity + quantity > product.quantity:
        flash(
            f"Only {product.quantity} unit(s) are currently available.",
            "error"
        )
        return redirect(url_for('product_detail', product_id=product.id))

    cart[product_key] = current_quantity + quantity
    session['cart'] = cart

    flash(f"{product.name} added to your cart!", "success")
    return redirect(url_for('cart'))

# view shopping cart
@app.route('/cart')
@login_required
def cart():
    if not has_role('customer'):
        flash("Please log in with a customer account to view your cart.", "error")
        return redirect(url_for('shop'))

    cart_items, total, adjusted = get_cart_items()

    if adjusted:
        flash("Some items in your cart were updated or removed because of stock changes.", "error")

    return render_template('customer/cart.html', cart_items=cart_items, total=total)

@app.route('/checkout', methods=['GET', 'POST'])
@login_required
def checkout():
    if not has_role('customer'):
        flash("Please log in with a customer account to check out.", "error")
        return redirect(url_for('shop'))

    cart_items, total, adjusted = get_cart_items()

    if adjusted:
        flash("Some items in your cart were updated or removed because of stock changes.", "error")

    if not cart_items:
        flash("Your cart is empty.", "error")
        return redirect(url_for('cart'))

    if request.method == 'POST':
        full_name = request.form.get('full_name', '').strip()
        email = request.form.get('email', '').strip().lower()
        phone = re.sub(r'[\s\-().]', '', request.form.get('phone', '').strip())
        address = request.form.get('address', '').strip()
        payment_method = request.form.get('payment_method', '')

        errors = []

        if len(full_name) < 2 or len(full_name) > 100:
            errors.append("Enter your full name (2 to 100 characters).")

        if not EMAIL_PATTERN.match(email) or len(email) > 120:
            errors.append("Enter a valid email address.")

        if not re.fullmatch(r'\+?\d{10,15}', phone) or len(phone) > 20:
            errors.append("Enter a valid phone number (10 to 15 digits).")

        if len(address) < 10 or len(address) > 255:
            errors.append("Enter a full delivery address (10 to 255 characters).")

        if payment_method not in PAYMENT_LABELS:
            errors.append("Choose a payment method.")

        for error in errors:
            flash(error, "error")

        if not errors:
            try:
                product_ids = [item['product'].id for item in cart_items]
                locked_products = {
                    product.id: product
                    for product in Product.query
                    .filter(Product.id.in_(product_ids))
                    .with_for_update()
                    .all()
                }

                sale = Sale(
                    customer_id=current_user.id,
                    user_id=None,
                    full_name=full_name,
                    email=email,
                    phone=phone,
                    address=address,
                    payment_method=payment_method,
                    payment_status='pending',
                    status='pending',
                    total_amount=Decimal('0.00')
                )
                db.session.add(sale)
                db.session.flush()
                sale.order_number = f"ORD-{sale.id:05d}"

                order_total = Decimal('0.00')

                for item in cart_items:
                    quantity = item['quantity']
                    product = locked_products.get(item['product'].id)

                    if (
                        product is None
                        or product.selling_price is None
                        or quantity > product.quantity
                    ):
                        db.session.rollback()
                        flash(
                            f"Sorry, {item['product'].name} is no longer available in the quantity you selected.",
                            "error"
                        )
                        return redirect(url_for('cart'))

                    unit_price = product.selling_price
                    subtotal = unit_price * quantity
                    order_total += subtotal

                    db.session.add(SaleItem(
                        sale_id=sale.id,
                        product_id=product.id,
                        quantity=quantity,
                        unit_price=unit_price,
                        subtotal=subtotal
                    ))

                    product.quantity -= quantity

                    db.session.add(StockMovement(
                        product_id=product.id,
                        user_id=None,
                        movement_type='OUT',
                        quantity=quantity,
                        reason=f"Online order {sale.order_number}"
                    ))

                sale.total_amount = order_total
                db.session.commit()

                session['cart'] = {}
                flash("Your order has been placed.", "success")
                return redirect(url_for('order_success', order_id=sale.id))

            except Exception:
                db.session.rollback()
                app.logger.exception("Checkout failed")
                flash("Something went wrong while placing your order. Please try again.", "error")

    return render_template('customer/checkout.html', cart_items=cart_items,total=total,payment_methods=PAYMENT_METHODS)

@app.route('/order/<int:order_id>/success')
@login_required
def order_success(order_id):
    if not has_role('customer'):
        return redirect(dashboard_for(current_user))

    sale = get_customer_order(order_id)

    return render_template('customer/order_success.html', order=sale,payment_label=PAYMENT_LABELS.get(sale.payment_method, sale.payment_method))

@app.route('/orders')
@login_required
def my_orders():
    if not has_role('customer'):
        flash("Please log in with a customer account to view your orders.", "error")
        return redirect(dashboard_for(current_user))

    all_orders = (
        Sale.query
        .options(selectinload(Sale.sale_items).selectinload(SaleItem.product))
        .filter(Sale.customer_id == current_user.id, Sale.order_number.isnot(None))
        .order_by(Sale.sale_date.desc(), Sale.id.desc())
        .all()
    )

    current_status = request.args.get('status', 'all')
    if current_status not in ORDER_STATUSES:
        current_status = 'all'

    counts = {'all': len(all_orders)}
    for status in ORDER_STATUSES:
        counts[status] = sum(1 for order in all_orders if order.status == status)

    if current_status == 'all':
        orders = all_orders
    else:
        orders = [order for order in all_orders if order.status == current_status]

    return render_template(
        'customer/orders.html',
        orders=orders,
        counts=counts,
        tabs=ORDER_TABS,
        current_status=current_status,
        payment_labels=PAYMENT_LABELS
    )


@app.route('/orders/<int:order_id>')
@login_required
def order_detail(order_id):
    if not has_role('customer'):
        flash("Please log in with a customer account to view your orders.", "error")
        return redirect(dashboard_for(current_user))

    order = get_customer_order(order_id)

    return render_template('customer/order_detail.html', order=order,payment_label=PAYMENT_LABELS.get(order.payment_method, order.payment_method))


@app.route('/orders/<int:order_id>/cancel', methods=['POST'])
@login_required
def cancel_order(order_id):
    if not has_role('customer'):
        flash("Please log in with a customer account.", "error")
        return redirect(dashboard_for(current_user))

    order = get_customer_order(order_id)

    if order.status != 'pending' or order.payment_status != 'pending':
        flash("This order can no longer be cancelled online. Please contact us for help.", "error")
        return redirect(url_for('order_detail', order_id=order.id))

    try:
        result = db.session.execute(
            update(Sale)
            .where(
                Sale.id == order.id,
                Sale.status == 'pending',
                Sale.payment_status == 'pending'
            )
            .values(status='cancelled')
        )

        if result.rowcount == 0:
            db.session.rollback()
            flash("This order can no longer be cancelled online. Please contact us for help.", "error")
            return redirect(url_for('order_detail', order_id=order.id))

        for item in order.sale_items:
            db.session.execute(
                update(Product)
                .where(Product.id == item.product_id)
                .values(quantity=Product.quantity + item.quantity)
            )
            db.session.add(StockMovement(
                product_id=item.product_id,
                user_id=None,
                movement_type='IN',
                quantity=item.quantity,
                reason=f"Cancelled order {order.order_number}"
            ))

        db.session.commit()
        flash("Your order has been cancelled.", "success")

    except Exception:
        db.session.rollback()
        app.logger.exception("Failed to cancel order")
        flash("We couldn't cancel your order. Please try again.", "error")

    return redirect(url_for('order_detail', order_id=order.id))

@app.route('/cart/update/<int:product_id>', methods=['POST'])
@login_required
def update_cart(product_id):
    if not has_role('customer'):
        flash("Please log in with a customer account.", "error")
        return redirect(url_for('shop'))

    product = db.session.get(Product, product_id)
    saved_cart = session.get('cart', {})
    key = str(product_id)

    if product is None or key not in saved_cart:
        flash("Product not found in your cart.", "error")
        return redirect(url_for('cart'))

    try:
        quantity = int(saved_cart[key])
    except (ValueError, TypeError):
        saved_cart.pop(key, None)
        session['cart'] = saved_cart
        flash("Invalid cart quantity.", "error")
        return redirect(url_for('cart'))

    action = request.form.get('action')

    if action == 'increase':
        if quantity < product.quantity:
            saved_cart[key] = quantity + 1
            flash("Cart quantity increased.", "success")
        else:
            flash("You have reached the available stock limit.", "error")

    elif action == 'decrease':
        if quantity > 1:
            saved_cart[key] = quantity - 1
            flash("Cart quantity decreased.", "success")
        else:
            flash("Remove the product if you no longer want it.", "error")

    session['cart'] = saved_cart
    return redirect(url_for('cart'))


@app.route('/cart/remove/<int:product_id>', methods=['POST'])
@login_required
def remove_from_cart(product_id):
    if not has_role('customer'):
        flash("Please log in with a customer account.", "error")
        return redirect(url_for('shop'))

    saved_cart = session.get('cart', {})
    saved_cart.pop(str(product_id), None)
    session['cart'] = saved_cart

    flash("Product removed from your cart.", "success")
    return redirect(url_for('cart'))


# view all customers
@app.route('/customers')
@login_required
def customer():
    if not has_role('admin', 'staff'):
        flash("You're not authorized to view customers.", "error")
        return redirect(dashboard_for(current_user))
    customer_list = Customer.query.order_by(
        Customer.full_name.asc()
    ).all()
    return render_template(
        'customer/index.html',
        customers=customer_list
    )
# view customer details
@app.route('/customers/<int:customer_id>')
@login_required
def customer_detail(customer_id):
    if not has_role('admin', 'staff'):
        flash(
            "You are not authorized to view customer details.",
            "error"
        )
        return redirect(dashboard_for(current_user))
    customer = db.session.get(Customer, customer_id)
    if customer is None:
        flash("Customer not found.", "error")
        return redirect(url_for('customer'))
    return render_template(
        'customer/detail.html',
        customer=customer
    )
# edit customer details
@app.route('/customers/<int:customer_id>/edit', methods=['GET', 'POST'])
@login_required
def customer_edit(customer_id):
    if not has_role('admin', 'staff'):
        flash(
            "You are not authorized to edit customers.",
            "error"
        )
        return redirect(dashboard_for(current_user))
    customer = db.session.get(Customer, customer_id)
    if customer is None:
        flash("Customer not found.", "error")
        return redirect(url_for('customer'))
    if request.method == 'POST':
        full_name = request.form.get('full_name', '').strip()
        email = request.form.get('email', '').strip().lower()
        phone = request.form.get('phone', '').strip()
        errors = []
 
        if not full_name:
            errors.append("Full name is required.")
        elif len(full_name) > 100:
            errors.append(
                "Full name must not exceed 100 characters."
            )
     
        if not email:
            errors.append("Email is required.")
        elif len(email) > 120 or not EMAIL_PATTERN.match(email):
            errors.append("Enter a valid email address.")
        # Validate phone number
        if not phone:
            errors.append("Phone number is required.")
        elif len(phone) > 20:
            errors.append(
                "Phone number must not exceed 20 characters."
            )
        
        if email:
            existing_email = Customer.query.filter(
                func.lower(Customer.email) == email,
                Customer.id != customer.id
            ).first()
            if existing_email:
                errors.append(
                    "That email address is already in use."
                )
     
        if phone:
            existing_phone = Customer.query.filter(
                Customer.phone == phone,
                Customer.id != customer.id
            ).first()
            if existing_phone:
                errors.append(
                    "That phone number is already in use."
                )
     
        if errors:
            for error in errors:
                flash(error, "error")
        else:
            customer.full_name = full_name
            customer.email = email
            customer.phone = phone
            try:
                db.session.commit()
                flash(
                    "Customer details updated successfully.",
                    "success"
                )
                return redirect(
                    url_for(
                        'customer_detail',
                        customer_id=customer.id
                    )
                )
            except IntegrityError:
                db.session.rollback()
                flash(
                    "That email or phone number is already in use.",
                    "error"
                )
            except Exception:
                db.session.rollback()
                app.logger.exception(
                    "Failed to update customer"
                )
                flash(
                    "Unable to update customer details. "
                    "Please try again.",
                    "error"
                )
    return render_template(
        'customer/edit.html',
        customer=customer
    )
@app.route('/customer/dashboard')
def customer_dashboard():
    products = Product.query.all()
    return render_template(
        'customer/dashboard.html',
        products=products
    )


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
    low_stock_products = Product.query.filter(
    Product.quantity <= 5
).order_by(Product.quantity.asc()).all()
    return render_template('staff/staff_dashboard.html', low_stock_products=low_stock_products)


@app.route('/admin_dashboard')
@login_required
def admin_dashboard():
    if not has_role('admin'):
        flash("You are not authorized to access the admin area", "error")
        return redirect(dashboard_for(current_user))
    low_stock_products = Product.query.filter(
    Product.quantity <= 5
).order_by(Product.quantity.asc()).all()
    low_stock_count = len(low_stock_products)
    return render_template('admin/dashboard.html', low_stock_products=low_stock_products, low_stock_count=low_stock_count)



@app.route('/admin/stock-alerts')
@login_required
def admin_stock_alerts():
    if not has_role('admin'):
        flash("You are not authorized to access stock alerts.", "error")
        return redirect(dashboard_for(current_user))
    low_stock_products = Product.query.filter(
        Product.quantity <= 5
    ).order_by(Product.quantity.asc()).all()

    return render_template('admin/stock_alerts.html', low_stock_products=low_stock_products)


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

# view all staff
@app.route('/admin/staff')
@login_required
def staff_list():
    if not has_role('admin'):
        flash("You are not authorized to view staff members.", "error")
        return redirect(dashboard_for(current_user))
    staff_members = (User.query.filter_by(role='staff').order_by(User.full_name.asc()).all())
    return render_template('admin/staff/index.html', staff_members=staff_members)

# view staff details
@app.route('/admin/staff/<int:staff_id>')
@login_required
def staff_detail(staff_id):
    if not has_role('admin'):
        flash("You are not authorized to view staff details.", "error")
        return redirect(dashboard_for(current_user))
    staff = User.query.filter_by(id=staff_id, role='staff').first()
    if staff is None:
        flash("Staff member not found.", "error")
        return redirect(url_for('staff_list'))
    return render_template('admin/staff/detail.html', staff=staff)


# edit staff details
@app.route('/admin/staff/<int:staff_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_staff(staff_id):
    if not has_role('admin'):
        flash("You are not authorized to edit staff details.", "error")
        return redirect(dashboard_for(current_user))
    staff = User.query.filter_by(
        id=staff_id,
        role='staff'
    ).first()
    if staff is None:
        flash("Staff member not found.", "error")
        return redirect(url_for('staff_list'))
    if request.method == 'POST':
        full_name = request.form.get('full_name', '').strip()
        email = request.form.get('email', '').strip().lower()
        errors = []
        if not full_name:
            errors.append("Full name is required.")
        elif len(full_name) > 100:
            errors.append("Full name must not exceed 100 characters.")
        if not email:
            errors.append("Email address is required.")
        elif len(email) > 120 or not EMAIL_PATTERN.match(email):
            errors.append("Enter a valid email address.")
        else:
            existing_user = User.query.filter(
                func.lower(User.email) == email,
                User.id != staff.id
            ).first()
            if existing_user:
                errors.append("This email address is already in use.")
        if errors:
            for error in errors:
                flash(error, "error")
        else:
            staff.full_name = full_name
            staff.email = email
            try:
                db.session.commit()
                flash("Staff details updated successfully.", "success")
                return redirect(
                    url_for('staff_detail', staff_id=staff.id)
                )
            except IntegrityError:
                db.session.rollback()
                flash("This email address is already in use.", "error")
            except Exception:
                db.session.rollback()
                app.logger.exception("Failed to update staff details")
                flash("Unable to update staff details. Please try again.", "error")
    return render_template('admin/staff/edit.html', staff=staff)

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

    if product.sale_items or product.stock_movements:
        flash("This product has sales or stock history and cannot be deleted.", "error")
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
    if not has_role('admin'):
        flash("You are not authorized to manage suppliers.", "error")
        return redirect(dashboard_for(current_user))

    supplier_list = Supplier.query.order_by(Supplier.full_name.asc()).all()
    return render_template('suppliers/index.html', suppliers=supplier_list)



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

# stock
@app.route('/stock')
@login_required
def stock():
    if not has_role('admin', 'staff'):
        flash("You are not authorized to view stock.", "error")
        return redirect(dashboard_for(current_user))

    product_list = (
        Product.query
        .options(joinedload(Product.category))
        .order_by(Product.quantity.asc(), Product.name.asc())
        .all()
    )
    return render_template('stock/index.html', products=product_list)

@app.route('/stock/add', methods=['GET', 'POST'])
@login_required
def add_stock():
    if not has_role('admin', 'staff'):
        flash("You are not authorized to record stock movements.", "error")
        return redirect(dashboard_for(current_user))

    product_list = Product.query.order_by(Product.name.asc()).all()
    selected_product_id = request.args.get('product_id', type=int)

    if request.method == 'POST':
        movement_type = request.form.get('movement_type', '').strip().upper()
        reason = request.form.get('reason', '').strip()
        errors = []

        product = None
        try:
            product = db.session.get(Product, int(request.form.get('product_id', '')))
        except (ValueError, TypeError):
            pass

        quantity = None
        try:
            quantity = int(request.form.get('quantity', '').strip())
        except ValueError:
            pass

        if product is None:
            errors.append("Please select a valid product.")

        if movement_type not in ('IN', 'OUT'):
            errors.append("Select Stock IN or Stock OUT.")

        if quantity is None or quantity <= 0:
            errors.append("Quantity must be a positive whole number.")

        if not reason:
            errors.append("Please provide a reason for this stock movement.")
        elif len(reason) > REASON_MAX_LENGTH:
            errors.append(f"Reason must be {REASON_MAX_LENGTH} characters or fewer.")

        if product and movement_type == 'OUT' and quantity and quantity > product.quantity:
            errors.append("Stock OUT quantity cannot exceed the available stock.")

        if errors:
            for error in errors:
                flash(error, "error")
            return render_template('stock/add.html', products=product_list)

        try:
            change = quantity if movement_type == 'IN' else -quantity
            statement = update(Product).where(Product.id == product.id)

            if movement_type == 'OUT':
                statement = statement.where(Product.quantity >= quantity)

            result = db.session.execute(statement.values(quantity=Product.quantity + change))

            if result.rowcount == 0:
                db.session.rollback()
                flash("Stock OUT quantity cannot exceed the available stock.", "error")
                return render_template('stock/add.html', products=product_list)

            db.session.add(StockMovement(
                product_id=product.id,
                user_id=current_user.id,
                movement_type=movement_type,
                quantity=quantity,
                reason=reason
            ))
            db.session.commit()

            flash("Stock movement recorded successfully.", "success")
            return redirect(url_for('stock'))

        except Exception:
            db.session.rollback()
            app.logger.exception("Failed to record stock movement")
            flash("Unable to record stock movement. Please try again.", "error")

    return render_template('stock/add.html', products=product_list, selected_product_id=selected_product_id)

@app.route('/stock/movements')
@login_required
def stock_movements():
    if not has_role('admin', 'staff'):
        flash("You are not authorized to view stock movements.", "error")
        return redirect(dashboard_for(current_user))

    movements = (
        StockMovement.query
        .options(joinedload(StockMovement.product), joinedload(StockMovement.user))
        .order_by(StockMovement.movement_date.desc(), StockMovement.id.desc())
        .all()
    )

    return render_template('stock/movements.html', movements=movements)



@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash("You have been logged out successfully.", "success")
    return redirect(url_for('login'))
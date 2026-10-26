import re
import secrets
import string
import time
import os

from PIL import Image, ImageOps, UnidentifiedImageError
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from collections import Counter

from flask import render_template, redirect, flash, url_for, session, request, abort, jsonify
from flask_login import login_user, logout_user, login_required, current_user
from flask_mail import Message
from sqlalchemy import func, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload, selectinload
from werkzeug.security import check_password_hash, generate_password_hash

from app import app, db, mail
from app.forms import LoginForm, StaffForm, CustomerRegistrationForm, CustomerOTPForm, StaffLoginForm
from app.models import User, Customer, Product, Supplier, Category, StockMovement, Sale, SaleItem


LAGOS = timezone(timedelta(hours=1))

OTP_LIFETIME_SECONDS = 600
OTP_MAX_ATTEMPTS = 5
MAX_PRICE = Decimal('99999999.99')
REASON_MAX_LENGTH = 255
EMAIL_PATTERN = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

LOW_STOCK_LEVEL = 5

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

STAFF_PAYMENT_METHODS = [
    {'value': 'cash', 'label': 'Cash', 'icon': 'fa-money-bill-wave'},
    {'value': 'bank_transfer', 'label': 'Bank Transfer', 'icon': 'fa-building-columns'},
    {'value': 'card', 'label': 'Card Payment', 'icon': 'fa-credit-card'}
]

STAFF_PAYMENT_VALUES = {method['value'] for method in STAFF_PAYMENT_METHODS}

SALE_PAYMENT_LABELS = {**PAYMENT_LABELS, 'cash': 'Cash'}

ORDER_TABS = [
    ('all', 'All'),
    ('pending', 'Pending'),
    ('confirmed', 'Confirmed'),
    ('delivered', 'Delivered'),
    ('cancelled', 'Cancelled'),
]

ORDER_STATUSES = [key for key, label in ORDER_TABS if key != 'all']


class SaleError(Exception):
    pass


@app.template_filter('local_time')
def local_time(value, fmt='%d %b %Y, %H:%M'):
    if value is None:
        return 'N/A'
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(LAGOS).strftime(fmt)


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


def clean_phone(value):
    return re.sub(r'[\s\-()]', '', value)


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


def get_customer_order(order_id):
    sale = db.session.get(Sale, order_id)

    if (
        sale is None
        or not (sale.order_number or '').startswith('ORD-')
        or sale.customer_id != current_user.id
    ):
        abort(404)

    return sale


def sale_form_products():
    product_list = (
        Product.query
        .filter(Product.quantity > 0, Product.selling_price.isnot(None))
        .order_by(Product.name.asc())
        .all()
    )
    return [
        {
            'id': product.id,
            'name': product.name,
            'price': float(product.selling_price),
            'stock': product.quantity
        }
        for product in product_list
    ]

def sales_totals(*conditions):
    count, amount = (
        db.session.query(
            func.count(Sale.id),
            func.coalesce(func.sum(Sale.total_amount), 0)
        )
        .filter(Sale.status != 'cancelled', *conditions)
        .one()
    )
    return count, Decimal(str(amount))


def as_utc(value):
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def build_activities(limit=8):
    activities = []

    recent_sale_rows = (
        Sale.query
        .options(joinedload(Sale.user))
        .order_by(Sale.sale_date.desc(), Sale.id.desc())
        .limit(limit)
        .all()
    )

    for sale in recent_sale_rows:
        reference = sale.order_number or f"SAL-{sale.id:05d}"
        online = (sale.order_number or '').startswith('ORD-')

        if online:
            title = f"Online order {reference} placed"
            detail = f"{sale.full_name or 'Customer'} · ₦{sale.total_amount:,.2f}"
            icon, tone = 'fa-globe', 'purple'
        else:
            seller = sale.user.full_name if sale.user else 'Staff'
            title = f"Sale {reference} recorded"
            detail = f"{seller} · ₦{sale.total_amount:,.2f}"
            icon, tone = 'fa-receipt', 'blue'

        activities.append({
            'icon': icon,
            'tone': tone,
            'title': title,
            'detail': detail,
            'when': sale.sale_date,
            'url': url_for('sale_detail', sale_id=sale.id)
        })

    manual_movements = (
        StockMovement.query
        .options(joinedload(StockMovement.product), joinedload(StockMovement.user))
        .filter(
            ~StockMovement.reason.like('Sale %'),
            ~StockMovement.reason.like('Online order %'),
            ~StockMovement.reason.like('Cancelled order %')
        )
        .order_by(StockMovement.movement_date.desc(), StockMovement.id.desc())
        .limit(limit)
        .all()
    )

    for movement in manual_movements:
        incoming = movement.movement_type == 'IN'
        product_name = movement.product.name if movement.product else 'Deleted product'
        person = movement.user.full_name if movement.user else 'System'

        activities.append({
            'icon': 'fa-arrow-down' if incoming else 'fa-arrow-up',
            'tone': 'green' if incoming else 'amber',
            'title': f"Stock {'IN' if incoming else 'OUT'}: {product_name}",
            'detail': f"{'+' if incoming else '-'}{movement.quantity} · {movement.reason} · {person}",
            'when': movement.movement_date,
            'url': url_for('stock_movements')
        })

    activities.sort(key=lambda activity: as_utc(activity['when']), reverse=True)
    return activities[:limit]

REPORT_PERIODS = [
    ('today', 'Today'),
    ('7d', 'Last 7 Days'),
    ('30d', 'Last 30 Days'),
    ('month', 'This Month'),
    ('all', 'All Time'),
]

REPORT_SOURCES = [
    ('all', 'All Sales'),
    ('store', 'In-store'),
    ('online', 'Online'),
]

REPORT_PERIOD_KEYS = {key for key, label in REPORT_PERIODS}
REPORT_SOURCE_KEYS = {key for key, label in REPORT_SOURCES}


def to_local_date(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(LAGOS).date()


def local_day_start_utc(day):
    start = datetime(day.year, day.month, day.day, tzinfo=LAGOS)
    return start.astimezone(timezone.utc).replace(tzinfo=None)


def parse_report_date(value):
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


STORE_INFO = {
    'address': 'No 3, Aladejubelo Street, Akobo Ojurin, Ibadan, Oyo State',
    'phone': '+234 815 433 5354',
    'email': 'adedokunmuhammed2022@gmail.com',
    'hours': 'Mon to Sat, 8:00 AM to 6:00 PM'
}

CATEGORY_ICONS = [
    (('phone', 'mobile'), 'fa-mobile-screen-button'),
    (('gadget', 'electronic', 'tech'), 'fa-microchip'),
    (('computer', 'laptop'), 'fa-laptop'),
    (('audio', 'sound', 'head', 'speaker'), 'fa-headphones'),
    (('watch', 'accessor'), 'fa-clock'),
    (('fashion', 'cloth', 'wear', 'shoe', 'bag'), 'fa-shirt'),
    (('food', 'drink', 'grocer', 'snack'), 'fa-utensils'),
    (('home', 'kitchen', 'furniture'), 'fa-couch'),
    (('beauty', 'cosmetic', 'health'), 'fa-spa'),
    (('book', 'stationery', 'office'), 'fa-book')
]


def category_icon(name):
    label = (name or '').lower()

    for keywords, icon in CATEGORY_ICONS:
        if any(word in label for word in keywords):
            return icon

    return 'fa-tag'

@app.route('/')
def home():
    products = (
        Product.query
        .options(joinedload(Product.category))
        .order_by(Product.name.asc())
        .all()
    )

    newest_ids = []
    if len(products) > 4:
        newest_ids = [
            product.id
            for product in sorted(products, key=lambda item: item.id, reverse=True)[:4]
        ]

    category_counts = Counter(product.category.name for product in products)
    category_cards = [
        {'name': name, 'count': count, 'icon': category_icon(name)}
        for name, count in sorted(category_counts.items())
    ]

    products_by_id = {product.id: product for product in products}

    sold_rows = (
        db.session.query(SaleItem.product_id, func.sum(SaleItem.quantity))
        .join(Sale, SaleItem.sale_id == Sale.id)
        .filter(Sale.status != 'cancelled')
        .group_by(SaleItem.product_id)
        .order_by(func.sum(SaleItem.quantity).desc())
        .limit(4)
        .all()
    )

    best_sellers = [
        {'product': products_by_id[product_id], 'units': int(units)}
        for product_id, units in sold_rows
        if product_id in products_by_id and units
    ]

    return render_template(
        'customer/dashboard.html',
        products=products,
        newest_ids=newest_ids,
        category_cards=category_cards,
        best_sellers=best_sellers,
        store=STORE_INFO
    )

CONTACT_WINDOW_SECONDS = 600
CONTACT_MAX_MESSAGES = 3


@app.route('/contact', methods=['POST'])
def contact_message():
    if request.form.get('website'):
        return jsonify(ok=True, message="Thank you! Your message has been sent.")

    name = ' '.join(request.form.get('name', '').split())
    email = request.form.get('email', '').strip().lower()
    message_text = request.form.get('message', '').strip()

    if len(name) < 2 or len(name) > 100:
        return jsonify(ok=False, message="Enter your name (2 to 100 characters)."), 400

    if not EMAIL_PATTERN.match(email) or len(email) > 120:
        return jsonify(ok=False, message="Enter a valid email address."), 400

    if len(message_text) < 10 or len(message_text) > 2000:
        return jsonify(ok=False, message="Your message must be 10 to 2000 characters long."), 400

    now = time.time()
    recent = [
        stamp for stamp in session.get('contact_times', [])
        if now - stamp < CONTACT_WINDOW_SECONDS
    ]

    if len(recent) >= CONTACT_MAX_MESSAGES:
        return jsonify(
            ok=False,
            message="You have sent several messages recently. Please try again in a few minutes."
        ), 429

    recipient = STORE_INFO.get('email')

    if not recipient:
        return jsonify(ok=False, message="Messaging is not available right now."), 503

    account_line = (
        f"Logged-in customer ID: {current_user.id}"
        if current_user.is_authenticated and has_role('customer')
        else "Sent by a visitor who was not logged in"
    )

    mail_message = Message(
        subject=f"New message from {name} via StockFlow",
        sender=app.config['MAIL_USERNAME'],
        recipients=[recipient],
        reply_to=email
    )
    mail_message.body = (
        f"You have a new message from your StockFlow website.\n\n"
        f"Name: {name}\n"
        f"Email: {email}\n"
        f"{account_line}\n\n"
        f"Message:\n{message_text}\n\n"
        f"Press Reply to answer {name} directly."
    )

    try:
        mail.send(mail_message)
    except Exception:
        app.logger.exception("Contact message failed")
        return jsonify(
            ok=False,
            message="We couldn't send your message. Please try again or call us."
        ), 500

    recent.append(now)
    session['contact_times'] = recent

    return jsonify(ok=True, message="Thank you! Your message has been sent. We will reply to your email.")

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

    saved_cart = session.get('cart', {})
    product_key = str(product.id)
    current_quantity = saved_cart.get(product_key, 0)

    if current_quantity + quantity > product.quantity:
        flash(f"Only {product.quantity} unit(s) are currently available.", "error")
        return redirect(url_for('product_detail', product_id=product.id))

    saved_cart[product_key] = current_quantity + quantity
    session['cart'] = saved_cart

    flash(f"{product.name} added to your cart!", "success")
    return redirect(url_for('cart'))


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


@app.route('/cart/update/<int:product_id>', methods=['POST'])
@login_required
def update_cart(product_id):
    if not has_role('customer'):
        flash("Please log in with a customer account.", "error")
        return redirect(url_for('shop'))

    product = db.session.get(Product, product_id)
    saved_cart = session.get('cart', {})
    key = str(product_id)

    if key not in saved_cart:
        flash("Product not found in your cart.", "error")
        return redirect(url_for('cart'))

    if product is None:
        saved_cart.pop(key, None)
        session['cart'] = saved_cart
        flash("That product is no longer available and was removed.", "error")
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
        else:
            flash("You have reached the available stock limit.", "error")

    elif action == 'decrease':
        if quantity > 1:
            saved_cart[key] = quantity - 1
        else:
            flash("Remove the product if you no longer want it.", "error")

    else:
        flash("Invalid action.", "error")

    session['cart'] = saved_cart
    return redirect(url_for('cart'))


@app.route('/cart/remove/<int:product_id>', methods=['POST'])
@login_required
def remove_from_cart(product_id):
    if not has_role('customer'):
        flash("Please log in with a customer account.", "error")
        return redirect(url_for('shop'))

    saved_cart = session.get('cart', {})

    if saved_cart.pop(str(product_id), None) is None:
        flash("Product was not in your cart.", "error")
    else:
        flash("Product removed from your cart.", "success")

    session['cart'] = saved_cart
    return redirect(url_for('cart'))


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
                    product = db.session.get(Product, item['product'].id)

                    if product is None or product.selling_price is None:
                        db.session.rollback()
                        flash(f"Sorry, {item['product'].name} is no longer available.", "error")
                        return redirect(url_for('cart'))

                    result = db.session.execute(
                        update(Product)
                        .where(Product.id == product.id, Product.quantity >= quantity)
                        .values(quantity=Product.quantity - quantity)
                    )

                    if result.rowcount == 0:
                        db.session.rollback()
                        flash(
                            f"Sorry, {product.name} is no longer available in the quantity you selected.",
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

    return render_template(
        'customer/checkout.html',
        cart_items=cart_items,
        total=total,
        payment_methods=PAYMENT_METHODS
    )


@app.route('/order/<int:order_id>/success')
@login_required
def order_success(order_id):
    if not has_role('customer'):
        return redirect(dashboard_for(current_user))

    sale = get_customer_order(order_id)

    return render_template(
        'customer/order_success.html',
        order=sale,
        payment_label=PAYMENT_LABELS.get(sale.payment_method, sale.payment_method)
    )


@app.route('/orders')
@login_required
def my_orders():
    if not has_role('customer'):
        flash("Please log in with a customer account to view your orders.", "error")
        return redirect(dashboard_for(current_user))

    all_orders = (
        Sale.query
        .options(selectinload(Sale.sale_items).selectinload(SaleItem.product))
        .filter(Sale.customer_id == current_user.id, Sale.order_number.like('ORD-%'))
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

    return render_template(
        'customer/order_detail.html',
        order=order,
        payment_label=PAYMENT_LABELS.get(order.payment_method, order.payment_method)
    )


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


@app.route('/customers')
@login_required
def customer():
    if not has_role('admin', 'staff'):
        flash("You're not authorized to view customers.", "error")
        return redirect(dashboard_for(current_user))

    customer_list = Customer.query.order_by(Customer.full_name.asc()).all()
    return render_template('customer/index.html', customers=customer_list)


@app.route('/customers/<int:customer_id>')
@login_required
def customer_detail(customer_id):
    if not has_role('admin', 'staff'):
        flash("You are not authorized to view customer details.", "error")
        return redirect(dashboard_for(current_user))

    customer = db.session.get(Customer, customer_id)

    if customer is None:
        flash("Customer not found.", "error")
        return redirect(url_for('customer'))

    return render_template('customer/detail.html', customer=customer)


@app.route('/customers/<int:customer_id>/edit', methods=['GET', 'POST'])
@login_required
def customer_edit(customer_id):
    if not has_role('admin', 'staff'):
        flash("You are not authorized to edit customers.", "error")
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
            errors.append("Full name must not exceed 100 characters.")

        if not email:
            errors.append("Email is required.")
        elif len(email) > 120 or not EMAIL_PATTERN.match(email):
            errors.append("Enter a valid email address.")

        if not phone:
            errors.append("Phone number is required.")
        elif len(phone) > 20:
            errors.append("Phone number must not exceed 20 characters.")

        if email:
            existing_email = Customer.query.filter(
                func.lower(Customer.email) == email,
                Customer.id != customer.id
            ).first()
            if existing_email:
                errors.append("That email address is already in use.")

        if phone:
            existing_phone = Customer.query.filter(
                Customer.phone == phone,
                Customer.id != customer.id
            ).first()
            if existing_phone:
                errors.append("That phone number is already in use.")

        if errors:
            for error in errors:
                flash(error, "error")
        else:
            customer.full_name = full_name
            customer.email = email
            customer.phone = phone

            try:
                db.session.commit()
                flash("Customer details updated successfully.", "success")
                return redirect(url_for('customer_detail', customer_id=customer.id))
            except IntegrityError:
                db.session.rollback()
                flash("That email or phone number is already in use.", "error")
            except Exception:
                db.session.rollback()
                app.logger.exception("Failed to update customer")
                flash("Unable to update customer details. Please try again.", "error")

    return render_template('customer/edit.html', customer=customer)


@app.route('/customer/dashboard')
@login_required
def customer_dashboard():
    if not has_role('customer'):
        flash("Please log in with a customer account to view your dashboard.", "error")
        return redirect(dashboard_for(current_user))

    order_filter = (
        Sale.customer_id == current_user.id,
        Sale.order_number.like('ORD-%')
    )

    status_counts = dict(
        db.session.query(Sale.status, func.count(Sale.id))
        .filter(*order_filter)
        .group_by(Sale.status)
        .all()
    )

    cart_items, cart_total, adjusted = get_cart_items()

    recent_orders = (
        Sale.query
        .options(selectinload(Sale.sale_items).selectinload(SaleItem.product))
        .filter(*order_filter)
        .order_by(Sale.sale_date.desc(), Sale.id.desc())
        .limit(5)
        .all()
    )

    active_order = next(
        (order for order in recent_orders if order.status in ('pending', 'confirmed')),
        None
    )

    featured_products = (
        Product.query
        .filter(Product.quantity > 0, Product.selling_price.isnot(None))
        .order_by(Product.id.desc())
        .limit(4)
        .all()
    )

    hour = datetime.now(LAGOS).hour
    if hour < 12:
        greeting = 'Good morning'
    elif hour < 17:
        greeting = 'Good afternoon'
    else:
        greeting = 'Good evening'

    name_parts = (current_user.full_name or '').split()

    return render_template(
        'customer/account.html',
        greeting=greeting,
        first_name=name_parts[0] if name_parts else 'there',
        total_orders=sum(status_counts.values()),
        pending_orders=status_counts.get('pending', 0),
        completed_orders=status_counts.get('delivered', 0),
        cart_count=sum(item['quantity'] for item in cart_items),
        recent_orders=recent_orders,
        active_order=active_order,
        featured_products=featured_products
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


@app.route('/staff/login', methods=['GET', 'POST'])
def staff_login():
    if current_user.is_authenticated:
        return redirect(dashboard_for(current_user))

    form = StaffLoginForm()

    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        user = User.query.filter_by(email=email).first()

        if (
            user
            and user.role == 'staff'
            and check_password_hash(user.password, form.staff_code.data.strip())
        ):
            login_user(user)
            flash("Welcome to StockFlow!", "success")
            return redirect(url_for('staff_dashboard'))

        flash("Invalid email or staff code.", "error")

    return render_template('staff_login.html', form=form)



@app.route('/staff/dashboard')
@login_required
def staff_dashboard():
    if not has_role('staff'):
        flash("You are not authorized to access the staff dashboard.", "error")
        return redirect(dashboard_for(current_user))

    start_local = datetime.now(LAGOS).replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_local.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = start_utc + timedelta(days=1)

    total_products = Product.query.count()
    available_stock = db.session.query(func.coalesce(func.sum(Product.quantity), 0)).scalar()

    today_sales, today_revenue = sales_totals(
        Sale.sale_date >= start_utc,
        Sale.sale_date < end_utc
    )

    total_customers = Customer.query.count()

    low_stock_query = Product.query.filter(Product.quantity <= LOW_STOCK_LEVEL)
    low_stock_count = low_stock_query.count()
    low_stock_products = (
        low_stock_query
        .order_by(Product.quantity.asc(), Product.name.asc())
        .limit(8)
        .all()
    )

    recent_sales = (
        Sale.query
        .options(joinedload(Sale.customer), joinedload(Sale.user))
        .order_by(Sale.sale_date.desc(), Sale.id.desc())
        .limit(6)
        .all()
    )

    pending_orders = Sale.query.filter(
        Sale.order_number.like('ORD-%'),
        Sale.status == 'pending'
    ).count()

    return render_template(
        'staff/staff_dashboard.html',
        total_products=total_products,
        available_stock=available_stock,
        today_sales=today_sales,
        today_revenue=today_revenue,
        total_customers=total_customers,
        low_stock_count=low_stock_count,
        low_stock_products=low_stock_products,
        recent_sales=recent_sales,
        pending_orders=pending_orders
    )



@app.route('/admin_dashboard')
@login_required
def admin_dashboard():
    if not has_role('admin'):
        flash("You are not authorized to access the admin area", "error")
        return redirect(dashboard_for(current_user))

    start_local = datetime.now(LAGOS).replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_local.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = start_utc + timedelta(days=1)

    total_sales, total_revenue = sales_totals()
    today_sales, today_revenue = sales_totals(
        Sale.sale_date >= start_utc,
        Sale.sale_date < end_utc
    )

    low_stock_query = Product.query.filter(Product.quantity <= LOW_STOCK_LEVEL)
    low_stock_count = low_stock_query.count()
    low_stock_products = (
        low_stock_query
        .order_by(Product.quantity.asc(), Product.name.asc())
        .limit(8)
        .all()
    )

    recent_sales = (
        Sale.query
        .options(joinedload(Sale.customer), joinedload(Sale.user))
        .order_by(Sale.sale_date.desc(), Sale.id.desc())
        .limit(6)
        .all()
    )

    return render_template(
        'admin/dashboard.html',
        total_products=Product.query.count(),
        total_units=db.session.query(func.coalesce(func.sum(Product.quantity), 0)).scalar(),
        total_customers=Customer.query.count(),
        total_staff=User.query.filter_by(role='staff').count(),
        total_suppliers=Supplier.query.count(),
        active_suppliers=Supplier.query.filter(Supplier.is_active.is_(True)).count(),
        total_sales=total_sales,
        total_revenue=total_revenue,
        today_sales=today_sales,
        today_revenue=today_revenue,
        low_stock_count=low_stock_count,
        out_of_stock_count=Product.query.filter(Product.quantity == 0).count(),
        pending_orders=Sale.query.filter(
            Sale.order_number.like('ORD-%'),
            Sale.status == 'pending'
        ).count(),
        recent_sales=recent_sales,
        low_stock_products=low_stock_products,
        activities=build_activities()
    )


@app.route('/admin/stock-alerts')
@login_required
def admin_stock_alerts():
    if not has_role('admin'):
        flash("You are not authorized to access stock alerts.", "error")
        return redirect(dashboard_for(current_user))

    low_stock_products = (
        Product.query
        .filter(Product.quantity <= 5)
        .order_by(Product.quantity.asc())
        .all()
    )
    return render_template('admin/stock_alerts.html', low_stock_products=low_stock_products)


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


@app.route('/admin/staff')
@login_required
def staff_list():
    if not has_role('admin'):
        flash("You are not authorized to view staff members.", "error")
        return redirect(dashboard_for(current_user))

    staff_members = User.query.filter_by(role='staff').order_by(User.full_name.asc()).all()
    return render_template('admin/staff/index.html', staff_members=staff_members)


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


@app.route('/admin/staff/<int:staff_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_staff(staff_id):
    if not has_role('admin'):
        flash("You are not authorized to edit staff details.", "error")
        return redirect(dashboard_for(current_user))

    staff = User.query.filter_by(id=staff_id, role='staff').first()

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
                return redirect(url_for('staff_detail', staff_id=staff.id))
            except IntegrityError:
                db.session.rollback()
                flash("This email address is already in use.", "error")
            except Exception:
                db.session.rollback()
                app.logger.exception("Failed to update staff details")
                flash("Unable to update staff details. Please try again.", "error")

    return render_template('admin/staff/edit.html', staff=staff)


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
            return render_template(
                'products/edit.html',
                product=product,
                categories=category_list,
                suppliers=supplier_list
            )

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

    return render_template(
        'products/edit.html',
        product=product,
        categories=category_list,
        suppliers=supplier_list
    )


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


@app.route('/suppliers')
@login_required
def suppliers():
    if not has_role('admin'):
        flash("You are not authorized to manage suppliers.", "error")
        return redirect(dashboard_for(current_user))

    supplier_list = Supplier.query.order_by(Supplier.full_name.asc()).all()
    return render_template('suppliers/index.html', suppliers=supplier_list)


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

    return render_template(
        'stock/add.html',
        products=product_list,
        selected_product_id=selected_product_id
    )


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


@app.route('/sales')
@login_required
def sales():
    if not has_role('admin', 'staff'):
        flash("You are not authorized to view sales.", "error")
        return redirect(dashboard_for(current_user))

    sale_list = (
        Sale.query
        .options(selectinload(Sale.sale_items), joinedload(Sale.customer))
        .order_by(Sale.sale_date.desc(), Sale.id.desc())
        .all()
    )

    revenue = sum(
        (sale.total_amount for sale in sale_list if sale.status != 'cancelled'),
        Decimal('0.00')
    )
    pending_online = sum(
        1 for sale in sale_list if sale.user_id is None and sale.status == 'pending'
    )

    return render_template(
        'sales/index.html',
        sales=sale_list,
        revenue=revenue,
        pending_online=pending_online
    )


@app.route('/sales/new', methods=['GET', 'POST'])
@login_required
def new_sale():
    if not has_role('admin', 'staff'):
        flash("You are not authorized to record sales.", "error")
        return redirect(dashboard_for(current_user))

    customer_id = ''
    payment_method = 'cash'
    rows = []

    if request.method == 'POST':
        customer_id = request.form.get('customer_id', '').strip()
        payment_method = request.form.get('payment_method', '')
        product_ids = request.form.getlist('product_id')
        quantities = request.form.getlist('quantity')
        rows = [
            {'product_id': raw_id, 'quantity': raw_quantity}
            for raw_id, raw_quantity in zip(product_ids, quantities)
            if raw_id
        ]
        errors = []

        customer = None
        if customer_id:
            try:
                customer = db.session.get(Customer, int(customer_id))
            except (ValueError, TypeError):
                customer = None
            if customer is None:
                errors.append("Select a valid customer or choose walk-in customer.")

        if payment_method not in STAFF_PAYMENT_VALUES:
            errors.append("Choose a payment method.")

        lines = {}
        line_error = False
        for row in rows:
            try:
                product_id = int(row['product_id'])
                quantity = int(row['quantity'])
            except (ValueError, TypeError):
                line_error = True
                break
            if quantity <= 0:
                line_error = True
                break
            lines[product_id] = lines.get(product_id, 0) + quantity

        if line_error:
            errors.append("Enter a valid product and a whole-number quantity on every line.")
        elif not lines:
            errors.append("Add at least one product to the sale.")

        for error in errors:
            flash(error, "error")

        if not errors:
            try:
                sale = Sale(
                    customer_id=customer.id if customer else None,
                    user_id=current_user.id,
                    full_name=customer.full_name if customer else 'Walk-in customer',
                    email=customer.email if customer else None,
                    phone=customer.phone if customer else None,
                    payment_method=payment_method,
                    payment_status='paid',
                    status='completed',
                    total_amount=Decimal('0.00')
                )
                db.session.add(sale)
                db.session.flush()
                sale.order_number = f"SAL-{sale.id:05d}"

                sale_total = Decimal('0.00')

                for product_id, quantity in lines.items():
                    product = db.session.get(Product, product_id)

                    if product is None or product.selling_price is None:
                        raise SaleError("One of the selected products is no longer available.")

                    if quantity > product.quantity:
                        raise SaleError(
                            f"Only {product.quantity} unit(s) of {product.name} in stock."
                        )

                    result = db.session.execute(
                        update(Product)
                        .where(Product.id == product.id, Product.quantity >= quantity)
                        .values(quantity=Product.quantity - quantity)
                    )

                    if result.rowcount == 0:
                        raise SaleError(
                            f"Not enough stock for {product.name}. Please check and try again."
                        )

                    unit_price = product.selling_price
                    subtotal = unit_price * quantity
                    sale_total += subtotal

                    db.session.add(SaleItem(
                        sale_id=sale.id,
                        product_id=product.id,
                        quantity=quantity,
                        unit_price=unit_price,
                        subtotal=subtotal
                    ))

                    db.session.add(StockMovement(
                        product_id=product.id,
                        user_id=current_user.id,
                        movement_type='OUT',
                        quantity=quantity,
                        reason=f"Sale {sale.order_number}"
                    ))

                sale.total_amount = sale_total
                db.session.commit()

                flash(f"Sale {sale.order_number} completed.", "success")
                return redirect(url_for('sale_detail', sale_id=sale.id))

            except SaleError as error:
                db.session.rollback()
                flash(str(error), "error")

            except Exception:
                db.session.rollback()
                app.logger.exception("Failed to record sale")
                flash("Unable to complete the sale. Please try again.", "error")

    return render_template(
        'sales/new.html',
        customers=Customer.query.order_by(Customer.full_name.asc()).all(),
        products_data=sale_form_products(),
        rows=rows,
        customer_id=customer_id,
        payment_method=payment_method,
        payment_methods=STAFF_PAYMENT_METHODS
    )


@app.route('/sales/<int:sale_id>')
@login_required
def sale_detail(sale_id):
    if not has_role('admin', 'staff'):
        flash("You are not authorized to view sales.", "error")
        return redirect(dashboard_for(current_user))

    sale = db.session.get(Sale, sale_id)

    if sale is None:
        flash("Sale not found.", "error")
        return redirect(url_for('sales'))

    return render_template(
        'sales/detail.html',
        sale=sale,
        payment_label=SALE_PAYMENT_LABELS.get(sale.payment_method, sale.payment_method or 'N/A')
    )

@app.route('/sales/<int:sale_id>/action', methods=['POST'])
@login_required
def sale_action(sale_id):
    if not has_role('admin', 'staff'):
        flash("You are not authorized to process orders.", "error")
        return redirect(dashboard_for(current_user))

    sale = db.session.get(Sale, sale_id)

    if sale is None:
        flash("Order not found.", "error")
        return redirect(url_for('sales'))

    if not (sale.order_number or '').startswith('ORD-'):
        flash("Only online orders can be processed here.", "error")
        return redirect(url_for('sale_detail', sale_id=sale.id))

    action = request.form.get('action', '')

    try:
        if action == 'confirm':
            result = db.session.execute(
                update(Sale)
                .where(Sale.id == sale.id, Sale.status == 'pending')
                .values(status='confirmed')
            )
            if result.rowcount == 0:
                raise SaleError("This order can no longer be confirmed.")
            message = f"Order {sale.order_number} confirmed."

        elif action == 'paid':
            result = db.session.execute(
                update(Sale)
                .where(
                    Sale.id == sale.id,
                    Sale.payment_status == 'pending',
                    Sale.status != 'cancelled'
                )
                .values(payment_status='paid')
            )
            if result.rowcount == 0:
                raise SaleError("This payment cannot be marked as paid.")
            message = f"Payment for {sale.order_number} marked as paid."

        elif action == 'deliver':
            if sale.payment_status != 'paid' and sale.payment_method != 'pay_on_delivery':
                raise SaleError("Mark the payment as received before delivering this order.")
            result = db.session.execute(
                update(Sale)
                .where(Sale.id == sale.id, Sale.status == 'confirmed')
                .values(status='delivered', payment_status='paid')
            )
            if result.rowcount == 0:
                raise SaleError("Only a confirmed order can be marked as delivered.")
            message = f"Order {sale.order_number} marked as delivered."

        elif action == 'cancel':
            was_paid = sale.payment_status == 'paid'
            result = db.session.execute(
                update(Sale)
                .where(Sale.id == sale.id, Sale.status.in_(['pending', 'confirmed']))
                .values(status='cancelled')
            )
            if result.rowcount == 0:
                raise SaleError("This order can no longer be cancelled.")

            for item in sale.sale_items:
                db.session.execute(
                    update(Product)
                    .where(Product.id == item.product_id)
                    .values(quantity=Product.quantity + item.quantity)
                )
                db.session.add(StockMovement(
                    product_id=item.product_id,
                    user_id=current_user.id,
                    movement_type='IN',
                    quantity=item.quantity,
                    reason=f"Cancelled order {sale.order_number}"
                ))

            message = f"Order {sale.order_number} cancelled and stock returned."
            if was_paid:
                message += " The payment was already received, so remember to refund the customer."

        else:
            raise SaleError("Invalid action.")

        db.session.commit()
        flash(message, "success")

    except SaleError as error:
        db.session.rollback()
        flash(str(error), "error")

    except Exception:
        db.session.rollback()
        app.logger.exception("Failed to process order")
        flash("Unable to update the order. Please try again.", "error")

    return redirect(url_for('sale_detail', sale_id=sale.id))


@app.route('/reports')
@login_required
def reports():
    if not has_role('admin'):
        flash("You are not authorized to view reports.", "error")
        return redirect(dashboard_for(current_user))

    today = datetime.now(LAGOS).date()

    total_count, total_amount = sales_totals()
    today_count, today_amount = sales_totals(
        Sale.sale_date >= local_day_start_utc(today),
        Sale.sale_date < local_day_start_utc(today + timedelta(days=1))
    )

    return render_template(
        'reports/index.html',
        total_count=total_count,
        total_amount=total_amount,
        today_count=today_count,
        today_amount=today_amount,
        movement_count=StockMovement.query.count(),
        low_stock_count=Product.query.filter(Product.quantity <= LOW_STOCK_LEVEL).count()
    )


@app.route('/reports/sales')
@login_required
def sales_report():
    if not has_role('admin'):
        flash("You are not authorized to view reports.", "error")
        return redirect(dashboard_for(current_user))

    period = request.args.get('period', '30d')
    source = request.args.get('source', 'all')
    raw_from = request.args.get('from', '').strip()
    raw_to = request.args.get('to', '').strip()
    page = request.args.get('page', 1, type=int)

    if period not in REPORT_PERIOD_KEYS:
        period = '30d'

    if source not in REPORT_SOURCE_KEYS:
        source = 'all'

    today = datetime.now(LAGOS).date()
    custom_from = parse_report_date(raw_from)
    custom_to = parse_report_date(raw_to)
    is_custom = bool(custom_from or custom_to)

    if is_custom:
        start_date = custom_from or custom_to
        end_date = custom_to or custom_from
        if end_date < start_date:
            start_date, end_date = end_date, start_date
        range_args = {'from': start_date.isoformat(), 'to': end_date.isoformat()}
    else:
        if period == 'today':
            start_date = today
        elif period == '7d':
            start_date = today - timedelta(days=6)
        elif period == 'month':
            start_date = today.replace(day=1)
        elif period == 'all':
            first_sale = db.session.query(func.min(Sale.sale_date)).scalar()
            start_date = to_local_date(first_sale) if first_sale else today
        else:
            start_date = today - timedelta(days=29)
        end_date = today
        range_args = {'period': period}

    if start_date == end_date:
        range_label = start_date.strftime('%d %b %Y')
    else:
        range_label = f"{start_date.strftime('%d %b %Y')} to {end_date.strftime('%d %b %Y')}"

    source_condition = None
    if source == 'store':
        source_condition = Sale.order_number.like('SAL-%')
    elif source == 'online':
        source_condition = Sale.order_number.like('ORD-%')

    conditions = [
        Sale.sale_date >= local_day_start_utc(start_date),
        Sale.sale_date < local_day_start_utc(end_date + timedelta(days=1))
    ]
    today_conditions = [
        Sale.sale_date >= local_day_start_utc(today),
        Sale.sale_date < local_day_start_utc(today + timedelta(days=1))
    ]

    if source_condition is not None:
        conditions.append(source_condition)
        today_conditions.append(source_condition)

    total_count, total_amount = sales_totals(*conditions)
    today_count, today_amount = sales_totals(*today_conditions)
    cancelled_count = Sale.query.filter(Sale.status == 'cancelled', *conditions).count()

    average_sale = (
        (total_amount / total_count).quantize(Decimal('0.01'))
        if total_count else Decimal('0.00')
    )

    by_day = {}
    for sale_date, amount, order_number in (
        db.session.query(Sale.sale_date, Sale.total_amount, Sale.order_number)
        .filter(Sale.status != 'cancelled', *conditions)
        .all()
    ):
        day = to_local_date(sale_date)
        entry = by_day.setdefault(
            day,
            {'count': 0, 'amount': Decimal('0.00'), 'online': 0, 'store': 0}
        )
        entry['count'] += 1
        entry['amount'] += Decimal(str(amount or 0))
        if (order_number or '').startswith('ORD-'):
            entry['online'] += 1
        else:
            entry['store'] += 1

    day_rows = [{'date': day, **by_day[day]} for day in sorted(by_day, reverse=True)]

    peak = max((row['amount'] for row in day_rows), default=Decimal('0.00'))
    best_day = None
    for row in day_rows:
        row['pct'] = int(row['amount'] / peak * 100) if peak > 0 else 0
        if best_day is None or row['amount'] > best_day['amount']:
            best_day = row

    chart_start = max(start_date, end_date - timedelta(days=29))
    chart_peak = max(
        (entry['amount'] for day, entry in by_day.items() if chart_start <= day <= end_date),
        default=Decimal('0.00')
    )

    chart_days = []
    cursor = chart_start
    while cursor <= end_date:
        entry = by_day.get(cursor)
        amount = entry['amount'] if entry else Decimal('0.00')
        height = max(int(amount / chart_peak * 100), 4) if chart_peak > 0 and amount > 0 else 0
        chart_days.append({
            'date': cursor,
            'amount': amount,
            'count': entry['count'] if entry else 0,
            'height': height
        })
        cursor += timedelta(days=1)

    pagination = (
        Sale.query
        .options(
            joinedload(Sale.customer),
            joinedload(Sale.user),
            selectinload(Sale.sale_items)
        )
        .filter(*conditions)
        .order_by(Sale.sale_date.desc(), Sale.id.desc())
        .paginate(page=page, per_page=15, error_out=False)
    )

    return render_template(
        'reports/sales.html',
        today=today,
        start_date=start_date,
        end_date=end_date,
        range_label=range_label,
        is_custom=is_custom,
        period=period,
        source=source,
        periods=REPORT_PERIODS,
        sources=REPORT_SOURCES,
        range_args=range_args,
        filter_args={**range_args, 'source': source},
        total_count=total_count,
        total_amount=total_amount,
        today_count=today_count,
        today_amount=today_amount,
        cancelled_count=cancelled_count,
        average_sale=average_sale,
        chart_days=chart_days,
        day_rows=day_rows[:60],
        rows_truncated=len(day_rows) > 60,
        best_day=best_day,
        pagination=pagination
    )


Image.MAX_IMAGE_PIXELS = 40000000
AVATAR_SIZE = 400


def avatar_folder():
    folder = os.path.join(app.static_folder, 'uploads', 'avatars')
    os.makedirs(folder, exist_ok=True)
    return folder


def delete_avatar_file(filename):
    if not filename:
        return

    try:
        os.remove(os.path.join(avatar_folder(), os.path.basename(filename)))
    except OSError:
        pass


@app.template_global()
def avatar_url(user):
    filename = getattr(user, 'profile_image', None)

    if not filename:
        return None

    return url_for('static', filename=f'uploads/avatars/{filename}')


@app.errorhandler(413)
def file_too_large(error):
    flash("That image is too large. Please choose one under 10 MB.", "error")
    return redirect(url_for('profile'))


@app.route('/profile/photo', methods=['POST'])
@login_required
def upload_photo():
    user = current_user._get_current_object()
    upload = request.files.get('photo')

    if upload is None or not upload.filename:
        flash("Please choose a photo to upload.", "error")
        return redirect(url_for('profile'))

    try:
        image = Image.open(upload.stream)
        image = ImageOps.exif_transpose(image)
        image = image.convert('RGB')
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        flash("That file is not a supported image. Please choose a JPG or PNG photo.", "error")
        return redirect(url_for('profile'))

    image = ImageOps.fit(image, (AVATAR_SIZE, AVATAR_SIZE), Image.Resampling.LANCZOS)

    prefix = 'customer' if has_role('customer') else 'user'
    filename = f"{prefix}-{user.id}-{secrets.token_hex(8)}.jpg"
    old_filename = user.profile_image

    try:
        image.save(os.path.join(avatar_folder(), filename), 'JPEG', quality=86, optimize=True)
        user.profile_image = filename
        db.session.commit()
    except Exception:
        db.session.rollback()
        delete_avatar_file(filename)
        app.logger.exception("Failed to save profile photo")
        flash("Unable to save your photo. Please try again.", "error")
        return redirect(url_for('profile'))

    delete_avatar_file(old_filename)
    flash("Your profile photo has been updated.", "success")
    return redirect(url_for('profile'))


@app.route('/profile/photo/remove', methods=['POST'])
@login_required
def remove_photo():
    user = current_user._get_current_object()
    old_filename = user.profile_image

    if not old_filename:
        flash("You don't have a profile photo to remove.", "error")
        return redirect(url_for('profile'))

    try:
        user.profile_image = None
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("Failed to remove profile photo")
        flash("Unable to remove your photo. Please try again.", "error")
        return redirect(url_for('profile'))

    delete_avatar_file(old_filename)
    flash("Your profile photo has been removed.", "success")
    return redirect(url_for('profile'))

@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    user = current_user._get_current_object()
    is_customer = has_role('customer')
    is_staff = has_role('staff')
    model = Customer if is_customer else User

    if request.method == 'POST':
        full_name = request.form.get('full_name', '').strip()
        email = request.form.get('email', '').strip().lower()
        phone = re.sub(r'[\s\-().]', '', request.form.get('phone', '').strip()) if is_customer else None
        errors = []

        if len(full_name) < 2 or len(full_name) > 100:
            errors.append("Enter your full name (2 to 100 characters).")

        if not EMAIL_PATTERN.match(email) or len(email) > 120:
            errors.append("Enter a valid email address.")
        elif model.query.filter(func.lower(model.email) == email, model.id != user.id).first():
            errors.append("That email address is already in use.")

        if is_customer:
            if not re.fullmatch(r'\+?\d{10,15}', phone) or len(phone) > 20:
                errors.append("Enter a valid phone number (10 to 15 digits).")
            elif Customer.query.filter(Customer.phone == phone, Customer.id != user.id).first():
                errors.append("That phone number is already in use.")

        if errors:
            for error in errors:
                flash(error, "error")
        else:
            user.full_name = full_name
            user.email = email

            if is_customer:
                user.phone = phone

            try:
                db.session.commit()
                flash("Your profile has been updated.", "success")
                return redirect(url_for('profile'))
            except IntegrityError:
                db.session.rollback()
                flash("That email or phone number is already in use.", "error")
            except Exception:
                db.session.rollback()
                app.logger.exception("Failed to update profile")
                flash("Unable to update your profile. Please try again.", "error")

    name_parts = (user.full_name or '').split()
    initials = ''.join(part[0] for part in name_parts[:2]).upper() or '?'

    if is_customer:
        role_key, role_label = 'customer', 'Customer'
        dashboard_url = url_for('customer_dashboard')
        order_count = Sale.query.filter(
            Sale.customer_id == user.id,
            Sale.order_number.like('ORD-%')
        ).count()
    else:
        role_key = 'staff' if is_staff else 'admin'
        role_label = 'Staff' if is_staff else 'Administrator'
        dashboard_url = dashboard_for(user)
        order_count = 0

    return render_template(
        'account/profile.html',
        is_customer=is_customer,
        is_staff=is_staff,
        role_key=role_key,
        role_label=role_label,
        initials=initials,
        dashboard_url=dashboard_url,
        order_count=order_count,
        staff_id=f"STF-{user.id:04d}" if is_staff else None
    )


@app.route('/profile/password', methods=['POST'])
@login_required
def change_password():
    user = current_user._get_current_object()
    label = 'staff code' if has_role('staff') else 'password'

    current_password = request.form.get('current_password', '')
    new_password = request.form.get('new_password', '')
    confirm_password = request.form.get('confirm_password', '')
    errors = []

    if not check_password_hash(user.password, current_password):
        errors.append(f"Your current {label} is incorrect.")

    if len(new_password) < 8:
        errors.append(f"Your new {label} must be at least 8 characters.")
    elif len(new_password) > 128:
        errors.append(f"Your new {label} is too long.")
    elif new_password == current_password:
        errors.append(f"Your new {label} must be different from the current one.")

    if new_password != confirm_password:
        errors.append(f"The new {label} and its confirmation do not match.")

    if errors:
        for error in errors:
            flash(error, "error")
        return redirect(url_for('profile') + '#security')

    try:
        user.password = generate_password_hash(new_password)
        db.session.commit()
        flash(f"Your {label} has been changed.", "success")
    except Exception:
        db.session.rollback()
        app.logger.exception("Failed to change password")
        flash("Unable to change your password. Please try again.", "error")

    return redirect(url_for('profile') + '#security')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash("You have been logged out successfully.", "success")
    return redirect(url_for('login'))
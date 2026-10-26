from datetime import datetime, timezone

from flask_login import UserMixin

from app import db


def utc_now():
    return datetime.now(timezone.utc)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='staff')
    profile_image = db.Column(db.String(120), nullable=True)

    sales = db.relationship('Sale', back_populates='user', lazy=True)
    stock_movements = db.relationship('StockMovement', back_populates='user', lazy=True)

    def get_id(self):
        return f"user:{self.id}"


class Customer(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    phone = db.Column(db.String(20), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now)
    profile_image = db.Column(db.String(120), nullable=True)
    role = 'customer'

    sales = db.relationship('Sale', back_populates='customer', lazy=True)

    def get_id(self):
        return f"customer:{self.id}"


class Supplier(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    phone = db.Column(db.String(20), unique=True, nullable=False)
    address = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now)
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    products = db.relationship('Product', back_populates='supplier', lazy=True)


class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    description = db.Column(db.String(300), nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    products = db.relationship('Product', back_populates='category', lazy=True)


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    description = db.Column(db.String(300), nullable=True)
    purchase_price = db.Column(db.Numeric(10, 2), nullable=True)
    selling_price = db.Column(db.Numeric(10, 2), nullable=True)
    quantity = db.Column(db.Integer, nullable=False, default=0)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=False)
    supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id'), nullable=False)

    category = db.relationship('Category', back_populates='products')
    supplier = db.relationship('Supplier', back_populates='products')
    sale_items = db.relationship('SaleItem', back_populates='product', lazy=True)
    stock_movements = db.relationship('StockMovement', back_populates='product', lazy=True)


class Sale(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_number = db.Column(db.String(20), unique=True, nullable=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    full_name = db.Column(db.String(100), nullable=True)
    email = db.Column(db.String(120), nullable=True)
    phone = db.Column(db.String(20), nullable=True)
    address = db.Column(db.String(255), nullable=True)
    payment_method = db.Column(db.String(30), nullable=True)
    payment_status = db.Column(db.String(20), nullable=False,default='paid', server_default='paid')
    status = db.Column(db.String(20), nullable=False, default='completed', server_default='completed')
    total_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    sale_date = db.Column(db.DateTime, default=utc_now)

    customer = db.relationship('Customer', back_populates='sales')
    user = db.relationship('User', back_populates='sales')
    sale_items = db.relationship('SaleItem', back_populates='sale', lazy=True, cascade='all, delete-orphan')


class SaleItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    sale_id = db.Column(db.Integer, db.ForeignKey('sale.id'), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=0)
    unit_price = db.Column(db.Numeric(10, 2), nullable=False)
    subtotal = db.Column(db.Numeric(10, 2), nullable=False)

    product = db.relationship('Product', back_populates='sale_items')
    sale = db.relationship('Sale', back_populates='sale_items')


class StockMovement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    movement_type = db.Column(db.String(20), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=0)
    reason = db.Column(db.String(255), nullable=False)
    movement_date = db.Column(db.DateTime, default=utc_now, nullable=False)

    product = db.relationship('Product', back_populates='stock_movements')
    user = db.relationship('User', back_populates='stock_movements')
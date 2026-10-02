from flask import render_template, redirect, flash, url_for
from app import app
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash
from app import app, db
from app.forms import LoginForm
from app.models import User

@app.route('/')
def home():
    return render_template('base.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('admin_dashboard'))

    form = LoginForm()
    if form.validate_on_submit():
        email = form.email.data
        password = form.password.data

        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password, password):
            if user.role != 'admin':
                flash("You are not authorized to access the admin area", "error")
                return redirect(url_for('login'))
            
            login_user(user)
            flash("Welcome to StockFlow, Admin!", "success")
            return redirect(url_for('admin_dashboard'))
        flash("Invalid email or password.", 'error')
    return render_template('login.html', form=form)

@app.route('/admin_dashboard')
@login_required
def admin_dashboard():
    if current_user.role != 'admin':
        flash("You are not authorized to access the admin area", "error")
        return redirect(url_for('login'))
    return render_template('admin/dashboard.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out successfully.', 'success')
    return redirect(url_for('login'))
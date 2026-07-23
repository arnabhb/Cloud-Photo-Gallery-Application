"""SQL-backed Flask implementation of the AWS photo gallery"""

import io
import json
import os
import re
import uuid
from datetime import datetime
from functools import wraps

import boto3
import exifread
import pymysql
from botocore.exceptions import ClientError
from flask import Flask, flash, jsonify, make_response, redirect, render_template, request, session, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pytz import timezone
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__, static_folder="../static", static_url_path="/static")
app.secret_key = os.environ.get("SQL_FLASK_KEY", "change-this-development-key")

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
PHOTO_BUCKET = os.environ.get("PHOTOGALLERY_S3_BUCKET_NAME", "")
SES_EMAIL_SOURCE = os.environ.get("SES_EMAIL_SOURCE", "")
PUBLIC_BASE_URL = os.environ.get("SQL_PUBLIC_BASE_URL", "http://localhost:5000")
RDS_DB_HOSTNAME = os.environ.get("RDS_DB_HOSTNAME", "")
RDS_DB_USERNAME = os.environ.get("RDS_DB_USERNAME", "")
RDS_DB_PASSWORD = os.environ.get("RDS_DB_PASSWORD", "")
RDS_DB_NAME = os.environ.get("RDS_DB_NAME", "photogallerydb")

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg"}
serializer = URLSafeTimedSerializer(app.secret_key)
s3 = boto3.client("s3", region_name=AWS_REGION)
ses = boto3.client("ses", region_name=AWS_REGION)


# Authentication and shared helpers
def login_required(view):
    """Require an authenticated session before running a route"""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login_page"))
        return view(*args, **kwargs)
    return wrapped


def get_database_connection():
    """Create a dictionary-based MySQL connection"""
    return pymysql.connect(host=RDS_DB_HOSTNAME, user=RDS_DB_USERNAME, password=RDS_DB_PASSWORD, database=RDS_DB_NAME, charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor, autocommit=False)


def allowed_file(filename):
    """Validate uploaded image extensions"""
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def normalize_upload(file_storage, identifier):
    """Create a collision-resistant S3 object name while preserving the extension"""
    original = secure_filename(file_storage.filename or "")
    extension = original.rsplit(".", 1)[1].lower()
    return f"{identifier}.{extension}"


def upload_to_s3(file_storage, object_key):
    """Upload a file through the AWS credential chain and return its public URL"""
    file_storage.stream.seek(0)
    s3.upload_fileobj(file_storage.stream, PHOTO_BUCKET, object_key, ExtraArgs={"ContentType": file_storage.mimetype or "application/octet-stream"})
    return f"https://{PHOTO_BUCKET}.s3.{AWS_REGION}.amazonaws.com/{object_key}"


def delete_s3_url(url):
    """Delete an S3 object represented by a stored public URL"""
    if url:
        key = url.split(".amazonaws.com/", 1)[-1]
        s3.delete_object(Bucket=PHOTO_BUCKET, Key=key)


def extract_exif(file_storage):
    """Extract serializable EXIF values without writing the upload to disk"""
    file_storage.stream.seek(0)
    tags = exifread.process_file(file_storage.stream, details=False)
    file_storage.stream.seek(0)
    excluded = {"JPEGThumbnail", "TIFFThumbnail", "Filename", "EXIF MakerNote"}
    return {str(key): str(value) for key, value in tags.items() if key not in excluded}


def generate_user_id(email):
    """Create a deterministic identifier compatible with the original database design"""
    return "".join(str(ord(char)) for char in email.strip().lower())


def generate_token(email):
    return serializer.dumps(email, salt="email-confirmation-salt")


def validate_token(token):
    try:
        return serializer.loads(token, salt="email-confirmation-salt", max_age=3600)
    except (SignatureExpired, BadSignature):
        return None


def send_confirmation_email(name, email, token):
    """Send a confirmation email when SES is configured"""
    confirmation_url = f"{PUBLIC_BASE_URL.rstrip('/')}/confirm/{token}"
    if not SES_EMAIL_SOURCE:
        app.logger.warning("SES_EMAIL_SOURCE is not configured Confirmation URL: %s", confirmation_url)
        return
    ses.send_email(Source=SES_EMAIL_SOURCE, Destination={"ToAddresses": [email]}, Message={"Subject": {"Data": "Photo Gallery account confirmation"}, "Body": {"Text": {"Data": f"Hi {name}\n\nConfirm your account within one hour:\n{confirmation_url}"}}})


def format_utc(value):
    """Convert stored UTC timestamps to a readable Eastern time date"""
    if not value:
        return ""
    parsed = value if isinstance(value, datetime) else datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    return timezone("UTC").localize(parsed).astimezone(timezone("US/Eastern")).strftime("%B %d, %Y")


# Error handlers and application routes
@app.errorhandler(400)
def bad_request(_error):
    return make_response(jsonify({"error": "Bad request"}), 400)


@app.errorhandler(404)
def not_found(_error):
    return make_response(jsonify({"error": "Not found"}), 404)


@app.route("/", methods=["GET", "POST"])
@login_required
def home_page():
    if request.method == "POST":
        with get_database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT photoURL FROM Photo WHERE userID=%s", (session["user_id"],))
                for photo in cursor.fetchall(): delete_s3_url(photo["photoURL"])
                cursor.execute("SELECT thumbnailURL FROM Album WHERE userID=%s", (session["user_id"],))
                for album in cursor.fetchall(): delete_s3_url(album["thumbnailURL"])
                cursor.execute("DELETE FROM Photo WHERE userID=%s", (session["user_id"],))
                cursor.execute("DELETE FROM Album WHERE userID=%s", (session["user_id"],))
                cursor.execute("DELETE FROM User WHERE userID=%s", (session["user_id"],))
            connection.commit()
        session.clear()
        flash("Account deleted", "success")
        return redirect(url_for("login_page"))

    with get_database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT albumID,name,description,thumbnailURL,user,createdAt FROM Album ORDER BY createdAt DESC")
            albums = cursor.fetchall()
    for album in albums: album["createdAt"] = format_utc(album["createdAt"])
    return render_template("index.html", albums=albums, user_name=session["name"])


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        with get_database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT userID,name,password,verified FROM User WHERE email=%s", (email,))
                user = cursor.fetchone()
        if not user or not user["verified"] or not check_password_hash(user["password"], password):
            return render_template("login.html", error="Invalid credentials or unverified account")
        session["user_id"], session["name"] = user["userID"], user["name"]
        return redirect(url_for("home_page"))
    return render_template("login.html")


@app.route("/logout")
def logout_page():
    session.clear()
    return redirect(url_for("login_page"))


@app.route("/signup", methods=["GET", "POST"])
def signup_page():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if password != request.form.get("password1", ""):
            return render_template("signup.html", error="Passwords do not match")
        if len(password) < 8:
            return render_template("signup.html", error="Password must contain at least eight characters")
        with get_database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM User WHERE email=%s", (email,))
                if cursor.fetchone(): return render_template("signup.html", error="This email is already registered")
                cursor.execute("INSERT INTO User (userID,email,name,password,verified) VALUES (%s,%s,%s,%s,%s)", (generate_user_id(email), email, name, generate_password_hash(password), False))
            connection.commit()
        token = generate_token(email)
        send_confirmation_email(name, email, token)
        flash("Account created Check the confirmation email or application logs", "success")
        return redirect(url_for("login_page"))
    return render_template("signup.html")


@app.route("/confirm/<token>")
def confirm_email(token):
    email = validate_token(token)
    if not email: return "Invalid or expired token", 400
    with get_database_connection() as connection:
        with connection.cursor() as cursor: cursor.execute("UPDATE User SET verified=1 WHERE email=%s", (email,))
        connection.commit()
    return "Email verified You may now log in"


@app.route("/createAlbum", methods=["GET", "POST"])
@login_required
def add_album():
    if request.method == "POST":
        image = request.files.get("imagefile")
        if not image or not allowed_file(image.filename):
            flash("Choose a PNG or JPEG thumbnail", "danger"); return redirect(request.url)
        album_id = str(uuid.uuid4())
        key = f"thumbnails/{normalize_upload(image, album_id)}"
        url = upload_to_s3(image, key)
        with get_database_connection() as connection:
            with connection.cursor() as cursor: cursor.execute("INSERT INTO Album (albumID,name,description,thumbnailURL,user,userID) VALUES (%s,%s,%s,%s,%s,%s)", (album_id, request.form.get("name", "").strip(), request.form.get("description", "").strip(), url, session["name"], session["user_id"]))
            connection.commit()
        return redirect(url_for("home_page"))
    return render_template("albumForm.html")


def get_owned_album(cursor, album_id):
    cursor.execute("SELECT * FROM Album WHERE albumID=%s", (album_id,))
    album = cursor.fetchone()
    if not album: return None
    return album


@app.route("/album/<string:album_id>", methods=["GET", "POST"])
@login_required
def view_photos(album_id):
    with get_database_connection() as connection:
        with connection.cursor() as cursor:
            album = get_owned_album(cursor, album_id)
            if not album: return "Album not found", 404
            if request.method == "POST":
                if album["userID"] != session["user_id"]: return "Forbidden", 403
                cursor.execute("SELECT photoURL FROM Photo WHERE albumID=%s", (album_id,))
                for photo in cursor.fetchall(): delete_s3_url(photo["photoURL"])
                delete_s3_url(album["thumbnailURL"])
                cursor.execute("DELETE FROM Photo WHERE albumID=%s", (album_id,))
                cursor.execute("DELETE FROM Album WHERE albumID=%s", (album_id,))
                connection.commit(); return redirect(url_for("home_page"))
            cursor.execute("SELECT photoID,albumID,title,description,photoURL,user FROM Photo WHERE albumID=%s ORDER BY createdAt DESC", (album_id,))
            photos = cursor.fetchall()
    return render_template("viewphotos.html", photos=photos, albumID=album_id, albumName=album["name"])


@app.route("/album/<string:album_id>/addPhoto", methods=["GET", "POST"])
@login_required
def add_photo(album_id):
    with get_database_connection() as connection:
        with connection.cursor() as cursor: album = get_owned_album(cursor, album_id)
    if not album: return "Album not found", 404
    if album["userID"] != session["user_id"]: return "Forbidden", 403
    if request.method == "POST":
        image = request.files.get("imagefile")
        if not image or not allowed_file(image.filename): flash("Choose a PNG or JPEG image", "danger"); return redirect(request.url)
        photo_id = str(uuid.uuid4())
        exif_data = extract_exif(image)
        key = f"photos/{normalize_upload(image, photo_id)}"
        url = upload_to_s3(image, key)
        with get_database_connection() as connection:
            with connection.cursor() as cursor: cursor.execute("INSERT INTO Photo (photoID,albumID,title,description,tags,photoURL,EXIF,user,userID) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", (photo_id, album_id, request.form.get("title", "").strip(), request.form.get("description", "").strip(), request.form.get("tags", "").strip(), url, json.dumps(exif_data), session["name"], session["user_id"]))
            connection.commit()
        return redirect(url_for("view_photos", album_id=album_id))
    return render_template("photoForm.html", albumID=album_id, albumName=album["name"])


@app.route("/album/<string:album_id>/photo/<string:photo_id>", methods=["GET", "POST"])
@login_required
def view_photo(album_id, photo_id):
    with get_database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM Album WHERE albumID=%s", (album_id,)); album = cursor.fetchone()
            cursor.execute("SELECT * FROM Photo WHERE albumID=%s AND photoID=%s", (album_id, photo_id)); photo = cursor.fetchone()
            if not album or not photo: return render_template("photodetail.html", photo={}, tags=[], exifdata={}, albumID=album_id, albumName=""), 404
            if request.method == "POST":
                if photo["userID"] != session["user_id"]: return "Forbidden", 403
                delete_s3_url(photo["photoURL"]); cursor.execute("DELETE FROM Photo WHERE photoID=%s AND albumID=%s", (photo_id, album_id)); connection.commit(); return redirect(url_for("view_photos", album_id=album_id))
    photo["createdAt"], photo["updatedAt"] = format_utc(photo["createdAt"]), format_utc(photo["updatedAt"])
    exif = json.loads(photo.get("EXIF") or "{}")
    tags = [tag.strip() for tag in (photo.get("tags") or "").split(",") if tag.strip()]
    return render_template("photodetail.html", photo=photo, tags=tags, exifdata=exif, albumID=album_id, albumName=album["name"])


@app.route("/album/search")
@login_required
def search_album_page():
    query = request.args.get("query", "").strip()
    pattern = f"%{query}%"
    with get_database_connection() as connection:
        with connection.cursor() as cursor: cursor.execute("SELECT albumID,name,description,thumbnailURL FROM Album WHERE name LIKE %s OR description LIKE %s", (pattern, pattern)); albums = cursor.fetchall()
    return render_template("searchAlbum.html", albums=albums, searchquery=query)


@app.route("/album/<string:album_id>/search")
@login_required
def search_photo_page(album_id):
    query = request.args.get("query", "").strip(); pattern = f"%{query}%"
    with get_database_connection() as connection:
        with connection.cursor() as cursor: cursor.execute("SELECT photoID,albumID,title,description,photoURL FROM Photo WHERE albumID=%s AND (title LIKE %s OR description LIKE %s OR tags LIKE %s OR EXIF LIKE %s)", (album_id, pattern, pattern, pattern, pattern)); photos = cursor.fetchall()
    return render_template("searchPhoto.html", photos=photos, searchquery=query, albumID=album_id)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=os.environ.get("FLASK_DEBUG") == "1")

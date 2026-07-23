"""DynamoDB-backed Flask implementation of the AWS photo gallery"""

import json
import os
import uuid
from datetime import datetime
from functools import wraps

import boto3
import exifread
import pytz
from boto3.dynamodb.conditions import Attr, Key
from flask import Flask, flash, jsonify, make_response, redirect, render_template, request, session, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__, static_folder="../static", static_url_path="/static")
app.secret_key = os.environ.get("NOSQL_FLASK_KEY", "change-this-development-key")

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
PHOTO_BUCKET = os.environ.get("PHOTOGALLERY_S3_BUCKET_NAME", "")
PHOTO_TABLE_NAME = os.environ.get("DYNAMODB_TABLE", "photogallerydb")
USER_TABLE_NAME = os.environ.get("DYNAMODB_TABLE_USERS", "photogalleryusers")
SES_EMAIL_SOURCE = os.environ.get("SES_EMAIL_SOURCE", "")
PUBLIC_BASE_URL = os.environ.get("NOSQL_PUBLIC_BASE_URL", "http://localhost:5001")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg"}

serializer = URLSafeTimedSerializer(app.secret_key)
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
photo_table = dynamodb.Table(PHOTO_TABLE_NAME)
user_table = dynamodb.Table(USER_TABLE_NAME)
s3 = boto3.client("s3", region_name=AWS_REGION)
ses = boto3.client("ses", region_name=AWS_REGION)


# Authentication and shared helpers
def login_required(view):
    """Require an authenticated session before running a route"""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session: return redirect(url_for("login_page"))
        return view(*args, **kwargs)
    return wrapped


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def normalize_upload(file_storage, identifier):
    original = secure_filename(file_storage.filename or "")
    return f"{identifier}.{original.rsplit('.', 1)[1].lower()}"


def upload_to_s3(file_storage, object_key):
    file_storage.stream.seek(0)
    s3.upload_fileobj(file_storage.stream, PHOTO_BUCKET, object_key, ExtraArgs={"ContentType": file_storage.mimetype or "application/octet-stream"})
    return f"https://{PHOTO_BUCKET}.s3.{AWS_REGION}.amazonaws.com/{object_key}"


def delete_s3_url(url):
    if url:
        key = url.split(".amazonaws.com/", 1)[-1]
        s3.delete_object(Bucket=PHOTO_BUCKET, Key=key)


def extract_exif(file_storage):
    file_storage.stream.seek(0)
    tags = exifread.process_file(file_storage.stream, details=False)
    file_storage.stream.seek(0)
    excluded = {"JPEGThumbnail", "TIFFThumbnail", "Filename", "EXIF MakerNote"}
    return {str(key): str(value) for key, value in tags.items() if key not in excluded}


def generate_user_id(email):
    return "".join(str(ord(char)) for char in email.strip().lower())


def generate_token(email):
    return serializer.dumps(email, salt="email-confirmation-salt")


def validate_token(token):
    try: return serializer.loads(token, salt="email-confirmation-salt", max_age=3600)
    except (SignatureExpired, BadSignature): return None


def send_confirmation_email(name, email, token):
    confirmation_url = f"{PUBLIC_BASE_URL.rstrip('/')}/confirm/{token}"
    if not SES_EMAIL_SOURCE:
        app.logger.warning("SES_EMAIL_SOURCE is not configured Confirmation URL: %s", confirmation_url); return
    ses.send_email(Source=SES_EMAIL_SOURCE, Destination={"ToAddresses": [email]}, Message={"Subject": {"Data": "Photo Gallery account confirmation"}, "Body": {"Text": {"Data": f"Hi {name}\n\nConfirm your account within one hour:\n{confirmation_url}"}}})


def scan_all(table, **kwargs):
    """Read every page returned by a DynamoDB scan"""
    items = []
    while True:
        response = table.scan(**kwargs); items.extend(response.get("Items", []))
        key = response.get("LastEvaluatedKey")
        if not key: return items
        kwargs["ExclusiveStartKey"] = key


def format_utc(value):
    parsed = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    return pytz.utc.localize(parsed).astimezone(pytz.timezone("US/Eastern")).strftime("%B %d, %Y")


def get_album(album_id):
    return photo_table.get_item(Key={"albumID": album_id, "photoID": "thumbnail"}).get("Item")


def get_album_photos(album_id):
    """Query every item in an album and exclude the thumbnail metadata record"""
    response = photo_table.query(KeyConditionExpression=Key("albumID").eq(album_id))
    return [item for item in response.get("Items", []) if item.get("photoID") != "thumbnail"]


def delete_items(items, delete_album=False):
    for item in items:
        delete_s3_url(item.get("photoURL"))
        photo_table.delete_item(Key={"albumID": item["albumID"], "photoID": item["photoID"]})
    if delete_album and items is not None:
        pass


# Error handlers and application routes
@app.errorhandler(400)
def bad_request(_error): return make_response(jsonify({"error": "Bad request"}), 400)


@app.errorhandler(404)
def not_found(_error): return make_response(jsonify({"error": "Not found"}), 404)


@app.route("/", methods=["GET", "POST"])
@login_required
def home_page():
    if request.method == "POST":
        owned = scan_all(photo_table, FilterExpression=Attr("user_id").eq(session["user_id"]))
        for item in owned:
            delete_s3_url(item.get("photoURL") or item.get("thumbnailURL"))
            photo_table.delete_item(Key={"albumID": item["albumID"], "photoID": item["photoID"]})
        user_table.delete_item(Key={"UserID": session["user_id"]})
        session.clear(); flash("Account deleted", "success"); return redirect(url_for("login_page"))
    albums = scan_all(photo_table, FilterExpression=Attr("photoID").eq("thumbnail"))
    for album in albums: album["createdAt"] = format_utc(album["createdAt"])
    albums.sort(key=lambda item: item.get("createdAt", ""), reverse=True)
    return render_template("index.html", albums=albums, user_name=session["name"])


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower(); password = request.form.get("password", "")
        user = user_table.get_item(Key={"UserID": generate_user_id(email)}).get("Item")
        if not user or not user.get("verified") or not check_password_hash(user["password"], password): return render_template("login.html", error="Invalid credentials or unverified account")
        session["user_id"], session["name"] = user["UserID"], user["name"]
        return redirect(url_for("home_page"))
    return render_template("login.html")


@app.route("/logout")
def logout_page(): session.clear(); return redirect(url_for("login_page"))


@app.route("/signup", methods=["GET", "POST"])
def signup_page():
    if request.method == "POST":
        name = request.form.get("name", "").strip(); email = request.form.get("email", "").strip().lower(); password = request.form.get("password", "")
        if password != request.form.get("password1", ""): return render_template("signup.html", error="Passwords do not match")
        if len(password) < 8: return render_template("signup.html", error="Password must contain at least eight characters")
        user_id = generate_user_id(email)
        if user_table.get_item(Key={"UserID": user_id}).get("Item"): return render_template("signup.html", error="This email is already registered")
        user_table.put_item(Item={"UserID": user_id, "email": email, "name": name, "password": generate_password_hash(password), "verified": False})
        send_confirmation_email(name, email, generate_token(email)); flash("Account created Check the confirmation email or application logs", "success")
        return redirect(url_for("login_page"))
    return render_template("signup.html")


@app.route("/confirm/<token>")
def confirm_email(token):
    email = validate_token(token)
    if not email: return "Invalid or expired token", 400
    user_table.update_item(Key={"UserID": generate_user_id(email)}, UpdateExpression="SET verified=:value", ExpressionAttributeValues={":value": True})
    return "Email verified You may now log in"


@app.route("/createAlbum", methods=["GET", "POST"])
@login_required
def add_album():
    if request.method == "POST":
        image = request.files.get("imagefile")
        if not image or not allowed_file(image.filename): flash("Choose a PNG or JPEG thumbnail", "danger"); return redirect(request.url)
        album_id = str(uuid.uuid4()); key = f"thumbnails/{normalize_upload(image, album_id)}"; url = upload_to_s3(image, key)
        photo_table.put_item(Item={"albumID": album_id, "photoID": "thumbnail", "name": request.form.get("name", "").strip(), "description": request.form.get("description", "").strip(), "thumbnailURL": url, "user": session["name"], "user_id": session["user_id"], "createdAt": datetime.now(pytz.utc).strftime("%Y-%m-%d %H:%M:%S")})
        return redirect(url_for("home_page"))
    return render_template("albumForm.html")


@app.route("/album/<string:album_id>", methods=["GET", "POST"])
@login_required
def view_photos(album_id):
    album = get_album(album_id)
    if not album: return "Album not found", 404
    if request.method == "POST":
        if album.get("user_id") != session["user_id"]: return "Forbidden", 403
        for photo in get_album_photos(album_id): delete_s3_url(photo.get("photoURL")); photo_table.delete_item(Key={"albumID": album_id, "photoID": photo["photoID"]})
        delete_s3_url(album.get("thumbnailURL")); photo_table.delete_item(Key={"albumID": album_id, "photoID": "thumbnail"})
        return redirect(url_for("home_page"))
    return render_template("viewphotos.html", photos=get_album_photos(album_id), albumID=album_id, albumName=album["name"])


@app.route("/album/<string:album_id>/addPhoto", methods=["GET", "POST"])
@login_required
def add_photo(album_id):
    album = get_album(album_id)
    if not album: return "Album not found", 404
    if album.get("user_id") != session["user_id"]: return "Forbidden", 403
    if request.method == "POST":
        image = request.files.get("imagefile")
        if not image or not allowed_file(image.filename): flash("Choose a PNG or JPEG image", "danger"); return redirect(request.url)
        photo_id = str(uuid.uuid4()); exif = extract_exif(image); key = f"photos/{normalize_upload(image, photo_id)}"; url = upload_to_s3(image, key); now = datetime.now(pytz.utc).strftime("%Y-%m-%d %H:%M:%S")
        photo_table.put_item(Item={"albumID": album_id, "photoID": photo_id, "title": request.form.get("title", "").strip(), "description": request.form.get("description", "").strip(), "tags": request.form.get("tags", "").strip(), "photoURL": url, "EXIF": json.dumps(exif), "user": session["name"], "user_id": session["user_id"], "createdAt": now, "updatedAt": now})
        return redirect(url_for("view_photos", album_id=album_id))
    return render_template("photoForm.html", albumID=album_id, albumName=album["name"])


@app.route("/album/<string:album_id>/photo/<string:photo_id>", methods=["GET", "POST"])
@login_required
def view_photo(album_id, photo_id):
    album = get_album(album_id); photo = photo_table.get_item(Key={"albumID": album_id, "photoID": photo_id}).get("Item")
    if not album or not photo: return render_template("photodetail.html", photo={}, tags=[], exifdata={}, albumID=album_id, albumName=""), 404
    if request.method == "POST":
        if photo.get("user_id") != session["user_id"]: return "Forbidden", 403
        delete_s3_url(photo.get("photoURL")); photo_table.delete_item(Key={"albumID": album_id, "photoID": photo_id}); return redirect(url_for("view_photos", album_id=album_id))
    photo["createdAt"], photo["updatedAt"] = format_utc(photo["createdAt"]), format_utc(photo["updatedAt"])
    tags = [tag.strip() for tag in photo.get("tags", "").split(",") if tag.strip()]
    return render_template("photodetail.html", photo=photo, tags=tags, exifdata=json.loads(photo.get("EXIF") or "{}"), albumID=album_id, albumName=album["name"])


@app.route("/album/search")
@login_required
def search_album_page():
    query = request.args.get("query", "").strip()
    albums = scan_all(photo_table, FilterExpression=Attr("photoID").eq("thumbnail") & (Attr("name").contains(query) | Attr("description").contains(query)))
    return render_template("searchAlbum.html", albums=albums, searchquery=query)


@app.route("/album/<string:album_id>/search")
@login_required
def search_photo_page(album_id):
    query = request.args.get("query", "").strip()
    photos = scan_all(photo_table, FilterExpression=Attr("albumID").eq(album_id) & Attr("photoID").ne("thumbnail") & (Attr("title").contains(query) | Attr("description").contains(query) | Attr("tags").contains(query) | Attr("EXIF").contains(query)))
    return render_template("searchPhoto.html", photos=photos, searchquery=query, albumID=album_id)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=os.environ.get("FLASK_DEBUG") == "1")

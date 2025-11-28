import hashlib
import json
import random
import os
import uuid
from flask import Flask, url_for, render_template, send_from_directory, send_file, abort, request

app = Flask(__name__)
app.config.from_file("./config.json", load=json.load)
app.config.from_file("./.config_secret.json", load=json.load)

CAPTCHA = ['kala', 'kasi', 'kili', 'kiwen', 'len', 'lipu', 'luka', 'mani', 'mun', 'noka', 'pan', 'pipi', 'poki', 'soweli', 'tomo', 'waso']

def compute_challenge_hash(session_id, image_id):
	m = hashlib.sha256()
	m.update(session_id.encode())
	m.update(image_id.encode())
	m.update(app.config["SALT"].encode())
	return m.hexdigest()

@app.route('/', methods=['GET', 'POST'])
def form():
	error_message = {}
	available_us={"ilonena": 3}
	available={"ilonena": 1}

	if request.method == 'POST':
		if not (request.form.get('recipient') and request.form.get('line1') and request.form.get('city') and request.form.get('country') and request.form.get('warehouse')):
			error_message["address"] = "nimi \"*\" li lon la pana e ona!"

		if not request.form.get('contact'):
			error_message["contact"] = "o pana e nasin toki tawa mi!"

		if sum([int(request.form.get(i)) for i in app.config["LISTING"] if request.form.get(i, "").isnumeric()]) == 0:
			error_message["items"] = "o esun e ijo!"

		warehouse = request.form.get('warehouse')
		for k,v in app.config["LISTING"].items():
			available_quantity = available.get(k, 0)
			if request.form.get('warehouse') == "US":
				available_quantity += available_us.get(k, 0)
			if request.form.get(k, "").isnumeric() and int(request.form.get(k)) > available_quantity:
				error_message["items"] = "sina wile e ijo pi lon ala!"

		if request.form.get("mama") != "Sonja" or request.form.get("challenge") != compute_challenge_hash(request.form.get("session_id", ""), request.form.get("sitelen", "")):
			error_message["captcha"] = "sina toki e ijo ike! o toki pona!"

		if not error_message:
			pass # all good! Redirect to /lukin/<token>!

	session_id = str(uuid.uuid4())
	challenge = compute_challenge_hash(session_id, random.choice(CAPTCHA))
	return render_template('form.html',
			session_id=session_id,
			challenge=challenge,
			error_message=error_message,
			listing=app.config["LISTING"],
			available_us=available_us,
			available=available,
	)

@app.route('/lukin/<token>')
def view():
	return "<p>Hello, World!</p>"

@app.route('/sitelen/<session_id>/<challenge>')
def captcha(session_id, challenge):
	for i in CAPTCHA:
		if challenge == compute_challenge_hash(session_id, i):
			return send_from_directory(os.path.join(app.root_path, "captcha"),
										f"{i}.jpg", mimetype="image/jpeg", download_name="sitelen.jpg")
	abort(404)

@app.route('/lawa')
def admin():
	return "<p>Hello, World!</p>"

@app.route('/favicon.ico')
def favicon():
	return send_from_directory(os.path.join(app.root_path, "static"),
								"favicon.ico", mimetype="image/vnd.microsoft.icon")

@app.route('/robots.txt')
def robots():
	return send_from_directory(app.root_path, "robots.txt", mimetype="text/plain")

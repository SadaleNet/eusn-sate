import datetime
import hashlib
import json
import random
import sqlite3
import os
import time
import uuid
from flask import Flask, url_for, render_template, send_from_directory, send_file, abort, request, redirect, g, Response

app = Flask(__name__)
app.config.from_file("./config.json", load=json.load)
app.config.from_file("./.config_secret.json", load=json.load)

STATUS_MAP = {-1: "moli", 0: "sin", 1: "pana-mani", 2: "lukin-mani", 3: "tawa-ma-US", 4: "lon-ma-US", 5: "tawa-tomo", 6: "pini"}
STATUS_DESCRIPTION_MAP = {
	-1: "esun ni li kama moli.",
	0: "esun ni li kama lon.",
	1: "sina toki e ni: sina pini pana e mani. mi wile lukin lon tenpo kama.",
	2: "mi lukin e ni: sina pana e mani.",
	3: "mi open pana e poki tawa tomo pi jan pona Mewika mi.",
	4: "poki li lon tomo pi jan pona Mewika mi.",
	5: "poki li open tawa tomo sina.",
	6: "sina jo e poki. esun ni li pini."}


CAPTCHA = ['kala', 'kasi', 'kili', 'kiwen', 'len', 'lipu', 'luka', 'mani', 'mun', 'noka', 'pan', 'pipi', 'poki', 'soweli', 'tomo', 'waso']

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(app.config["DATABASE"])
    return db

def connect_database():
	con = get_db()
	cur = con.cursor()
	cur.execute("""
CREATE TABLE IF NOT EXISTS orders (
	-- rowid INTEGER PRIMARY KEY AUTOINCREMENT, -- Comes with sqlite by default for free!
	session_id TEXT UNIQUE NOT NULL,
	warehouse TEXT NOT NULL,
	address_recipient TEXT NOT NULL,
	address_phone TEXT,
	address_email TEXT,
	address_line1 TEXT NOT NULL,
	address_line2 TEXT,
	address_line3 TEXT,
	address_line4 TEXT,
	address_city TEXT NOT NULL,
	address_zip TEXT,
	address_country TEXT NOT NULL,
	contact TEXT NOT NULL,
	expired INTEGER NOT NULL,
	ip TEXT,
	ref TEXT,
	message TEXT
	);
""")
	cur.execute("""
CREATE TABLE IF NOT EXISTS status_change (
	order_id INTEGER NOT NULL,
	datetime INTEGER NOT NULL,
	status INTEGER NOT NULL,
	FOREIGN KEY(order_id) REFERENCES orders(rowid)
	);
""")
	cur.execute("""
CREATE TABLE IF NOT EXISTS inventory_checkout (
	order_id INTEGER NOT NULL,
	item TEXT NOT NULL,
	quantity INTEGER NOT NULL,
	price_each REAL NOT NULL,
	FOREIGN KEY(order_id) REFERENCES orders(rowid)
	);
""")
	cur.execute("""
CREATE TABLE IF NOT EXISTS inventory_list (
	item TEXT NOT NULL,
	warehouse TEXT NOT NULL,
	quantity INTEGER NOT NULL
	);
""")
	return con

def get_utc_timestr_from_timestamp(timestamp):
	return datetime.datetime.strftime(datetime.datetime.fromtimestamp(timestamp).astimezone(datetime.timezone.utc), '%Y-%m-%d %H:%M:%S UTC')

def get_stale_expiry(cur, order_id):
	cur.execute("SELECT datetime, status FROM status_change WHERE order_id = ? ORDER BY datetime DESC LIMIT 1", (order_id,))
	ret = {"type": None, "datetime": None, "datetime_str": None}
	result = cur.fetchone()
	if result is not None:
		dt = result[0]
		status = result[1]
		if status == 0:
			# the unpaid order becomes dead
			ret["type"] = "stale"
			ret["datetime"] = dt + app.config["PAYMENT_TIMEOUT"]
		elif status == -1:
			ret["type"] = "expiry"
			ret["datetime"] = dt + app.config["DEAD_ORDER_EXPIRY"]
		elif status == 6:
			ret["type"] = "expiry"
			ret["datetime"] = dt + app.config["COMPLETED_ORDER_EXPIRY"]

	if ret["datetime"] is not None:
		ret["datetime_str"] = get_utc_timestr_from_timestamp(ret["datetime"])
	return ret

def compute_stale_and_expiry(cur):
	cur.execute("SELECT rowid FROM orders WHERE expired = FALSE;")
	timenow = time.time()
	for i in cur.fetchall():
		order_id = i[0]
		need_process = True
		while need_process:
			need_process = False
			event = get_stale_expiry(cur, order_id)
			if event["type"] is not None and timenow >= event["datetime"]:
				if event["type"] == "stale":
					cur.execute("INSERT INTO status_change(order_id, datetime, status) VALUES (?,?,?)",
						(order_id, event["datetime"], -1,)
					)
					# Reprocess, just in case stale + expiry would happen at the same time
					need_process = True
				elif event["type"] == "expiry":
					cur.execute("UPDATE orders SET expired=? WHERE rowid = ?", (True, order_id,))
	cur.connection.commit()

def get_available_stock():
	con = connect_database()
	total = {warehouse:{k:0 for k in app.config["LISTING"].keys()} for warehouse in ["US", "ANTE"]}
	available = {warehouse:{k:0 for k in app.config["LISTING"].keys()} for warehouse in ["US", "ANTE"]}

	# Compute the consumed stock here
	cur = con.cursor()
	cur.execute("SELECT item, warehouse, quantity FROM inventory_list")
	for i in cur.fetchall():
		total[i[1]][i[0]] = i[2]
		available[i[1]][i[0]] = i[2]

	# Compute the consumed stock here
	cur.execute("""
SELECT orders.warehouse, item, SUM(inventory_checkout.quantity) FROM inventory_checkout, orders
	WHERE inventory_checkout.order_id = orders.rowid AND (SELECT status FROM status_change WHERE order_id = orders.rowid ORDER BY datetime DESC LIMIT 1) >= 0
	GROUP BY orders.warehouse, inventory_checkout.item;
""")
	for i in cur.fetchall():
		if i[0] in available:
			if i[1] == "pokitawa":
				continue # No availability counter for pokitawa
			available[i[0]][i[1]] -= i[2]

	return {"total_ante": total["ANTE"], "total_us": total["US"],
			"available_ante": available["ANTE"], "available_us": available["US"]}

def get_order_order_id_by_session_id(cur, session_id):
	cur.execute(f"SELECT rowid FROM orders WHERE session_id = ?", (session_id,))
	if cur.rowcount == 0:
		return None
	return cur.fetchone()[0]

def get_status_by_order_id(cur, order_id):
	if order_id is None:
		return None
	status = []
	cur.execute(f"SELECT datetime, status FROM status_change WHERE order_id = ? ORDER BY datetime", (order_id,))
	for status_change in cur.fetchall():
		entry = {"datetime": status_change[0], "status": status_change[1]}
		entry["datetime_str"] = get_utc_timestr_from_timestamp(entry["datetime"])
		entry["status_str"] = STATUS_MAP[entry["status"]] if entry["status"] in STATUS_MAP else ""
		entry["description"] = STATUS_DESCRIPTION_MAP[entry["status"]] if entry["status"] in STATUS_DESCRIPTION_MAP else ""
		status.append(entry)
	return status

def compute_challenge_hash(session_id, image_id):
	m = hashlib.sha256()
	m.update(session_id.encode())
	m.update(image_id.encode())
	m.update(app.config["SALT"].encode())
	return m.hexdigest()

def check_auth():
	return request.headers.get("Host").rsplit(":", 1)[0] == app.config["ADMIN_HOST"] and request.cookies.get("Tracking") == app.config["ADMIN_COOKIES"]

@app.route('/', methods=['GET', 'POST'])
def form():
	post_action = (request.method == 'POST' and not request.form.get('skip-validation'))
	if post_action:
		con = connect_database()
		cur = con.cursor()
		# Use IMMEDIATE TRANSACTION to hold the database lock before obtaining the stock's quantity.
		# This is to make sure whatever stock we've read would still be available by the time we consume it,
		# which is performed by inserting into the inventory_checkout table
		cur.execute("BEGIN IMMEDIATE TRANSACTION")

	stock = get_available_stock()
	available = stock["available_ante"]
	available_us = stock["available_us"]

	error_message = {}

	if post_action:
		if not (request.form.get('recipient') and request.form.get('line1') and request.form.get('city') and request.form.get('country') and request.form.get('warehouse')):
			error_message["address"] = "nimi \"*\" li lon la pana e ona!"

		if not request.form.get('contact'):
			error_message["contact"] = "o pana e nasin toki tawa mi!"

		if sum([int(request.form.get(i)) for i in app.config["LISTING"] if request.form.get(i, "").isnumeric()]) == 0:
			error_message["items"] = "o esun e ijo!"

		warehouse = request.form.get('warehouse')
		if warehouse not in ["US", "ANTE"]:
			error_message["address"] = "ma sina li pakala!"

		for k,v in app.config["LISTING"].items():
			if warehouse == "US":
				available_quantity = available_us.get(k, 0)
			elif warehouse == "ANTE":
				available_quantity = available.get(k, 0)
			if request.form.get(k, "").isnumeric() and int(request.form.get(k)) > available_quantity:
				error_message["items"] = "sina wile e ijo pi lon ala!"

		if request.form.get("mama") != "Sonja" or request.form.get("challenge") != compute_challenge_hash(request.form.get("session_id", ""), request.form.get("sitelen", "")):
			error_message["captcha"] = "sina toki e ijo ike! o toki pona!"

		if not error_message:

			cur.execute("SELECT COUNT(*) FROM orders WHERE session_id = ?", (request.form.get("session_id", ""),))
			if cur.fetchone()[0] == 0:
				# Only perform insertation if session_id hasn't been recorded yet
				# Known issue: If the same form got sent twice at the same time, one of them would fail
				# due to duplicate session_id
				timenow = round(time.time())
				cur.execute("""
					INSERT INTO orders(
						session_id, expired, warehouse, ip, contact,
						address_recipient, address_phone, address_email,
						address_line1, address_line2, address_line3, address_line4,
						address_city, address_zip, address_country
					) VALUES (?,?,?,?,?,  ?,?,?,  ?,?,?,?,  ?,?,?)
					""",
					(request.form.get("session_id", ""), False, request.form.get("warehouse", ""), request.headers.get('X-Real-IP', request.remote_addr), request.form.get('contact'),
					request.form.get("recipient", ""), request.form.get("phone", ""), request.form.get("email", ""), 
					request.form.get("line1", ""), request.form.get("line2", ""), request.form.get("line3", ""), request.form.get("line4", ""),
					request.form.get("city", ""), request.form.get("zip", ""), request.form.get("country", ""),)
				)
				if cur.rowcount == 0:
					abort(500)
				order_id = cur.lastrowid

				cur.execute("INSERT INTO status_change(order_id, datetime, status) VALUES (?,?,?)",
					(order_id, timenow, 0,)
				)
				if cur.rowcount == 0:
					abort(500)

				shipping = 0.0
				for k,v in app.config["LISTING"].items():
					if request.form.get(k, "").isnumeric():
						quantity_of_item = int(request.form.get(k, ""))
						cur.execute("INSERT INTO inventory_checkout(order_id, item, quantity, price_each) VALUES (?,?,?,?)",
							(order_id, k, quantity_of_item, v["price"],)
						)
						if cur.rowcount == 0:
							abort(500)
						shipping += v["shipping"][warehouse] * quantity_of_item
				cur.execute("INSERT INTO inventory_checkout(order_id, item, quantity, price_each) VALUES (?,?,?,?)",
					(order_id, "pokitawa", 1, shipping,)
				)
				if cur.rowcount == 0:
					abort(500)

				cur.execute("COMMIT TRANSACTION")

			# all good! Redirect to /lukin/<token>!
			session_id = request.form.get("session_id", "")
			return redirect(f"/lukin/{session_id}", code=302)

	session_id = str(uuid.uuid4()).replace('-', '')[::-1]
	challenge = compute_challenge_hash(session_id, random.choice(CAPTCHA))
	return render_template('form.html',
			session_id=session_id,
			challenge=challenge,
			error_message=error_message,
			listing=app.config["LISTING"],
			available_us=available_us,
			available=available,
	)

@app.route('/sitelen/<session_id>/<challenge>')
def captcha(session_id, challenge):
	for i in CAPTCHA:
		if challenge == compute_challenge_hash(session_id, i):
			return send_from_directory(os.path.join(app.root_path, "captcha"),
										f"{i}.jpg", mimetype="image/jpeg", download_name="sitelen.jpg")
	abort(404)

@app.route('/lukin/<session_id>')
def view(session_id):
	con = connect_database()
	cur = con.cursor()

	compute_stale_and_expiry(cur)
	entry = {}
	fields = ["rowid", "warehouse", "contact", "message",
				"address_recipient", "address_phone", "address_email",
				"address_line1", "address_line2", "address_line3", "address_line4",
				"address_city", "address_zip", "address_country", "expired"]
	cur.execute(f"SELECT {','.join(fields)} FROM orders WHERE session_id = ?", (session_id,))
	if cur.rowcount == 0:
		abort(404)
	for i, v in enumerate(cur.fetchone()):
		entry[fields[i]] = v

	# Do not show expired entries
	if entry["expired"] and not check_auth():
		abort(404)

	status = get_status_by_order_id(cur, entry["rowid"])

	items = []
	cur.execute(f"SELECT item, quantity, price_each FROM inventory_checkout WHERE order_id = ?", (entry["rowid"],))
	shipping_item = None
	for item in cur.fetchall():
		item_dict = {"item": item[0], "quantity": item[1], "price_each": item[2]}
		if item_dict["item"] == "pokitawa":
			shipping_item = item_dict
			shipping_item["name"] = "poki tawa"
			continue
		item_dict["name"] = app.config["LISTING"][item_dict["item"]]["title"]
		items.append(item_dict)
	# Always put the shipping entry the last on the list
	if shipping_item is not None:
		items.append(shipping_item)

	total_price = sum([i["quantity"]*i["price_each"] for i in items])
	return render_template('view.html', session_id=session_id, entry=entry, status=status, items=items, total_price=total_price, paypal_link=app.config["PAYPAL_LINK"], expiry=get_stale_expiry(cur, entry["rowid"]))

@app.route('/lawa')
def admin():
	if not check_auth():
		abort(404)

	con = connect_database()
	cur = con.cursor()
	compute_stale_and_expiry(cur)
	cur.execute("""SELECT session_id, warehouse, expired, ip, ref, message,
		(SELECT status FROM status_change WHERE order_id = orders.rowid ORDER BY datetime DESC LIMIT 1) as current_status,
		(SELECT datetime FROM status_change WHERE order_id = orders.rowid ORDER BY datetime LIMIT 1) as creation_datetime
		FROM orders ORDER BY creation_datetime""")

	orders = []
	for i in cur.fetchall():
		orders.append({"session_id": i[0], "warehouse": i[1], "expired": i[2], "ip": i[3], "ref": i[4], "message": i[5], "status": i[6],
			"datetime": i[7], "datetime_str": get_utc_timestr_from_timestamp(i[7])})
	
	return render_template('admin.html', listing=app.config["LISTING"],  visitor_url=app.config["VISITOR_URL"], stock=get_available_stock(), orders=orders,
		status_map=STATUS_MAP,
		status_title={i:f"{STATUS_MAP[i]}: {STATUS_DESCRIPTION_MAP[i]}" for i in STATUS_MAP},
		)


@app.route('/ante-nanpa-ijo', methods=['POST'])
def update_inventory():
	if not check_auth():
		abort(404)
	con = connect_database()
	cur = con.cursor()
	for key in app.config["LISTING"]:
		for warehouse in ["US", "ANTE"]:
			update_content = (request.form.get(f"{key}_{warehouse}"), key, warehouse)
			cur.execute("UPDATE inventory_list SET quantity = ? WHERE item = ? AND warehouse = ?", update_content)
			if cur.rowcount == 0:
				cur.execute("INSERT INTO inventory_list (quantity, item, warehouse) VALUES (?,?,?)", update_content)
	con.commit()
	return redirect("/lawa", code=302)

@app.route('/ante-e-esun', methods=['POST'])
def update_status():
	if not request.form.get("session_id") or not request.form.get("status"):
		abort(500)

	con = connect_database()
	cur = con.cursor()

	session_id = request.form.get("session_id", "")
	new_status = request.form.get("status", "")

	order_id = get_order_order_id_by_session_id(cur, session_id)
	status = get_status_by_order_id(cur, order_id)

	error = False

	# Only allows changing status if the status's "NEW"
	if status == None or status[-1]["status"] != 0:
		error = True

	# Only allows changing status to either -1 (moli) or 1 (pana-mani)
	if new_status not in ["-1", "1"]:
		error = True

	if not error:
		timenow = round(time.time())
		cur.execute("INSERT INTO status_change (order_id, datetime, status) VALUES (?,?,?)", (order_id, timenow, int(new_status)))
		con.commit()

	# Regardless if there's an error, redirect the user back to the view order page
	return redirect(f"/lukin/{session_id}", code=302)

@app.route('/lawa/ante-e-esun', methods=['POST'])
def update_order():
	if not check_auth():
		abort(404)

	con = connect_database()
	cur = con.cursor()
	if not (request.form.get("session_id") and ("ref" in request.form) and ("expired" in request.form) and ("message" in request.form) and ("status" in request.form)):
		abort(500)

	cur.execute("BEGIN TRANSACTION")
	cur.execute("UPDATE orders SET ref=?,expired=?,message=? WHERE session_id = ?",
		(request.form["ref"] if request.form["ref"] else None,
		request.form["expired"],
		request.form["message"] if request.form["message"] else None,
		request.form["session_id"],)
	)
	order_id = get_order_order_id_by_session_id(cur, request.form["session_id"])
	status = get_status_by_order_id(cur, order_id)
	previous_status = status[-1]["status"]
	new_status = int(request.form["status"])
	if previous_status != new_status:
		timenow = round(time.time())
		# Force insert a lukin-mani state if it's been skipped, with datetime of now minus a second
		if new_status > 2 and 2 not in [s["status"] for s in status]:
			cur.execute("INSERT INTO status_change(order_id, datetime, status) VALUES (?,?,?)",
						(order_id, timenow-1, 2,))
		cur.execute("INSERT INTO status_change(order_id, datetime, status) VALUES (?,?,?)",
					(order_id, timenow, new_status,))
	cur.execute("COMMIT TRANSACTION")

	return redirect(f"/lawa", code=302)

@app.route("/lukin-pana-mani")
def notification_api():
	if not check_auth():
		abort(404)

	con = connect_database()
	cur = con.cursor()
	cur.execute("SELECT (SELECT rowid FROM status_change WHERE order_id = orders.rowid ORDER BY datetime DESC LIMIT 1) as entry_status_rowid FROM orders WHERE (SELECT status FROM status_change WHERE rowid = entry_status_rowid) = 1")
	
	status = cur.fetchall()
	if len(status) > 0:
		result = {"mute": len(status), "toki": f"jan {len(status)} li pana e mani. o lukin a!"}
	else:
		result = {"mute": 0, "toki": f"jan ala li pana e mani"}
	return Response(json.dumps(result), mimetype='application/json')


@app.route('/favicon.ico')
def favicon():
	return send_from_directory(os.path.join(app.root_path, "static"),
								"favicon.ico", mimetype="image/vnd.microsoft.icon")

@app.route('/robots.txt')
def robots():
	return send_from_directory(os.path.join(app.root_path, "static"), "robots.txt", mimetype="text/plain")

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

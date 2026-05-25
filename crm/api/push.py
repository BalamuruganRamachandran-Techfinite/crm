"""Push notification plumbing for the Flutter mobile app.

This module owns:
  - device-token registration (the app posts here on launch + token rotation)
  - the helper that fans out a payload to all of a user's devices

It does NOT own the trigger logic. The Asterisk dialplan or AMI listener
that detects an inbound INVITE and decides "this user is offline, send a
push" is project infrastructure that runs alongside FreePBX — it calls
`fire_incoming_call_push(user, from_number, display_name, call_id)`.
"""

import json

import frappe
from frappe import _
from frappe.utils import now_datetime

try:
	# Optional dependency — only required when push is actually enabled.
	# Install via: pip install firebase-admin
	import firebase_admin
	from firebase_admin import credentials, messaging
	_FIREBASE_AVAILABLE = True
except ImportError:
	_FIREBASE_AVAILABLE = False


# ── Doctype bootstrapping ─────────────────────────────────────────────────────


def _ensure_device_doctype():
	"""Create a minimal CRM Mobile Device doctype on first call.

	Done lazily so this module loads even before bench migrate. Production
	deployments should define this as a proper fixture instead.
	"""
	if frappe.db.exists("DocType", "CRM Mobile Device"):
		return

	doc = frappe.new_doc("DocType")
	doc.update({
		"name": "CRM Mobile Device",
		"module": "FCRM",
		"custom": 1,
		"naming_rule": "Random",
		"autoname": "hash",
		"fields": [
			{"fieldname": "user", "label": "User", "fieldtype": "Link", "options": "User", "reqd": 1, "in_list_view": 1},
			{"fieldname": "platform", "label": "Platform", "fieldtype": "Select", "options": "android\nios", "reqd": 1, "in_list_view": 1},
			{"fieldname": "token", "label": "Token", "fieldtype": "Long Text", "reqd": 1},
			{"fieldname": "last_seen", "label": "Last Seen", "fieldtype": "Datetime"},
		],
		"permissions": [
			{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
			{"role": "Sales Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
			{"role": "Sales User", "read": 1, "write": 1, "create": 1},
		],
	})
	doc.insert(ignore_permissions=True)
	frappe.db.commit()


# ── Whitelisted endpoints ─────────────────────────────────────────────────────


@frappe.whitelist()
def register_device(token: str, platform: str):
	"""Store an FCM/APNs device token for the signed-in user."""
	if not token or not platform:
		frappe.throw(_("token and platform are required"))
	if platform.lower() not in ("android", "ios"):
		frappe.throw(_("platform must be 'android' or 'ios'"))

	_ensure_device_doctype()

	existing = frappe.db.get_value(
		"CRM Mobile Device",
		{"user": frappe.session.user, "token": token},
		"name",
	)
	if existing:
		doc = frappe.get_doc("CRM Mobile Device", existing)
		doc.last_seen = now_datetime()
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc({
			"doctype": "CRM Mobile Device",
			"user": frappe.session.user,
			"token": token,
			"platform": platform.lower(),
			"last_seen": now_datetime(),
		})
		doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True}


@frappe.whitelist()
def unregister_device(token: str):
	"""Remove a device token (call on logout)."""
	_ensure_device_doctype()
	frappe.db.delete("CRM Mobile Device", {"user": frappe.session.user, "token": token})
	frappe.db.commit()
	return {"ok": True}


# ── Sending pushes ────────────────────────────────────────────────────────────


def _init_firebase():
	"""Initialise the firebase-admin SDK once per worker."""
	if not _FIREBASE_AVAILABLE:
		raise RuntimeError("firebase-admin not installed. pip install firebase-admin")
	if firebase_admin._apps:
		return
	# Path to your service-account JSON. Set this in site_config.json:
	#   "firebase_service_account": "/path/to/serviceAccountKey.json"
	sa_path = frappe.conf.get("firebase_service_account")
	if not sa_path:
		raise RuntimeError("firebase_service_account not configured in site_config.json")
	cred = credentials.Certificate(sa_path)
	firebase_admin.initialize_app(cred)


def fire_incoming_call_push(user: str, from_number: str, display_name: str | None, call_id: str):
	"""Send an incoming-call push to every device of `user`.

	Called by the Asterisk → backend bridge when an INVITE arrives and the
	target agent is not currently registered over WSS (i.e. mobile app is
	closed or backgrounded).
	"""
	if not _FIREBASE_AVAILABLE:
		frappe.log_error(title="push: firebase-admin not installed")
		return

	_ensure_device_doctype()
	devices = frappe.get_all(
		"CRM Mobile Device",
		filters={"user": user},
		fields=["token", "platform"],
	)
	if not devices:
		return

	try:
		_init_firebase()
	except Exception as e:
		frappe.log_error(title=f"push: firebase init failed: {e}")
		return

	data = {
		"type": "incoming_call",
		"from": from_number,
		"displayName": display_name or from_number,
		"callId": call_id,
	}

	for d in devices:
		try:
			msg = messaging.Message(
				token=d.token,
				data={k: str(v) for k, v in data.items()},
				android=messaging.AndroidConfig(
					priority="high",
					ttl=30,
				),
				apns=messaging.APNSConfig(
					headers={"apns-priority": "10", "apns-push-type": "voip"},
					payload=messaging.APNSPayload(
						aps=messaging.Aps(
							content_available=True,
							sound="default",
						),
					),
				),
			)
			messaging.send(msg)
		except Exception as e:
			# Token may be invalid (uninstalled / rotated). Log and prune.
			frappe.log_error(title=f"push: send failed for {d.token[:12]}…: {e}")
			frappe.db.delete("CRM Mobile Device", {"token": d.token})

	frappe.db.commit()

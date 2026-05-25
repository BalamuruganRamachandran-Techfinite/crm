"""Mobile-app authentication endpoints.

The Flutter app trades an email+password for an API key/secret pair, then uses
`Authorization: token <key>:<secret>` for all subsequent calls. This avoids
keeping the password on the device and sidesteps cookie/CORS headaches.
"""

import frappe
from frappe import _
from frappe.utils.password import check_password


CRM_ALLOWED_ROLES = {"System Manager", "Sales Manager", "Sales User"}


@frappe.whitelist(allow_guest=True)
def login(email: str, password: str):
	"""Verify credentials and return an API key/secret pair plus the user profile."""
	if not email or not password:
		frappe.throw(_("Email and password are required"), frappe.AuthenticationError)

	user_name = frappe.db.get_value("User", {"email": email}, "name") or email
	try:
		check_password(user_name, password)
	except frappe.AuthenticationError:
		frappe.throw(_("Invalid email or password"), frappe.AuthenticationError)

	user = frappe.get_doc("User", user_name)
	if not user.enabled:
		frappe.throw(_("User is disabled"), frappe.AuthenticationError)

	roles = set(frappe.get_roles(user_name))
	if not roles.intersection(CRM_ALLOWED_ROLES):
		frappe.throw(_("You don't have access to the CRM"), frappe.PermissionError)

	api_key = user.api_key or frappe.generate_hash(length=15)
	api_secret = frappe.generate_hash(length=15)
	user.api_key = api_key
	user.api_secret = api_secret
	user.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"api_key": api_key,
		"api_secret": api_secret,
		"user": {
			"name": user.name,
			"email": user.email,
			"full_name": user.full_name,
			"user_image": user.user_image,
			"roles": list(roles),
			"is_telephony_agent": bool(
				frappe.db.exists("CRM Telephony Agent", {"user": user.name})
			),
		},
	}


@frappe.whitelist()
def me():
	"""Return the authenticated user's profile. Used by the app on launch to verify the stored token."""
	user_name = frappe.session.user
	if user_name == "Guest":
		frappe.throw(_("Not authenticated"), frappe.AuthenticationError)

	user = frappe.get_doc("User", user_name)
	roles = set(frappe.get_roles(user_name))
	return {
		"name": user.name,
		"email": user.email,
		"full_name": user.full_name,
		"user_image": user.user_image,
		"roles": list(roles),
		"is_telephony_agent": bool(
			frappe.db.exists("CRM Telephony Agent", {"user": user.name})
		),
	}


@frappe.whitelist()
def logout():
	"""Rotate the API secret to invalidate the token on this device."""
	user = frappe.get_doc("User", frappe.session.user)
	user.api_secret = frappe.generate_hash(length=15)
	user.save(ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True}

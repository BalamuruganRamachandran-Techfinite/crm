"""Mobile-app specific endpoints that aggregate data for the Flutter UI."""

import frappe
from frappe import _


@frappe.whitelist()
def get_recent_conversations(limit: int = 50):
	"""Return recent WhatsApp conversations grouped by reference doc.

	Each row gives the mobile app what it needs to render a chat-inbox list:
	  - reference_doctype + reference_name (so it knows where to navigate)
	  - the latest message preview + timestamp
	  - the counterparty (from for incoming, to for outgoing)
	"""
	if not frappe.db.exists("DocType", "WhatsApp Message"):
		return []

	rows = frappe.db.sql(
		"""
		SELECT
			m.reference_doctype,
			m.reference_name,
			m.message AS last_message,
			m.type AS last_type,
			m.creation AS last_at,
			m.`from` AS counter_from,
			m.`to` AS counter_to
		FROM `tabWhatsApp Message` m
		INNER JOIN (
			SELECT reference_doctype, reference_name, MAX(creation) AS latest
			FROM `tabWhatsApp Message`
			WHERE reference_doctype IS NOT NULL
			  AND reference_name IS NOT NULL
			  AND reference_name != ''
			GROUP BY reference_doctype, reference_name
		) latest_msg
			ON latest_msg.reference_doctype = m.reference_doctype
			AND latest_msg.reference_name = m.reference_name
			AND latest_msg.latest = m.creation
		ORDER BY m.creation DESC
		LIMIT %(limit)s
		""",
		{"limit": int(limit)},
		as_dict=True,
	)

	# Filter by per-doc read permission so users only see conversations they
	# can access. Skip references whose target doc no longer exists.
	visible = []
	for row in rows:
		try:
			if not frappe.db.exists(row.reference_doctype, row.reference_name):
				continue
			doc = frappe.get_doc(row.reference_doctype, row.reference_name)
			if not doc.has_permission("read"):
				continue
			row["counterparty"] = (
				row.counter_from if row.last_type == "Incoming" else row.counter_to
			)
			row["display_name"] = _resolve_display_name(doc)
			visible.append(row)
		except frappe.PermissionError:
			continue
		except Exception:
			# Bad reference — skip rather than failing the whole list
			continue

	return visible


def _resolve_display_name(doc):
	"""Friendly label for a CRM Lead / CRM Deal / Contact."""
	# CRM Lead
	if hasattr(doc, "lead_name") and doc.lead_name:
		return doc.lead_name
	# CRM Deal
	if hasattr(doc, "organization") and doc.organization:
		return doc.organization
	# Generic
	for attr in ("full_name", "first_name", "name"):
		val = getattr(doc, attr, None)
		if val:
			return val
	return doc.name

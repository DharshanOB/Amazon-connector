# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class AmazonProductType(Document):
	def validate(self):
		if self.product_type:
			self.product_type = str(self.product_type).strip().upper()
		if not self.display_name:
			self.display_name = self.product_type


@frappe.whitelist()
def get_product_types_list(doctype, txt, searchfield, start, page_len, filters):
	"""Custom query for Amazon Product Type returning 'display_name (product_type)'."""
	query = frappe.qb.from_(frappe.qb.DocType("Amazon Product Type")).select(
		"name", "display_name", "product_type"
	)
	if txt:
		like_txt = f"%{txt}%"
		query = query.where(
			(frappe.qb.DocType("Amazon Product Type").name.like(like_txt))
			| (frappe.qb.DocType("Amazon Product Type").display_name.like(like_txt))
			| (frappe.qb.DocType("Amazon Product Type").product_type.like(like_txt))
		)
	query = query.orderby("display_name").offset(start).limit(page_len)
	results = query.run(as_dict=True)
	return [[r.name, f"{r.display_name} ({r.product_type})"] for r in results]

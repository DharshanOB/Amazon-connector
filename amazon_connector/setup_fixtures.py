# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe

EXT_MODULE = "Amazon Connector"

DEPRECATED_CUSTOM_FIELDS = (
	"Sales Order-amazon_fulfillment_channel",
	"Delivery Note-amazon_fulfillment_channel",
	"Item-amazon_tab",
	"Item-amazon_section",
	"Item-amazon_col_break",
	"Item-amazon_details_col_break",
)

CUSTOM_FIELDS: dict[str, list[dict]] = {
	"Amazon SP API Settings": [
		{
			"fieldname": "seller_id",
			"label": "Seller ID",
			"fieldtype": "Data",
			"insert_after": "client_secret",
			"description": "Amazon Selling Partner / Merchant Token (falls back to Client ID if blank).",
		},
		{
			"fieldname": "use_sandbox",
			"label": "Use Sandbox",
			"fieldtype": "Check",
			"default": "0",
			"insert_after": "country",
			"description": "Use Amazon SP-API Sandbox endpoints for testing.",
		},
		{
			"fieldname": "custom_endpoint",
			"label": "Custom SP-API Endpoint",
			"fieldtype": "Data",
			"insert_after": "use_sandbox",
			"description": "Optional local/mock SP-API base URL (e.g. http://127.0.0.1:9099) for local testing. Leave blank for live Amazon or Amazon Sandbox.",
		},
		{
			"fieldname": "fba_warehouse",
			"label": "FBA Warehouse",
			"fieldtype": "Link",
			"options": "Warehouse",
			"insert_after": "warehouse",
			"description": "Warehouse used for Amazon Fulfilled (AFN / FBA) orders and Delivery Notes (falls back to default Warehouse if blank).",
		},
		{
			"fieldname": "compliance_defaults_section",
			"label": "Listing & Compliance Defaults",
			"fieldtype": "Section Break",
			"collapsible": 1,
			"insert_after": "market_place_account_group",
			"description": "Default India/SP-API listing compliance attributes applied when pushing items.",
		},
		{
			"fieldname": "default_country_of_origin",
			"label": "Default Country of Origin",
			"fieldtype": "Link",
			"options": "Country",
			"insert_after": "compliance_defaults_section",
			"description": "Fallback Country of Origin used when Item.country_of_origin is blank.",
		},
		{
			"fieldname": "default_dg_regulation",
			"label": "Default Dangerous Goods Regulation",
			"fieldtype": "Select",
			"options": "\nnot_applicable\nghs\nstorage\ntransportation\nwaste\nother\nunknown",
			"default": "not_applicable",
			"insert_after": "default_country_of_origin",
			"description": "Default Amazon supplier_declared_dg_hz_regulation value for pushed listings.",
		},
		{
			"fieldname": "column_break_compliance",
			"fieldtype": "Column Break",
			"insert_after": "default_dg_regulation",
		},
		{
			"fieldname": "packer_contact_info",
			"label": "Packer Contact Information",
			"fieldtype": "Small Text",
			"insert_after": "column_break_compliance",
			"description": "Default Legal Metrology packer name & address for Amazon IN listings.",
		},
		{
			"fieldname": "importer_contact_info",
			"label": "Importer Contact Information",
			"fieldtype": "Small Text",
			"insert_after": "packer_contact_info",
			"description": "Default Legal Metrology importer name & address for Amazon IN listings.",
		},
		{
			"fieldname": "manufacturer_contact_info",
			"label": "Manufacturer Contact Information",
			"fieldtype": "Small Text",
			"insert_after": "importer_contact_info",
			"description": "Default manufacturer contact details (rtip_manufacturer_contact_information) for Amazon listings.",
		},
		{
			"fieldname": "enable_rdt_pii",
			"label": "Enable RDT PII Access",
			"fieldtype": "Check",
			"default": "0",
			"insert_after": "taxes_charges",
			"description": "Request Restricted Data Tokens (RDT) before fetching Orders to include restricted BuyerInfo and ShippingAddress PII.",
		},
		{
			"fieldname": "sync_inventory",
			"label": "Sync Inventory to Amazon",
			"fieldtype": "Check",
			"default": "1",
			"insert_after": "enable_rdt_pii",
			"description": "Automatically push ERPNext MFN stock changes to Amazon SP-API.",
		},
		{
			"fieldname": "sync_delivery_notes",
			"label": "Import Delivery Notes from Amazon on Shipment",
			"fieldtype": "Check",
			"default": "0",
			"insert_after": "sync_inventory",
			"description": "Automatically create Delivery Notes when fetched Amazon orders have Shipped or PartiallyShipped status.",
		},
		{
			"fieldname": "sync_cancellations",
			"label": "Sync Order Cancellations",
			"fieldtype": "Check",
			"default": "1",
			"insert_after": "sync_delivery_notes",
			"description": "Automatically cancel open ERPNext Sales Orders and Draft Delivery Notes when Amazon orders are canceled.",
		},
		{
			"fieldname": "sync_sales_invoice",
			"label": "Auto Create Sales Invoice",
			"fieldtype": "Check",
			"default": "0",
			"insert_after": "sync_cancellations",
			"description": "Automatically create and submit Sales Invoices for imported Amazon Sales Orders.",
		},
		{
			"fieldname": "sync_returns",
			"label": "Sync Customer Returns & Refunds",
			"fieldtype": "Check",
			"default": "0",
			"insert_after": "sync_sales_invoice",
			"description": "Automatically create Return Credit Notes from Amazon Finances Refund Events.",
		},
		{
			"fieldname": "sync_settlements",
			"label": "Sync Settlements & Payouts",
			"fieldtype": "Check",
			"default": "0",
			"insert_after": "sync_returns",
			"description": "Automatically create Journal Entries reconciling closed Amazon Financial Event Groups.",
		},
		{
			"fieldname": "import_opening_stock",
			"label": "Import Opening Stock from Listings",
			"fieldtype": "Check",
			"default": "1",
			"insert_after": "sync_settlements",
			"description": "Create opening Stock Entry / Valuation Rate when importing listings with quantity > 0.",
		},
		{
			"fieldname": "sync_delivery_notes_btn",
			"label": "Sync Delivery Notes",
			"fieldtype": "Button",
			"options": "get_delivery_notes",
			"depends_on": "eval: !doc.__islocal && doc.is_active",
			"insert_after": "sync_orders",
			"description": "Click this button to pull Shipped orders from Amazon SP-API and create Delivery Notes.",
		},
		{
			"fieldname": "sync_listings",
			"label": "Sync Listings",
			"fieldtype": "Button",
			"options": "get_listings",
			"depends_on": "eval: !doc.__islocal && doc.is_active",
			"insert_after": "sync_delivery_notes_btn",
			"description": "Click this button to pull your product listings from Amazon SP-API.",
		},
		{
			"fieldname": "sync_returns_btn",
			"label": "Sync Returns & Refunds",
			"fieldtype": "Button",
			"options": "sync_returns_and_refunds",
			"depends_on": "eval: !doc.__islocal && doc.is_active",
			"insert_after": "sync_listings",
			"description": "Click this button to pull Customer Returns / Refund Events from Amazon SP-API.",
		},
		{
			"fieldname": "sync_settlements_btn",
			"label": "Sync Settlements",
			"fieldtype": "Button",
			"options": "sync_settlement_entries",
			"depends_on": "eval: !doc.__islocal && doc.is_active",
			"insert_after": "sync_returns_btn",
			"description": "Click this button to reconcile closed Amazon Financial Event Groups into Journal Entries.",
		},
		{
			"fieldname": "enable_listings_sync",
			"label": "Enable Scheduled Listings Sync",
			"fieldtype": "Check",
			"default": "0",
			"depends_on": "eval: !doc.__islocal",
			"insert_after": "enable_sync",
			"description": "Check this to enable a scheduled daily listings synchronization routine via scheduler.",
		},
		{
			"fieldname": "last_orders_sync",
			"label": "Last Orders Sync",
			"fieldtype": "Datetime",
			"read_only": 1,
			"insert_after": "max_retry_limit",
		},
		{
			"fieldname": "last_delivery_notes_sync",
			"label": "Last Delivery Notes Sync",
			"fieldtype": "Datetime",
			"read_only": 1,
			"insert_after": "last_orders_sync",
		},
		{
			"fieldname": "last_listings_sync",
			"label": "Last Listings Sync",
			"fieldtype": "Datetime",
			"read_only": 1,
			"insert_after": "last_delivery_notes_sync",
		},
		{
			"fieldname": "last_returns_sync",
			"label": "Last Returns Sync",
			"fieldtype": "Datetime",
			"read_only": 1,
			"insert_after": "last_listings_sync",
		},
		{
			"fieldname": "last_settlements_sync",
			"label": "Last Settlements Sync",
			"fieldtype": "Datetime",
			"read_only": 1,
			"insert_after": "last_returns_sync",
		},
	],
	"Item Group": [
		{
			"fieldname": "amazon_section",
			"label": "Amazon SP-API Defaults",
			"fieldtype": "Section Break",
			"collapsible": 1,
			"insert_after": "defaults",
		},
		{
			"fieldname": "amazon_product_type",
			"label": "Default Amazon Product Type",
			"fieldtype": "Link",
			"options": "Amazon Product Type",
			"insert_after": "amazon_section",
			"description": "Default Amazon category applied when creating items in this group.",
		},
		{
			"fieldname": "amazon_attributes_template",
			"label": "Amazon Attributes Template (JSON)",
			"fieldtype": "Code",
			"options": "JSON",
			"insert_after": "amazon_product_type",
			"description": "Default category-specific Amazon attributes template preloaded for items in this group.",
		},
	],
	"Item": [
		{
			"fieldname": "amazon_product_type",
			"label": "Amazon Product Type",
			"fieldtype": "Link",
			"options": "Amazon Product Type",
			"insert_after": "item_group",
			"print_hide": 1,
			"description": "Select from 1,859 official Amazon India categories (e.g. Printed Book, Headphones).",
		},
		{
			"fieldname": "amazon_asin",
			"label": "Amazon ASIN",
			"fieldtype": "Data",
			"insert_after": "amazon_product_type",
			"print_hide": 1,
			"description": "10-character Amazon Standard Identification Number mapped to merchant_suggested_asin.",
		},
		{
			"fieldname": "amazon_details_col_break",
			"fieldtype": "Column Break",
			"insert_after": "amazon_asin",
		},
		{
			"fieldname": "amazon_mrp",
			"label": "Amazon MRP",
			"fieldtype": "Currency",
			"insert_after": "amazon_details_col_break",
			"print_hide": 1,
			"description": "Maximum Retail Price for Amazon IN listings (falls back to selling price if zero).",
		},
		{
			"fieldname": "amazon_attributes_tab",
			"label": "Amazon Attributes",
			"fieldtype": "Tab Break",
			"insert_after": "description",
		},
		{
			"fieldname": "amazon_attrs_section",
			"label": "Amazon Category Attributes",
			"fieldtype": "Section Break",
			"insert_after": "amazon_attributes_tab",
			"description": "Category-specific listing attributes dynamically generated from Amazon schema.",
		},
		{
			"fieldname": "amazon_dynamic_attributes_html",
			"label": "Category Attributes Form",
			"fieldtype": "HTML",
			"insert_after": "amazon_attrs_section",
		},
		{
			"fieldname": "amazon_raw_json_section",
			"label": "Raw JSON Payload",
			"fieldtype": "Section Break",
			"collapsible": 1,
			"collapsed": 1,
			"insert_after": "amazon_dynamic_attributes_html",
		},
		{
			"fieldname": "amazon_attributes",
			"label": "Amazon Attributes (JSON)",
			"fieldtype": "Code",
			"options": "JSON",
			"insert_after": "amazon_raw_json_section",
			"print_hide": 1,
			"description": "JSON object of category-specific Amazon SP-API attributes. Kept in sync with dynamic fields.",
		},
	],
	"Ecommerce Item": [
		{
			"fieldname": "status",
			"label": "Amazon Sync Status",
			"fieldtype": "Data",
			"insert_after": "sku",
			"read_only": 1,
			"print_hide": 1,
		},
	],
	"Sales Order": [
		{
			"fieldname": "marketplace_id",
			"label": "Marketplace ID",
			"fieldtype": "Data",
			"insert_after": "amazon_order_id",
			"read_only": 1,
			"print_hide": 1,
		},
		{
			"fieldname": "amazon_order_status",
			"label": "Amazon Order Status",
			"fieldtype": "Data",
			"insert_after": "marketplace_id",
			"read_only": 1,
			"print_hide": 1,
		},
		{
			"fieldname": "fulfillment_channel",
			"label": "Fulfillment Channel",
			"fieldtype": "Select",
			"options": "\nMFN\nAFN",
			"insert_after": "amazon_order_status",
			"read_only": 1,
			"print_hide": 1,
		},
		{
			"fieldname": "is_prime",
			"label": "Is Prime",
			"fieldtype": "Check",
			"default": "0",
			"insert_after": "fulfillment_channel",
			"read_only": 1,
			"print_hide": 1,
		},
		{
			"fieldname": "amazon_canceled",
			"label": "Amazon Canceled",
			"fieldtype": "Check",
			"default": "0",
			"insert_after": "is_prime",
			"read_only": 1,
			"print_hide": 1,
		},
	],
	"Sales Order Item": [
		{
			"fieldname": "amazon_order_item_id",
			"label": "Amazon Order Item ID",
			"fieldtype": "Data",
			"insert_after": "item_code",
			"read_only": 1,
			"print_hide": 1,
		},
	],
	"Delivery Note": [
		{
			"fieldname": "amazon_order_id",
			"label": "Amazon Order ID",
			"fieldtype": "Data",
			"insert_after": "title",
			"read_only": 1,
			"print_hide": 1,
		},
		{
			"fieldname": "imported_from_amazon",
			"label": "Imported from Amazon",
			"fieldtype": "Check",
			"default": "0",
			"insert_after": "amazon_order_id",
			"read_only": 1,
			"hidden": 1,
			"print_hide": 1,
		},
		{
			"fieldname": "marketplace_id",
			"label": "Marketplace ID",
			"fieldtype": "Data",
			"insert_after": "imported_from_amazon",
			"read_only": 1,
			"print_hide": 1,
		},
		{
			"fieldname": "fulfillment_channel",
			"label": "Fulfillment Channel",
			"fieldtype": "Select",
			"options": "\nMFN\nAFN",
			"insert_after": "marketplace_id",
			"read_only": 1,
			"print_hide": 1,
		},
		{
			"fieldname": "amazon_carrier",
			"label": "Amazon Carrier",
			"fieldtype": "Data",
			"insert_after": "fulfillment_channel",
			"print_hide": 1,
		},
		{
			"fieldname": "amazon_tracking_number",
			"label": "Amazon Tracking Number",
			"fieldtype": "Data",
			"insert_after": "amazon_carrier",
			"print_hide": 1,
		},
		{
			"fieldname": "amazon_shipment_status",
			"label": "Amazon Shipment Status",
			"fieldtype": "Select",
			"options": "\nPending\nConfirmed\nFailed\nCancelled\nSkipped",
			"insert_after": "amazon_tracking_number",
			"read_only": 1,
			"print_hide": 1,
		},
		{
			"fieldname": "amazon_last_sync_error",
			"label": "Amazon Last Sync Error",
			"fieldtype": "Small Text",
			"insert_after": "amazon_shipment_status",
			"read_only": 1,
			"print_hide": 1,
		},
	],
	"Delivery Note Item": [
		{
			"fieldname": "amazon_order_item_id",
			"label": "Amazon Order Item ID",
			"fieldtype": "Data",
			"insert_after": "item_code",
			"read_only": 1,
			"print_hide": 1,
		},
	],
	"Sales Invoice": [
		{
			"fieldname": "amazon_order_id",
			"label": "Amazon Order ID",
			"fieldtype": "Data",
			"insert_after": "title",
			"read_only": 1,
			"print_hide": 1,
		},
		{
			"fieldname": "imported_from_amazon",
			"label": "Imported from Amazon",
			"fieldtype": "Check",
			"default": "0",
			"insert_after": "amazon_order_id",
			"read_only": 1,
			"hidden": 1,
			"print_hide": 1,
		},
		{
			"fieldname": "marketplace_id",
			"label": "Marketplace ID",
			"fieldtype": "Data",
			"insert_after": "imported_from_amazon",
			"read_only": 1,
			"print_hide": 1,
		},
		{
			"fieldname": "amazon_return_id",
			"label": "Amazon Return ID",
			"fieldtype": "Data",
			"insert_after": "marketplace_id",
			"read_only": 1,
			"print_hide": 1,
		},
	],
	"Journal Entry": [
		{
			"fieldname": "amazon_financial_event_group_id",
			"label": "Amazon Financial Event Group ID",
			"fieldtype": "Data",
			"insert_after": "title",
			"read_only": 1,
			"print_hide": 1,
		},
	],
}

PROPERTY_SETTERS: list[dict] = [
	{
		"doctype_or_field": "DocField",
		"doc_type": "Amazon Fields Map",
		"field_name": "amazon_field",
		"property": "unique",
		"property_type": "Check",
		"value": "0",
	},
	{
		"doctype_or_field": "DocField",
		"doc_type": "Amazon Fields Map",
		"field_name": "item_field",
		"property": "unique",
		"property_type": "Check",
		"value": "0",
	},
	{
		"doctype_or_field": "DocField",
		"doc_type": "Amazon SP API Settings",
		"field_name": "enable_sync",
		"property": "description",
		"property_type": "Text",
		"value": "Check this to enable a scheduled hourly synchronization routine via scheduler",
	},
]

DEFAULT_ITEM_ATTRIBUTES = [
	{
		"attribute_name": "Color",
		"values": [
			("Black", "BLK"),
			("White", "WHT"),
			("Blue", "BLU"),
			("Red", "RED"),
			("Green", "GRN"),
			("Grey", "GRY"),
			("Silver", "SLV"),
			("Gold", "GLD"),
			("Pink", "PNK"),
			("Yellow", "YLW"),
		],
	},
	{
		"attribute_name": "Size",
		"values": [
			("XS", "XS"),
			("S", "S"),
			("M", "M"),
			("L", "L"),
			("XL", "XL"),
			("XXL", "XXL"),
			("Free Size", "FREE"),
		],
	},
	{
		"attribute_name": "Binding",
		"values": [
			("Paperback", "PB"),
			("Hardcover", "HC"),
			("Board Book", "BB"),
			("Spiral Bound", "SB"),
		],
	},
	{
		"attribute_name": "Storage Capacity",
		"values": [
			("32 GB", "32G"),
			("64 GB", "64G"),
			("128 GB", "128G"),
			("256 GB", "256G"),
			("512 GB", "512G"),
			("1 TB", "1TB"),
		],
	},
	{
		"attribute_name": "Pack Size",
		"values": [
			("1", "PK1"),
			("2", "PK2"),
			("3", "PK3"),
			("5", "PK5"),
			("10", "PK10"),
		],
	},
]


def _seed_default_item_attributes() -> None:
	"""Seed starter ERPNext Item Attributes for common Amazon variation axes without overwriting existing ones."""
	if not frappe.db.table_exists("Item Attribute"):
		return

	for attr_def in DEFAULT_ITEM_ATTRIBUTES:
		attr_name = attr_def["attribute_name"]
		if frappe.db.exists("Item Attribute", attr_name):
			continue
		doc = frappe.get_doc(
			{
				"doctype": "Item Attribute",
				"attribute_name": attr_name,
				"numeric_values": 0,
				"item_attribute_values": [
					{"attribute_value": val, "abbr": abbr} for val, abbr in attr_def["values"]
				],
			}
		)
		doc.insert(ignore_permissions=True)


def _upsert_custom_field(dt: str, df: dict) -> None:
	name = f"{dt}-{df['fieldname']}"
	if frappe.db.exists("Custom Field", name):
		doc = frappe.get_doc("Custom Field", name)
		if df.get("fieldtype") and doc.fieldtype != df["fieldtype"]:
			frappe.delete_doc("Custom Field", name, force=1)
			doc = frappe.get_doc({"doctype": "Custom Field", "dt": dt, "module": EXT_MODULE, **df})
			doc.insert(ignore_permissions=True)
			return
		dirty = False
		for key, val in {**df, "module": EXT_MODULE}.items():
			if doc.get(key) != val:
				doc.set(key, val)
				dirty = True
		if dirty:
			doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc({"doctype": "Custom Field", "dt": dt, "module": EXT_MODULE, **df})
		doc.insert(ignore_permissions=True)


def _upsert_property_setter(ps_def: dict) -> None:
	existing = frappe.db.get_value(
		"Property Setter",
		{
			"doc_type": ps_def["doc_type"],
			"field_name": ps_def["field_name"],
			"property": ps_def["property"],
		},
		"name",
	)
	if existing:
		doc = frappe.get_doc("Property Setter", existing)
		dirty = False
		for key, val in {**ps_def, "module": EXT_MODULE}.items():
			if doc.get(key) != val:
				doc.set(key, val)
				dirty = True
		if dirty:
			doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc({"doctype": "Property Setter", "module": EXT_MODULE, **ps_def})
		doc.insert(ignore_permissions=True)


def _drop_amazon_fields_map_unique_indexes() -> None:
	"""Ensure Property Setters (unique=0) are also enforced at SQL level after upstream schema sync."""
	if not frappe.db.table_exists("Amazon Fields Map"):
		return

	if frappe.db.db_type == "mariadb":
		for col in ("amazon_field", "item_field"):
			try:
				rows = frappe.db.sql(
					"""
					SHOW INDEX FROM `tabAmazon Fields Map`
					WHERE Column_name = %s AND Non_unique = 0 AND Key_name != 'PRIMARY'
					""",
					(col,),
					as_dict=True,
				)
				for row in rows:
					key_name = row.get("Key_name")
					if key_name:
						frappe.db.sql(f"ALTER TABLE `tabAmazon Fields Map` DROP INDEX `{key_name}`")
			except Exception as e:
				frappe.logger("amazon_connector").warning(f"Could not drop unique index on {col}: {e}")
	elif frappe.db.db_type == "postgres":
		for col in ("amazon_field", "item_field"):
			try:
				rows = frappe.db.sql(
					"""
					SELECT indexname FROM pg_indexes
					WHERE tablename = 'tabAmazon Fields Map' AND indexdef LIKE %s
					""",
					(f"%({col})%",),
					as_dict=True,
				)
				for row in rows:
					index_name = row.get("indexname")
					if index_name and "pkey" not in index_name:
						frappe.db.sql(f'DROP INDEX IF EXISTS "{index_name}"')
			except Exception as e:
				frappe.logger("amazon_connector").warning(f"Could not drop PostgreSQL unique index on {col}: {e}")


def sync_amazon_extensions_fixtures():
	"""Drop deprecated dev fields and ensure all Amazon extension fixtures are tagged to EXT_MODULE."""
	for cf_name in DEPRECATED_CUSTOM_FIELDS:
		if frappe.db.exists("Custom Field", cf_name):
			frappe.delete_doc("Custom Field", cf_name, ignore_permissions=True)

	for dt, fields in CUSTOM_FIELDS.items():
		for df in fields:
			_upsert_custom_field(dt, df)

	for ps_def in PROPERTY_SETTERS:
		_upsert_property_setter(ps_def)

	_drop_amazon_fields_map_unique_indexes()
	_seed_default_item_attributes()

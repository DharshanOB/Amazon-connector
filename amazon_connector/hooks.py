app_name = "amazon_connector"
app_title = "Amazon Connector"
app_publisher = "Custom"
app_description = "Clean custom Frappe extension app for Amazon SP-API"
app_email = "admin@example.com"
app_license = "mit"

required_apps = ["ecommerce_integrations"]

doctype_js = {
	"Amazon SP API Settings": "public/js/amazon/amazon_sp_api_settings.js",
	"Sales Order": "public/js/amazon/sales_order.js",
	"Delivery Note": "public/js/amazon/delivery_note.js",
	"Item": "public/js/amazon/item.js",
	"Item Group": "public/js/amazon/item_group.js",
}

doctype_list_js = {
	"Sales Order": "public/js/amazon/sales_order_list.js",
	"Delivery Note": "public/js/amazon/delivery_note_list.js",
}

override_doctype_class = {
	"Amazon SP API Settings": "amazon_connector.overrides.amazon_sp_api_settings.CustomAmazonSPAPISettings",
}

override_whitelisted_methods = {
	"ecommerce_integrations.ecommerce_integrations.doctype.ecommerce_integration_log.ecommerce_integration_log.resync": (
		"amazon_connector.amazon_ext.log_patch.resync"
	),
	"ecommerce_integrations.amazon.doctype.amazon_sp_api_settings.amazon_repository.get_orders": (
		"amazon_connector.amazon_ext.repository.get_orders"
	),
	"ecommerce_integrations.amazon.doctype.amazon_sp_api_settings.amazon_repository.validate_amazon_sp_api_credentials": (
		"amazon_connector.amazon_ext.repository.validate_amazon_sp_api_credentials"
	),
}

doc_events = {
	"Item": {
		"after_insert": "amazon_connector.amazon_ext.outbound.handle_item_update",
		"on_update": "amazon_connector.amazon_ext.outbound.handle_item_update",
		"on_trash": "amazon_connector.amazon_ext.outbound.handle_item_trash",
	},
	"Sales Order": {
		"on_submit": "amazon_connector.amazon_ext.fulfillment.handle_sales_order_stock_reservation",
		"on_cancel": [
			"amazon_connector.amazon_ext.lifecycle.handle_sales_order_cancel",
			"amazon_connector.amazon_ext.fulfillment.handle_sales_order_stock_reservation",
		],
	},
	"Item Price": {
		"on_update": "amazon_connector.amazon_ext.fulfillment.handle_item_price_change",
	},
	"Delivery Note": {
		"on_submit": "amazon_connector.amazon_ext.fulfillment.handle_delivery_note_submit",
		"on_cancel": "amazon_connector.amazon_ext.fulfillment.handle_delivery_note_cancel",
	},
	"Stock Ledger Entry": {
		"on_submit": "amazon_connector.amazon_ext.fulfillment.handle_stock_ledger_entry",
	},
}

scheduler_events = {
	"daily_long": [
		"amazon_connector.overrides.amazon_sp_api_settings.schedule_get_listings",
	],
	"hourly_long": [
		"amazon_connector.overrides.amazon_sp_api_settings.schedule_get_order_details",
		"amazon_connector.overrides.amazon_sp_api_settings.schedule_get_delivery_notes",
		"amazon_connector.overrides.amazon_sp_api_settings.schedule_sync_returns",
		"amazon_connector.overrides.amazon_sp_api_settings.schedule_sync_settlements",
		"amazon_connector.amazon_ext.fulfillment.reconcile_amazon_stock_and_prices",
	],
}

before_request = ["amazon_connector.patches_runtime.apply_amazon_patches"]
before_job = ["amazon_connector.patches_runtime.apply_amazon_patches"]
before_tests = "amazon_connector.patches_runtime.before_tests"

after_install = "amazon_connector.setup_fixtures.sync_amazon_extensions_fixtures"
after_migrate = ["amazon_connector.setup_fixtures.sync_amazon_extensions_fixtures"]

fixtures = [
	{"dt": "Custom Field", "filters": [["module", "=", "Amazon Connector"]]},
	{"dt": "Property Setter", "filters": [["module", "=", "Amazon Connector"]]},
]

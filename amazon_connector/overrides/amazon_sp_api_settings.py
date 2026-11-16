# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import ecommerce_integrations.amazon.doctype.amazon_sp_api_settings.amazon_sp_api_settings as _upstream_settings
import frappe
from frappe import _
from frappe.utils import add_days, cint, getdate, today

from amazon_connector.amazon_ext.repository import (
	CustomAmazonRepository,
	resolve_custom_endpoint,
	validate_amazon_sp_api_credentials,
)
from amazon_connector.patches_runtime import apply_amazon_patches

apply_amazon_patches()

_BaseAmazonSPAPISettings = _upstream_settings.AmazonSPAPISettings


class CustomAmazonSPAPISettings(_BaseAmazonSPAPISettings):
	"""Extension controller for Amazon SP API Settings via override_doctype_class."""

	def validate(self):
		self.validate_amazon_fields_map()
		self.validate_after_date()

		if self.is_active == 1:
			self.validate_credentials()
		else:
			self.enable_sync = 0
			self.enable_listings_sync = 0

		if not self.max_retry_limit:
			self.max_retry_limit = 1
		elif self.max_retry_limit and self.max_retry_limit > 5:
			frappe.throw(_("Value for <b>Max Retry Limit</b> must be less than or equal to 5."))

	def validate_after_date(self):
		flags = getattr(self, "flags", None)
		if (flags and flags.get("ignore_after_date_validation")) or frappe.flags.in_patch:
			return

		is_new = getattr(self, "is_new", None)
		has_changed = getattr(self, "has_value_changed", None)
		should_validate = (
			not callable(is_new)
			or is_new()
			or (callable(has_changed) and has_changed("after_date"))
		)
		if should_validate and getdate(add_days(today(), -30)) > getdate(self.after_date):
			frappe.throw(_("The date must be within the last 30 days."))

	def validate_credentials(self):
		validate_amazon_sp_api_credentials(
			iam_arn=self.get("iam_arn"),
			client_id=self.get("client_id"),
			client_secret=self.get_password("client_secret"),
			refresh_token=self.get("refresh_token"),
			aws_access_key=self.get("aws_access_key"),
			aws_secret_key=self.get_password("aws_secret_key"),
			country=self.get("country"),
			use_sandbox=self.get("use_sandbox"),
			custom_endpoint=resolve_custom_endpoint(self),
		)

	def _enqueue_sync_job(
		self,
		job_name: str,
		method,
		running_message: str,
		queued_message: str,
		queue: str = "default",
		**kwargs,
	):
		self.check_permission("write")
		if self.is_active != 1:
			frappe.msgprint(
				_("Please enable the Amazon SP API Settings {0}.").format(frappe.bold(self.name))
			)
			return

		if frappe.db.get_all("RQ Job", {"job_name": job_name, "status": ["in", ["queued", "started"]]}):
			return frappe.msgprint(running_message)

		frappe.enqueue(
			job_name=job_name,
			method=method,
			queue=queue,
			timeout=4000,
			now=frappe.flags.in_test,
			**kwargs,
		)

		frappe.msgprint(queued_message)

	@frappe.whitelist()
	def get_order_details(self):
		from amazon_connector.amazon_ext.repository import get_orders

		return self._enqueue_sync_job(
			job_name=f"Get Amazon Orders - {self.name}",
			method=get_orders,
			running_message=_("The order details are currently being fetched in the background."),
			queued_message=_("Order details will be fetched in the background."),
			queue="long",
			amz_setting_name=self.name,
			created_after=self.after_date,
			last_updated_after=self.get("last_orders_sync"),
		)

	@frappe.whitelist()
	def get_delivery_notes(self):
		from amazon_connector.amazon_ext.repository import get_delivery_notes

		return self._enqueue_sync_job(
			job_name=f"Get Amazon Delivery Notes - {self.name}",
			method=get_delivery_notes,
			running_message=_("Delivery Notes are currently being fetched in the background."),
			queued_message=_("Delivery Notes will be fetched in the background."),
			queue="long",
			amz_setting_name=self.name,
			created_after=self.after_date,
			last_updated_after=self.get("last_delivery_notes_sync"),
		)

	@frappe.whitelist()
	def get_listings(self):
		from amazon_connector.amazon_ext.repository import get_listings

		return self._enqueue_sync_job(
			job_name=f"Get Amazon Listings - {self.name}",
			method=get_listings,
			running_message=_("The product listings are currently being fetched in the background."),
			queued_message=_("Product listings will be fetched in the background."),
			queue="long",
			amz_setting_name=self.name,
		)

	@frappe.whitelist()
	def sync_returns_and_refunds(self):
		from amazon_connector.amazon_ext.lifecycle import sync_returns

		return self._enqueue_sync_job(
			job_name=f"Sync Amazon Returns - {self.name}",
			method=sync_returns,
			running_message=_("Customer returns and refunds are currently being fetched in the background."),
			queued_message=_("Customer returns and refunds will be fetched in the background."),
			queue="long",
			amz_setting_name=self.name,
			posted_after=self.get("last_returns_sync") or self.after_date,
		)

	@frappe.whitelist()
	def sync_settlement_entries(self):
		from amazon_connector.amazon_ext.lifecycle import sync_settlements

		return self._enqueue_sync_job(
			job_name=f"Sync Amazon Settlements - {self.name}",
			method=sync_settlements,
			running_message=_("Settlement entries are currently being synced in the background."),
			queued_message=_("Settlement entries will be synced in the background."),
			queue="long",
			amz_setting_name=self.name,
			started_after=self.get("last_settlements_sync") or self.after_date,
		)


def _run_scheduled_for_settings(filters: dict, callback, **kwargs_builder):
	select_fields = ["name", "after_date"]
	for opt_field in (
		"last_orders_sync",
		"last_delivery_notes_sync",
		"last_returns_sync",
		"last_settlements_sync",
	):
		if frappe.db.has_column("Amazon SP API Settings", opt_field):
			select_fields.append(opt_field)

	amz_settings = frappe.get_all(
		"Amazon SP API Settings",
		filters=filters,
		fields=select_fields,
	)

	for amz_setting in amz_settings:
		try:
			kwargs = {k: fn(amz_setting) for k, fn in kwargs_builder.items()}
			callback(amz_setting_name=amz_setting.name, **kwargs)
		except Exception:
			frappe.log_error(
				title=f"Amazon Scheduled Sync Failed ({amz_setting.name})",
				message=frappe.get_traceback(),
			)


def schedule_get_order_details():
	from amazon_connector.amazon_ext.repository import get_orders

	_run_scheduled_for_settings(
		{"is_active": 1, "enable_sync": 1},
		get_orders,
		created_after=lambda s: s.after_date,
		last_updated_after=lambda s: s.get("last_orders_sync") or s.after_date,
	)


def schedule_get_delivery_notes():
	from amazon_connector.amazon_ext.repository import get_delivery_notes

	_run_scheduled_for_settings(
		{"is_active": 1, "enable_sync": 1, "sync_delivery_notes": 1},
		get_delivery_notes,
		created_after=lambda s: s.after_date,
		last_updated_after=lambda s: s.get("last_delivery_notes_sync") or s.after_date,
	)


def schedule_sync_returns():
	from amazon_connector.amazon_ext.lifecycle import sync_returns

	_run_scheduled_for_settings(
		{"is_active": 1, "enable_sync": 1, "sync_returns": 1},
		sync_returns,
		posted_after=lambda s: s.get("last_returns_sync") or s.after_date,
	)


def schedule_sync_settlements():
	from amazon_connector.amazon_ext.lifecycle import sync_settlements

	_run_scheduled_for_settings(
		{"is_active": 1, "enable_sync": 1, "sync_settlements": 1},
		sync_settlements,
		started_after=lambda s: s.get("last_settlements_sync") or s.after_date,
	)


def schedule_get_listings():
	from amazon_connector.amazon_ext.repository import get_listings

	_run_scheduled_for_settings(
		{"is_active": 1, "enable_listings_sync": 1},
		get_listings,
	)


@frappe.whitelist()
def sync_from_listview(doctype: str) -> dict:
	"""Fetch delta orders or delivery notes from Amazon when List View refresh button is clicked."""
	frappe.has_permission(doctype, "read", throw=True)
	frappe.has_permission(doctype, "write", throw=True)
	if doctype == "Sales Order":
		schedule_get_order_details()
		return {
			"status": "success",
			"message": _("Checked Amazon for new orders."),
		}
	elif doctype == "Delivery Note":
		schedule_get_delivery_notes()
		return {
			"status": "success",
			"message": _("Checked Amazon for shipment updates."),
		}

	return {"status": "skipped"}


def _resolve_amz_setting_name(amz_setting_name: str | None = None, company: str | None = None) -> str:
	filters = {"is_active": 1}
	if company:
		filters["company"] = company

	if not amz_setting_name:
		amz_setting_name = (
			frappe.db.get_value("Amazon SP API Settings", filters, "name")
			or frappe.db.get_value("Amazon SP API Settings", {"is_active": 1}, "name")
			or frappe.db.get_value("Amazon SP API Settings", {}, "name")
		)
	if not amz_setting_name:
		frappe.throw(_("No active Amazon SP API Settings found."))
	return amz_setting_name


def _sync_outbound_for_item(item_code: str, amz_setting_name: str | None, repo_method: str) -> dict:
	frappe.has_permission("Item", "write", throw=True)
	if item_code and frappe.db.exists("Item", item_code):
		frappe.has_permission("Item", "write", doc=item_code, throw=True)
	resolved_setting = _resolve_amz_setting_name(amz_setting_name)
	frappe.has_permission("Amazon SP API Settings", "write", doc=resolved_setting, throw=True)
	repo = CustomAmazonRepository(resolved_setting)
	return getattr(repo, repo_method)(item_code)


@frappe.whitelist()
def sync_outbound_item(item_code: str, amz_setting_name: str | None = None) -> dict:
	return _sync_outbound_for_item(item_code, amz_setting_name, "sync_outbound_listing")


@frappe.whitelist()
def sync_outbound_item_stock(item_code: str, amz_setting_name: str | None = None) -> dict:
	return _sync_outbound_for_item(item_code, amz_setting_name, "sync_outbound_stock")


@frappe.whitelist()
def sync_outbound_item_price(item_code: str, amz_setting_name: str | None = None) -> dict:
	return _sync_outbound_for_item(item_code, amz_setting_name, "sync_outbound_price")


@frappe.whitelist()
def sync_amazon_order(
	sales_order_name: str,
	sync_delivery_notes: int | str = 1,
	sync_sales_invoice: int | str = 0,
	amz_setting_name: str | None = None,
) -> dict:
	frappe.has_permission("Sales Order", "write", doc=sales_order_name, throw=True)
	so = frappe.get_doc("Sales Order", sales_order_name)
	amazon_order_id = so.get("amazon_order_id")
	if not amazon_order_id:
		frappe.throw(_("Sales Order {0} is not linked to an Amazon Order ID.").format(sales_order_name))

	resolved_setting = _resolve_amz_setting_name(amz_setting_name, company=so.company)
	frappe.has_permission("Amazon SP API Settings", "write", doc=resolved_setting, throw=True)
	repo = CustomAmazonRepository(resolved_setting)
	return repo.sync_single_order(
		amazon_order_id=amazon_order_id,
		sales_order_name=sales_order_name,
		sync_delivery_notes=bool(cint(sync_delivery_notes)),
		sync_sales_invoice=bool(cint(sync_sales_invoice)),
	)

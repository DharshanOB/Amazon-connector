# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from __future__ import annotations

import re
import time
import zlib
from datetime import datetime, timezone

import dateutil.parser
import frappe
from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note
from frappe import _
from frappe.utils import cint, flt, get_datetime, getdate, nowdate

from amazon_connector.amazon_ext.lifecycle import format_sp_api_utc_datetime
from amazon_connector.amazon_ext.listings import get_amazon_module_name
from amazon_connector.amazon_ext.outbound import (
	AmazonOutboundSync,
	get_configured_warehouses,
	get_expanded_warehouses,
	safe_create_amazon_log,
)
from amazon_connector.amazon_ext.repository import (
	CustomAmazonRepository as AmazonRepository,
)
from amazon_connector.amazon_ext.sp_api import Orders, SPAPIError

STANDARD_CARRIER_CODES = {
	"ups": "UPS",
	"upsn": "UPSN",
	"usps": "USPS",
	"fedex": "FedEx",
	"dhl": "DHL",
	"dhl express": "DHL",
	"blue dart": "Blue Dart",
	"bluedart": "Blue Dart",
	"delhivery": "Delhivery",
	"dtdc": "DTDC",
	"ecom express": "Ecom Express",
	"xpressbees": "Xpressbees",
	"shadowfax": "Shadowfax",
	"amazon logistics": "Amazon Logistics",
	"amzl": "AMZL",
	"royal mail": "Royal Mail",
	"dpd": "DPD",
	"canada post": "Canada Post",
	"australia post": "Australia Post",
	"ontrac": "OnTrac",
	"lasership": "LaserShip",
	"other": "Other",
}

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_SP_API_RETRIES = 3
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str | None) -> str:
	if not text:
		return ""
	return _HTML_TAG_RE.sub("", str(text)).strip()


def create_amazon_log(
	status: str = "Error",
	message: str | None = None,
	method: str | None = None,
	response_data: dict | str | None = None,
	request_data: dict | str | None = None,
	exception: Exception | None = None,
	rollback: bool = False,
	make_new: bool = False,
):
	"""Write plain-text Amazon integration log without leaking HTML or illegal commit/rollback."""
	clean_message = _strip_html(message) if message else None
	safe_method = (method or "").strip() or "amazon_connector.amazon_ext.fulfillment"
	return safe_create_amazon_log(
		status=status,
		message=clean_message,
		method=safe_method,
		response_data=response_data,
		request_data=request_data,
		exception=exception,
		rollback=rollback,
		make_new=make_new,
	)


def map_carrier(carrier_name: str | None) -> tuple[str, str]:
	"""Map a free-text ERPNext transporter name to (Amazon carrierCode, carrierName)."""
	cleaned = str(carrier_name or "").strip()
	if not cleaned:
		return ("Other", "Other")
	low = cleaned.lower()
	exact = STANDARD_CARRIER_CODES.get(low)
	if exact:
		return (exact, cleaned)

	for alias, code in sorted(STANDARD_CARRIER_CODES.items(), key=lambda kv: len(kv[0]), reverse=True):
		if alias == "other":
			continue
		if len(alias) >= 4:
			if alias in low:
				return (code, cleaned)
		elif re.search(rf"\b{re.escape(alias)}\b", low):
			return (code, cleaned)

	return ("Other", cleaned)


def format_iso_datetime(dt=None) -> str:
	"""Format a datetime/date/str or current timestamp as UTC ISO 8601 ending in Z."""
	target = datetime.now(timezone.utc) if dt is None else dt
	return format_sp_api_utc_datetime(target) or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_item_mapped_to_amazon(item_code: str | None) -> bool:
	"""Return True if an ERPNext Item has an Ecommerce Item mapping for Amazon."""
	if not item_code:
		return False
	return bool(
		frappe.db.exists(
			"Ecommerce Item",
			{"integration": get_amazon_module_name(), "erpnext_item_code": item_code},
		)
	)


def get_active_amazon_settings() -> list[frappe._dict]:
	"""Return active Amazon SP API Settings rows."""
	fields = ["name", "company", "warehouse", "price_list", "country", "use_sandbox", "enable_sync"]
	for extra_field in ("enable_listings_sync", "sync_inventory", "fba_warehouse"):
		if frappe.db.has_column("Amazon SP API Settings", extra_field):
			fields.append(extra_field)
	return frappe.get_all("Amazon SP API Settings", filters={"is_active": 1}, fields=fields)


def get_active_amazon_setting(company_or_dn_name: str | None = None) -> str | None:
	"""Resolve active Amazon SP API Settings name optionally scoped by Company or Delivery Note."""
	company = None
	if company_or_dn_name:
		if frappe.db.exists("Company", company_or_dn_name):
			company = company_or_dn_name
		elif frappe.db.exists("Delivery Note", company_or_dn_name):
			company = frappe.db.get_value("Delivery Note", company_or_dn_name, "company")

	if company:
		matched = frappe.db.get_value(
			"Amazon SP API Settings",
			filters={"is_active": 1, "company": company},
			fieldname="name",
		)
		if matched:
			return matched

	return frappe.db.get_value("Amazon SP API Settings", filters={"is_active": 1}, fieldname="name")


def handle_delivery_note_submit(doc, method=None):
	"""Delivery Note on_submit hook: confirm MFN shipment to Amazon unless FBA/imported/missing tracking."""
	if getattr(frappe.flags, "from_amazon_fba_sync", False):
		_update_dn_shipment_fields(doc.name, status="Skipped")
		return

	ctx = _extract_dn_amazon_context(doc)
	amazon_order_id = ctx.get("amazon_order_id")
	if not amazon_order_id:
		return

	if ctx.get("imported_from_amazon") or ctx.get("fulfillment_channel") == "AFN":
		_update_dn_shipment_fields(doc.name, status="Skipped")
		return

	if ctx.get("amazon_canceled"):
		err_msg = "Linked Amazon Sales Order is marked canceled."
		_update_dn_shipment_fields(doc.name, status="Failed", error=err_msg)
		create_amazon_log(
			status="Error",
			method="amazon_connector.amazon_ext.fulfillment.handle_delivery_note_submit",
			message=f"Delivery Note {doc.name}: {err_msg}",
			request_data={"delivery_note": doc.name, "amazon_order_id": amazon_order_id},
			rollback=False,
		)
		return

	amz_setting_name = get_active_amazon_setting(doc.name) or get_active_amazon_setting(doc.get("company"))
	if not amz_setting_name:
		return

	carrier_raw, tracking = _extract_dn_carrier_and_tracking(doc)
	if not tracking or not carrier_raw:
		missing = []
		if not carrier_raw:
			missing.append("carrier")
		if not tracking:
			missing.append("tracking number")
		err_msg = f"Missing {' and '.join(missing)}; enter carrier/tracking and confirm shipment manually."
		_update_dn_shipment_fields(
			doc.name,
			status="Pending",
			carrier=carrier_raw or None,
			tracking=tracking or None,
			error=err_msg,
		)
		create_amazon_log(
			status="Error",
			method="amazon_connector.amazon_ext.fulfillment.handle_delivery_note_submit",
			message=f"Delivery Note {doc.name} ({amazon_order_id}): {err_msg}",
			request_data={"delivery_note": doc.name, "amazon_order_id": amazon_order_id},
			rollback=False,
		)
		return

	carrier_code, carrier_name = map_carrier(carrier_raw)
	amz_setting = frappe.get_doc("Amazon SP API Settings", amz_setting_name)
	repo = AmazonRepository(amz_setting)
	orders_client = repo.get_orders_instance()
	order_items = resolve_order_item_ids(
		orders_client=orders_client,
		amazon_order_id=amazon_order_id,
		dn_items=list(doc.get("items") or []),
		amz_setting=amz_setting,
	)
	if not order_items:
		err_msg = f"Could not resolve Amazon OrderItemIds for order {amazon_order_id}."
		_update_dn_shipment_fields(
			doc.name,
			status="Failed",
			carrier=carrier_name,
			tracking=tracking,
			error=err_msg,
		)
		create_amazon_log(
			status="Error",
			method="amazon_connector.amazon_ext.fulfillment.handle_delivery_note_submit",
			message=f"Delivery Note {doc.name}: {err_msg}",
			request_data={"delivery_note": doc.name, "amazon_order_id": amazon_order_id},
			rollback=False,
		)
		return

	marketplace_id = ctx.get("marketplace_id") or AmazonOutboundSync(repo).get_marketplace_id()
	package_detail = _build_package_detail(
		delivery_note_name=doc.name,
		carrier_code=carrier_code,
		carrier_name=carrier_name,
		tracking_number=tracking,
		ship_date=doc.get("posting_date"),
		order_items=order_items,
	)

	_update_dn_shipment_fields(
		doc.name,
		status="Pending",
		carrier=carrier_name,
		tracking=tracking,
		error="",
	)

	enqueue_kwargs = dict(
		queue="short",
		delivery_note_name=doc.name,
		amazon_order_id=amazon_order_id,
		package_detail=package_detail,
		marketplace_id=marketplace_id,
		amz_setting_name=amz_setting_name,
	)
	if frappe.flags.in_test:
		frappe.enqueue(
			"amazon_connector.amazon_ext.fulfillment.confirm_delivery_note_shipment",
			now=True,
			**enqueue_kwargs,
		)
	else:
		frappe.enqueue(
			"amazon_connector.amazon_ext.fulfillment.confirm_delivery_note_shipment",
			job_id=f"amazon_confirm_shipment::{doc.name}",
			deduplicate=True,
			enqueue_after_commit=True,
			**enqueue_kwargs,
		)


def handle_delivery_note_cancel(doc, method=None):
	"""Delivery Note on_cancel hook: update Amazon shipment status & queue stock resync."""
	ctx = _extract_dn_amazon_context(doc)
	if ctx.get("amazon_order_id"):
		prev_status = doc.get("amazon_shipment_status") if frappe.db.has_column("Delivery Note", "amazon_shipment_status") else None
		if ctx.get("imported_from_amazon") or ctx.get("fulfillment_channel") == "AFN":
			new_status = "Skipped"
			note = None
		elif prev_status == "Confirmed":
			new_status = "Cancelled"
			note = "Delivery Note cancelled in ERPNext after Amazon confirmation; update Seller Central manually if needed."
		else:
			new_status = "Cancelled"
			note = "Delivery Note cancelled in ERPNext."
		_update_dn_shipment_fields(doc.name, status=new_status, error=note)

	if getattr(frappe.flags, "from_amazon_fba_sync", False):
		return
	if ctx.get("fulfillment_channel") == "AFN" or (
		ctx.get("imported_from_amazon") and ctx.get("fulfillment_channel") != "MFN"
	):
		return

	_enqueue_dn_items_stock_sync(doc)


@frappe.whitelist()
def confirm_delivery_note_shipment_now(delivery_note_name: str) -> dict:
	"""Manual Desk action to validate carrier/tracking and confirm Amazon shipment now."""
	doc = frappe.get_doc("Delivery Note", delivery_note_name)
	doc.check_permission("write")

	if doc.docstatus != 1:
		frappe.throw(_("Delivery Note {0} must be submitted before confirming Amazon shipment.").format(doc.name))

	ctx = _extract_dn_amazon_context(doc)
	amazon_order_id = ctx.get("amazon_order_id")
	if not amazon_order_id:
		frappe.throw(_("Delivery Note {0} is not linked to an Amazon Order ID.").format(doc.name))

	if ctx.get("imported_from_amazon") or ctx.get("fulfillment_channel") == "AFN":
		frappe.throw(_("Delivery Note {0} is Amazon-fulfilled or imported from Amazon and cannot be confirmed manually.").format(doc.name))

	if ctx.get("amazon_canceled"):
		frappe.throw(_("Linked Amazon Sales Order for Delivery Note {0} is marked canceled.").format(doc.name))

	carrier_raw, tracking = _extract_dn_carrier_and_tracking(doc)
	if not carrier_raw or not tracking:
		frappe.throw(_("Carrier and Tracking Number are required before confirming shipment with Amazon SP-API."))

	amz_setting_name = get_active_amazon_setting(doc.name) or get_active_amazon_setting(doc.get("company"))
	if not amz_setting_name:
		frappe.throw(_("No active Amazon SP API Settings found for company {0}.").format(doc.company))

	carrier_code, carrier_name = map_carrier(carrier_raw)
	amz_setting = frappe.get_doc("Amazon SP API Settings", amz_setting_name)
	repo = AmazonRepository(amz_setting)
	orders_client = repo.get_orders_instance()
	order_items = resolve_order_item_ids(
		orders_client=orders_client,
		amazon_order_id=amazon_order_id,
		dn_items=list(doc.get("items") or []),
		amz_setting=amz_setting,
	)
	if not order_items:
		frappe.throw(_("Could not resolve Amazon OrderItemIds for order {0}.").format(amazon_order_id))

	marketplace_id = ctx.get("marketplace_id") or AmazonOutboundSync(repo).get_marketplace_id()
	package_detail = _build_package_detail(
		delivery_note_name=doc.name,
		carrier_code=carrier_code,
		carrier_name=carrier_name,
		tracking_number=tracking,
		ship_date=doc.get("posting_date"),
		order_items=order_items,
	)
	result = confirm_delivery_note_shipment(
		delivery_note_name=doc.name,
		amazon_order_id=amazon_order_id,
		package_detail=package_detail,
		marketplace_id=marketplace_id,
		amz_setting_name=amz_setting_name,
	)
	return {
		"status": result.get("status"),
		"carrier": carrier_name,
		"tracking_number": tracking,
		"error": result.get("error"),
		"response": result.get("response"),
	}


@frappe.whitelist()
def retry_confirm_shipment(delivery_note_name: str) -> dict:
	"""Alias whitelisted helper for Delivery Note Retry Amazon Shipment button."""
	return confirm_delivery_note_shipment_now(delivery_note_name)


def confirm_delivery_note_shipment(
	delivery_note_name: str,
	amazon_order_id: str,
	package_detail: dict,
	marketplace_id: str | None = None,
	amz_setting_name: str | None = None,
) -> dict:
	"""Call SP-API Orders.confirm_shipment with retries and record status on Delivery Note."""
	if not amz_setting_name:
		amz_setting_name = get_active_amazon_setting(delivery_note_name)

	if not amz_setting_name:
		err_msg = "No active Amazon SP API Settings configured."
		_update_dn_shipment_fields(delivery_note_name, status="Failed", error=err_msg)
		return {"status": "Failed", "error": err_msg}

	pkg = package_detail.get("packageDetail", package_detail) if isinstance(package_detail, dict) else {}
	carrier_code = str(pkg.get("carrierCode") or "").strip()
	carrier_name = str(pkg.get("carrierName") or carrier_code).strip()
	tracking = str(pkg.get("trackingNumber") or "").strip()
	order_items = pkg.get("orderItems") or []

	if not tracking or not carrier_code or not order_items:
		err_msg = "Shipment confirmation skipped: non-empty carrierCode, trackingNumber, and orderItems are required."
		_update_dn_shipment_fields(
			delivery_note_name,
			status="Failed",
			carrier=carrier_name or None,
			tracking=tracking or None,
			error=err_msg,
		)
		create_amazon_log(
			status="Error",
			method="amazon_connector.amazon_ext.fulfillment.confirm_delivery_note_shipment",
			message=f"Delivery Note {delivery_note_name}: {err_msg}",
			request_data={
				"delivery_note_name": delivery_note_name,
				"amazon_order_id": amazon_order_id,
				"package_detail": pkg,
				"marketplace_id": marketplace_id,
				"amz_setting_name": amz_setting_name,
			},
			rollback=False,
		)
		return {"status": "Failed", "error": err_msg}

	amz_setting = frappe.get_doc("Amazon SP API Settings", amz_setting_name)
	repo = AmazonRepository(amz_setting)
	resolved_marketplace = marketplace_id or AmazonOutboundSync(repo).get_marketplace_id()
	orders_client = repo.get_orders_instance()

	try:
		response = _call_with_retries(
			orders_client.confirm_shipment,
			order_id=amazon_order_id,
			marketplace_id=resolved_marketplace,
			package_detail=pkg,
		)
		if response is None or response == "":
			response = {}

		if isinstance(response, dict) and response.get("errors"):
			raise SPAPIError(response)

		_update_dn_shipment_fields(
			delivery_note_name,
			status="Confirmed",
			carrier=carrier_name,
			tracking=tracking,
			error="",
		)
		create_amazon_log(
			status="Success",
			method="amazon_connector.amazon_ext.fulfillment.confirm_delivery_note_shipment",
			message=f"Shipment confirmed on Amazon for Delivery Note {delivery_note_name} (Order {amazon_order_id}).",
			request_data={
				"delivery_note_name": delivery_note_name,
				"amazon_order_id": amazon_order_id,
				"marketplace_id": resolved_marketplace,
				"package_detail": pkg,
			},
			response_data=response if isinstance(response, dict) else {"status_code": 204},
			rollback=False,
		)
		return {"status": "Confirmed", "response": response}
	except Exception as exc:
		err_msg = str(exc)[:500]
		_update_dn_shipment_fields(
			delivery_note_name,
			status="Failed",
			carrier=carrier_name,
			tracking=tracking,
			error=err_msg,
		)
		create_amazon_log(
			status="Error",
			method="amazon_connector.amazon_ext.fulfillment.confirm_delivery_note_shipment",
			message=f"Failed to confirm shipment for Delivery Note {delivery_note_name} (Order {amazon_order_id}): {err_msg}",
			request_data={
				"delivery_note_name": delivery_note_name,
				"amazon_order_id": amazon_order_id,
				"package_detail": pkg,
				"marketplace_id": resolved_marketplace,
				"amz_setting_name": amz_setting_name,
			},
			exception=exc,
			rollback=False,
			make_new=True,
		)
		return {"status": "Failed", "error": err_msg}


def resolve_order_item_ids(
	orders_client,
	amazon_order_id: str,
	dn_items: list,
	amz_setting=None,
) -> list[dict]:
	"""Map Delivery Note items to Amazon [{orderItemId, quantity}] with paginated get_order_items."""
	if not dn_items:
		return []

	resolved: dict[str, int] = {}
	unresolved = []
	for item in dn_items:
		order_item_id = item.get("amazon_order_item_id") if hasattr(item, "get") else getattr(item, "amazon_order_item_id", None)
		qty = int(round(flt(item.get("qty") if hasattr(item, "get") else getattr(item, "qty", 0))))
		if qty <= 0:
			continue
		if order_item_id:
			resolved[str(order_item_id)] = resolved.get(str(order_item_id), 0) + qty
		else:
			unresolved.append((item, qty))

	if not unresolved:
		return [{"orderItemId": oid, "quantity": q} for oid, q in resolved.items()]

	raw_order_items = _fetch_all_order_items(orders_client, amazon_order_id)
	if not raw_order_items:
		return [{"orderItemId": oid, "quantity": q} for oid, q in resolved.items()]

	item_to_skus = _map_items_to_amazon_skus(unresolved)
	sku_buckets: dict[str, list[dict]] = {}
	for raw in raw_order_items:
		seller_sku = str(raw.get("SellerSKU") or "").strip()
		if seller_sku:
			sku_buckets.setdefault(seller_sku, []).append(
				{
					"orderItemId": raw.get("OrderItemId"),
					"remaining": int(round(flt(raw.get("QuantityOrdered") or 0))) or 999999,
				}
			)

	for dn_item, qty in unresolved:
		item_code = dn_item.get("item_code") if hasattr(dn_item, "get") else getattr(dn_item, "item_code", None)
		candidates = list(item_to_skus.get(item_code, []))
		if item_code and item_code not in candidates:
			candidates.append(item_code)

		remaining_to_match = qty
		for sku in candidates:
			for bucket in sku_buckets.get(sku, []):
				if not bucket.get("orderItemId") or bucket["remaining"] <= 0 or remaining_to_match <= 0:
					continue
				take = min(remaining_to_match, bucket["remaining"])
				oid = str(bucket["orderItemId"])
				resolved[oid] = resolved.get(oid, 0) + take
				bucket["remaining"] -= take
				remaining_to_match -= take
			if remaining_to_match <= 0:
				break

		# Fallback: if order has exactly one remaining unmatched OrderItemId, assign by position
		if remaining_to_match > 0 and len(raw_order_items) == 1 and raw_order_items[0].get("OrderItemId"):
			oid = str(raw_order_items[0]["OrderItemId"])
			resolved[oid] = resolved.get(oid, 0) + remaining_to_match

	return [{"orderItemId": oid, "quantity": q} for oid, q in resolved.items() if q > 0]


def create_delivery_note_from_amazon_order(
	repo,
	order: dict,
	sales_order_name: str | None = None,
) -> str | None:
	"""Create or incrementally update an ERPNext Delivery Note from an Amazon order safely."""
	amazon_order_id = order.get("AmazonOrderId")
	order_status = order.get("OrderStatus")
	if not amazon_order_id or order_status not in ("Shipped", "PartiallyShipped"):
		return None

	savepoint_name = f"amazon_dn_{zlib.crc32(str(amazon_order_id).encode('utf-8')) % 1000000}"
	frappe.db.savepoint(savepoint_name)
	prev_sp_flag = getattr(frappe.flags, "in_amazon_sync_savepoint", False)
	frappe.flags.in_amazon_sync_savepoint = True
	try:
		so_name = sales_order_name
		if not so_name and frappe.db.has_column("Sales Order", "amazon_order_id"):
			so_name = frappe.db.get_value(
				"Sales Order",
				{"amazon_order_id": amazon_order_id, "docstatus": ["<", 2]},
				"name",
			)
		if not so_name:
			return None

		so_doc = frappe.get_doc("Sales Order", so_name)
		if so_doc.docstatus != 1:
			return None

		shipped_rows = _get_shipped_order_items(repo, order, amazon_order_id, order_status)
		if not shipped_rows:
			return None

		submitted_by_key, submitted_by_item, draft_dns = _summarize_existing_delivery_notes(amazon_order_id)
		target_by_key = _aggregate_target_shipped_qty(shipped_rows, so_doc, repo)
		if not target_by_key:
			return None

		channel = order.get("FulfillmentChannel") or so_doc.get("fulfillment_channel")
		target_warehouse = None
		if channel == "AFN" and getattr(repo, "amz_setting", None):
			target_warehouse = repo.amz_setting.get("fba_warehouse") or None

		if draft_dns:
			return _reconcile_open_draft_dn(
				draft_dns=draft_dns,
				so_doc=so_doc,
				target_by_key=target_by_key,
				submitted_by_key=submitted_by_key,
				submitted_by_item=submitted_by_item,
				order=order,
				target_warehouse=target_warehouse,
			)

		dn_doc = _build_new_delivery_note(
			so_doc=so_doc,
			target_by_key=target_by_key,
			submitted_by_key=submitted_by_key,
			submitted_by_item=submitted_by_item,
			order=order,
			target_warehouse=target_warehouse,
		)
		if not dn_doc:
			return None

		return _insert_and_try_submit_dn(dn_doc)
	except Exception as exc:
		frappe.db.rollback(save_point=savepoint_name)
		frappe.flags.in_amazon_sync_savepoint = prev_sp_flag
		create_amazon_log(
			status="Error",
			method="amazon_connector.amazon_ext.fulfillment.create_delivery_note_from_amazon_order",
			message=f"Failed to import Delivery Note for Amazon Order {amazon_order_id}: {exc}",
			request_data={"amazon_order_id": amazon_order_id, "order_status": order_status},
			exception=exc,
			rollback=False,
		)
		return None
	finally:
		frappe.flags.in_amazon_sync_savepoint = prev_sp_flag


def handle_sales_order_stock_reservation(doc, method=None):
	"""Sales Order on_submit / on_cancel hook: push updated reserved stock availability to Amazon."""
	if getattr(frappe.flags, "from_amazon_fba_sync", False):
		return
	channel = (
		doc.get("fulfillment_channel")
		if frappe.db.has_column("Sales Order", "fulfillment_channel")
		else None
	)
	if channel == "AFN":
		return
	_enqueue_dn_items_stock_sync(doc)


def handle_stock_ledger_entry(doc, method=None):
	"""Stock Ledger Entry on_submit hook: push accurate post-SLE net qty to Amazon."""
	if getattr(doc, "is_cancelled", 0):
		return
	if getattr(frappe.flags, "from_amazon_fba_sync", False):
		return
	if not doc.get("item_code") or not is_item_mapped_to_amazon(doc.item_code):
		return
	if frappe.db.get_value("Item", doc.item_code, "disabled"):
		return
	if _is_fba_voucher(doc):
		return

	active_settings = get_active_amazon_settings()
	for setting in active_settings:
		if not cint(setting.get("sync_inventory", 1)):
			continue
		configured_warehouses = get_configured_warehouses(setting)
		if configured_warehouses and doc.get("warehouse") not in configured_warehouses:
			continue

		if frappe.flags.in_test:
			post_sle_qty = _compute_post_sle_stock_qty(doc, setting)
			pending = getattr(frappe.flags, "_amazon_pending_sle_qty", None)
			if pending is None:
				pending = {}
				frappe.flags._amazon_pending_sle_qty = pending
			key = (setting.name, doc.item_code)
			pending[key] = post_sle_qty
			try:
				frappe.enqueue(
					"amazon_connector.amazon_ext.fulfillment.sync_stock_job",
					queue="short",
					now=True,
					amz_setting_name=setting.name,
					item_code=doc.item_code,
					warehouse=doc.get("warehouse"),
				)
			finally:
				pending.pop(key, None)
		else:
			frappe.enqueue(
				"amazon_connector.amazon_ext.fulfillment.sync_stock_job",
				queue="short",
				job_id=f"amazon_stock_sync::{setting.name}::{doc.item_code}",
				deduplicate=True,
				enqueue_after_commit=True,
				amz_setting_name=setting.name,
				item_code=doc.item_code,
				warehouse=doc.get("warehouse"),
			)


def sync_stock_job(amz_setting_name: str, item_code: str, warehouse: str | None = None):
	"""Background job pushing ERPNext stock quantity for one item to Amazon SP-API."""
	if not item_code or not frappe.db.exists("Item", item_code):
		return
	if frappe.db.get_value("Item", item_code, "disabled"):
		return

	pending = getattr(frappe.flags, "_amazon_pending_sle_qty", None) or {}
	stock_qty = pending.pop((amz_setting_name, item_code), None)

	try:
		amz_setting = frappe.get_doc("Amazon SP API Settings", amz_setting_name)
		if not cint(amz_setting.is_active) or not cint(amz_setting.get("sync_inventory", 1)):
			return
		repo = AmazonRepository(amz_setting)
		outbound = AmazonOutboundSync(repo)
		if stock_qty is None:
			stock_qty = outbound.get_stock_qty(item_code)
		outbound.update_stock_and_price(item_code=item_code, stock_qty=stock_qty, price=None)
	except Exception as exc:
		create_amazon_log(
			status="Error",
			method="amazon_connector.amazon_ext.fulfillment.sync_stock_job",
			message=f"Stock sync failed for Item {item_code}: {exc}",
			request_data={"amz_setting_name": amz_setting_name, "item_code": item_code, "warehouse": warehouse},
			exception=exc,
			rollback=False,
		)


def handle_item_price_change(doc, method=None):
	"""Item Price on_update hook: enqueue selling price push to Amazon SP-API."""
	if (
		getattr(getattr(doc, "flags", None), "from_amazon_sync", False)
		or frappe.flags.get("in_amazon_listing_sync")
		or frappe.flags.get("from_amazon_fba_sync")
	):
		return
	if not cint(getattr(doc, "selling", 0)):
		return
	if (
		doc.get("customer")
		or doc.get("batch_no")
		or flt(doc.get("packing_unit") or doc.get("min_qty") or 0) > 1.0
	):
		return
	if not doc.get("item_code") or not is_item_mapped_to_amazon(doc.item_code):
		return
	if frappe.db.get_value("Item", doc.item_code, "disabled"):
		return

	for setting in get_active_amazon_settings():
		if setting.get("price_list") and doc.get("price_list") and doc.price_list != setting.price_list:
			continue

		enqueue_kwargs = dict(
			queue="short",
			amz_setting_name=setting.name,
			item_code=doc.item_code,
			price_list=setting.get("price_list"),
		)
		if frappe.flags.in_test:
			frappe.enqueue(
				"amazon_connector.amazon_ext.fulfillment.sync_price_job",
				now=True,
				**enqueue_kwargs,
			)
		else:
			frappe.enqueue(
				"amazon_connector.amazon_ext.fulfillment.sync_price_job",
				job_id=f"amazon_price_sync::{setting.name}::{doc.item_code}",
				deduplicate=True,
				enqueue_after_commit=True,
				**enqueue_kwargs,
			)


def sync_price_job(amz_setting_name: str, item_code: str, price_list: str | None = None):
	"""Background job pushing active selling price for one item to Amazon SP-API."""
	if not item_code or not frappe.db.exists("Item", item_code):
		return
	if frappe.db.get_value("Item", item_code, "disabled"):
		return

	try:
		amz_setting = frappe.get_doc("Amazon SP API Settings", amz_setting_name)
		if not cint(amz_setting.is_active):
			return
		repo = AmazonRepository(amz_setting)
		outbound = AmazonOutboundSync(repo)
		price = outbound.get_item_price(item_code, price_list=price_list)
		if price <= 0:
			return
		outbound.update_stock_and_price(item_code=item_code, stock_qty=None, price=price)
	except Exception as exc:
		create_amazon_log(
			status="Error",
			method="amazon_connector.amazon_ext.fulfillment.sync_price_job",
			message=f"Price sync failed for Item {item_code}: {exc}",
			request_data={"amz_setting_name": amz_setting_name, "item_code": item_code, "price_list": price_list},
			exception=exc,
			rollback=False,
		)


def reconcile_amazon_stock_and_prices():
	"""Hourly scheduler: push only changed MFN stock and active selling price deltas to Amazon."""
	for setting in get_active_amazon_settings():
		if not cint(setting.get("enable_sync")):
			continue

		try:
			_reconcile_setting_items(setting)
		except Exception as exc:
			create_amazon_log(
				status="Error",
				method="amazon_connector.amazon_ext.fulfillment.reconcile_amazon_stock_and_prices",
				message=f"Hourly Amazon reconciliation failed for {setting.name}: {exc}",
				request_data={"amz_setting_name": setting.name},
				exception=exc,
				rollback=False,
			)


def _reconcile_setting_items(setting) -> None:
	amz_setting = frappe.get_doc("Amazon SP API Settings", setting.name)
	sync_inventory_enabled = cint(amz_setting.get("sync_inventory", 1))
	repo = AmazonRepository(amz_setting)
	outbound = AmazonOutboundSync(repo)
	configured_warehouses = get_configured_warehouses(amz_setting)
	afn_only_items = _get_afn_only_items(amz_setting)

	ecom_fields = ["name", "erpnext_item_code", "sku", "inventory_synced_on", "item_synced_on"]
	if frappe.db.has_column("Ecommerce Item", "status"):
		ecom_fields.append("status")

	mapped_items = frappe.get_all(
		"Ecommerce Item",
		filters={"integration": get_amazon_module_name()},
		fields=ecom_fields,
	)
	for mapped in mapped_items:
		item_code = mapped.get("erpnext_item_code")
		if not item_code:
			continue
		if str(mapped.get("status") or "").upper() in ("DELETED", "TRASHED"):
			continue
		if item_code in afn_only_items:
			continue

		item_meta = frappe.db.get_value("Item", item_code, ["disabled", "is_stock_item", "has_variants"], as_dict=True)
		if not item_meta or cint(item_meta.disabled) or cint(item_meta.has_variants):
			continue

		try:
			stock_to_push = None
			if (
				sync_inventory_enabled
				and cint(item_meta.is_stock_item)
				and _has_stock_delta(item_code, mapped.get("inventory_synced_on"), configured_warehouses)
			):
				stock_to_push = outbound.get_stock_qty(item_code)

			price_to_push = None
			if _has_price_delta(item_code, amz_setting.price_list, mapped.get("item_synced_on")):
				candidate_price = outbound.get_item_price(item_code, price_list=amz_setting.price_list)
				if candidate_price > 0:
					price_to_push = candidate_price

			if stock_to_push is None and price_to_push is None:
				continue

			outbound.update_stock_and_price(
				item_code=item_code,
				stock_qty=stock_to_push,
				price=price_to_push,
				sku=mapped.get("sku"),
			)
		except Exception as exc:
			create_amazon_log(
				status="Error",
				method="amazon_connector.amazon_ext.fulfillment.reconcile_amazon_stock_and_prices",
				message=f"Reconciliation skipped Item {item_code}: {exc}",
				request_data={"amz_setting_name": setting.name, "item_code": item_code},
				exception=exc,
				rollback=False,
			)


def _get_afn_only_items(amz_setting=None) -> set[str]:
	channels_by_item: dict[str, set[str]] = {}
	if frappe.db.has_column("Sales Order", "fulfillment_channel"):
		rows = frappe.db.sql(
			"""
			SELECT soi.item_code, so.fulfillment_channel
			FROM `tabSales Order Item` soi
			INNER JOIN `tabSales Order` so ON so.name = soi.parent
			WHERE so.docstatus < 2
			  AND so.fulfillment_channel IN ('AFN', 'MFN')
			GROUP BY soi.item_code, so.fulfillment_channel
			""",
			as_dict=True,
		)
		for row in rows:
			channels_by_item.setdefault(row.item_code, set()).add(row.fulfillment_channel)

	afn_only = {item for item, channels in channels_by_item.items() if "AFN" in channels and "MFN" not in channels}

	if amz_setting:
		fba_wh = (
			amz_setting.get("fba_warehouse")
			if isinstance(amz_setting, dict)
			else getattr(amz_setting, "fba_warehouse", None)
		)
		mfn_warehouses = set(get_configured_warehouses(amz_setting))
		if fba_wh:
			fba_warehouses = set(get_expanded_warehouses([fba_wh])) - mfn_warehouses
			if fba_warehouses:
				fba_bins = frappe.get_all(
					"Bin",
					filters={"warehouse": ["in", list(fba_warehouses)], "actual_qty": [">", 0]},
					pluck="item_code",
				)
				for item_code in set(fba_bins):
					if "MFN" in channels_by_item.get(item_code, set()):
						continue
					has_mfn_stock = False
					if mfn_warehouses:
						mfn_qty = frappe.get_all(
							"Bin",
							filters={"item_code": item_code, "warehouse": ["in", list(mfn_warehouses)], "actual_qty": [">", 0]},
							limit=1,
						)
						has_mfn_stock = bool(mfn_qty)
					if not has_mfn_stock:
						afn_only.add(item_code)

	return afn_only


def _has_stock_delta(item_code: str, last_synced_on, warehouses: tuple[str, ...]) -> bool:
	if not last_synced_on:
		return True
	last_sync_dt = get_datetime(last_synced_on)
	filters: dict = {"item_code": item_code}
	if warehouses:
		filters["warehouse"] = ["in", list(warehouses)]
	latest = frappe.get_all(
		"Bin",
		filters=filters,
		fields=["modified"],
		order_by="modified desc",
		limit=1,
	)
	latest_modified = latest[0].get("modified") if latest else None
	if not latest_modified:
		return False
	return get_datetime(latest_modified) > last_sync_dt


def _has_price_delta(item_code: str, price_list: str | None, last_synced_on) -> bool:
	filters = {"item_code": item_code, "selling": 1}
	if price_list:
		filters["price_list"] = price_list
	prices = frappe.get_all("Item Price", filters=filters, fields=["modified", "valid_from", "valid_upto"])
	if not prices:
		return False
	if not last_synced_on:
		return True
	last_sync_dt = get_datetime(last_synced_on)
	last_sync_date = getdate(last_sync_dt)
	today = getdate(nowdate())
	for row in prices:
		if get_datetime(row.modified) > last_sync_dt:
			return True
		if row.valid_from and last_sync_date < getdate(row.valid_from) <= today:
			return True
	return False


def _extract_dn_amazon_context(dn_doc) -> dict:
	amazon_order_id = dn_doc.get("amazon_order_id") if frappe.db.has_column("Delivery Note", "amazon_order_id") else None
	imported = cint(dn_doc.get("imported_from_amazon")) if frappe.db.has_column("Delivery Note", "imported_from_amazon") else 0
	channel = dn_doc.get("fulfillment_channel") if frappe.db.has_column("Delivery Note", "fulfillment_channel") else None
	marketplace_id = dn_doc.get("marketplace_id") if frappe.db.has_column("Delivery Note", "marketplace_id") else None
	amazon_canceled = 0

	so_names = sorted({item.get("against_sales_order") for item in (dn_doc.get("items") or []) if item.get("against_sales_order")})
	so_fields = ["name"]
	for field in ("amazon_order_id", "marketplace_id", "fulfillment_channel", "amazon_canceled", "amazon_order_status"):
		if frappe.db.has_column("Sales Order", field):
			so_fields.append(field)

	if len(so_fields) > 1:
		so_rows = []
		if so_names:
			so_rows = frappe.get_all("Sales Order", filters={"name": ["in", so_names]}, fields=so_fields)
		elif amazon_order_id and "amazon_order_id" in so_fields:
			so_rows = frappe.get_all(
				"Sales Order",
				filters={"amazon_order_id": amazon_order_id, "docstatus": ["<", 2]},
				fields=so_fields,
			)
		for row in so_rows:
			if not amazon_order_id and row.get("amazon_order_id"):
				amazon_order_id = row.get("amazon_order_id")
			if not marketplace_id and row.get("marketplace_id"):
				marketplace_id = row.get("marketplace_id")
			if not channel and row.get("fulfillment_channel"):
				channel = row.get("fulfillment_channel")
			if cint(row.get("amazon_canceled")) or str(row.get("amazon_order_status") or "").strip().lower() in ("canceled", "cancelled"):
				amazon_canceled = 1

	return {
		"amazon_order_id": amazon_order_id,
		"imported_from_amazon": imported,
		"fulfillment_channel": channel,
		"marketplace_id": marketplace_id,
		"amazon_canceled": amazon_canceled,
	}


def _extract_dn_carrier_and_tracking(dn_doc) -> tuple[str, str]:
	carrier = str(dn_doc.get("amazon_carrier") or dn_doc.get("transporter_name") or "").strip()
	if not carrier and dn_doc.get("transporter"):
		transporter_id = str(dn_doc.get("transporter")).strip()
		carrier = str(frappe.db.get_value("Supplier", transporter_id, "supplier_name") or transporter_id).strip()
	tracking = str(dn_doc.get("amazon_tracking_number") or dn_doc.get("lr_no") or dn_doc.get("tracking_number") or "").strip()
	return (carrier, tracking)


def _build_package_detail(
	delivery_note_name: str,
	carrier_code: str,
	carrier_name: str,
	tracking_number: str,
	ship_date,
	order_items: list[dict],
) -> dict:
	raw_ref = str(delivery_note_name or "1").strip()
	pkg_ref_id = (
		raw_ref
		if raw_ref.isdigit() and int(raw_ref) > 0
		else str((zlib.crc32(raw_ref.encode("utf-8")) % 900000) + 100000)
	)
	pkg = {
		"packageReferenceId": pkg_ref_id,
		"carrierCode": carrier_code,
		"carrierName": carrier_name or carrier_code,
		"trackingNumber": tracking_number,
		"shipDate": format_iso_datetime(ship_date),
		"orderItems": order_items,
	}
	return pkg


def _update_dn_shipment_fields(
	dn_name: str | None,
	status: str | None = None,
	carrier: str | None = None,
	tracking: str | None = None,
	error: str | None = None,
) -> None:
	if not dn_name or not frappe.db.exists("Delivery Note", dn_name):
		return

	updates = {}
	candidates = {
		"amazon_shipment_status": status,
		"amazon_carrier": carrier,
		"amazon_tracking_number": tracking,
		"amazon_last_sync_error": error,
	}
	for field, value in candidates.items():
		if value is not None and frappe.db.has_column("Delivery Note", field):
			updates[field] = value

	if updates:
		frappe.db.set_value("Delivery Note", dn_name, updates, update_modified=False)


def _call_with_retries(fn, *args, max_retries: int = MAX_SP_API_RETRIES, **kwargs):
	last_exc = None
	for attempt in range(max_retries + 1):
		try:
			return fn(*args, **kwargs)
		except SPAPIError as exc:
			last_exc = exc
			status_code = getattr(getattr(exc, "error_response", None), "status_code", None)
			err_text = str(exc)
			retryable = status_code in RETRYABLE_STATUS_CODES or "429" in err_text or "QuotaExceeded" in err_text or "503" in err_text
			if not retryable or attempt >= max_retries:
				raise
			time.sleep(min(2**attempt, 4) if not frappe.flags.in_test else 0)
		except Exception as exc:
			last_exc = exc
			if attempt >= max_retries or not _is_transient_network_error(exc):
				raise
			time.sleep(min(2**attempt, 4) if not frappe.flags.in_test else 0)
	if last_exc:
		raise last_exc


def _is_transient_network_error(exc: Exception) -> bool:
	text = f"{type(exc).__name__} {exc}".lower()
	return any(token in text for token in ("timeout", "timed out", "connection", "503", "502", "504", "429"))


def _fetch_all_order_items(orders_client, amazon_order_id: str) -> list[dict]:
	items: list[dict] = []
	next_token = None
	while True:
		response = _call_with_retries(
			orders_client.get_order_items,
			amazon_order_id,
			next_token=next_token,
		)
		payload = (response or {}).get("payload") or response or {}
		page_items = payload.get("OrderItems") or []
		items.extend(page_items)
		next_token = payload.get("NextToken")
		if not next_token:
			break
	return items


def _map_items_to_amazon_skus(unresolved: list[tuple]) -> dict[str, list[str]]:
	item_codes = sorted({(row.get("item_code") if hasattr(row, "get") else getattr(row, "item_code", None)) for row, _q in unresolved if (row.get("item_code") if hasattr(row, "get") else getattr(row, "item_code", None))})
	if not item_codes:
		return {}
	rows = frappe.get_all(
		"Ecommerce Item",
		filters={"integration": get_amazon_module_name(), "erpnext_item_code": ["in", item_codes]},
		fields=["erpnext_item_code", "sku", "integration_item_code"],
	)
	mapping: dict[str, list[str]] = {}
	for row in rows:
		skus = mapping.setdefault(row.erpnext_item_code, [])
		for candidate in (row.get("sku"), row.get("integration_item_code")):
			if candidate and candidate not in skus:
				skus.append(candidate)
	return mapping


def _get_shipped_order_items(repo, order: dict, amazon_order_id: str, order_status: str) -> list[dict]:
	raw_items = order.get("OrderItems")
	if not raw_items:
		if hasattr(repo, "get_raw_order_items"):
			raw_items = repo.get_raw_order_items(amazon_order_id) or []
		else:
			orders_client = repo.get_orders_instance()
			raw_items = _fetch_all_order_items(orders_client, amazon_order_id)

	shipped = []
	for raw in raw_items:
		if "QuantityShipped" in raw and raw.get("QuantityShipped") is not None:
			qty_shipped = flt(raw.get("QuantityShipped"))
		elif order_status == "Shipped":
			qty_shipped = flt(raw.get("QuantityOrdered") or 0)
		else:
			qty_shipped = 0.0
		if qty_shipped > 0:
			shipped.append((raw, qty_shipped))
	return shipped


def _summarize_existing_delivery_notes(amazon_order_id: str) -> tuple[dict[str, float], dict[str, float], list[str]]:
	if not frappe.db.has_column("Delivery Note", "amazon_order_id"):
		return ({}, {}, [])
	dns = frappe.get_all(
		"Delivery Note",
		filters={"amazon_order_id": amazon_order_id, "docstatus": ["<", 2]},
		fields=["name", "docstatus"],
		order_by="creation asc",
	)
	submitted_by_key: dict[str, float] = {}
	submitted_by_item: dict[str, float] = {}
	draft_names: list[str] = []
	has_oi_col = frappe.db.has_column("Delivery Note Item", "amazon_order_item_id")

	for dn in dns:
		if cint(dn.docstatus) == 0:
			draft_names.append(dn.name)
			continue
		item_fields = ["item_code", "qty"]
		if has_oi_col:
			item_fields.append("amazon_order_item_id")
		rows = frappe.get_all("Delivery Note Item", filters={"parent": dn.name}, fields=item_fields)
		for row in rows:
			oid = row.get("amazon_order_item_id")
			if oid:
				submitted_by_key[oid] = submitted_by_key.get(oid, 0.0) + flt(row.qty)
			if row.get("item_code"):
				submitted_by_item[row.item_code] = submitted_by_item.get(row.item_code, 0.0) + flt(row.qty)

	return (submitted_by_key, submitted_by_item, draft_names)


def _aggregate_target_shipped_qty(shipped_rows: list[tuple[dict, float]], so_doc, repo) -> dict[str, dict]:
	target: dict[str, dict] = {}
	so_by_oi = {}
	so_by_item = {}
	for so_item in so_doc.items:
		oid = so_item.get("amazon_order_item_id")
		if oid:
			so_by_oi[oid] = so_item
		so_by_item.setdefault(so_item.item_code, so_item)

	for raw, qty_shipped in shipped_rows:
		oid = raw.get("OrderItemId")
		item_code = None
		if oid and oid in so_by_oi:
			item_code = so_by_oi[oid].item_code
		else:
			item_code = repo.get_item_code(raw) if hasattr(repo, "get_item_code") else raw.get("SellerSKU")

		key = oid if (oid and oid in so_by_oi) else item_code
		if not key or (key not in so_by_oi and item_code not in so_by_item):
			continue

		entry = target.setdefault(key, {"item_code": item_code, "amazon_order_item_id": oid, "qty": 0.0})
		entry["qty"] += flt(qty_shipped)
	return target


def _reconcile_open_draft_dn(
	draft_dns: list[str],
	so_doc,
	target_by_key: dict[str, dict],
	submitted_by_key: dict[str, float],
	submitted_by_item: dict[str, float],
	order: dict,
	target_warehouse: str | None = None,
) -> str | None:
	draft_dn = frappe.get_doc("Delivery Note", draft_dns[0])
	draft_rows_by_key = {}
	draft_rows_by_item = {}
	for row in draft_dn.items:
		oid = row.get("amazon_order_item_id")
		if oid:
			draft_rows_by_key[oid] = row
		draft_rows_by_item[row.item_code] = row

	changed = False
	has_dn_oi = frappe.db.has_column("Delivery Note Item", "amazon_order_item_id")
	for key, spec in target_by_key.items():
		item_code = spec["item_code"]
		so_item = _find_matching_so_item(so_doc, key, item_code)
		max_open = max(0.0, flt(so_item.qty) - flt(so_item.delivered_qty)) if so_item else flt(spec["qty"])
		already_submitted = submitted_by_key.get(key)
		if already_submitted is None:
			already_submitted = submitted_by_item.get(item_code, 0.0)
		needed_in_draft = min(max_open, max(0.0, flt(spec["qty"]) - flt(already_submitted)))
		if needed_in_draft <= 0:
			continue

		existing_row = draft_rows_by_key.get(key) or draft_rows_by_item.get(item_code)
		if existing_row:
			if has_dn_oi and spec.get("amazon_order_item_id") and not existing_row.get("amazon_order_item_id") and existing_row.get("name"):
				# Quietly backfill missing order item reference without triggering a duplicate shipment return.
				frappe.db.set_value("Delivery Note Item", existing_row.name, "amazon_order_item_id", spec["amazon_order_item_id"], update_modified=False)
				existing_row.amazon_order_item_id = spec["amazon_order_item_id"]
			if needed_in_draft > flt(existing_row.qty):
				existing_row.qty = needed_in_draft
				changed = True
		else:
			if not so_item:
				continue
			new_row = {
				"item_code": so_item.item_code,
				"item_name": so_item.item_name,
				"description": so_item.description,
				"qty": needed_in_draft,
				"uom": so_item.uom,
				"stock_uom": so_item.stock_uom,
				"conversion_factor": so_item.conversion_factor,
				"rate": so_item.rate,
				"warehouse": target_warehouse or so_item.warehouse,
				"against_sales_order": so_doc.name,
				"so_detail": so_item.name,
			}
			if has_dn_oi and spec.get("amazon_order_item_id"):
				new_row["amazon_order_item_id"] = spec["amazon_order_item_id"]
			draft_dn.append("items", new_row)
			changed = True

	if not changed:
		return None

	draft_dn.flags.ignore_permissions = True
	prev_flag = getattr(frappe.flags, "from_amazon_fba_sync", False)
	frappe.flags.from_amazon_fba_sync = True
	try:
		draft_dn.save()
		return _try_submit_imported_dn(draft_dn)
	finally:
		frappe.flags.from_amazon_fba_sync = prev_flag


def _build_new_delivery_note(
	so_doc,
	target_by_key: dict[str, dict],
	submitted_by_key: dict[str, float],
	submitted_by_item: dict[str, float],
	order: dict,
	target_warehouse: str | None = None,
):
	remaining_map = {}
	for key, spec in target_by_key.items():
		already_submitted = submitted_by_key.get(key)
		if already_submitted is None:
			already_submitted = submitted_by_item.get(spec["item_code"], 0.0)
		rem = max(0.0, flt(spec["qty"]) - flt(already_submitted))
		if rem > 0:
			remaining_map[key] = rem

	if not remaining_map:
		return None

	dn_doc = make_delivery_note(so_doc.name)
	so_oi_by_detail = {item.name: item.get("amazon_order_item_id") for item in so_doc.items}
	has_dn_oi = frappe.db.has_column("Delivery Note Item", "amazon_order_item_id")

	kept_items = []
	for dn_item in dn_doc.items:
		oid = so_oi_by_detail.get(dn_item.get("so_detail")) or (
			target_by_key.get(dn_item.item_code, {}).get("amazon_order_item_id")
		)
		key = oid if oid in remaining_map else dn_item.item_code
		rem_qty = remaining_map.get(key, 0.0)
		if rem_qty <= 0:
			continue
		dn_item.qty = min(flt(dn_item.qty), rem_qty)
		remaining_map[key] = max(0.0, rem_qty - flt(dn_item.qty))
		if target_warehouse:
			dn_item.warehouse = target_warehouse
		if has_dn_oi and oid:
			dn_item.amazon_order_item_id = oid
		kept_items.append(dn_item)

	if not kept_items:
		return None

	dn_doc.items = kept_items
	if target_warehouse and hasattr(dn_doc, "set_warehouse"):
		dn_doc.set_warehouse = target_warehouse
	dn_doc.posting_date = _parse_safe_posting_date(order.get("LastUpdateDate") or order.get("PurchaseDate"))
	dn_doc.set_posting_time = 1

	carrier = str(order.get("CarrierName") or order.get("CarrierCode") or "").strip()
	tracking = str(order.get("TrackingNumber") or "").strip()
	if carrier:
		dn_doc.transporter_name = carrier
	if tracking:
		dn_doc.lr_no = tracking

	dn_fields = {
		"amazon_order_id": order.get("AmazonOrderId"),
		"imported_from_amazon": 1,
		"marketplace_id": order.get("MarketplaceId") or so_doc.get("marketplace_id"),
		"fulfillment_channel": order.get("FulfillmentChannel") or so_doc.get("fulfillment_channel"),
		"amazon_carrier": carrier or None,
		"amazon_tracking_number": tracking or None,
		"amazon_shipment_status": "Skipped",
	}
	for field, value in dn_fields.items():
		if value is not None and frappe.db.has_column("Delivery Note", field):
			setattr(dn_doc, field, value)

	return dn_doc


def _insert_and_try_submit_dn(dn_doc) -> str:
	dn_doc.flags.ignore_permissions = True
	prev_flag = getattr(frappe.flags, "from_amazon_fba_sync", False)
	frappe.flags.from_amazon_fba_sync = True
	try:
		dn_doc.insert()
		return _try_submit_imported_dn(dn_doc)
	finally:
		frappe.flags.from_amazon_fba_sync = prev_flag


def _try_submit_imported_dn(dn_doc) -> str:
	submit_sp = f"amazon_dn_submit_{zlib.crc32(str(dn_doc.name).encode('utf-8')) % 1000000}"
	frappe.db.savepoint(submit_sp)
	try:
		dn_doc.submit()
		return dn_doc.name
	except frappe.ValidationError as exc:
		frappe.db.rollback(save_point=submit_sp)
		create_amazon_log(
			status="Error",
			method="amazon_connector.amazon_ext.fulfillment.create_delivery_note_from_amazon_order",
			message=f"Saved Draft Delivery Note {dn_doc.name}; auto-submit deferred ({exc}).",
			request_data={"delivery_note": dn_doc.name, "amazon_order_id": dn_doc.get("amazon_order_id")},
			rollback=False,
		)
		return dn_doc.name


def _find_matching_so_item(so_doc, key: str, item_code: str):
	for item in so_doc.items:
		if item.get("amazon_order_item_id") == key:
			return item
	for item in so_doc.items:
		if item.item_code == item_code:
			return item
	return None


def _parse_safe_posting_date(value) -> str:
	if not value:
		return nowdate()
	try:
		if isinstance(value, str):
			parsed = dateutil.parser.parse(value)
			return parsed.date().isoformat()
		return getdate(value).isoformat()
	except Exception:
		return nowdate()


def _is_fba_voucher(sle_doc) -> bool:
	if sle_doc.get("voucher_type") != "Delivery Note" or not sle_doc.get("voucher_no"):
		return False
	dn_name = sle_doc.voucher_no
	has_channel = frappe.db.has_column("Delivery Note", "fulfillment_channel")
	has_imported = frappe.db.has_column("Delivery Note", "imported_from_amazon")

	fields = []
	if has_channel:
		fields.append("fulfillment_channel")
	if has_imported:
		fields.append("imported_from_amazon")

	if fields and frappe.db.exists("Delivery Note", dn_name):
		dn_meta = frappe.db.get_value("Delivery Note", dn_name, fields, as_dict=True) or {}
		channel = dn_meta.get("fulfillment_channel")
		if channel == "AFN":
			return True
		if cint(dn_meta.get("imported_from_amazon")) and channel != "MFN":
			return True

	if frappe.db.has_column("Sales Order", "fulfillment_channel"):
		afn_so = frappe.db.sql(
			"""
			SELECT so.name
			FROM `tabDelivery Note Item` dni
			INNER JOIN `tabSales Order` so ON so.name = dni.against_sales_order
			WHERE dni.parent = %s AND so.fulfillment_channel = 'AFN'
			LIMIT 1
			""",
			(dn_name,),
		)
		if afn_so:
			return True

	return False


def _compute_post_sle_stock_qty(sle_doc, setting) -> float:
	configured = get_configured_warehouses(setting)
	warehouses = configured or ((sle_doc.get("warehouse"),) if sle_doc.get("warehouse") else ())
	if not warehouses:
		return 0.0

	total_stock_uom_qty = 0.0
	for wh in warehouses:
		bin_row = frappe.db.get_value(
			"Bin",
			{"item_code": sle_doc.item_code, "warehouse": wh},
			["actual_qty", "reserved_qty"],
			as_dict=True,
		)
		reserved = flt(bin_row.reserved_qty) if bin_row else 0.0

		if wh == sle_doc.get("warehouse"):
			if (
				sle_doc.get("voucher_type") == "Stock Reconciliation"
				and sle_doc.get("qty_after_transaction") is not None
				and flt(sle_doc.get("actual_qty")) == 0
			):
				wh_actual = flt(sle_doc.get("qty_after_transaction"))
			elif sle_doc.get("name") and frappe.db.exists("Stock Ledger Entry", sle_doc.name):
				row = frappe.db.sql(
					"""
					SELECT COALESCE(SUM(actual_qty), 0) AS total_actual
					FROM `tabStock Ledger Entry`
					WHERE item_code = %s AND warehouse = %s AND is_cancelled = 0
					""",
					(sle_doc.item_code, wh),
					as_dict=True,
				)
				wh_actual = flt(row[0].total_actual) if row else 0.0
			else:
				wh_actual = flt(bin_row.actual_qty if bin_row else 0.0) + flt(sle_doc.get("actual_qty"))
		else:
			wh_actual = flt(bin_row.actual_qty if bin_row else 0.0)

		total_stock_uom_qty += max(0.0, wh_actual - reserved)

	item = frappe.get_cached_doc("Item", sle_doc.item_code)
	conv = _get_item_uom_conversion_factor(item)
	return max(0.0, total_stock_uom_qty / conv) if conv > 0 else total_stock_uom_qty


def _get_item_uom_conversion_factor(item) -> float:
	sales_uom = getattr(item, "sales_uom", None)
	stock_uom = getattr(item, "stock_uom", None)
	if not sales_uom or sales_uom == stock_uom:
		return 1.0
	for row in getattr(item, "uoms", None) or []:
		if row.get("uom") == sales_uom and flt(row.get("conversion_factor")) > 0:
			return flt(row.get("conversion_factor"))
	return 1.0


def _enqueue_dn_items_stock_sync(dn_doc) -> None:
	item_wh_pairs = []
	seen = set()
	for row in dn_doc.get("items") or []:
		item_code = row.get("item_code")
		if not item_code or item_code in seen or not is_item_mapped_to_amazon(item_code):
			continue
		if frappe.db.get_value("Item", item_code, "disabled"):
			continue
		seen.add(item_code)
		item_wh_pairs.append((item_code, row.get("warehouse")))

	if not item_wh_pairs:
		return

	for setting in get_active_amazon_settings():
		if not cint(setting.get("sync_inventory", 1)):
			continue
		configured = get_configured_warehouses(setting)
		for item_code, wh in item_wh_pairs:
			if configured and wh and wh not in configured:
				continue
			if frappe.flags.in_test:
				frappe.enqueue(
					"amazon_connector.amazon_ext.fulfillment.sync_stock_job",
					queue="short",
					now=True,
					amz_setting_name=setting.name,
					item_code=item_code,
					warehouse=wh,
				)
			else:
				frappe.enqueue(
					"amazon_connector.amazon_ext.fulfillment.sync_stock_job",
					queue="short",
					job_id=f"amazon_stock_sync::{setting.name}::{item_code}",
					deduplicate=True,
					enqueue_after_commit=True,
					amz_setting_name=setting.name,
					item_code=item_code,
					warehouse=wh,
				)

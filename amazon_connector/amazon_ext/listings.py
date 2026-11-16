# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import csv
import io
import time
from urllib.parse import urlparse

import frappe
from frappe import _
from frappe.utils import cint, flt, now
from frappe.utils.nestedset import get_root_of

from amazon_connector.amazon_ext.log_patch import create_log
from amazon_connector.amazon_ext.sp_api import SPAPIError

LISTINGS_REPORT_TYPE = "GET_MERCHANT_LISTINGS_ALL_DATA"
SANDBOX_REPORT_DOCUMENT_ID = "0356cf79-b8b0-4226-b4b9-0ee058ea5760"
SANDBOX_CREATE_REPORT_PARAMS = {
	"data_start_time": "2024-03-10T20:11:24.000Z",
	"marketplace_ids": ["A1PA6795UKMFR9", "ATVPDKIKX0DER"],
}
REPORT_POLL_INTERVAL_SECONDS = 5
REPORT_POLL_MAX_ATTEMPTS = 60


class AmazonListingSync:
	"""Fetches Amazon seller listings via SP-API Reports and syncs them to ERPNext Items."""

	def __init__(self, repository) -> None:
		self.repository = repository
		self.amz_setting = repository.amz_setting

	def sync(self) -> dict:
		listings = self.fetch_listings()
		created = 0
		updated = 0
		failed = 0
		synced_items = []
		errors = []

		for listing in listings:
			identifier = listing.get("ASIN") or listing.get("SellerSKU") or "Unknown"
			frappe.db.savepoint("amazon_listing_sync")
			prev_sp_flag = getattr(frappe.flags, "in_amazon_sync_savepoint", False)
			frappe.flags.in_amazon_sync_savepoint = True
			try:
				status, item_code = self.sync_listing(listing)
				if status == "created":
					created += 1
				else:
					updated += 1
				synced_items.append(item_code)
			except Exception as e:
				frappe.db.rollback(save_point="amazon_listing_sync")
				failed += 1
				errors.append({"identifier": identifier, "error": str(e)})
				frappe.log_error(
					message=frappe.get_traceback(),
					title=f"Amazon Listing Sync failed for {identifier}",
				)
			finally:
				frappe.flags.in_amazon_sync_savepoint = prev_sp_flag

		summary = {
			"total": len(listings),
			"created": created,
			"updated": updated,
			"failed": failed,
			"items": synced_items,
			"errors": errors,
		}
		setting_name = getattr(self.amz_setting, "name", None)
		if not failed and setting_name and frappe.db.exists("Amazon SP API Settings", setting_name):
			frappe.db.set_value(
				"Amazon SP API Settings", setting_name, "last_listings_sync", now(), update_modified=False
			)
		create_log(
			module_def=get_amazon_module_name(),
			status="Error" if (failed and not synced_items) else "Success",
			method="amazon_connector.amazon_ext.repository.get_listings",
			message=f"Amazon listing sync: {created} created, {updated} updated, {failed} failed (total {len(listings)}).",
			response_data=summary,
			make_new=True,
		)
		return summary

	def fetch_listings(self) -> list[dict]:
		report_text = self._fetch_report_text()
		return parse_listings_tsv(report_text)

	def sync_listing(self, listing: dict) -> tuple[str, str]:
		prev_listing_flag = getattr(frappe.flags, "in_amazon_listing_sync", False)
		frappe.flags.in_amazon_listing_sync = True
		try:
			catalog = self._fetch_catalog_details(listing.get("ASIN"))
			enriched = dict(listing)
			enriched["Title"] = (
				enriched.get("Title")
				or catalog.get("title")
				or enriched.get("SellerSKU")
				or enriched.get("ASIN")
			)
			enriched["Description"] = enriched.get("Description") or enriched["Title"]
			price = enriched.get("Price") if enriched.get("Price") is not None else catalog.get("list_price")

			existing_item_code = self._find_existing_item(enriched)

			if existing_item_code:
				status = "updated"
				item_code = existing_item_code
				self._update_erpnext_item(item_code, enriched, catalog)
			else:
				status = "created"
				item_code = self._create_erpnext_item(enriched, catalog)

			if price is not None and flt(price) > 0:
				upsert_item_price(item_code, self.amz_setting.price_list, flt(price))

			if cint(self.amz_setting.get("import_opening_stock", 1)) and flt(enriched.get("Quantity")) > 0:
				self._ensure_opening_stock(
					item_code=item_code,
					qty=flt(enriched.get("Quantity")),
					valuation_rate=0.0,
					fulfillment_channel=enriched.get("FulfillmentChannel"),
				)

			ensure_ecommerce_item(
				integration_item_code=enriched.get("ASIN") or enriched.get("SellerSKU"),
				item_code=item_code,
				sku=enriched.get("SellerSKU"),
			)
			return status, item_code
		finally:
			frappe.flags.in_amazon_listing_sync = prev_listing_flag

	def _find_existing_item(self, enriched: dict) -> str | None:
		lookup_field, lookup_value = get_item_lookup(self.amz_setting.amazon_fields_map, enriched)
		item_code = frappe.db.get_value(
			"Item", filters={lookup_field: lookup_value}, fieldname="item_code"
		)
		if item_code:
			return item_code

		integration = get_amazon_module_name()
		for fieldname, value in (
			("integration_item_code", enriched.get("ASIN")),
			("sku", enriched.get("SellerSKU")),
		):
			if not value:
				continue
			mapped_code = frappe.db.get_value(
				"Ecommerce Item",
				filters={"integration": integration, fieldname: value},
				fieldname="erpnext_item_code",
			)
			if mapped_code and frappe.db.exists("Item", mapped_code):
				return mapped_code
		return None

	def _update_erpnext_item(self, item_code: str, listing: dict, catalog: dict) -> None:
		item = frappe.get_doc("Item", item_code)
		dirty = False

		title = (listing.get("Title") or "").strip()[:140]
		if title and item.item_name != title:
			item.item_name = title
			dirty = True

		description = (listing.get("Description") or "").strip()
		if description and item.description != description:
			item.description = description
			dirty = True

		if catalog.get("brand"):
			brand = ensure_brand(catalog.get("brand"))
			if brand and item.brand != brand:
				item.brand = brand
				dirty = True

		if catalog.get("manufacturer"):
			manufacturer = ensure_manufacturer(catalog.get("manufacturer"))
			if manufacturer and item.default_item_manufacturer != manufacturer:
				item.default_item_manufacturer = manufacturer
				dirty = True

		safe_image = resolve_safe_catalog_image_url(catalog.get("image"))
		if safe_image and item.image != safe_image:
			item.image = safe_image
			dirty = True

		if (
			catalog.get("product_type")
			and frappe.db.has_column("Item", "amazon_product_type")
			and not item.get("amazon_product_type")
		):
			item.amazon_product_type = catalog.get("product_type")
			dirty = True

		if dirty:
			item.flags.from_integration = True
			item.save(ignore_permissions=True)

	def _ensure_opening_stock(
		self,
		item_code: str,
		qty: float,
		valuation_rate: float = 0.0,
		fulfillment_channel: str | None = None,
	) -> str | None:
		configured_wh = (
			(self.amz_setting.get("fba_warehouse") or self.amz_setting.warehouse)
			if fulfillment_channel == "AFN"
			else self.amz_setting.warehouse
		)
		if flt(qty) <= 0 or not configured_wh:
			return None
		if not cint(frappe.db.get_value("Item", item_code, "is_stock_item")):
			return None

		target_warehouse = _resolve_target_warehouse(configured_wh)
		if not target_warehouse:
			return None

		marker = f"Amazon Opening Stock: {item_code}"
		if frappe.db.exists("Stock Entry", {"docstatus": 1, "remarks": marker}):
			return None
		current_qty = flt(
			frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": target_warehouse}, "actual_qty")
		)
		if current_qty > 0:
			return None

		# Only use true cost (explicit cost valuation_rate, Item.valuation_rate, or Buying Item Price).
		# Never use the retail selling price as inventory cost valuation.
		rate = flt(valuation_rate)
		if rate <= 0:
			rate = flt(frappe.db.get_value("Item", item_code, "valuation_rate"))
		if rate <= 0:
			rate = flt(
				frappe.db.get_value(
					"Item Price",
					{"item_code": item_code, "buying": 1},
					"price_list_rate",
				)
			)

		if rate > 0 and flt(frappe.db.get_value("Item", item_code, "valuation_rate")) <= 0:
			frappe.db.set_value("Item", item_code, "valuation_rate", rate, update_modified=False)

		se = frappe.new_doc("Stock Entry")
		se.stock_entry_type = "Material Receipt"
		se.company = self.amz_setting.company
		se.to_warehouse = target_warehouse
		se.remarks = marker
		se.append(
			"items",
			{
				"item_code": item_code,
				"t_warehouse": target_warehouse,
				"qty": flt(qty),
				"basic_rate": rate,
				"allow_zero_valuation_rate": 1 if rate <= 0 else 0,
			},
		)
		# Guard against SLE hook bouncing imported opening stock back to Amazon.
		prev_flag = getattr(frappe.flags, "from_amazon_fba_sync", False)
		frappe.flags.from_amazon_fba_sync = True
		se.flags.from_amazon_import = True
		try:
			se.insert(ignore_permissions=True)
			se.submit()
		finally:
			frappe.flags.from_amazon_fba_sync = prev_flag
		return se.name

	def _create_erpnext_item(self, listing: dict, catalog: dict) -> str:
		item = frappe.new_doc("Item")
		apply_amazon_fields_map(item, self.amz_setting.amazon_fields_map, listing)

		if not item.item_name:
			item.item_name = listing.get("Title") or item.item_code
		if item.item_name and len(item.item_name) > 140:
			item.item_name = item.item_name[:140]
		if not item.description:
			item.description = listing.get("Description") or listing.get("Title") or item.item_name

		item.item_group = ensure_item_group(
			catalog.get("product_group"), self.amz_setting.parent_item_group
		)
		item.brand = ensure_brand(catalog.get("brand"))
		item.default_item_manufacturer = ensure_manufacturer(catalog.get("manufacturer"))
		safe_image = resolve_safe_catalog_image_url(catalog.get("image"))
		if safe_image:
			item.image = safe_image
		if catalog.get("product_type") and frappe.db.has_column("Item", "amazon_product_type"):
			item.amazon_product_type = catalog.get("product_type")

		item.is_sales_item = 1
		item.is_stock_item = 1
		item.flags.from_integration = True
		item.insert(ignore_permissions=True)
		return item.item_code

	def _fetch_report_text(self) -> str:
		reports = self.repository.get_reports_instance()
		try:
			conf_endpoint = frappe.conf.get("amazon_sp_api_endpoint") if getattr(frappe, "conf", None) else None
		except (RuntimeError, AttributeError):
			conf_endpoint = None
		custom_endpoint = (
			(self.amz_setting.get("custom_endpoint") or "").strip()
			or (conf_endpoint or "").strip()
			or getattr(reports, "custom_endpoint", None)
		)

		if self.amz_setting.get("use_sandbox") and not custom_endpoint:
			sandbox_params = {
				"data_start_time": SANDBOX_CREATE_REPORT_PARAMS["data_start_time"],
				"marketplace_ids": (
					[reports.marketplace_id]
					if getattr(reports, "marketplace_id", None)
					else SANDBOX_CREATE_REPORT_PARAMS["marketplace_ids"]
				),
			}
			create_resp = self.repository.call_sp_api_method(
				sp_api_method=reports.create_report,
				report_type=LISTINGS_REPORT_TYPE,
				**sandbox_params,
			)
			report_id = create_resp.get("reportId")
			report = self.repository.call_sp_api_method(
				sp_api_method=reports.get_report,
				report_id=report_id,
			)
			report_document_id = report.get("reportDocumentId") or SANDBOX_REPORT_DOCUMENT_ID
		else:
			create_resp = self.repository.call_sp_api_method(
				sp_api_method=reports.create_report,
				report_type=LISTINGS_REPORT_TYPE,
				marketplace_ids=[reports.marketplace_id],
			)
			report_id = create_resp.get("reportId")
			report_document_id = self._wait_for_report_document_id(reports, report_id)

		report_document = self.repository.call_sp_api_method(
			sp_api_method=reports.get_report_document,
			report_document_id=report_document_id,
		)
		return reports.download_report_document(report_document)

	def _wait_for_report_document_id(self, reports, report_id: str) -> str:
		for _attempt in range(REPORT_POLL_MAX_ATTEMPTS):
			report = self.repository.call_sp_api_method(
				sp_api_method=reports.get_report,
				report_id=report_id,
			)
			status = report.get("processingStatus")
			if status == "DONE":
				report_document_id = report.get("reportDocumentId")
				if not report_document_id:
					frappe.throw(_("Amazon report {0} completed without a reportDocumentId.").format(report_id))
				return report_document_id
			if status in ("CANCELLED", "FATAL"):
				frappe.throw(_("Amazon report {0} failed with status {1}.").format(report_id, status))
			time.sleep(REPORT_POLL_INTERVAL_SECONDS)

		frappe.throw(_("Timed out waiting for Amazon report {0} to complete.").format(report_id))

	def _fetch_catalog_details(self, asin: str | None) -> dict:
		if not asin:
			return {}

		if hasattr(self.repository, "get_catalog_items_v2_instance"):
			catalog_v2 = self.repository.get_catalog_items_v2_instance()
			if catalog_v2:
				payload = _call_catalog_with_rate_limit(catalog_v2.get_catalog_item, asin=asin)
				if payload:
					return normalize_catalog_v2_item(payload)

		if hasattr(self.repository, "get_catalog_items_instance"):
			catalog_v0 = self.repository.get_catalog_items_instance()
			if catalog_v0:
				result = _call_catalog_with_rate_limit(catalog_v0.get_catalog_item, asin=asin)
				payload = (
					result.get("payload") if isinstance(result, dict) and "payload" in result else result
				)
				if payload:
					return normalize_catalog_v0_item(payload)

		return {}


def parse_listings_tsv(report_text: str) -> list[dict]:
	"""Parse an Amazon GET_MERCHANT_LISTINGS_ALL_DATA tab-delimited report."""
	if not report_text or not report_text.strip():
		return []

	reader = csv.DictReader(io.StringIO(report_text), delimiter="\t", quoting=csv.QUOTE_NONE)
	listings = []
	for row in reader:
		asin = (row.get("asin1") or row.get("product-id") or "").strip()
		seller_sku = (row.get("seller-sku") or "").strip()
		if not asin and not seller_sku:
			continue

		raw_price = (row.get("price") or "").strip()
		raw_qty = (row.get("quantity") or "").strip()
		raw_fc = (row.get("fulfillment-channel") or "").strip().upper()
		fulfillment_channel = (
			"AFN" if (raw_fc.startswith("AMAZON") or raw_fc == "AFN") else "MFN"
		)
		listings.append(
			{
				"ASIN": asin,
				"SellerSKU": seller_sku,
				"Title": (row.get("item-name") or "").strip(),
				"Description": (row.get("item-description") or "").strip(),
				"Price": flt(raw_price) if raw_price else None,
				"Quantity": flt(raw_qty) if raw_qty else 0.0,
				"ListingId": (row.get("listing-id") or "").strip(),
				"FulfillmentChannel": fulfillment_channel,
			}
		)
	return listings


def normalize_catalog_v2_item(payload: dict) -> dict:
	summaries = payload.get("summaries") or []
	summary = summaries[0] if summaries else {}
	classifications = payload.get("classifications") or []
	class_list = classifications[0].get("classifications", []) if classifications else []
	product_types = payload.get("productTypes") or []
	images_groups = payload.get("images") or []
	images = images_groups[0].get("images", []) if images_groups else []
	attributes = payload.get("attributes") or {}

	product_type = product_types[0].get("productType") if product_types else None
	product_group = (
		summary.get("websiteDisplayGroupName")
		or (summary.get("browseClassification") or {}).get("displayName")
		or (class_list[0].get("displayName") if class_list else None)
		or product_type
	)
	main_image = next((img.get("link") for img in images if img.get("variant") == "MAIN"), None)
	if not main_image and images:
		main_image = images[0].get("link")

	list_price = _extract_v2_list_price(attributes, summary)

	return {
		"title": summary.get("itemName"),
		"product_group": product_group,
		"product_type": product_type,
		"brand": summary.get("brand"),
		"manufacturer": summary.get("manufacturer"),
		"list_price": list_price,
		"image": main_image,
	}


def _extract_v2_list_price(attributes: dict, summary: dict) -> float | None:
	raw_lp = attributes.get("list_price")
	if isinstance(raw_lp, list) and raw_lp:
		raw_lp = raw_lp[0]
	if isinstance(raw_lp, dict):
		val = raw_lp.get("value") if raw_lp.get("value") is not None else raw_lp.get("amount")
		if val is not None:
			return flt(val)

	summary_lp = summary.get("listPrice") or {}
	if isinstance(summary_lp, dict):
		val = summary_lp.get("amount") if summary_lp.get("amount") is not None else summary_lp.get("Amount")
		if val is not None:
			return flt(val)
	return None


def normalize_catalog_v0_item(payload: dict) -> dict:
	attr_sets = payload.get("AttributeSets") or []
	attrs = attr_sets[0] if attr_sets else {}
	return {
		"title": attrs.get("Title"),
		"product_group": attrs.get("ProductGroup"),
		"brand": attrs.get("Brand"),
		"manufacturer": attrs.get("Manufacturer"),
		"list_price": (attrs.get("ListPrice") or {}).get("Amount"),
		"image": (attrs.get("SmallImage") or {}).get("URL"),
	}


def get_item_lookup(amazon_fields_map, source_data: dict) -> tuple[str, str]:
	for field_map in amazon_fields_map:
		if field_map.use_to_find_item_code:
			value = source_data.get(field_map.amazon_field)
			if not value:
				frappe.throw(
					_("Amazon field {0} is missing in listing data.").format(
						frappe.bold(field_map.amazon_field)
					)
				)
			return field_map.item_field, value

	frappe.throw(_("At least one field must be selected to find the item code."))


def apply_amazon_fields_map(item, amazon_fields_map, source_data: dict) -> None:
	for field_map in amazon_fields_map:
		value = source_data.get(field_map.amazon_field)
		if field_map.use_to_find_item_code:
			item.item_code = value
		if field_map.item_field and value is not None:
			setattr(item, field_map.item_field, value)


def ensure_item_group(item_group_name: str | None, parent_item_group: str) -> str:
	if not item_group_name:
		return parent_item_group

	existing = frappe.db.get_value("Item Group", filters={"item_group_name": item_group_name})
	if existing:
		return existing

	parent_is_group = frappe.db.get_value("Item Group", parent_item_group, "is_group")
	effective_parent = (
		parent_item_group if parent_is_group else (get_root_of("Item Group") or parent_item_group)
	)
	new_item_group = frappe.new_doc("Item Group")
	new_item_group.item_group_name = item_group_name
	new_item_group.parent_item_group = effective_parent
	new_item_group.insert(ignore_permissions=True)
	return new_item_group.item_group_name


def _ensure_master_record(doctype: str, fieldname: str, value: str | None) -> str | None:
	if not value:
		return None

	existing = frappe.db.get_value(doctype, filters={fieldname: value})
	if existing:
		return existing

	doc = frappe.new_doc(doctype)
	setattr(doc, fieldname, value)
	doc.insert(ignore_permissions=True)
	return getattr(doc, fieldname)


def ensure_brand(brand_name: str | None) -> str | None:
	return _ensure_master_record("Brand", "brand", brand_name)


def ensure_manufacturer(manufacturer_name: str | None) -> str | None:
	return _ensure_master_record("Manufacturer", "short_name", manufacturer_name)


def _resolve_target_warehouse(warehouse: str) -> str | None:
	if not warehouse:
		return None
	wh_meta = frappe.db.get_value("Warehouse", warehouse, ["is_group", "lft", "rgt"], as_dict=True)
	if not wh_meta:
		return None
	if not wh_meta.is_group:
		return warehouse
	return frappe.db.get_value(
		"Warehouse",
		{"lft": (">", wh_meta.lft), "rgt": ("<", wh_meta.rgt), "is_group": 0, "disabled": 0},
		"name",
		order_by="lft asc",
	)


def upsert_item_price(item_code: str, price_list: str, rate: float) -> None:
	rate = flt(rate)
	candidates = frappe.get_all(
		"Item Price",
		filters={"item_code": item_code, "price_list": price_list, "selling": 1},
		fields=["name", "customer", "batch_no", "packing_unit"],
		order_by="modified desc",
	)
	existing_name = next(
		(
			r.name
			for r in candidates
			if not r.get("customer") and not r.get("batch_no") and flt(r.get("packing_unit")) <= 1.0
		),
		None,
	)
	if existing_name:
		if rate > 0:
			frappe.db.set_value("Item Price", existing_name, "price_list_rate", rate, update_modified=False)
		return

	if rate <= 0:
		return

	item_price = frappe.new_doc("Item Price")
	item_price.price_list = price_list
	item_price.price_list_rate = rate
	item_price.item_code = item_code
	item_price.selling = 1
	item_price.flags.from_amazon_sync = True
	item_price.insert(ignore_permissions=True)


def ensure_ecommerce_item(integration_item_code: str, item_code: str, sku: str | None = None) -> None:
	integration = get_amazon_module_name()
	existing = frappe.db.get_value(
		"Ecommerce Item",
		filters={
			"integration": integration,
			"erpnext_item_code": item_code,
			"integration_item_code": integration_item_code,
		},
		fieldname="name",
	)
	if existing:
		frappe.db.set_value("Ecommerce Item", existing, "item_synced_on", now())
		return

	if sku and frappe.db.exists("Ecommerce Item", {"integration": integration, "sku": sku}):
		return

	ecommerce_item = frappe.new_doc("Ecommerce Item")
	ecommerce_item.integration = integration
	ecommerce_item.erpnext_item_code = item_code
	ecommerce_item.integration_item_code = integration_item_code
	ecommerce_item.sku = sku
	ecommerce_item.item_synced_on = now()
	ecommerce_item.insert(ignore_permissions=True)


def get_amazon_module_name() -> str:
	return frappe.get_meta("Amazon SP API Settings").module


def resolve_safe_catalog_image_url(image_url: str | None) -> str | None:
	"""Return image_url only when it is a reachable HTTP(S) URL safe to attach on Item."""
	if not image_url or not isinstance(image_url, str):
		return None

	candidate = image_url.strip()
	if not candidate:
		return None
	if candidate.startswith(("/files/", "/private/files/")):
		return candidate

	parsed = urlparse(candidate)
	if parsed.scheme not in ("http", "https") or not parsed.hostname:
		return None

	hostname = parsed.hostname.lower().rstrip(".")
	if hostname in {"localhost", "127.0.0.1", "::1"} or hostname.endswith(".local") or "." not in hostname:
		return None

	try:
		frappe.utils.get_url()
	except Exception:
		return None

	return candidate


def _call_catalog_with_rate_limit(method, **kwargs) -> dict | None:
	for attempt in range(2):
		try:
			return method(**kwargs)
		except SPAPIError as e:
			if "QuotaExceeded" in str(e.error) or "TooManyRequests" in str(e.error) or "429" in str(e.error):
				if attempt == 0:
					time.sleep(1)
					continue
			return None
		except Exception:
			return None
	return None


def fetch_amazon_listings(amz_setting_name: str | None = None, print_output: bool = True) -> list[dict]:
	"""CLI / terminal helper to fetch and preview Amazon SP-API listings."""
	from amazon_connector.amazon_ext.repository import CustomAmazonRepository

	if not amz_setting_name:
		amz_setting_name = (
			frappe.db.get_value("Amazon SP API Settings", {"is_active": 1}, "name")
			or frappe.db.get_value("Amazon SP API Settings", {}, "name")
		)

	setting = frappe.get_doc("Amazon SP API Settings", amz_setting_name)
	repo = CustomAmazonRepository(setting)
	listings = AmazonListingSync(repo).fetch_listings()

	if print_output:
		print(f"\nFetched {len(listings)} listings from Amazon SP-API ({setting.name}):\n" + "-" * 85)
		for item in listings:
			sku = item.get("SellerSKU") or "-"
			asin = item.get("ASIN") or "-"
			price = item.get("Price") or "-"
			qty = item.get("Quantity") or "-"
			title = (item.get("Title") or "")[:45]
			print(f"SKU: {sku:<15} | ASIN: {asin:<12} | Qty: {str(qty):<5} | Price: ₹{str(price):<8} | {title}")
		print("-" * 85 + "\n")

	return listings

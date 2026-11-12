# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import json
import time

import frappe
from frappe import _
from frappe.utils import flt, getdate, now, strip_html, today
from frappe.utils.nestedset import get_descendants_of
from requests.exceptions import RequestException

from amazon_connector.amazon_ext.listings import get_amazon_module_name
from amazon_connector.amazon_ext.log_patch import create_log
from amazon_connector.amazon_ext.sp_api import SPAPIError

DEFAULT_ITEM_METHOD = "amazon_connector.amazon_ext.outbound.create_or_update_listing"
DEFAULT_PATCH_METHOD = "amazon_connector.amazon_ext.outbound.update_stock_and_price"
DEFAULT_DELETE_METHOD = "amazon_connector.amazon_ext.outbound.delete_amazon_listing_by_sku"
DEFAULT_FETCH_METHOD = "amazon_connector.amazon_ext.outbound.fetch_amazon_listing"

COUNTRY_CURRENCIES = {
	"US": "USD",
	"CA": "CAD",
	"MX": "MXN",
	"BR": "BRL",
	"GB": "GBP",
	"DE": "EUR",
	"FR": "EUR",
	"IT": "EUR",
	"ES": "EUR",
	"NL": "EUR",
	"SE": "SEK",
	"PL": "PLN",
	"TR": "TRY",
	"EG": "EGP",
	"SA": "SAR",
	"AE": "AED",
	"IN": "INR",
	"SG": "SGD",
	"AU": "AUD",
	"JP": "JPY",
}

TRANSIENT_SPAPI_CODES = {
	"429",
	"500",
	"502",
	"503",
	"504",
	"QuotaExceeded",
	"TooManyRequests",
	"RequestThrottled",
	"ThrottlingException",
	"InternalFailure",
	"ServiceUnavailable",
}


class AmazonOutboundSync:
	"""Handles outbound listings, inventory, price, and schema sync to Amazon SP-API."""

	def __init__(self, repository) -> None:
		if isinstance(repository, str) or (
			hasattr(repository, "doctype") and repository.doctype == "Amazon SP API Settings"
		):
			from amazon_connector.amazon_ext.repository import (
				CustomAmazonRepository as AmazonRepository,
			)

			repository = AmazonRepository(repository)

		self.repository = repository
		self.amz_setting = repository.amz_setting
		self._ptd_cache: dict[tuple[str, str], dict] = {}

	def get_seller_id(self, seller_id: str | None = None) -> str:
		"""Resolve seller/merchant identifier from override, settings, or fallback."""
		if isinstance(seller_id, str) and seller_id.strip():
			return seller_id.strip()

		for attr in ("seller_id", "merchant_id"):
			val = self.amz_setting.get(attr) if hasattr(self.amz_setting, "get") else getattr(self.amz_setting, attr, None)
			if isinstance(val, str) and val.strip():
				return val.strip()

		for attr in ("seller_id", "merchant_id"):
			val = getattr(self.repository, attr, None)
			if isinstance(val, str) and val.strip():
				return val.strip()

		fallback = (
			(self.amz_setting.get("client_id") if hasattr(self.amz_setting, "get") else None)
			or getattr(self.amz_setting, "client_id", None)
			or getattr(self.amz_setting, "name", None)
		)
		return str(fallback or "").strip()

	def get_marketplace_id(self, marketplace_id: str | None = None) -> str:
		if isinstance(marketplace_id, str) and marketplace_id.strip():
			return marketplace_id.strip()
		repo_mp = getattr(self.repository, "marketplace_id", None)
		if isinstance(repo_mp, str) and repo_mp.strip():
			return repo_mp.strip()
		return self.repository.get_listings_items_instance().marketplace_id

	def get_currency(self) -> str:
		if self.amz_setting.get("price_list"):
			curr = frappe.db.get_value("Price List", self.amz_setting.price_list, "currency")
			if curr:
				return curr
		if self.amz_setting.get("company"):
			curr = frappe.db.get_value("Company", self.amz_setting.company, "default_currency")
			if curr:
				return curr
		return COUNTRY_CURRENCIES.get(self.amz_setting.get("country"), "USD")

	def resolve_item_code(self, item_code: str) -> str:
		"""Resolve ERPNext Item code from Item name or mapped Amazon SKU/ASIN."""
		if frappe.db.exists("Item", item_code):
			return item_code

		integration = get_amazon_module_name()
		for field in ("sku", "integration_item_code"):
			erp_code = frappe.db.get_value(
				"Ecommerce Item",
				filters={field: item_code, "integration": integration},
				fieldname="erpnext_item_code",
			)
			if erp_code and frappe.db.exists("Item", erp_code):
				return erp_code

		return item_code

	def get_sku_for_item(self, item_code: str, sku: str | None = None) -> str:
		if sku:
			return str(sku).strip()

		integration = get_amazon_module_name()
		ecom_data = frappe.db.get_value(
			"Ecommerce Item",
			filters={"erpnext_item_code": item_code, "integration": integration},
			fieldname=["sku", "integration_item_code"],
			as_dict=True,
		)
		if ecom_data:
			if ecom_data.get("sku"):
				return ecom_data["sku"]
			if ecom_data.get("integration_item_code"):
				return ecom_data["integration_item_code"]

		if frappe.db.has_column("Item", "amazon_item_code"):
			amz_code = frappe.db.get_value("Item", item_code, "amazon_item_code")
			if amz_code:
				return amz_code

		return item_code

	def get_product_type(self, item=None, product_type: str | None = None) -> str:
		if product_type and str(product_type).strip():
			return str(product_type).strip()
		if item is not None:
			item_pt = (
				item.get("amazon_product_type")
				if isinstance(item, dict)
				else getattr(item, "amazon_product_type", None)
			)
			if item_pt and str(item_pt).strip():
				return str(item_pt).strip()
		return "PRODUCT"

	def get_product_type_definition(
		self,
		product_type: str,
		seller_id: str | None = None,
		marketplace_id: str | None = None,
		requirements: str = "LISTING",
	) -> dict:
		"""Fetch and normalize ProductTypeDefinitions metadata for a product type."""
		pt = (product_type or "PRODUCT").strip()
		mkt = self.get_marketplace_id(marketplace_id)
		cache_key = (pt, mkt)
		if cache_key in self._ptd_cache:
			return self._ptd_cache[cache_key]

		if not hasattr(self.repository, "get_product_type_definitions_instance"):
			return {}

		client = self.repository.get_product_type_definitions_instance()
		resolved_seller = self.get_seller_id(seller_id)
		raw = self._call_sp_api(
			client.get_definitions_product_type,
			product_type=pt,
			marketplace_ids=[mkt],
			seller_id=resolved_seller,
			requirements=requirements,
		)
		if isinstance(raw, dict) and "payload" in raw:
			raw = raw.get("payload") or {}

		schema_props = (raw or {}).get("schema") or {}
		property_groups = (raw or {}).get("propertyGroups") or {}
		required_attrs: list[str] = []
		for group in property_groups.values() if isinstance(property_groups, dict) else []:
			for pname in group.get("propertyNames") or []:
				if pname not in required_attrs:
					required_attrs.append(pname)

		definition = {
			"productType": (raw or {}).get("productType") or pt,
			"productTypeVersion": ((raw or {}).get("productTypeVersion") or {}).get("version"),
			"requirements": (raw or {}).get("requirements") or requirements,
			"propertyNames": required_attrs,
			"schemaLink": (schema_props.get("link") or {}).get("resource"),
			"raw": raw or {},
		}
		self._ptd_cache[cache_key] = definition
		return definition

	def get_uom_conversion_factor(self, item, target_uom: str | None = None) -> float:
		"""Return stock_uom units per target UOM (e.g. 12 when selling by Box of 12)."""
		stock_uom = item.get("stock_uom") if isinstance(item, dict) else getattr(item, "stock_uom", None)
		effective_uom = target_uom or (
			item.get("sales_uom") if isinstance(item, dict) else getattr(item, "sales_uom", None)
		)
		if not stock_uom or not effective_uom or effective_uom == stock_uom:
			return 1.0

		for row in item.get("uoms") or []:
			row_uom = row.get("uom") if isinstance(row, dict) else getattr(row, "uom", None)
			row_factor = row.get("conversion_factor") if isinstance(row, dict) else getattr(row, "conversion_factor", None)
			if row_uom == effective_uom and flt(row_factor) > 0:
				return flt(row_factor)

		factor = frappe.db.get_value(
			"UOM Conversion Detail",
			{"parent": item.get("name"), "uom": effective_uom},
			"conversion_factor",
		)
		if flt(factor) > 0:
			return flt(factor)

		global_factor = frappe.db.get_value(
			"UOM Conversion Factor",
			{"from_uom": effective_uom, "to_uom": stock_uom},
			"value",
		)
		return flt(global_factor) if flt(global_factor) > 0 else 1.0

	def get_stock_qty(
		self, item_code: str, warehouse: str | None = None, target_uom: str | None = None
	) -> float:
		"""Compute available stock (actual_qty - reserved_qty) across configured warehouse(s) in listing UOM."""
		warehouses = (
			get_expanded_warehouses([warehouse])
			if warehouse
			else get_configured_warehouses(self.amz_setting)
		)
		if not warehouses:
			return 0.0

		bins = frappe.get_all(
			"Bin",
			filters={"item_code": item_code, "warehouse": ["in", list(warehouses)]},
			fields=["actual_qty", "reserved_qty"],
		)
		available_stock_uom = sum(
			max(0.0, flt(b.get("actual_qty")) - max(0.0, flt(b.get("reserved_qty"))))
			for b in bins
		)
		if available_stock_uom <= 0:
			return 0.0

		item = frappe.get_cached_doc("Item", item_code) if frappe.db.exists("Item", item_code) else None
		conv_factor = self.get_uom_conversion_factor(item, target_uom=target_uom) if item else 1.0
		return float(int(max(0.0, available_stock_uom / conv_factor)))

	def get_item_price(
		self, item_code: str, price_list: str | None = None, uom: str | None = None
	) -> float:
		"""Resolve active positive selling price respecting selling=1, UOM, and valid date window."""
		pl = price_list or self.amz_setting.get("price_list")
		item = frappe.get_cached_doc("Item", item_code) if frappe.db.exists("Item", item_code) else None
		stock_uom = getattr(item, "stock_uom", None) if item else None
		target_uom = uom or (getattr(item, "sales_uom", None) if item else None) or stock_uom
		current_date = getdate(today())

		if pl:
			price_rows = frappe.get_all(
				"Item Price",
				filters={"item_code": item_code, "price_list": pl, "selling": 1},
				fields=["price_list_rate", "uom", "valid_from", "valid_upto", "modified"],
				order_by="modified desc",
			)
			valid_rows = [row for row in price_rows if _is_price_row_active(row, current_date)]
			chosen = _pick_best_price_row(valid_rows, target_uom, stock_uom)
			if chosen and flt(chosen.get("price_list_rate")) > 0:
				rate = flt(chosen.get("price_list_rate"))
				row_uom = chosen.get("uom") or stock_uom
				if item and target_uom and row_uom and row_uom != target_uom:
					rate = rate * self.get_uom_conversion_factor(item, target_uom=target_uom) / self.get_uom_conversion_factor(item, target_uom=row_uom)
				return flt(rate)

		if item and flt(item.standard_rate) > 0:
			rate = flt(item.standard_rate)
			if target_uom and stock_uom and target_uom != stock_uom:
				rate *= self.get_uom_conversion_factor(item, target_uom=target_uom)
			return flt(rate)

		std_rate = frappe.db.get_value("Item", item_code, "standard_rate")
		return flt(std_rate) if flt(std_rate) > 0 else 0.0

	def get_barcodes(self, item_code: str) -> list[dict]:
		barcodes = frappe.get_all(
			"Item Barcode",
			filters={"parent": item_code},
			fields=["barcode", "barcode_type"],
			order_by="idx asc",
		)
		if not barcodes and frappe.db.exists("Item", item_code):
			item_doc = frappe.get_doc("Item", item_code)
			for b in item_doc.get("barcodes", []):
				barcodes.append({"barcode": b.barcode, "barcode_type": b.barcode_type})
		return barcodes

	def build_identifier_attribute(self, barcodes: list[dict], marketplace_id: str) -> list[dict]:
		"""Build externally_assigned_product_identifier from valid GTIN/UPC/EAN/ISBN barcodes only."""
		if not barcodes:
			return []

		candidates: list[tuple[int, str, str]] = []
		for idx, row in enumerate(barcodes):
			val = str(row.get("barcode") or "").strip()
			raw_type = str(row.get("barcode_type") or "").strip().lower()
			classified = _classify_product_identifier(val, raw_type)
			if not classified:
				continue
			ident_type, has_valid_checksum = classified
			# Prefer checksum-valid identifiers over unverified numeric test barcodes.
			score = (0 if has_valid_checksum else 1, idx)
			candidates.append((score[0] * 1000 + score[1], ident_type, val))

		if not candidates:
			return []

		candidates.sort(key=lambda c: c[0])
		_, best_type, best_val = candidates[0]
		return [{"type": best_type, "value": best_val, "marketplace_id": marketplace_id}]

	def record_status_on_ecommerce_item(
		self, item_code: str, sku: str, status: str, sync_type: str = "both"
	) -> None:
		"""Update sync timestamps/status on Ecommerce Item if ERPNext Item exists."""
		if not item_code or not frappe.db.exists("Item", item_code):
			return

		integration = get_amazon_module_name()
		ecom_item_name = frappe.db.get_value(
			"Ecommerce Item", {"erpnext_item_code": item_code, "integration": integration}, "name"
		) or frappe.db.get_value("Ecommerce Item", {"sku": sku, "integration": integration}, "name")

		now_time = now()
		has_status_col = frappe.db.has_column("Ecommerce Item", "status")

		if not ecom_item_name:
			ecom_doc = frappe.new_doc("Ecommerce Item")
			ecom_doc.integration = integration
			ecom_doc.erpnext_item_code = item_code
			ecom_doc.integration_item_code = sku
			ecom_doc.sku = sku
			ecom_doc.has_variants = 0
			ecom_doc.item_synced_on = now_time
			if sync_type in ("stock", "both"):
				ecom_doc.inventory_synced_on = now_time
			if has_status_col:
				ecom_doc.status = status
			ecom_doc.flags.ignore_mandatory = True
			ecom_doc.insert(ignore_permissions=True)
			return

		updates = {}
		if sync_type in ("item", "both"):
			updates["item_synced_on"] = now_time
		if sync_type in ("stock", "both"):
			updates["inventory_synced_on"] = now_time
		if has_status_col:
			updates["status"] = status

		if updates:
			frappe.db.set_value("Ecommerce Item", ecom_item_name, updates)

	def _call_sp_api(self, sp_api_method, **kwargs):
		"""Execute an SP-API method with bounded retries for 429/5xx without mutating Settings."""
		max_retries = max(1, int(self.amz_setting.get("max_retry_limit") or 3))
		last_exc = None

		for attempt in range(max_retries):
			try:
				return sp_api_method(**kwargs)
			except (SPAPIError, RequestException) as exc:
				last_exc = exc
				if not _is_transient_spapi_error(exc) or attempt >= max_retries - 1:
					raise
				delay = 0.05 if frappe.flags.in_test else min(4.0, 0.5 * (2**attempt))
				time.sleep(delay)

		if last_exc:
			raise last_exc

	def create_or_update_listing(
		self,
		item_code: str,
		sku: str | None = None,
		seller_id: str | None = None,
		product_type: str | None = None,
		price_override: float | None = None,
	) -> dict:
		"""Build and PUT a Listings Items v2021-08-01 payload for an ERPNext Item."""
		real_item_code = self.resolve_item_code(item_code)
		item = frappe.get_doc("Item", real_item_code)

		seller_id = self.get_seller_id(seller_id)
		sku = self.get_sku_for_item(real_item_code, sku=sku)
		marketplace_id = self.get_marketplace_id()
		currency = self.get_currency()
		p_type = self.get_product_type(item, product_type)

		stock_qty = self.get_stock_qty(real_item_code)
		price = flt(price_override) if price_override is not None else self.get_item_price(real_item_code)
		barcodes = self.get_barcodes(real_item_code)

		attributes: dict = {
			"item_name": [{"value": item.item_name or item.item_code, "marketplace_id": marketplace_id}],
			"brand": [{"value": item.brand or "Generic", "marketplace_id": marketplace_id}],
			"fulfillment_availability": [
				{
					"fulfillment_channel_code": "DEFAULT",
					"quantity": int(max(0, flt(stock_qty))),
				}
			],
		}
		if item.description:
			attributes["product_description"] = [
				{"value": strip_html(item.description).strip(), "marketplace_id": marketplace_id}
			]

		# Never publish an unintended 0.0 price offer when no positive selling price exists.
		if flt(price) > 0:
			attributes["purchasable_offer"] = [
				{
					"currency": currency,
					"our_price": [{"schedule": [{"value_with_tax": flt(price)}]}],
					"marketplace_id": marketplace_id,
				}
			]

		ident_attr = self.build_identifier_attribute(barcodes, marketplace_id)
		if ident_attr:
			attributes["externally_assigned_product_identifier"] = ident_attr

		payload = {"productType": p_type, "requirements": "LISTING", "attributes": attributes}
		retry_args = {"item_code": real_item_code, "amz_setting_name": getattr(self.amz_setting, "name", None)}

		log = safe_create_amazon_log(
			status="Queued",
			method=DEFAULT_ITEM_METHOD,
			request_data=retry_args,
			make_new=True,
		)
		frappe.flags.request_id = log.name if log else None

		listings_client = self.repository.get_listings_items_instance()
		try:
			response = self._call_sp_api(
				listings_client.put_listings_item,
				seller_id=seller_id,
				sku=sku,
				marketplace_ids=[marketplace_id],
				body=payload,
			)
			response = response if isinstance(response, dict) else {"status": "ACCEPTED"}
			has_err, err_summary = _extract_listing_issues_error(response)

			if has_err:
				response["error"] = err_summary
				self.record_status_on_ecommerce_item(real_item_code, sku, status="Error", sync_type="both")
				safe_create_amazon_log(
					status="Error",
					method=DEFAULT_ITEM_METHOD,
					message=f"Amazon listing creation failed for SKU {sku}: {err_summary}",
					response_data=response,
					request_data=retry_args,
				)
			else:
				status = response.get("status") or "ACCEPTED"
				self.record_status_on_ecommerce_item(real_item_code, sku, status=status, sync_type="both")
				safe_create_amazon_log(
					status="Success",
					method=DEFAULT_ITEM_METHOD,
					message=f"Successfully submitted listing for SKU {sku} to Amazon SP-API.",
					response_data=response,
					request_data=retry_args,
				)
			return response

		except Exception as e:
			self.record_status_on_ecommerce_item(real_item_code, sku, status="Error", sync_type="both")
			safe_create_amazon_log(
				status="Error",
				method=DEFAULT_ITEM_METHOD,
				exception=e,
				message=f"Exception while putting listing for SKU {sku}: {strip_html(str(e))}",
				request_data=retry_args,
				rollback=True,
			)
			raise
		finally:
			frappe.flags.request_id = None

	def update_stock_and_price(
		self,
		item_code: str,
		stock_qty: float | None = None,
		price: float | None = None,
		sku: str | None = None,
		seller_id: str | None = None,
		product_type: str | None = None,
	) -> dict:
		"""Construct and PATCH /attributes/fulfillment_availability and/or /attributes/purchasable_offer."""
		real_item_code = self.resolve_item_code(item_code)
		item = (
			frappe.get_cached_doc("Item", real_item_code)
			if frappe.db.exists("Item", real_item_code)
			else None
		)
		seller_id = self.get_seller_id(seller_id)
		sku = self.get_sku_for_item(real_item_code, sku=sku)
		marketplace_id = self.get_marketplace_id()
		currency = self.get_currency()
		p_type = self.get_product_type(item, product_type)

		explicit_mode = stock_qty is not None or price is not None
		if not explicit_mode:
			stock_qty = self.get_stock_qty(real_item_code)
			price = self.get_item_price(real_item_code)

		patches = []
		sync_parts: list[str] = []

		if stock_qty is not None:
			patches.append(
				{
					"op": "replace",
					"path": "/attributes/fulfillment_availability",
					"value": [
						{
							"fulfillment_channel_code": "DEFAULT",
							"quantity": int(max(0, flt(stock_qty))),
						}
					],
				}
			)
			sync_parts.append("stock")

		# Guard against overwriting an Amazon price with 0.0 when no active selling price exists.
		if price is not None and flt(price) > 0:
			patches.append(
				{
					"op": "replace",
					"path": "/attributes/purchasable_offer",
					"value": [
						{
							"currency": currency,
							"our_price": [{"schedule": [{"value_with_tax": flt(price)}]}],
							"marketplace_id": marketplace_id,
						}
					],
				}
			)
			sync_parts.append("item")

		if not patches:
			msg = f"Skipped price/stock patch for SKU {sku}: no active positive selling price or stock update."
			safe_create_amazon_log(
				status="Success",
				method=DEFAULT_PATCH_METHOD,
				message=msg,
				request_data={"item_code": real_item_code, "amz_setting_name": getattr(self.amz_setting, "name", None)},
				make_new=True,
			)
			return {"status": "SKIPPED", "sku": sku, "message": msg}

		sync_type = "both" if len(sync_parts) == 2 else sync_parts[0]
		payload = {"productType": p_type, "patches": patches}
		retry_args = {
			"item_code": real_item_code,
			"stock_qty": stock_qty if stock_qty is not None else None,
			"price": price if (price is not None and flt(price) > 0) else None,
			"amz_setting_name": getattr(self.amz_setting, "name", None),
		}

		log = safe_create_amazon_log(
			status="Queued",
			method=DEFAULT_PATCH_METHOD,
			request_data=retry_args,
			make_new=True,
		)
		frappe.flags.request_id = log.name if log else None

		listings_client = self.repository.get_listings_items_instance()
		try:
			response = self._call_sp_api(
				listings_client.patch_listings_item,
				seller_id=seller_id,
				sku=sku,
				marketplace_ids=[marketplace_id],
				body=payload,
			)
			response = response if isinstance(response, dict) else {"status": "ACCEPTED"}
			has_err, err_summary = _extract_listing_issues_error(response)

			if has_err:
				response["error"] = err_summary
				self.record_status_on_ecommerce_item(real_item_code, sku, status="Error", sync_type=sync_type)
				safe_create_amazon_log(
					status="Error",
					method=DEFAULT_PATCH_METHOD,
					message=f"Amazon stock/price patch rejected for SKU {sku}: {err_summary}",
					response_data=response,
					request_data=retry_args,
				)
			else:
				status = response.get("status") or "ACCEPTED"
				self.record_status_on_ecommerce_item(real_item_code, sku, status=status, sync_type=sync_type)
				safe_create_amazon_log(
					status="Success",
					method=DEFAULT_PATCH_METHOD,
					message=f"Successfully patched stock/price for SKU {sku} on Amazon SP-API.",
					response_data=response,
					request_data=retry_args,
				)
			return response

		except Exception as e:
			self.record_status_on_ecommerce_item(real_item_code, sku, status="Error", sync_type=sync_type)
			safe_create_amazon_log(
				status="Error",
				method=DEFAULT_PATCH_METHOD,
				exception=e,
				message=f"Exception while patching stock/price for SKU {sku}: {strip_html(str(e))}",
				request_data=retry_args,
				rollback=True,
			)
			raise
		finally:
			frappe.flags.request_id = None

	def get_listings_item(
		self,
		item_code: str,
		sku: str | None = None,
		seller_id: str | None = None,
		included_data: list[str] | None = None,
	) -> dict:
		"""Fetch current listing state from Amazon Listings Items API and update local status."""
		real_item_code = self.resolve_item_code(item_code)
		seller_id = self.get_seller_id(seller_id)
		sku = self.get_sku_for_item(real_item_code, sku=sku)
		marketplace_id = self.get_marketplace_id()
		included = included_data or ["summaries", "attributes", "issues", "offers", "fulfillmentAvailability"]

		listings_client = self.repository.get_listings_items_instance()
		try:
			response = self._call_sp_api(
				listings_client.get_listings_item,
				seller_id=seller_id,
				sku=sku,
				marketplace_ids=[marketplace_id],
				included_data=included,
			)
		except SPAPIError as exc:
			err_text = f"{getattr(exc, 'error', '')} {exc}"
			if "404" in err_text or "NOT_FOUND" in err_text.upper():
				safe_create_amazon_log(
					status="Success",
					method=DEFAULT_FETCH_METHOD,
					message=f"Amazon listing SKU {sku} not found on marketplace {marketplace_id}.",
					request_data={"item_code": real_item_code, "amz_setting_name": getattr(self.amz_setting, "name", None)},
					make_new=True,
				)
				return {"found": False, "sku": sku, "status": "NOT_FOUND"}
			raise

		summaries = (response or {}).get("summaries") or []
		summary = summaries[0] if summaries else {}
		statuses = summary.get("status") or []
		status = (statuses[0] if statuses else None) or (response or {}).get("status") or "ACCEPTED"
		if frappe.db.exists("Item", real_item_code):
			self.record_status_on_ecommerce_item(real_item_code, sku, status=status, sync_type="item")

		safe_create_amazon_log(
			status="Success",
			method=DEFAULT_FETCH_METHOD,
			message=f"Fetched Amazon listing details for SKU {sku}.",
			response_data=response,
			request_data={"item_code": real_item_code, "amz_setting_name": getattr(self.amz_setting, "name", None)},
			make_new=True,
		)
		return {
			"found": True,
			"sku": (response or {}).get("sku") or sku,
			"asin": summary.get("asin"),
			"product_type": summary.get("productType"),
			"status": status,
			"issues": (response or {}).get("issues") or [],
			"raw": response,
		}

	def delete_listing(
		self,
		item_code: str | None = None,
		sku: str | None = None,
		seller_id: str | None = None,
		remove_mapping: bool = True,
	) -> dict:
		"""Delete an Amazon listing via ListingsItems.delete_listings_item and clean Ecommerce Item."""
		real_item_code = self.resolve_item_code(item_code) if item_code else None
		resolved_sku = sku or (self.get_sku_for_item(real_item_code) if real_item_code else None)
		if not resolved_sku:
			frappe.throw(_("SKU or Item Code is required to delete an Amazon listing."))

		resolved_seller = self.get_seller_id(seller_id)
		marketplace_id = self.get_marketplace_id()
		retry_args = {
			"sku": resolved_sku,
			"item_code": real_item_code,
			"amz_setting_name": getattr(self.amz_setting, "name", None),
			"remove_mapping": remove_mapping,
		}
		log = safe_create_amazon_log(
			status="Queued",
			method=DEFAULT_DELETE_METHOD,
			request_data=retry_args,
			make_new=True,
		)
		frappe.flags.request_id = log.name if log else None

		listings_client = self.repository.get_listings_items_instance()
		try:
			try:
				response = self._call_sp_api(
					listings_client.delete_listings_item,
					seller_id=resolved_seller,
					sku=resolved_sku,
					marketplace_ids=[marketplace_id],
				)
			except SPAPIError as exc:
				if "404" in str(exc.error) or "NOT_FOUND" in str(exc.error).upper():
					response = {"status": "DELETED", "sku": resolved_sku}
				else:
					raise

			response = response if isinstance(response, dict) and response else {"status": "DELETED", "sku": resolved_sku}
			_clear_or_mark_deleted_ecommerce_item(real_item_code, resolved_sku, remove_mapping=remove_mapping)
			safe_create_amazon_log(
				status="Success",
				method=DEFAULT_DELETE_METHOD,
				message=f"Deleted Amazon listing for SKU {resolved_sku}.",
				response_data=response,
				request_data=retry_args,
			)
			return response
		except Exception as e:
			safe_create_amazon_log(
				status="Error",
				method=DEFAULT_DELETE_METHOD,
				exception=e,
				message=f"Failed to delete Amazon listing for SKU {resolved_sku}: {strip_html(str(e))}",
				request_data=retry_args,
				rollback=True,
			)
			raise
		finally:
			frappe.flags.request_id = None


def handle_item_update(doc, method=None) -> None:
	"""Hook on Item.after_insert / Item.on_update to sync edits on mapped Amazon items."""
	if (
		getattr(getattr(doc, "flags", None), "from_integration", False)
		or frappe.flags.get("from_amazon_sync")
		or frappe.flags.get("in_import")
		or frappe.flags.in_patch
	):
		return

	item_code = doc.get("name") or doc.get("item_code")
	if not item_code or not _has_active_amazon_mapping(item_code):
		return

	settings_list = frappe.get_all(
		"Amazon SP API Settings",
		filters={"is_active": 1},
		fields=["name", "enable_sync", "enable_listings_sync"],
	)
	eligible = [
		s for s in settings_list if frappe.utils.cint(s.enable_listings_sync) or frappe.utils.cint(s.enable_sync)
	]
	if not eligible:
		return

	for setting in eligible:
		job_id = f"amazon_item_update_{item_code}_{setting.name}"
		if doc.get("disabled"):
			frappe.enqueue(
				method=DEFAULT_PATCH_METHOD,
				queue="default",
				job_id=job_id,
				deduplicate=not frappe.flags.in_test,
				enqueue_after_commit=not frappe.flags.in_test,
				now=frappe.flags.in_test,
				item_code=item_code,
				stock_qty=0,
				amz_setting_name=setting.name,
			)
		else:
			frappe.enqueue(
				method=DEFAULT_ITEM_METHOD,
				queue="default",
				job_id=job_id,
				deduplicate=not frappe.flags.in_test,
				enqueue_after_commit=not frappe.flags.in_test,
				now=frappe.flags.in_test,
				item_code=item_code,
				amz_setting_name=setting.name,
			)


def handle_item_trash(doc, method=None) -> None:
	"""Hook on Item.on_trash to delete mapped Amazon listing and unlink Ecommerce Item."""
	if (
		getattr(getattr(doc, "flags", None), "from_integration", False)
		or frappe.flags.get("from_amazon_sync")
		or frappe.flags.in_patch
	):
		return

	item_code = doc.get("name") or doc.get("item_code")
	if not item_code:
		return

	integration = get_amazon_module_name()
	ecom_rows = frappe.get_all(
		"Ecommerce Item",
		filters={"erpnext_item_code": item_code, "integration": ["in", [integration, "amazon"]]},
		fields=["name", "sku", "integration_item_code"],
	)
	if not ecom_rows:
		return

	settings_list = frappe.get_all(
		"Amazon SP API Settings",
		filters={"is_active": 1},
		fields=["name", "enable_sync", "enable_listings_sync"],
	)
	eligible = [
		s for s in settings_list if frappe.utils.cint(s.enable_listings_sync) or frappe.utils.cint(s.enable_sync)
	]

	for ecom in ecom_rows:
		sku = ecom.get("sku") or ecom.get("integration_item_code") or item_code
		for setting in eligible:
			# Pass resolved SKU because the ERPNext Item record is deleted after on_trash finishes.
			frappe.enqueue(
				method=DEFAULT_DELETE_METHOD,
				queue="default",
				job_id=f"amazon_item_delete_{sku}_{setting.name}",
				deduplicate=not frappe.flags.in_test,
				enqueue_after_commit=not frappe.flags.in_test,
				now=frappe.flags.in_test,
				sku=sku,
				item_code=None,
				amz_setting_name=setting.name,
				remove_mapping=True,
			)
		frappe.delete_doc("Ecommerce Item", ecom["name"], force=1, ignore_permissions=True)


def create_or_update_listing(
	item_code: str | None = None,
	amz_setting_name: str | None = None,
	payload: dict | None = None,
) -> dict:
	if payload and not item_code:
		item_code = payload.get("item_code")
		amz_setting_name = amz_setting_name or payload.get("amz_setting_name")
	amz_setting_name = _resolve_setting_name(amz_setting_name)
	from amazon_connector.amazon_ext.repository import (
		CustomAmazonRepository as AmazonRepository,
	)

	repo = AmazonRepository(amz_setting_name)
	return AmazonOutboundSync(repo).create_or_update_listing(item_code)


def update_stock_and_price(
	item_code: str | None = None,
	stock_qty: float | None = None,
	price: float | None = None,
	amz_setting_name: str | None = None,
	payload: dict | None = None,
) -> dict:
	if payload and not item_code:
		item_code = payload.get("item_code")
		stock_qty = payload.get("stock_qty", stock_qty)
		price = payload.get("price", price)
		amz_setting_name = amz_setting_name or payload.get("amz_setting_name")
	amz_setting_name = _resolve_setting_name(amz_setting_name)
	from amazon_connector.amazon_ext.repository import (
		CustomAmazonRepository as AmazonRepository,
	)

	repo = AmazonRepository(amz_setting_name)
	return AmazonOutboundSync(repo).update_stock_and_price(
		item_code=item_code, stock_qty=stock_qty, price=price
	)


@frappe.whitelist()
def fetch_amazon_listing(item_code: str, amz_setting_name: str | None = None) -> dict:
	frappe.has_permission("Item", "read", throw=True)
	if item_code and frappe.db.exists("Item", item_code):
		frappe.has_permission("Item", "read", doc=item_code, throw=True)
	amz_setting_name = _resolve_setting_name(amz_setting_name)
	frappe.has_permission("Amazon SP API Settings", "read", doc=amz_setting_name, throw=True)
	from amazon_connector.amazon_ext.repository import (
		CustomAmazonRepository as AmazonRepository,
	)

	repo = AmazonRepository(amz_setting_name)
	return AmazonOutboundSync(repo).get_listings_item(item_code)


@frappe.whitelist()
def delete_amazon_listing(
	item_code: str, amz_setting_name: str | None = None, remove_mapping: int = 1
) -> dict:
	frappe.has_permission("Item", "delete", throw=True)
	if item_code and frappe.db.exists("Item", item_code):
		frappe.has_permission("Item", "delete", doc=item_code, throw=True)
	amz_setting_name = _resolve_setting_name(amz_setting_name)
	frappe.has_permission("Amazon SP API Settings", "write", doc=amz_setting_name, throw=True)
	from amazon_connector.amazon_ext.repository import (
		CustomAmazonRepository as AmazonRepository,
	)

	repo = AmazonRepository(amz_setting_name)
	return AmazonOutboundSync(repo).delete_listing(
		item_code=item_code, remove_mapping=bool(frappe.utils.cint(remove_mapping))
	)


def delete_amazon_listing_by_sku(
	sku: str,
	item_code: str | None = None,
	amz_setting_name: str | None = None,
	remove_mapping: bool = True,
) -> dict:
	amz_setting_name = _resolve_setting_name(amz_setting_name)
	from amazon_connector.amazon_ext.repository import (
		CustomAmazonRepository as AmazonRepository,
	)

	repo = AmazonRepository(amz_setting_name)
	return AmazonOutboundSync(repo).delete_listing(
		item_code=item_code, sku=sku, remove_mapping=remove_mapping
	)


@frappe.whitelist()
def get_amazon_product_type_definition(
	product_type: str, amz_setting_name: str | None = None
) -> dict:
	frappe.has_permission("Item", "read", throw=True)
	amz_setting_name = _resolve_setting_name(amz_setting_name)
	frappe.has_permission("Amazon SP API Settings", "read", doc=amz_setting_name, throw=True)
	from amazon_connector.amazon_ext.repository import (
		CustomAmazonRepository as AmazonRepository,
	)

	repo = AmazonRepository(amz_setting_name)
	return AmazonOutboundSync(repo).get_product_type_definition(product_type)


def get_expanded_warehouses(warehouses: list[str] | tuple[str, ...]) -> tuple[str, ...]:
	"""Expand group warehouses into leaf warehouses while preserving order."""
	expanded: list[str] = []
	for wh in warehouses:
		if not wh or wh in expanded:
			continue
		is_group = frappe.db.get_value("Warehouse", wh, "is_group")
		if is_group:
			for child in get_descendants_of("Warehouse", wh, ignore_permissions=True) or []:
				if child not in expanded:
					expanded.append(child)
			if wh not in expanded:
				expanded.append(wh)
		else:
			expanded.append(wh)
	return tuple(expanded)


def get_configured_warehouses(amz_setting) -> tuple[str, ...]:
	"""Return all ERPNext warehouses mapped to an Amazon SP API Settings record."""
	raw_wh = (
		amz_setting.get("warehouse")
		if isinstance(amz_setting, dict)
		else getattr(amz_setting, "warehouse", None)
	)
	if not raw_wh:
		return ()
	return get_expanded_warehouses([raw_wh])


def safe_create_amazon_log(
	status: str,
	message: str | None = None,
	method: str | None = None,
	response_data=None,
	request_data=None,
	exception=None,
	rollback: bool = False,
	make_new: bool = False,
):
	"""Write plain-text Integration Log without violating doc-event commit/rollback guards."""
	clean_message = strip_html(message) if message else None
	effective_method = method or DEFAULT_ITEM_METHOD
	tx_disabled = bool(getattr(frappe.db, "_disable_transaction_control", 0))

	if tx_disabled and not frappe.flags.in_test:
		# Defer log commit until after doc event finishes to avoid ImplicitCommitError.
		err_text = clean_message or (strip_html(str(exception)) if exception else "")
		frappe.enqueue(
			method=_deferred_log_write,
			queue="short",
			enqueue_after_commit=True,
			status=status,
			message=err_text,
			method_path=effective_method,
			response_data=response_data,
			request_data=request_data,
		)
		return None

	if tx_disabled:
		return None

	return create_log(
		module_def=get_amazon_module_name(),
		status=status,
		response_data=response_data,
		request_data=request_data,
		exception=exception,
		rollback=rollback and not tx_disabled,
		method=effective_method,
		message=clean_message,
		make_new=make_new,
	)


def _deferred_log_write(
	status: str,
	message: str | None = None,
	method_path: str | None = None,
	response_data=None,
	request_data=None,
) -> None:
	create_log(
		module_def=get_amazon_module_name(),
		status=status,
		message=strip_html(message) if message else None,
		method=method_path or DEFAULT_ITEM_METHOD,
		response_data=response_data,
		request_data=request_data,
		make_new=True,
	)


def _resolve_setting_name(amz_setting_name: str | None = None) -> str:
	if amz_setting_name:
		return amz_setting_name
	resolved = frappe.db.get_value("Amazon SP API Settings", {"is_active": 1}, "name") or frappe.db.get_value(
		"Amazon SP API Settings", {}, "name"
	)
	if not resolved:
		frappe.throw(_("No Amazon SP API Settings configured."))
	return resolved


def _is_transient_spapi_error(exc: Exception) -> bool:
	if isinstance(exc, RequestException):
		return True
	if isinstance(exc, SPAPIError):
		status_code = getattr(getattr(exc, "error_response", None), "status_code", None)
		if status_code in (429, 500, 502, 503, 504):
			return True
		parts = [
			str(getattr(exc, "error", "") or ""),
			str(getattr(exc, "error_description", "") or ""),
			str(exc),
		]
		text = " ".join(parts).lower()
		transient_tokens = (
			"429",
			"quotaexceeded",
			"throttl",
			"too many requests",
			"500",
			"502",
			"503",
			"504",
			"serviceunavailable",
			"internalerror",
		)
		return any(token in text or token in [t.lower() for t in TRANSIENT_SPAPI_CODES] and token in text for token in transient_tokens)
	return False


def _extract_listing_issues_error(response: dict | None) -> tuple[bool, str]:
	if not isinstance(response, dict):
		return False, ""
	status = response.get("status")
	issues = response.get("issues") or []
	error_issues = [i for i in issues if i.get("severity") == "ERROR"]
	if status in ("INVALID", "VALIDATION_FAILED", "ERROR") or error_issues:
		summary = "; ".join(
			strip_html(str(i.get("message") or i.get("code") or "Issue"))
			for i in (error_issues or issues)
		)
		return True, summary or str(status)
	return False, ""


def _is_gs1_checksum_valid(digits: str) -> bool:
	if not digits.isdigit() or len(digits) not in (8, 12, 13, 14):
		return False
	body, check = digits[:-1], int(digits[-1])
	total = 0
	for offset, ch in enumerate(reversed(body)):
		weight = 3 if offset % 2 == 0 else 1
		total += int(ch) * weight
	expected = (10 - (total % 10)) % 10
	return expected == check


def _is_isbn10_valid(val: str) -> bool:
	if len(val) != 10 or not val[:-1].isdigit():
		return False
	last = val[-1].upper()
	if not (last.isdigit() or last == "X"):
		return False
	total = sum((10 - idx) * (10 if ch == "X" else int(ch)) for idx, ch in enumerate(val[:-1] + last))
	return total % 11 == 0


def _classify_product_identifier(val: str, raw_type: str) -> tuple[str, bool] | None:
	if not val or any(ch.isspace() for ch in val):
		return None

	if raw_type:
		if "isbn" in raw_type:
			upper = val.upper()
			if len(upper) == 10 and upper[:-1].isdigit() and (upper[-1].isdigit() or upper[-1] == "X"):
				return ("isbn", _is_isbn10_valid(upper))
			if val.isdigit() and len(val) == 13 and val.startswith(("978", "979")):
				return ("isbn", _is_gs1_checksum_valid(val))
			return None
		if "upc" in raw_type:
			return ("upc", _is_gs1_checksum_valid(val)) if (val.isdigit() and len(val) == 12) else None
		if "ean" in raw_type:
			return ("ean", _is_gs1_checksum_valid(val)) if (val.isdigit() and len(val) in (8, 13)) else None
		if "gtin" in raw_type:
			return (
				("gtin", _is_gs1_checksum_valid(val))
				if (val.isdigit() and len(val) in (8, 12, 13, 14))
				else None
			)
		return None

	if not val.isdigit():
		return None
	if len(val) == 13:
		return ("ean", _is_gs1_checksum_valid(val))
	if len(val) == 12:
		return ("upc", _is_gs1_checksum_valid(val))
	if len(val) == 14:
		return ("gtin", _is_gs1_checksum_valid(val))
	return None


def _is_price_row_active(row: dict, current_date) -> bool:
	if flt(row.get("price_list_rate")) <= 0:
		return False
	valid_from = row.get("valid_from")
	valid_upto = row.get("valid_upto")
	if valid_from and getdate(valid_from) > current_date:
		return False
	if valid_upto and getdate(valid_upto) < current_date:
		return False
	return True


def _pick_best_price_row(rows: list[dict], target_uom: str | None, stock_uom: str | None) -> dict | None:
	if not rows:
		return None

	def rank(row: dict):
		row_uom = row.get("uom") or ""
		if target_uom and row_uom == target_uom:
			uom_prio = 0
		elif stock_uom and row_uom == stock_uom:
			uom_prio = 1
		elif not row_uom:
			uom_prio = 2
		else:
			uom_prio = 3
		return (uom_prio, -(getdate(row["valid_from"]).toordinal() if row.get("valid_from") else 0))

	return sorted(rows, key=rank)[0]


def _has_active_amazon_mapping(item_code: str) -> bool:
	filters: dict = {"erpnext_item_code": item_code, "integration": ["in", [get_amazon_module_name(), "amazon"]]}
	if frappe.db.has_column("Ecommerce Item", "status"):
		filters["status"] = ["!=", "Deleted"]
	return bool(frappe.db.exists("Ecommerce Item", filters))


def _clear_or_mark_deleted_ecommerce_item(
	item_code: str | None, sku: str, remove_mapping: bool = True
) -> None:
	integration = get_amazon_module_name()
	filters_list = []
	if item_code:
		filters_list.append({"erpnext_item_code": item_code, "integration": ["in", [integration, "amazon"]]})
	if sku:
		filters_list.append({"sku": sku, "integration": ["in", [integration, "amazon"]]})

	matched_names = []
	for filt in filters_list:
		for name in frappe.get_all("Ecommerce Item", filters=filt, pluck="name"):
			if name not in matched_names:
				matched_names.append(name)

	for name in matched_names:
		if remove_mapping:
			frappe.delete_doc("Ecommerce Item", name, force=1, ignore_permissions=True)
		elif frappe.db.has_column("Ecommerce Item", "status"):
			frappe.db.set_value("Ecommerce Item", name, "status", "Deleted")

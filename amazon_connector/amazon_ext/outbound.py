# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import json
import re
import time

import requests
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

COUNTRY_LANGUAGE_TAGS = {
	"IN": "en_IN",
	"US": "en_US",
	"GB": "en_GB",
	"CA": "en_CA",
	"AU": "en_AU",
	"SG": "en_SG",
	"AE": "en_AE",
	"DE": "de_DE",
	"FR": "fr_FR",
	"IT": "it_IT",
	"ES": "es_ES",
	"NL": "nl_NL",
	"SE": "sv_SE",
	"PL": "pl_PL",
	"TR": "tr_TR",
	"SA": "ar_SA",
	"EG": "ar_EG",
	"JP": "ja_JP",
	"MX": "es_MX",
	"BR": "pt_BR",
}

AMAZON_WEIGHT_UNIT_MAP = {
	"g": "grams",
	"gm": "grams",
	"gms": "grams",
	"gram": "grams",
	"grams": "grams",
	"kg": "kilograms",
	"kgs": "kilograms",
	"kilo": "kilograms",
	"kilogram": "kilograms",
	"kilograms": "kilograms",
	"mg": "milligrams",
	"mgs": "milligrams",
	"milligram": "milligrams",
	"milligrams": "milligrams",
	"oz": "ounces",
	"ounce": "ounces",
	"ounces": "ounces",
	"lb": "pounds",
	"lbs": "pounds",
	"pound": "pounds",
	"pounds": "pounds",
	"ton": "tons",
	"tons": "tons",
	"tonne": "tons",
	"tonnes": "tons",
	"mt": "tons",
	"hundredths_pounds": "hundredths_pounds",
}

NO_MARKETPLACE_ID_ATTRS = frozenset({"rtip_manufacturer_contact_information", "fulfillment_availability"})
STANDARD_AUTO_ATTRS = frozenset({
	"item_name",
	"brand",
	"product_description",
	"country_of_origin",
	"external_product_information",
	"merchant_suggested_asin",
	"externally_assigned_product_identifier",
	"fulfillment_availability",
	"purchasable_offer",
	"item_weight",
	"supplier_declared_dg_hz_regulation",
	"packer_contact_information",
	"importer_contact_information",
	"rtip_manufacturer_contact_information",
	"manufacturer",
	"condition_type",
	"merchant_shipping_group",
})
ASIN_PATTERN = re.compile(r"^B0[A-Z0-9]{8}$")

VARIANT_ATTRIBUTE_ALIASES = {
	"colour": "color",
	"color_name": "color",
	"size_name": "size",
	"apparel_size": "size",
	"book_binding": "binding",
	"format": "binding",
	"pages": "number_of_pages",
	"page_count": "number_of_pages",
	"pages_count": "number_of_pages",
	"author_name": "author",
	"model": "model_name",
	"storage_capacity": "memory_storage_capacity",
	"storage": "memory_storage_capacity",
	"pack_size": "number_of_items",
	"flavour": "flavor",
	"material_type": "material",
	"edition_name": "edition",
	"style_name": "style",
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

	def get_language_tag(self) -> str:
		country_code = str(self.amz_setting.get("country") or "").strip().upper()
		return COUNTRY_LANGUAGE_TAGS.get(country_code, "en_US")

	def build_purchasable_offer(
		self, item, price: float, currency: str, marketplace_id: str
	) -> list[dict]:
		"""Build purchasable_offer payload, including Legal Metrology MRP for Amazon IN."""
		offer: dict = {
			"audience": "ALL",
			"currency": currency,
			"our_price": [{"schedule": [{"value_with_tax": flt(price)}]}],
			"marketplace_id": marketplace_id,
		}
		is_india = currency == "INR" or str(self.amz_setting.get("country") or "").strip().upper() == "IN"
		if is_india:
			item_mrp = flt(getattr(item, "amazon_mrp", 0) if item is not None else 0)
			mrp_val = item_mrp if item_mrp > 0 else flt(price)
			offer["maximum_retail_price"] = [{"schedule": [{"value_with_tax": mrp_val}]}]
		return [offer]

	def resolve_merchant_suggested_asin(self, item, item_code: str, sku: str) -> str | None:
		raw_asin = str(getattr(item, "amazon_asin", "") or "").strip().upper()
		if len(raw_asin) == 10 and raw_asin.isalnum():
			return raw_asin

		integration = get_amazon_module_name()
		ecom_asin = frappe.db.get_value(
			"Ecommerce Item",
			{"erpnext_item_code": item_code, "integration": ["in", [integration, "amazon"]]},
			"integration_item_code",
		)
		candidate = str(ecom_asin or "").strip().upper()
		if ASIN_PATTERN.match(candidate) and candidate != str(sku or "").strip().upper():
			return candidate
		return None

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

			variant_of = (
				item.get("variant_of")
				if isinstance(item, dict)
				else getattr(item, "variant_of", None)
			)
			if variant_of:
				parent_pt = frappe.db.get_value("Item", variant_of, "amazon_product_type")
				if parent_pt and str(parent_pt).strip():
					return str(parent_pt).strip()

			ig = (
				item.get("item_group")
				if isinstance(item, dict)
				else getattr(item, "item_group", None)
			)
			if ig:
				ig_pt = frappe.db.get_value("Item Group", ig, "amazon_product_type")
				if ig_pt and str(ig_pt).strip():
					return str(ig_pt).strip()

		return "PRODUCT"

	def get_product_type_definition(
		self,
		product_type: str,
		seller_id: str | None = None,
		marketplace_id: str | None = None,
		requirements: str | None = None,
	) -> dict:
		"""Fetch and normalize ProductTypeDefinitions metadata for a product type."""
		pt = (product_type or "PRODUCT").strip().upper()
		resolved_req = requirements or ("LISTING_OFFER_ONLY" if pt == "PRODUCT" else "LISTING")
		mkt = self.get_marketplace_id(marketplace_id)
		resolved_seller = self.get_seller_id(seller_id)
		cache_key = (pt, mkt, resolved_req, resolved_seller)
		if cache_key in self._ptd_cache:
			return self._ptd_cache[cache_key]

		if not hasattr(self.repository, "get_product_type_definitions_instance"):
			return {}

		client = self.repository.get_product_type_definitions_instance()
		raw = self._call_sp_api(
			client.get_definitions_product_type,
			product_type=pt,
			marketplace_ids=[mkt],
			seller_id=resolved_seller,
			requirements=resolved_req,
		)
		if isinstance(raw, dict) and "payload" in raw:
			raw = raw.get("payload") or {}
		if not isinstance(raw, dict):
			raw = {}

		schema_props = raw.get("schema") or {}
		property_groups = raw.get("propertyGroups") or {}
		required_attrs: list[str] = []
		for group in property_groups.values() if isinstance(property_groups, dict) else []:
			for pname in (group.get("propertyNames") or []) if isinstance(group, dict) else []:
				if pname not in required_attrs:
					required_attrs.append(pname)

		definition = {
			"productType": raw.get("productType") or pt,
			"productTypeVersion": (raw.get("productTypeVersion") or {}).get("version"),
			"requirements": raw.get("requirements") or resolved_req,
			"propertyNames": required_attrs,
			"schemaLink": (schema_props.get("link") or {}).get("resource"),
			"raw": raw,
		}
		if raw:
			self._ptd_cache[cache_key] = definition
		return definition

	def _resolve_product_type_schema(self, product_type: str) -> dict:
		"""Fetch and cache the JSON Schema for an Amazon product type."""
		pt = (product_type or "PRODUCT").strip().upper()
		if pt == "PRODUCT":
			return {}

		try:
			defn = self.get_product_type_definition(pt) or {}
		except Exception as exc:
			frappe.logger("amazon_connector").warning(
				f"Failed to fetch ProductTypeDefinition for {pt}: {exc}"
			)
			defn = {}

		schema_url = defn.get("schemaLink")
		schema: dict = {}
		if schema_url:
			mkt = self.get_marketplace_id()
			cache_key = f"amz_pt_schema::{mkt}::{pt}"
			cached = frappe.cache.get_value(cache_key) if hasattr(frappe, "cache") else None
			if isinstance(cached, dict) and cached:
				return cached
			try:
				from amazon_connector.amazon_ext.repository import _validate_endpoint_url

				safe_url = _validate_endpoint_url(schema_url)
				resp = requests.get(safe_url, timeout=15)
				if resp.status_code < 400:
					schema = resp.json() or {}
					if schema and hasattr(frappe, "cache"):
						frappe.cache.set_value(cache_key, schema, expires_in_sec=3600)
			except Exception as exc:
				frappe.logger("amazon_connector").warning(
					f"Failed to download product type schema for {pt} from {schema_url}: {exc}"
				)
				schema = {}

		if not schema:
			schema = (defn.get("raw") or {}).get("schema") or {}
		return schema

	def build_attributes_template(self, product_type: str) -> dict:
		"""Generate a UI-ready JSON attributes template for an Amazon product type."""
		pt = (product_type or "PRODUCT").strip().upper()
		if pt == "PRODUCT":
			return {}

		schema = self._resolve_product_type_schema(pt)
		props = schema.get("properties") or {}
		req_set = set(schema.get("required") or [])
		lang_tag = self.get_language_tag()

		target_keys: list[str] = []
		for key in (*_TEMPLATE_PRIORITY_FIELDS, *sorted(req_set)):
			if key in props and key not in STANDARD_AUTO_ATTRS and key not in target_keys:
				target_keys.append(key)

		return {
			key: _build_template_property_value(key, props.get(key) or {}, lang_tag)
			for key in target_keys
		}

	def build_attributes_metadata(self, product_type: str, tmpl: dict | None = None) -> list[dict]:
		"""Generate structured metadata for dynamic Desk input fields."""
		pt = (product_type or "PRODUCT").strip().upper()
		if pt == "PRODUCT":
			return []

		template = tmpl if tmpl is not None else self.build_attributes_template(pt)
		if not isinstance(template, dict):
			template = {}
		schema = self._resolve_product_type_schema(pt)
		props = schema.get("properties") or {}
		req_set = set(schema.get("required") or [])

		fields_meta = [
			_build_single_field_metadata(key, t_val, props.get(key) or {}, req_set)
			for key, t_val in template.items()
		]
		fields_meta.sort(key=lambda x: (0 if x["reqd"] else 1, x["label"]))
		return fields_meta

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

		item = _get_cached_item(item_code)
		conv_factor = self.get_uom_conversion_factor(item, target_uom=target_uom) if item else 1.0
		return float(int(max(0.0, available_stock_uom / (conv_factor if flt(conv_factor) > 0 else 1.0))))

	def get_item_price(
		self, item_code: str, price_list: str | None = None, uom: str | None = None
	) -> float:
		"""Resolve active positive selling price respecting selling=1, UOM, and valid date window."""
		pl = price_list or self.amz_setting.get("price_list")
		item = _get_cached_item(item_code)
		stock_uom = getattr(item, "stock_uom", None) if item else None
		target_uom = uom or (getattr(item, "sales_uom", None) if item else None) or stock_uom
		current_date = getdate(today())

		if pl:
			expected_currency = frappe.db.get_value("Price List", pl, "currency") or self.get_currency()
			price_fields = [
				"price_list_rate",
				"uom",
				"valid_from",
				"valid_upto",
				"modified",
				"customer",
				"batch_no",
				"packing_unit",
				"currency",
			]
			if frappe.db.has_column("Item Price", "min_qty"):
				price_fields.append("min_qty")
			price_rows = frappe.get_all(
				"Item Price",
				filters={"item_code": item_code, "price_list": pl, "selling": 1},
				fields=price_fields,
				order_by="modified desc",
			)
			valid_rows = [
				row
				for row in price_rows
				if _is_price_row_active(row, current_date, expected_currency=expected_currency)
			]
			chosen = _pick_best_price_row(valid_rows, target_uom, stock_uom)
			if chosen and flt(chosen.get("price_list_rate")) > 0:
				rate = flt(chosen.get("price_list_rate"))
				row_uom = chosen.get("uom") or stock_uom
				if item and target_uom and row_uom and row_uom != target_uom:
					row_factor = flt(self.get_uom_conversion_factor(item, target_uom=row_uom))
					if row_factor > 0:
						rate = rate * flt(self.get_uom_conversion_factor(item, target_uom=target_uom)) / row_factor
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

	def build_listing_attributes(
		self,
		item,
		item_code: str,
		sku: str,
		product_type: str,
		stock_qty: float,
		price: float,
		currency: str,
		marketplace_id: str,
		barcodes: list[dict],
	) -> dict:
		"""Build SP-API Listings Items attribute dictionary from ERPNext Item and Settings."""
		is_offer_only = product_type.upper() == "PRODUCT"
		lang_tag = self.get_language_tag()

		attributes: dict = {
			"fulfillment_availability": [
				{
					"fulfillment_channel_code": "DEFAULT",
					"quantity": int(max(0, flt(stock_qty))),
				}
			],
		}
		if is_offer_only:
			attributes["condition_type"] = [{"value": "new_new", "marketplace_id": marketplace_id}]
		else:
			attributes["item_name"] = [
				{"value": item.item_name or item.item_code, "marketplace_id": marketplace_id}
			]
			attributes["brand"] = [{"value": item.brand or "Generic", "marketplace_id": marketplace_id}]
			if item.description:
				attributes["product_description"] = [
					{"value": strip_html(item.description).strip(), "marketplace_id": marketplace_id}
				]
			warranty = getattr(item, "warranty_period", None)
			if warranty and str(warranty).strip():
				w_text = str(warranty).strip()
				if w_text.isdigit():
					w_text = f"{w_text} days manufacturer warranty"
				attributes["warranty_description"] = [
					{"language_tag": lang_tag, "value": w_text, "marketplace_id": marketplace_id}
				]

		# Never publish an unintended 0.0 price offer when no positive selling price exists.
		if flt(price) > 0:
			attributes["purchasable_offer"] = self.build_purchasable_offer(
				item, price=price, currency=currency, marketplace_id=marketplace_id
			)

		ident_attr = self.build_identifier_attribute(barcodes, marketplace_id)
		if ident_attr:
			attributes["externally_assigned_product_identifier"] = ident_attr

		hsn_code = (
			getattr(item, "gst_hsn_code", None)
			or getattr(item, "customs_tariff_number", None)
		)
		if hsn_code and 4 <= len(str(hsn_code).strip()) <= 8:
			attributes["external_product_information"] = [
				{
					"entity": "HSN Code",
					"value": str(hsn_code).strip(),
					"marketplace_id": marketplace_id,
				}
			]

		asin = self.resolve_merchant_suggested_asin(item, item_code, sku)
		if asin:
			attributes["merchant_suggested_asin"] = [
				{"value": asin, "marketplace_id": marketplace_id}
			]

		coo_iso = _resolve_country_iso2(
			getattr(item, "country_of_origin", None)
			or self.amz_setting.get("default_country_of_origin")
		)
		if coo_iso:
			attributes["country_of_origin"] = [
				{"value": coo_iso, "marketplace_id": marketplace_id}
			]

		weight_val = flt(getattr(item, "weight_per_unit", 0))
		weight_unit = _resolve_amazon_weight_unit(getattr(item, "weight_uom", None))
		if weight_val > 0 and weight_unit:
			attributes["item_weight"] = [
				{"unit": weight_unit, "value": weight_val, "marketplace_id": marketplace_id}
			]

		dg_reg = str(self.amz_setting.get("default_dg_regulation") or "").strip()
		if dg_reg:
			attributes["supplier_declared_dg_hz_regulation"] = [
				{"value": dg_reg, "marketplace_id": marketplace_id}
			]

		packer_info = str(self.amz_setting.get("packer_contact_info") or "").strip()
		if packer_info:
			attributes["packer_contact_information"] = [
				{"language_tag": lang_tag, "value": packer_info, "marketplace_id": marketplace_id}
			]

		importer_info = str(self.amz_setting.get("importer_contact_info") or "").strip()
		if importer_info:
			attributes["importer_contact_information"] = [
				{"language_tag": lang_tag, "value": importer_info, "marketplace_id": marketplace_id}
			]

		# Schema marks additionalProperties=false with only "value" permitted on this field.
		mfr_contact_info = str(self.amz_setting.get("manufacturer_contact_info") or "").strip()
		if mfr_contact_info:
			attributes["rtip_manufacturer_contact_information"] = [{"value": mfr_contact_info}]

		manufacturer = (
			getattr(item, "default_item_manufacturer", None)
			or getattr(item, "manufacturer", None)
		)
		if not manufacturer and item_code:
			manufacturer = frappe.db.get_value(
				"Item Manufacturer", {"item_code": item_code, "is_default": 1}, "manufacturer"
			) or frappe.db.get_value("Item Manufacturer", {"item_code": item_code}, "manufacturer")
		if manufacturer and str(manufacturer).strip():
			attributes["manufacturer"] = [
				{
					"language_tag": lang_tag,
					"value": str(manufacturer).strip()[:100],
					"marketplace_id": marketplace_id,
				}
			]

		# Merge category/template defaults, item overrides, and variant axes (Item Group -> Template -> Item -> Variants tab)
		item_group = item.get("item_group") if isinstance(item, dict) else getattr(item, "item_group", None)
		variant_of = item.get("variant_of") if isinstance(item, dict) else getattr(item, "variant_of", None)

		ig_attrs = _load_item_group_custom_attributes(item_group, variant_of, marketplace_id)
		if ig_attrs:
			attributes.update(ig_attrs)

		if variant_of:
			parent_attrs_raw = frappe.db.get_value("Item", variant_of, "amazon_attributes")
			if parent_attrs_raw:
				parent_custom_attrs = _parse_and_normalize_custom_attributes(
					parent_attrs_raw, marketplace_id, str(variant_of)
				)
				if parent_custom_attrs:
					attributes.update(parent_custom_attrs)

		custom_attrs = _parse_and_normalize_custom_attributes(
			item.get("amazon_attributes") if isinstance(item, dict) else getattr(item, "amazon_attributes", None),
			marketplace_id,
			item_code,
		)
		if custom_attrs:
			attributes.update(custom_attrs)

		variant_attrs = _map_variant_attributes(item, marketplace_id)
		if variant_attrs:
			attributes.update(variant_attrs)

		return attributes

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

		requirements = "LISTING_OFFER_ONLY" if p_type.upper() == "PRODUCT" else "LISTING"
		attributes = self.build_listing_attributes(
			item=item,
			item_code=real_item_code,
			sku=sku,
			product_type=p_type,
			stock_qty=stock_qty,
			price=price,
			currency=currency,
			marketplace_id=marketplace_id,
			barcodes=barcodes,
		)

		payload = {"productType": p_type, "requirements": requirements, "attributes": attributes}
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
					"value": self.build_purchasable_offer(
						item, price=price, currency=currency, marketplace_id=marketplace_id
					),
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
		or frappe.flags.get("in_amazon_listing_sync")
		or frappe.flags.get("from_amazon_fba_sync")
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
		s for s in settings_list if frappe.utils.cint(s.enable_listings_sync)
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
		or frappe.flags.get("in_amazon_listing_sync")
		or frappe.flags.get("from_amazon_fba_sync")
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
		s for s in settings_list if frappe.utils.cint(s.enable_listings_sync)
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


def _get_authorized_outbound_sync(
	item_code: str | None = None,
	item_ptype: str = "read",
	amz_setting_name: str | None = None,
	setting_ptype: str = "read",
) -> AmazonOutboundSync:
	frappe.has_permission("Item", item_ptype, throw=True)
	if item_code and frappe.db.exists("Item", item_code):
		frappe.has_permission("Item", item_ptype, doc=item_code, throw=True)
	resolved_setting = _resolve_setting_name(amz_setting_name)
	frappe.has_permission("Amazon SP API Settings", setting_ptype, doc=resolved_setting, throw=True)
	from amazon_connector.amazon_ext.repository import (
		CustomAmazonRepository as AmazonRepository,
	)

	return AmazonOutboundSync(AmazonRepository(resolved_setting))


@frappe.whitelist()
def fetch_amazon_listing(item_code: str, amz_setting_name: str | None = None) -> dict:
	sync = _get_authorized_outbound_sync(item_code=item_code, item_ptype="read", amz_setting_name=amz_setting_name, setting_ptype="read")
	return sync.get_listings_item(item_code)


@frappe.whitelist()
def delete_amazon_listing(
	item_code: str, amz_setting_name: str | None = None, remove_mapping: int = 1
) -> dict:
	sync = _get_authorized_outbound_sync(item_code=item_code, item_ptype="delete", amz_setting_name=amz_setting_name, setting_ptype="write")
	return sync.delete_listing(
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
	sync = _get_authorized_outbound_sync(item_ptype="read", amz_setting_name=amz_setting_name, setting_ptype="read")
	return sync.get_product_type_definition(product_type)


@frappe.whitelist()
def get_category_attributes_template(
	product_type: str, amz_setting_name: str | None = None
) -> dict:
	sync = _get_authorized_outbound_sync(item_ptype="read", amz_setting_name=amz_setting_name, setting_ptype="read")
	tmpl = sync.build_attributes_template(product_type)
	fields_meta = sync.build_attributes_metadata(product_type, tmpl=tmpl)
	return {
		"product_type": product_type,
		"template": tmpl,
		"fields_meta": fields_meta,
		"json": json.dumps(tmpl, indent=2) if tmpl else "{}",
	}


@frappe.whitelist()
def sync_amazon_product_types(amz_setting_name: str | None = None) -> dict:
	frappe.has_permission("Amazon SP API Settings", "write", throw=True)
	amz_setting_name = _resolve_setting_name(amz_setting_name)
	from amazon_connector.amazon_ext.repository import (
		CustomAmazonRepository as AmazonRepository,
	)

	repo = AmazonRepository(amz_setting_name)
	sync = AmazonOutboundSync(repo)
	if not hasattr(repo, "get_product_type_definitions_instance"):
		frappe.throw(_("Product Type Definitions client is not available."))

	ptd = repo.get_product_type_definitions_instance()
	raw_mkt = sync.get_marketplace_id()
	mkt = str(raw_mkt) if raw_mkt and isinstance(raw_mkt, str) else "A21TJRUUN4KGV"
	raw = ptd.make_request(method="GET", append_to_base_uri="", params={"marketplaceIds": mkt})
	pts = (raw or {}).get("productTypes") or []
	if not pts:
		return {"total": 0, "synced": 0, "marketplace_id": mkt}

	curr_time = frappe.utils.now()
	existing_names = set(frappe.get_all("Amazon Product Type", pluck="name"))
	to_insert = []
	synced = 0
	for pt in pts:
		pname = str(pt.get("name") or "").strip().upper()
		if not pname:
			continue
		dname = str(pt.get("displayName") or pname).strip()
		if pname in existing_names:
			frappe.db.set_value(
				"Amazon Product Type",
				pname,
				{"display_name": dname, "marketplace_id": mkt, "last_synced": curr_time},
				update_modified=False,
			)
		else:
			to_insert.append((
				pname,
				pname,
				dname,
				mkt,
				curr_time,
				"Administrator",
				"Administrator",
				curr_time,
				curr_time,
			))
			existing_names.add(pname)
		synced += 1

	if to_insert:
		frappe.db.bulk_insert(
			"Amazon Product Type",
			fields=[
				"name",
				"product_type",
				"display_name",
				"marketplace_id",
				"last_synced",
				"owner",
				"modified_by",
				"creation",
				"modified",
			],
			values=to_insert,
		)
	if not frappe.flags.in_test:
		frappe.db.commit()
	return {"total": len(pts), "synced": synced, "marketplace_id": mkt}


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


def _is_price_row_active(row: dict, current_date, expected_currency: str | None = None) -> bool:
	if flt(row.get("price_list_rate")) <= 0:
		return False
	if row.get("customer"):
		return False
	if row.get("batch_no"):
		return False
	if flt(row.get("packing_unit") or row.get("min_qty") or 0) > 1.0:
		return False
	if expected_currency and row.get("currency") and row.get("currency") != expected_currency:
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


def _resolve_country_iso2(country_val: str | None) -> str | None:
	if not country_val:
		return None
	cleaned = str(country_val).strip()
	if not cleaned:
		return None
	if len(cleaned) == 2 and cleaned.isalpha():
		return cleaned.upper()
	code = frappe.db.get_value("Country", cleaned, "code")
	if code and len(str(code).strip()) == 2:
		return str(code).strip().upper()
	return None


def _resolve_amazon_weight_unit(uom: str | None) -> str | None:
	if not uom:
		return None
	return AMAZON_WEIGHT_UNIT_MAP.get(str(uom).strip().lower())


def _normalize_attribute_entry(attr_key: str, item_val, marketplace_id: str) -> dict | None:
	if item_val is None:
		return None
	if isinstance(item_val, dict):
		entry = dict(item_val)
	elif isinstance(item_val, (str, int, float, bool)):
		if isinstance(item_val, str) and not item_val.strip():
			return None
		entry = {"value": item_val}
	else:
		return None

	if attr_key not in NO_MARKETPLACE_ID_ATTRS and "marketplace_id" not in entry:
		entry["marketplace_id"] = marketplace_id
	return entry


def _parse_and_normalize_custom_attributes(raw_attrs, marketplace_id: str, item_code: str) -> dict:
	if not raw_attrs:
		return {}

	if isinstance(raw_attrs, str):
		raw_text = raw_attrs.strip()
		if not raw_text:
			return {}
		try:
			parsed = json.loads(raw_text)
		except json.JSONDecodeError as exc:
			frappe.throw(_("Invalid JSON in Amazon Attributes on Item {0}: {1}").format(item_code, exc))
	elif isinstance(raw_attrs, dict):
		parsed = raw_attrs
	else:
		frappe.throw(_("Amazon Attributes on Item {0} must be a JSON object.").format(item_code))

	if not isinstance(parsed, dict):
		frappe.throw(_("Amazon Attributes on Item {0} must be a JSON object.").format(item_code))

	normalized: dict = {}
	for attr_key, attr_val in parsed.items():
		if not isinstance(attr_key, str) or not attr_key.strip() or attr_val is None:
			continue
		key = attr_key.strip()
		raw_entries = attr_val if isinstance(attr_val, list) else [attr_val]
		entries = []
		for raw_entry in raw_entries:
			entry = _normalize_attribute_entry(key, raw_entry, marketplace_id)
			if entry is not None:
				entries.append(entry)
		if entries:
			normalized[key] = entries

	return normalized


def _load_item_group_custom_attributes(
	item_group: str | None, variant_of: str | None, marketplace_id: str
) -> dict:
	"""Load and normalize category-level Amazon attributes template from the Item Group."""
	ig = item_group
	if not ig and variant_of:
		ig = frappe.db.get_value("Item", variant_of, "item_group")
	if not ig or not frappe.db.has_column("Item Group", "amazon_attributes_template"):
		return {}

	ig_attrs_raw = frappe.db.get_value("Item Group", ig, "amazon_attributes_template")
	if not ig_attrs_raw:
		return {}
	return _parse_and_normalize_custom_attributes(ig_attrs_raw, marketplace_id, f"Item Group {ig}")


def _map_variant_attributes(item, marketplace_id: str) -> dict:
	"""Map ERPNext Item Variant Attributes rows to normalized Amazon listing attributes."""
	rows = item.get("attributes") if isinstance(item, dict) else getattr(item, "attributes", None)
	if not rows:
		return {}

	mapped: dict = {}
	for row in rows:
		attr_name = row.get("attribute") if isinstance(row, dict) else getattr(row, "attribute", None)
		attr_val = row.get("attribute_value") if isinstance(row, dict) else getattr(row, "attribute_value", None)
		if not attr_name or attr_val is None or not str(attr_val).strip():
			continue

		val_str = str(attr_val).strip()
		raw_name = re.sub(r"[^a-zA-Z0-9_]+", "_", str(attr_name).strip().lower()).strip("_")
		norm_key = VARIANT_ATTRIBUTE_ALIASES.get(raw_name, raw_name)
		coerced_val: object = val_str
		if norm_key == "number_of_items" and val_str.isdigit():
			coerced_val = int(val_str)
		elif norm_key == "binding" and (" " in val_str or val_str.istitle()):
			coerced_val = re.sub(r"\s+", "_", val_str.lower())
		entry = _normalize_attribute_entry(norm_key, coerced_val, marketplace_id)
		if entry:
			mapped[norm_key] = [entry]

	return mapped


def _extract_schema_enums(prop: dict, item_props: dict, val_prop: dict) -> tuple[list | None, list | None]:
	"""Return (enum_values, enum_labels) from the first schema level defining an enum."""
	for node in (prop, item_props, val_prop):
		if isinstance(node, dict) and isinstance(node.get("enum"), list) and node["enum"]:
			enum_names = node.get("enumNames") if isinstance(node.get("enumNames"), list) else None
			return node["enum"], enum_names
	return None, None


def _match_enum_example(enums: list, example_val) -> object:
	"""Pick the enum value matching an example string (exact or normalized), falling back to enums[0]."""
	if not enums:
		return example_val or ""
	if example_val is None:
		return enums[0]
	if example_val in enums:
		return example_val

	norm_target = re.sub(r"[^a-z0-9]+", "_", str(example_val).strip().lower()).strip("_")
	for candidate in enums:
		norm_cand = re.sub(r"[^a-z0-9]+", "_", str(candidate).strip().lower()).strip("_")
		if norm_cand == norm_target:
			return candidate
	return enums[0]


_TEMPLATE_PRIORITY_FIELDS = (
	"item_type_name",
	"model_name",
	"model_number",
	"color",
	"author",
	"binding",
	"pages",
	"publication_date",
	"language",
	"recommended_browse_nodes",
	"product_tax_code",
	"headphones_form_factor",
	"connectivity_technology",
	"included_components",
	"bullet_point",
	"batteries_required",
	"number_of_items",
	"unit_count",
	"item_dimensions",
	"base_product_mfg_warranty_period",
	"warranty_provider_contact_information",
	"steps_to_avail_mfg_warranty_description",
	"base_product_mfg_warranty_tnc_description",
)

_SAFE_DEFAULT_KEYS = frozenset({
	"product_tax_code",
	"batteries_required",
	"number_of_items",
	"unit_count",
	"language",
	"base_product_mfg_warranty_period",
})

_LONG_TEXT_KEYS = frozenset({
	"bullet_point",
	"included_components",
	"steps_to_avail_mfg_warranty_description",
	"base_product_mfg_warranty_tnc_description",
	"warranty_provider_contact_information",
})

_FALLBACK_KEY_ENUMS: dict[str, tuple[list[str], list[str] | None]] = {
	"binding": (
		["paperback", "hardcover", "board_book", "spiral_bound", "mass_market", "audiobook"],
		["Paperback", "Hardcover", "Board Book", "Spiral Bound", "Mass Market Paperback", "Audiobook"],
	),
	"headphones_form_factor": (["over_ear", "on_ear", "in_ear"], ["Over Ear", "On Ear", "In Ear"]),
	"form_factor": (["over_ear", "on_ear", "in_ear"], ["Over Ear", "On Ear", "In Ear"]),
	"connectivity_technology": (["Wireless", "Bluetooth", "Wired", "NFC", "USB"], None),
}

_STATIC_TEMPLATE_VALUES: dict[str, object] = {
	"product_tax_code": "A_GEN_NOTAX",
	"batteries_required": False,
	"number_of_items": 1,
	"item_dimensions": {
		"length": {"unit": "centimeters", "value": 10.0},
		"width": {"unit": "centimeters", "value": 10.0},
		"height": {"unit": "centimeters", "value": 5.0},
	},
	"base_product_mfg_warranty_period": {"unit": "years", "value": 1},
	"pages": {"unit": "pages", "value": 100},
}


def _get_cached_item(item_code: str):
	"""Return cached Item doc or None without firing a redundant db.exists query."""
	if not item_code:
		return None
	try:
		return frappe.get_cached_doc("Item", item_code)
	except frappe.DoesNotExistError:
		return None


def _build_template_property_value(key: str, prop: dict, lang_tag: str):
	if key in _STATIC_TEMPLATE_VALUES:
		val = _STATIC_TEMPLATE_VALUES[key]
		return {k: dict(v) if isinstance(v, dict) else v for k, v in val.items()} if isinstance(val, dict) else val
	if key == "bullet_point":
		return [{"language_tag": lang_tag, "value": "Key feature 1"}, {"language_tag": lang_tag, "value": "Key feature 2"}]
	if key == "unit_count":
		return {"value": 1, "type": {"language_tag": lang_tag, "value": "count"}}

	prop_type = prop.get("type")
	items = prop.get("items") or {}
	item_props = items.get("properties") or {}
	if key == "language" and "type" in item_props and "value" in item_props:
		return {"type": "published", "value": "english"}

	if prop_type == "array" and items:
		if "language_tag" in item_props and "value" in item_props:
			val_prop = item_props.get("value") or {}
			v_enums = val_prop.get("enum") or prop.get("enum") or []
			ex = prop.get("examples") or val_prop.get("examples")
			first_ex = ex[0] if isinstance(ex, list) and ex else None
			val_ex = _match_enum_example(v_enums, first_ex) if v_enums else (first_ex or f"Enter {key.replace('_', ' ')}")
			return {"language_tag": lang_tag, "value": val_ex}
		if "unit" in item_props and "value" in item_props:
			u_enums = (item_props.get("unit") or {}).get("enum") or []
			return {"unit": u_enums[0] if u_enums else "centimeters", "value": 1.0}
		if "value" in item_props:
			val_prop = item_props.get("value") or {}
			v_enums = val_prop.get("enum") or []
			ex = prop.get("examples") or val_prop.get("examples")
			first_ex = ex[0] if isinstance(ex, list) and ex else None
			return _match_enum_example(v_enums, first_ex) if v_enums else (first_ex or "")
		return []

	if prop_type == "boolean":
		return False
	if prop_type in ("integer", "number"):
		return 1
	if "enum" in prop:
		return prop["enum"][0]
	ex = prop.get("examples")
	return ex[0] if isinstance(ex, list) and ex else ""


def _extract_field_unit_options(key: str, t_val, item_props: dict) -> tuple[str | None, list | None]:
	unit = t_val["unit"] if isinstance(t_val, dict) and "unit" in t_val else None
	unit_options = None
	if unit is None and "unit" in item_props:
		u_enums = (item_props["unit"] or {}).get("enum") or []
		if u_enums:
			unit, unit_options = u_enums[0], u_enums
	if key == "item_dimensions" and not unit_options:
		dim_len = ((item_props.get("length") or {}).get("properties") or {}).get("unit") or {}
		unit_options = dim_len.get("enum") or ["centimeters", "millimeters", "inches", "meters"]
	return unit, unit_options


def _infer_metadata_fieldtype(
	key: str, prop_type: str | None, val_prop: dict, t_val, enums: list | None, widget_type: str
) -> str:
	val_format = val_prop.get("format")
	if val_format == "date" or key in ("publication_date", "release_date", "first_available_date"):
		return "Date"
	if val_format == "date-time":
		return "Datetime"
	if prop_type == "boolean" or isinstance(t_val, bool) or val_prop.get("type") == "boolean":
		return "Check"
	if enums and isinstance(enums, list) and 1 < len(enums) <= 250:
		return "Select"

	inner_val = t_val.get("value") if isinstance(t_val, dict) else None
	if (
		prop_type == "integer"
		or val_prop.get("type") == "integer"
		or (isinstance(t_val, int) and not isinstance(t_val, bool))
		or (isinstance(inner_val, int) and not isinstance(inner_val, bool))
	):
		return "Int"
	if (
		prop_type == "number"
		or val_prop.get("type") == "number"
		or isinstance(t_val, float)
		or isinstance(inner_val, float)
	):
		return "Float"
	if key in _LONG_TEXT_KEYS:
		return "Small Text"
	if widget_type in ("dimensions", "language_obj"):
		return "Data"

	is_scalar_wrapped = (
		isinstance(t_val, dict)
		and "value" in t_val
		and set(t_val.keys()) <= {"value", "language_tag", "unit", "marketplace_id"}
	)
	return "Small Text" if (not is_scalar_wrapped and isinstance(t_val, (list, dict))) else "Data"


def _build_field_placeholder(key: str, widget_type: str, t_val) -> str:
	placeholder = ""
	if widget_type == "bullet_list" and isinstance(t_val, list):
		placeholder = "\n".join(str(b.get("value", b) if isinstance(b, dict) else b) for b in t_val if b)
	elif widget_type == "dimensions":
		placeholder = ""
	elif isinstance(t_val, dict):
		placeholder = str(t_val.get("value", ""))
	elif isinstance(t_val, list):
		placeholder = ", ".join(str(v.get("value", v) if isinstance(v, dict) else v) for v in t_val)
	elif t_val is not None and not isinstance(t_val, bool):
		placeholder = str(t_val)

	if key == "recommended_browse_nodes":
		node_ex = placeholder if placeholder.isdigit() else "3732888211"
		return f"e.g. {node_ex} (Browse Node ID)"
	return placeholder


def _build_single_field_metadata(key: str, t_val, prop: dict, req_set: set[str]) -> dict:
	item_props = (prop.get("items") or {}).get("properties") or {}
	val_prop = item_props.get("value") if isinstance(item_props.get("value"), dict) else {}
	if prop.get("format") and not val_prop.get("format"):
		val_prop = {**val_prop, "format": prop.get("format")}

	enums, enum_names = _extract_schema_enums(prop, item_props, val_prop)
	if not enums and key in _FALLBACK_KEY_ENUMS:
		enums, enum_names = _FALLBACK_KEY_ENUMS[key]

	unit, unit_options = _extract_field_unit_options(key, t_val, item_props)
	has_lang = bool(
		(isinstance(t_val, dict) and "language_tag" in t_val)
		or "language_tag" in item_props
		or "language_tag" in prop.get("properties", {})
	)

	widget_type = "standard"
	if key in ("item_dimensions", "bullet_point", "unit_count"):
		widget_type = {"item_dimensions": "dimensions", "bullet_point": "bullet_list", "unit_count": "unit_count"}[key]
	elif key == "language" and isinstance(t_val, dict) and "type" in t_val:
		widget_type = "language_obj"

	ft = _infer_metadata_fieldtype(key, prop.get("type"), val_prop, t_val, enums, widget_type)
	options_val: str | list | None = None
	if ft == "Select" and enums:
		if enum_names and isinstance(enum_names, list) and len(enum_names) == len(enums):
			options_val = [{"label": "", "value": ""}] + [
				{"label": str(lbl), "value": str(val)} for val, lbl in zip(enums, enum_names)
			]
		else:
			options_val = "\n" + "\n".join(str(e) for e in enums)

	return {
		"fieldname": key,
		"label": prop.get("title") or key.replace("_", " ").title(),
		"fieldtype": ft,
		"widget_type": widget_type,
		"reqd": 1 if (key in req_set or key in ("author", "binding", "model_name", "item_type_name")) else 0,
		"options": options_val,
		"unit": unit,
		"unit_options": unit_options,
		"has_lang": has_lang,
		"is_safe_default": key in _SAFE_DEFAULT_KEYS,
		"placeholder": _build_field_placeholder(key, widget_type, t_val),
		"description": prop.get("description") or val_prop.get("description") or "",
		"default": t_val,
	}

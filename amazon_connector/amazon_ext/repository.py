# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import time

from urllib.parse import urlparse

import dateutil
import ecommerce_integrations.amazon.doctype.amazon_sp_api_settings.amazon_repository as _upstream_repo
import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, strip_html, today

from amazon_connector.amazon_ext.lifecycle import (
	AMAZON_MODULE_DEF,
	cancel_erpnext_order_from_amazon,
	create_sales_invoice_from_sales_order,
	get_marketplace_id_for_setting,
	get_or_create_leaf_account,
	update_setting_sync_timestamp,
)
from amazon_connector.amazon_ext.listings import (
	AmazonListingSync,
	apply_amazon_fields_map,
	ensure_brand,
	ensure_ecommerce_item,
	ensure_item_group,
	ensure_manufacturer,
	normalize_catalog_v0_item,
	normalize_catalog_v2_item,
	resolve_safe_catalog_image_url,
	upsert_item_price,
)
from amazon_connector.amazon_ext.log_patch import create_log
from amazon_connector.amazon_ext.sp_api import (
	SPAPI,
	CatalogItems,
	CatalogItemsV2,
	Finances,
	ListingsItems,
	Orders,
	ProductTypeDefinitions,
	Reports,
	SPAPIError,
	Tokens,
)

_BaseAmazonRepository = getattr(_upstream_repo, "_OriginalAmazonRepository", _upstream_repo.AmazonRepository)

_IN_STATE_CODES = {
	"AP": "Andhra Pradesh",
	"DL": "Delhi",
	"GA": "Goa",
	"GJ": "Gujarat",
	"HR": "Haryana",
	"KA": "Karnataka",
	"KL": "Kerala",
	"MH": "Maharashtra",
	"MP": "Madhya Pradesh",
	"OR": "Odisha",
	"OD": "Odisha",
	"PB": "Punjab",
	"RJ": "Rajasthan",
	"TN": "Tamil Nadu",
	"TG": "Telangana",
	"TS": "Telangana",
	"UP": "Uttar Pradesh",
	"WB": "West Bengal",
}


def get_state_name_from_pincode(country_code: str = "", pincode: str = "", fallback_state: str = "") -> str:
	"""Normalize Indian 2-letter state codes or return fallback state name."""
	state = (fallback_state or "").strip()
	if (country_code or "").strip().upper() == "IN":
		mapped = _IN_STATE_CODES.get(state.upper())
		if mapped:
			return mapped
		digits = "".join(ch for ch in (pincode or "") if ch.isdigit())
		if not state and digits.startswith("56"):
			return "Karnataka"
	return state


_BLOCKED_ENDPOINT_HOSTS = {
	"169.254.169.254",
	"100.100.100.200",
	"metadata.google.internal",
	"fd00:ec2::254",
}

_PERMANENT_AUTH_ERROR_CODES = {
	"invalid_client",
	"invalid_grant",
	"unauthorized_client",
	"InvalidClientTokenId",
	"SignatureDoesNotMatch",
	"AccessDenied",
}


def _validate_endpoint_url(endpoint: str | None) -> str | None:
	if not endpoint:
		return None
	parsed = urlparse(endpoint)
	if parsed.scheme not in ("http", "https") or not parsed.netloc:
		frappe.throw(_("Custom SP-API endpoint must be a valid HTTP(S) URL."))
	host = (parsed.hostname or "").strip().lower()
	if host in _BLOCKED_ENDPOINT_HOSTS or host.startswith("169.254."):
		frappe.throw(_("Custom SP-API endpoint host is not allowed."))
	return endpoint


def resolve_custom_endpoint(amz_setting=None) -> str | None:
	endpoint = None
	if amz_setting is not None:
		endpoint = (
			amz_setting.get("custom_endpoint")
			if hasattr(amz_setting, "get")
			else getattr(amz_setting, "custom_endpoint", None)
		)
	if isinstance(endpoint, str):
		endpoint = endpoint.strip()
	if not endpoint:
		try:
			conf = getattr(frappe, "conf", None)
			if conf:
				endpoint = conf.get("amazon_sp_api_endpoint")
		except (RuntimeError, AttributeError):
			endpoint = None
	if isinstance(endpoint, str):
		endpoint = endpoint.strip().rstrip("/")
	return _validate_endpoint_url(endpoint or None)


class CustomAmazonRepository(_BaseAmazonRepository):
	"""Extended Amazon SP-API repository with lifecycle, RDT PII, FBA warehouse, and local/sandbox support."""

	def __init__(self, amz_setting) -> None:
		super().__init__(amz_setting)
		custom_endpoint = resolve_custom_endpoint(self.amz_setting)
		self.instance_params.update(
			{
				"use_sandbox": bool(self.amz_setting.get("use_sandbox")),
				"custom_endpoint": custom_endpoint,
			}
		)

	def call_sp_api_method(self, sp_api_method, **kwargs) -> dict:
		errors = {}
		max_retries = max(1, cint(self.amz_setting.max_retry_limit or 1))

		for attempt in range(max_retries):
			try:
				result = sp_api_method(**kwargs)
				if isinstance(result, dict) and "payload" in result:
					return result.get("payload")
				return result
			except SPAPIError as e:
				if e.error not in errors:
					errors[e.error] = e.error_description

				if attempt < max_retries - 1:
					time.sleep(0.05 if frappe.flags.in_test else min(2**attempt, 8))
				continue

		for error, desc in errors.items():
			frappe.msgprint(f"{error}: {desc}", alert=True, indicator="red")
			frappe.log_error(
				message=f"{error}: {desc}",
				title=f'Method "{sp_api_method.__name__}" failed',
			)

		# Only disable global scheduled sync on permanent credential/auth errors (not on transient 5xx/429 or single-record InvalidInput).
		is_permanent_auth_error = bool(set(errors.keys()) & _PERMANENT_AUTH_ERROR_CODES)
		if is_permanent_auth_error and sp_api_method.__name__ != "create_restricted_data_token":
			setting_name = getattr(self.amz_setting, "name", None)
			if setting_name and frappe.db.exists("Amazon SP API Settings", setting_name):
				frappe.db.set_value("Amazon SP API Settings", setting_name, "enable_sync", 0)
			else:
				self.amz_setting.enable_sync = 0

		frappe.throw(
			_("Amazon SP-API call failed after maximum retries: {0}").format(
				", ".join(f"{k}: {v}" for k, v in errors.items()) or _("Unknown error")
			)
		)

	def get_tokens_instance(self) -> Tokens:
		return Tokens(**self.instance_params)

	def get_finances_instance(self) -> Finances:
		return Finances(**self.instance_params)

	def get_orders_instance(self) -> Orders:
		return Orders(**self.instance_params)

	def get_catalog_items_instance(self) -> CatalogItems:
		return CatalogItems(**self.instance_params)

	def get_catalog_items_v2_instance(self) -> CatalogItemsV2:
		return CatalogItemsV2(**self.instance_params)

	def get_reports_instance(self) -> Reports:
		return Reports(**self.instance_params)

	def get_listings_items_instance(self) -> ListingsItems:
		return ListingsItems(**self.instance_params)

	def get_product_type_definitions_instance(self) -> ProductTypeDefinitions:
		return ProductTypeDefinitions(**self.instance_params)

	def _get_orders_rdt(self, order_id: str | None = None) -> str | None:
		if not cint(self.amz_setting.get("enable_rdt_pii")):
			return None

		if order_id:
			restricted_resources = [
				{
					"method": "GET",
					"path": f"/orders/v0/orders/{order_id}",
					"dataElements": ["buyerInfo", "shippingAddress"],
				},
				{"method": "GET", "path": f"/orders/v0/orders/{order_id}/buyerInfo"},
				{"method": "GET", "path": f"/orders/v0/orders/{order_id}/address"},
			]
		else:
			restricted_resources = [
				{
					"method": "GET",
					"path": "/orders/v0/orders",
					"dataElements": ["buyerInfo", "shippingAddress"],
				}
			]

		tokens = self.get_tokens_instance()
		token_payload = self.call_sp_api_method(
			sp_api_method=tokens.create_restricted_data_token,
			restricted_resources=restricted_resources,
		)
		return (token_payload or {}).get("restrictedDataToken")

	def get_account(self, name) -> str:
		company = self.amz_setting.company
		account_name = frappe.db.get_value(
			"Account", {"account_name": f"Amazon {name}", "company": company, "is_group": 0}, "name"
		)

		if not account_name:
			parent_account = self.amz_setting.get("market_place_account_group")
			if not parent_account:
				return get_or_create_leaf_account(
					company=company,
					account_name=f"Amazon {name}",
					root_type="Expense",
				)
			new_account = frappe.new_doc("Account")
			new_account.account_name = f"Amazon {name}"
			new_account.company = company
			new_account.parent_account = parent_account
			new_account.insert(ignore_permissions=True)
			account_name = new_account.name

		return account_name

	def _append_order_item_charges_fallback(self, charges: list, raw_items: list) -> None:
		for order_item in raw_items:
			sku = order_item.get("SellerSKU") or order_item.get("ASIN") or "Order Item"
			buyer_info = order_item.get("BuyerInfo") or {}
			gift_wrap_dict = (
				order_item.get("GiftWrapPrice")
				or order_item.get("GiftWrap")
				or buyer_info.get("GiftWrapPrice")
				or {}
			)
			gift_wrap_tax_dict = (
				order_item.get("GiftWrapTax")
				or buyer_info.get("GiftWrapTax")
				or {}
			)
			charge_specs = [
				("Tax", flt((order_item.get("ItemTax") or {}).get("Amount"))),
				("ShippingCharge", flt((order_item.get("ShippingPrice") or {}).get("Amount"))),
				("ShippingTax", flt((order_item.get("ShippingTax") or {}).get("Amount"))),
				("GiftWrap", flt(gift_wrap_dict.get("Amount"))),
				("GiftWrapTax", flt(gift_wrap_tax_dict.get("Amount"))),
				("CODFee", flt((order_item.get("CODFee") or {}).get("Amount"))),
				("PromotionDiscount", -abs(flt((order_item.get("PromotionDiscount") or {}).get("Amount")))),
				("ShippingDiscount", -abs(flt((order_item.get("ShippingDiscount") or {}).get("Amount")))),
				("CODFeeDiscount", -abs(flt((order_item.get("CODFeeDiscount") or {}).get("Amount")))),
			]
			for charge_type, amount in charge_specs:
				if amount != 0:
					charges.append(
						{
							"charge_type": "Actual",
							"account_head": self.get_account(charge_type),
							"tax_amount": amount,
							"description": f"{charge_type} for {sku}",
						}
					)
			for withheld_entry in order_item.get("ItemTaxWithheldList") or []:
				for tc in withheld_entry.get("TaxesWithheld") or []:
					w_amt = flt((tc.get("ChargeAmount") or {}).get("CurrencyAmount") or (tc.get("ChargeAmount") or {}).get("Amount"))
					if w_amt != 0:
						deduction = -abs(w_amt)
						charges.append(
							{
								"charge_type": "Actual",
								"account_head": self.get_account("Tax"),
								"tax_amount": deduction,
								"description": f"TaxWithheld ({tc.get('ChargeType') or 'MFT'}) for {sku}",
							}
						)

	def get_charges_and_fees(self, order_id, raw_items: list | None = None) -> dict:
		finances = self.get_finances_instance()
		financial_events_payload = self.call_sp_api_method(
			sp_api_method=finances.list_financial_events_by_order_id, order_id=order_id
		)

		charges_and_fees = {"charges": [], "fees": []}
		seen_tokens = set()

		while financial_events_payload:
			shipment_event_list = financial_events_payload.get("FinancialEvents", {}).get(
				"ShipmentEventList", []
			)
			next_token = financial_events_payload.get("NextToken")

			for shipment_event in shipment_event_list or []:
				if not shipment_event:
					continue
				for shipment_item in shipment_event.get("ShipmentItemList", []):
					charges = shipment_item.get("ItemChargeList", [])
					fees = shipment_item.get("ItemFeeList", [])
					promotions = shipment_item.get("PromotionList", [])
					withheld_list = shipment_item.get("ItemTaxWithheldList", [])
					seller_sku = shipment_item.get("SellerSKU") or shipment_item.get("ASIN") or "Order Item"

					for charge in charges:
						charge_type = charge.get("ChargeType")
						amount = charge.get("ChargeAmount", {}).get("CurrencyAmount", 0)

						if charge_type != "Principal" and flt(amount) != 0:
							charge_account = self.get_account(charge_type)
							charges_and_fees["charges"].append(
								{
									"charge_type": "Actual",
									"account_head": charge_account,
									"tax_amount": amount,
									"description": f"{charge_type} for {seller_sku}",
								}
							)

					for promo in promotions:
						promo_amt = flt((promo.get("PromotionAmount") or {}).get("CurrencyAmount"))
						if promo_amt != 0:
							charges_and_fees["charges"].append(
								{
									"charge_type": "Actual",
									"account_head": self.get_account("PromotionDiscount"),
									"tax_amount": -abs(promo_amt),
									"description": f"PromotionDiscount for {seller_sku}",
								}
							)

					for withheld_entry in withheld_list:
						for tc in withheld_entry.get("TaxesWithheld") or []:
							w_amt = flt((tc.get("ChargeAmount") or {}).get("CurrencyAmount"))
							if w_amt != 0:
								charges_and_fees["charges"].append(
									{
										"charge_type": "Actual",
										"account_head": self.get_account("Tax"),
										"tax_amount": -abs(w_amt),
										"description": f"TaxWithheld ({tc.get('ChargeType') or 'MFT'}) for {seller_sku}",
									}
								)

					for fee in fees:
						fee_type = fee.get("FeeType")
						amount = fee.get("FeeAmount", {}).get("CurrencyAmount", 0)

						if flt(amount) != 0:
							fee_account = get_or_create_leaf_account(
								self.amz_setting.company,
								f"Amazon {fee_type}",
								root_type="Expense",
								account_type="",
							)
							charges_and_fees["fees"].append(
								{
									"charge_type": "Actual",
									"account_head": fee_account,
									"tax_amount": amount,
									"description": f"{fee_type} for {seller_sku}",
								}
							)

			if not next_token or next_token in seen_tokens:
				break
			seen_tokens.add(next_token)

			financial_events_payload = self.call_sp_api_method(
				sp_api_method=finances.list_financial_events_by_order_id,
				order_id=order_id,
				next_token=next_token,
			)

		if not charges_and_fees["charges"] and raw_items:
			self._append_order_item_charges_fallback(charges_and_fees["charges"], raw_items)

		return charges_and_fees

	def _fetch_order_item_catalog(self, asin: str | None) -> dict:
		if not asin:
			return {}

		try:
			catalog_v2 = self.get_catalog_items_v2_instance()
			if catalog_v2:
				res_v2 = catalog_v2.get_catalog_item(asin)
				payload_v2 = (
					res_v2.get("payload") if isinstance(res_v2, dict) and "payload" in res_v2 else res_v2
				)
				if payload_v2:
					return normalize_catalog_v2_item(payload_v2)
		except Exception:
			pass

		try:
			catalog_v0 = self.get_catalog_items_instance()
			if catalog_v0:
				res_v0 = catalog_v0.get_catalog_item(asin)
				payload_v0 = (
					res_v0.get("payload") if isinstance(res_v0, dict) and "payload" in res_v0 else res_v0
				)
				if payload_v0:
					return normalize_catalog_v0_item(payload_v0)
		except Exception:
			pass

		return {}

	def create_item(self, order_item) -> str:
		catalog = self._fetch_order_item_catalog(order_item.get("ASIN"))

		item = frappe.new_doc("Item")
		apply_amazon_fields_map(item, self.amz_setting.amazon_fields_map, order_item)

		if not item.item_name:
			item.item_name = (
				catalog.get("title")
				or order_item.get("Title")
				or order_item.get("SellerSKU")
				or item.item_code
			)[:140]
		if not item.description:
			item.description = order_item.get("Title") or catalog.get("title") or item.item_name

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

		item.flags.from_integration = True
		item.insert(ignore_permissions=True)

		list_price = catalog.get("list_price") or 0
		upsert_item_price(item.item_code, self.amz_setting.price_list, list_price)
		ensure_ecommerce_item(
			integration_item_code=order_item.get("ASIN") or order_item.get("SellerSKU"),
			item_code=item.item_code,
			sku=order_item.get("SellerSKU"),
		)

		return item.item_code

	def get_raw_order_items(self, order_id) -> list:
		orders = self.get_orders_instance()
		order_items_payload = self.call_sp_api_method(sp_api_method=orders.get_order_items, order_id=order_id)
		raw_items = []
		seen_tokens = set()

		while order_items_payload:
			raw_items.extend(order_items_payload.get("OrderItems") or [])
			next_token = order_items_payload.get("NextToken")
			if not next_token or next_token in seen_tokens:
				break
			seen_tokens.add(next_token)
			order_items_payload = self.call_sp_api_method(
				sp_api_method=orders.get_order_items,
				order_id=order_id,
				next_token=next_token,
			)

		return raw_items

	def get_order_items(self, order_id, raw_items: list | None = None, warehouse: str | None = None) -> list:
		final_order_items = []
		target_warehouse = warehouse or self.amz_setting.warehouse
		source_items = raw_items if raw_items is not None else self.get_raw_order_items(order_id)
		include_taxes = bool(cint(self.amz_setting.get("taxes_charges")))

		for order_item in source_items:
			qty = flt(order_item.get("QuantityOrdered"))
			if qty > 0:
				item_code = self.get_item_code(order_item)
				stock_uom = frappe.db.get_value("Item", item_code, "stock_uom") or "Nos"
				total_amount = flt((order_item.get("ItemPrice") or {}).get("Amount", 0))
				if not include_taxes:
					promo_discount = abs(flt((order_item.get("PromotionDiscount") or {}).get("Amount", 0)))
					if promo_discount > 0 and total_amount >= promo_discount:
						total_amount = flt(total_amount - promo_discount)
				unit_rate = total_amount / qty
				row = {
					"item_code": item_code,
					"item_name": order_item.get("SellerSKU") or item_code,
					"description": order_item.get("Title") or item_code,
					"rate": unit_rate,
					"qty": qty,
					"stock_uom": stock_uom,
					"warehouse": target_warehouse,
					"conversion_factor": 1.0,
				}
				if frappe.db.has_column("Sales Order Item", "amazon_order_item_id"):
					row["amazon_order_item_id"] = order_item.get("OrderItemId")
				final_order_items.append(row)

		return final_order_items

	def _ensure_order_customer(self, order_doc: dict) -> str:
		buyer_info = order_doc.get("BuyerInfo") or {}
		order_customer_name = buyer_info.get("BuyerEmail") or f"Buyer - {order_doc.get('AmazonOrderId')}"
		contact_first_name = buyer_info.get("BuyerName") or order_customer_name

		existing_customer_name = frappe.db.get_value(
			"Customer", filters={"name": order_customer_name}, fieldname="name"
		) or frappe.db.get_value(
			"Customer", filters={"customer_name": order_customer_name}, fieldname="name"
		)

		if existing_customer_name:
			filters = [
				["Dynamic Link", "link_doctype", "=", "Customer"],
				["Dynamic Link", "link_name", "=", existing_customer_name],
				["Dynamic Link", "parenttype", "=", "Contact"],
			]
			if not frappe.get_all("Contact", filters=filters, limit=1):
				self._create_customer_contact(existing_customer_name, contact_first_name)
			return existing_customer_name

		new_customer = frappe.new_doc("Customer")
		new_customer.customer_name = order_customer_name
		new_customer.customer_group = self.amz_setting.customer_group
		new_customer.territory = self.amz_setting.territory
		new_customer.customer_type = self.amz_setting.customer_type
		new_customer.insert(ignore_permissions=True)

		self._create_customer_contact(new_customer.name, contact_first_name)
		return new_customer.name

	@staticmethod
	def _create_customer_contact(customer_name: str, first_name: str) -> None:
		new_contact = frappe.new_doc("Contact")
		new_contact.first_name = first_name
		new_contact.append("links", {"link_doctype": "Customer", "link_name": customer_name})
		new_contact.insert(ignore_permissions=True)

	def _resolve_country_name(self, country_code: str | None) -> str | None:
		code = (country_code or "").strip()
		if code:
			by_code = frappe.db.get_value("Country", {"code": code.lower()}, "name")
			if by_code:
				return by_code
			by_name = frappe.db.get_value("Country", {"name": code}, "name")
			if by_name:
				return by_name
		return frappe.db.get_value("Company", self.amz_setting.company, "country")

	def _ensure_order_address(self, order_doc: dict, customer_name: str) -> str | None:
		shipping_address = order_doc.get("ShippingAddress")
		if not shipping_address:
			return None

		address_line1 = shipping_address.get("AddressLine1") or "Not Provided"
		city = shipping_address.get("City") or "Not Provided"
		state = get_state_name_from_pincode(
			shipping_address.get("CountryCode") or "",
			shipping_address.get("PostalCode") or "",
			(shipping_address.get("StateOrRegion") or "").title(),
		)
		pincode = shipping_address.get("PostalCode")
		country = self._resolve_country_name(shipping_address.get("CountryCode"))

		if address_line1 != "Not Provided":
			filters = [
				["Dynamic Link", "link_doctype", "=", "Customer"],
				["Dynamic Link", "link_name", "=", customer_name],
				["Dynamic Link", "parenttype", "=", "Address"],
			]
			existing_addresses = frappe.get_all(
				"Address",
				filters=filters,
				fields=["name", "address_line1", "city", "pincode", "country"],
			)
			for addr in existing_addresses:
				if (
					addr.get("address_line1") == address_line1
					and (addr.get("city") or "") == (city or "")
					and (addr.get("pincode") or "") == (pincode or "")
					and (addr.get("country") or "") == (country or "")
				):
					return addr["name"]

		make_address = frappe.new_doc("Address")
		make_address.address_title = shipping_address.get("Name") or customer_name
		make_address.address_line1 = address_line1
		make_address.city = city
		make_address.state = state
		make_address.pincode = pincode
		make_address.country = country
		make_address.append("links", {"link_doctype": "Customer", "link_name": customer_name})
		make_address.address_type = "Shipping"
		make_address.insert(ignore_permissions=True)
		return make_address.name

	def _handle_existing_sales_order(
		self,
		so_name: str,
		order_status: str | None,
		fulfillment_channel: str | None,
		is_prime: int,
	) -> str | None:
		existing_so_meta = frappe.db.get_value(
			"Sales Order",
			so_name,
			["docstatus", "grand_total", "per_delivered", "per_billed", "amazon_order_status", "customer"],
			as_dict=True,
		)
		# Replace 0-value Pending placeholder once order pricing becomes available.
		if (
			existing_so_meta
			and existing_so_meta.docstatus != 2
			and flt(existing_so_meta.grand_total) == 0
			and flt(existing_so_meta.per_delivered) == 0
			and flt(existing_so_meta.per_billed) == 0
			and existing_so_meta.amazon_order_status in ("Pending", "PendingAvailability", None, "")
			and order_status not in ("Pending", "PendingAvailability", "Canceled")
		):
			old_so = frappe.get_doc("Sales Order", so_name)
			if old_so.docstatus == 1:
				old_so.flags.from_amazon_sync = True
				old_so.flags.ignore_permissions = True
				old_so.cancel()
			frappe.delete_doc("Sales Order", so_name, force=1, ignore_permissions=True)
			return None

		so_updates = {}
		if frappe.db.has_column("Sales Order", "amazon_order_status"):
			so_updates["amazon_order_status"] = order_status
		if frappe.db.has_column("Sales Order", "fulfillment_channel"):
			so_updates["fulfillment_channel"] = fulfillment_channel
		if frappe.db.has_column("Sales Order", "is_prime"):
			so_updates["is_prime"] = is_prime
		if so_updates:
			frappe.db.set_value("Sales Order", so_name, so_updates, update_modified=False)
		if cint(self.amz_setting.get("sync_sales_invoice")) and order_status not in (
			"Pending",
			"PendingAvailability",
		):
			create_sales_invoice_from_sales_order(self, so_name)
		return so_name

	def create_sales_order(self, order) -> str | None:
		order_id = order.get("AmazonOrderId")
		order_status = order.get("OrderStatus")
		fulfillment_channel = order.get("FulfillmentChannel")
		is_prime = cint(order.get("IsPrime"))
		so_name = frappe.db.get_value("Sales Order", filters={"amazon_order_id": order_id}, fieldname="name")

		if order_status == "Canceled":
			if so_name and cint(self.amz_setting.get("sync_cancellations", 1)):
				cancel_erpnext_order_from_amazon(self, order, so_name)
			return so_name

		if so_name:
			handled = self._handle_existing_sales_order(so_name, order_status, fulfillment_channel, is_prime)
			if handled:
				return handled

		target_warehouse = (
			(self.amz_setting.get("fba_warehouse") or self.amz_setting.warehouse)
			if fulfillment_channel == "AFN"
			else self.amz_setting.warehouse
		)
		raw_items = self.get_raw_order_items(order_id)
		items = self.get_order_items(order_id, raw_items=raw_items, warehouse=target_warehouse)
		if not items:
			return None

		customer_name = self._ensure_order_customer(order)
		address_name = self._ensure_order_address(order, customer_name)

		raw_purchase_date = order.get("PurchaseDate")
		transaction_date = (
			dateutil.parser.parse(raw_purchase_date).strftime("%Y-%m-%d")
			if raw_purchase_date
			else today()
		)
		raw_ship_date = order.get("LatestShipDate") or order.get("EarliestShipDate") or raw_purchase_date
		delivery_date = (
			dateutil.parser.parse(raw_ship_date).strftime("%Y-%m-%d") if raw_ship_date else transaction_date
		)
		if getdate(delivery_date) < getdate(transaction_date):
			delivery_date = transaction_date

		so = frappe.new_doc("Sales Order")
		so.amazon_order_id = order_id
		if frappe.db.has_column("Sales Order", "marketplace_id"):
			so.marketplace_id = order.get("MarketplaceId") or get_marketplace_id_for_setting(self.amz_setting)
		if frappe.db.has_column("Sales Order", "amazon_order_status"):
			so.amazon_order_status = order_status
		if frappe.db.has_column("Sales Order", "fulfillment_channel"):
			so.fulfillment_channel = fulfillment_channel
		if frappe.db.has_column("Sales Order", "is_prime"):
			so.is_prime = is_prime
		if frappe.db.has_column("Sales Order", "amazon_canceled"):
			so.amazon_canceled = 0
		so.customer = customer_name
		if address_name:
			so.shipping_address_name = address_name
		so.delivery_date = delivery_date
		so.transaction_date = transaction_date
		so.company = self.amz_setting.company
		if target_warehouse and hasattr(so, "set_warehouse"):
			so.set_warehouse = target_warehouse

		order_currency = (order.get("OrderTotal") or {}).get("CurrencyCode")
		if not order_currency and raw_items:
			order_currency = ((raw_items[0].get("ItemPrice") or {}).get("CurrencyCode") or "").strip()
		company_currency = frappe.db.get_value("Company", self.amz_setting.company, "default_currency")
		if order_currency and company_currency and order_currency != company_currency:
			if frappe.db.exists("Currency", order_currency):
				so.currency = order_currency
				try:
					from erpnext.setup.utils import get_exchange_rate

					ex_rate = flt(get_exchange_rate(order_currency, company_currency, transaction_date))
				except Exception:
					ex_rate = 0.0
				so.conversion_rate = ex_rate if ex_rate > 0 else 1.0

		for item in items:
			so.append("items", item)

		if self.amz_setting.taxes_charges:
			charges_and_fees = self.get_charges_and_fees(order_id, raw_items=raw_items)
			for charge in charges_and_fees.get("charges", []):
				so.append("taxes", charge)

		so.insert(ignore_permissions=True)
		so.submit()

		if cint(self.amz_setting.get("sync_sales_invoice")) and order_status not in (
			"Pending",
			"PendingAvailability",
		):
			create_sales_invoice_from_sales_order(self, so.name)

		return so.name

	def _iter_orders(
		self,
		created_after=None,
		order_statuses: list[str] | None = None,
		fulfillment_channels: list[str] | None = None,
		last_updated_after=None,
		marketplace_ids: list[str] | None = None,
		amazon_order_ids: list[str] | None = None,
	):
		orders = self.get_orders_instance()
		if order_statuses is None:
			order_statuses = [
				"PendingAvailability",
				"Pending",
				"Unshipped",
				"PartiallyShipped",
				"Shipped",
				"InvoiceUnconfirmed",
				"Canceled",
				"Unfulfillable",
			]
		if fulfillment_channels is None:
			fulfillment_channels = ["AFN", "MFN"]

		query_kwargs = {
			"order_statuses": order_statuses,
			"fulfillment_channels": fulfillment_channels,
			"marketplace_ids": marketplace_ids,
			"amazon_order_ids": amazon_order_ids,
			"max_results": 50,
		}
		if last_updated_after and not created_after:
			iso_val = (
				last_updated_after
				if isinstance(last_updated_after, str) and "T" in last_updated_after
				else dateutil.parser.parse(str(last_updated_after)).strftime("%Y-%m-%dT%H:%M:%SZ")
			)
			query_kwargs["last_updated_after"] = iso_val
		else:
			query_kwargs["created_after"] = created_after

		rdt = self._get_orders_rdt()
		if rdt:
			query_kwargs["restricted_data_token"] = rdt

		orders_payload = self.call_sp_api_method(sp_api_method=orders.get_orders, **query_kwargs)
		seen_tokens = set()

		while orders_payload:
			orders_list = orders_payload.get("Orders")
			next_token = orders_payload.get("NextToken")

			if not orders_list:
				break

			yield from orders_list

			if not next_token or next_token in seen_tokens:
				break
			seen_tokens.add(next_token)

			orders_payload = self.call_sp_api_method(
				sp_api_method=orders.get_orders,
				next_token=next_token,
				**query_kwargs,
			)

	def get_orders(
		self,
		created_after=None,
		order_statuses: list[str] | None = None,
		last_updated_after=None,
		amazon_order_ids: list[str] | None = None,
	) -> list:
		from amazon_connector.amazon_ext.fulfillment import create_delivery_note_from_amazon_order

		sales_orders = []
		had_errors = False
		import_dn = bool(cint(self.amz_setting.get("sync_delivery_notes")))
		after = created_after if created_after is not None else self.amz_setting.after_date

		for order in self._iter_orders(
			created_after=after if not last_updated_after else None,
			order_statuses=order_statuses,
			last_updated_after=last_updated_after,
			amazon_order_ids=amazon_order_ids,
		):
			order_id = order.get("AmazonOrderId") or "Unknown"
			frappe.db.savepoint("amazon_order_sync")
			prev_sp_flag = getattr(frappe.flags, "in_amazon_sync_savepoint", False)
			frappe.flags.in_amazon_sync_savepoint = True
			try:
				sales_order = self.create_sales_order(order)
				if sales_order:
					sales_orders.append(sales_order)
					if import_dn and order.get("OrderStatus") in ("Shipped", "PartiallyShipped"):
						create_delivery_note_from_amazon_order(self, order, sales_order)
			except Exception as e:
				had_errors = True
				frappe.db.rollback(save_point="amazon_order_sync")
				frappe.log_error(
					message=frappe.get_traceback(),
					title=f"Amazon Order Sync failed for {order_id}",
				)
				create_log(
					module_def=AMAZON_MODULE_DEF,
					status="Error",
					method="amazon_connector.amazon_ext.repository.get_orders",
					request_data={
						"amz_setting_name": getattr(self.amz_setting, "name", None),
						"amazon_order_id": order_id,
					},
					message=f"Failed syncing Amazon Order {order_id}: {strip_html(str(e))}",
					make_new=True,
					commit=False,
				)
			finally:
				frappe.flags.in_amazon_sync_savepoint = prev_sp_flag

		if not had_errors:
			update_setting_sync_timestamp(self.amz_setting, "last_orders_sync")
		return sales_orders

	def get_delivery_notes(self, created_after=None, last_updated_after=None) -> list:
		from amazon_connector.amazon_ext.fulfillment import create_delivery_note_from_amazon_order

		after = created_after or self.amz_setting.after_date
		delivery_notes = []
		had_errors = False

		for order in self._iter_orders(
			created_after=after if not last_updated_after else None,
			last_updated_after=last_updated_after,
			order_statuses=["Shipped", "PartiallyShipped"],
		):
			order_id = order.get("AmazonOrderId") or "Unknown"
			frappe.db.savepoint("amazon_dn_sync")
			prev_sp_flag = getattr(frappe.flags, "in_amazon_sync_savepoint", False)
			frappe.flags.in_amazon_sync_savepoint = True
			try:
				sales_order = self.create_sales_order(order)
				if not sales_order:
					continue
				dn_name = create_delivery_note_from_amazon_order(self, order, sales_order)
				if dn_name:
					delivery_notes.append(dn_name)
			except Exception as e:
				had_errors = True
				frappe.db.rollback(save_point="amazon_dn_sync")
				frappe.log_error(
					message=frappe.get_traceback(),
					title=f"Amazon Delivery Note Sync failed for {order_id}",
				)
				create_log(
					module_def=AMAZON_MODULE_DEF,
					status="Error",
					method="amazon_connector.amazon_ext.repository.get_delivery_notes",
					request_data={
						"amz_setting_name": getattr(self.amz_setting, "name", None),
						"amazon_order_id": order_id,
					},
					message=f"Failed syncing Delivery Note for Amazon Order {order_id}: {strip_html(str(e))}",
					make_new=True,
					commit=False,
				)
			finally:
				frappe.flags.in_amazon_sync_savepoint = prev_sp_flag

		if not had_errors:
			update_setting_sync_timestamp(self.amz_setting, "last_delivery_notes_sync")
		return delivery_notes

	def sync_single_order(
		self,
		amazon_order_id: str,
		sales_order_name: str | None = None,
		sync_delivery_notes: bool = True,
		sync_sales_invoice: bool = False,
	) -> dict:
		from amazon_connector.amazon_ext.fulfillment import create_delivery_note_from_amazon_order

		orders = self.get_orders_instance()
		rdt = self._get_orders_rdt(order_id=amazon_order_id)
		order_kwargs = {"order_id": amazon_order_id}
		if rdt:
			order_kwargs["restricted_data_token"] = rdt

		order = self.call_sp_api_method(sp_api_method=orders.get_order, **order_kwargs)
		if isinstance(order, dict) and "Orders" in order:
			order_list = order.get("Orders") or []
			order = order_list[0] if order_list else None
		if not order:
			frappe.throw(_("Amazon Order {0} was not found.").format(amazon_order_id))

		so_name = self.create_sales_order(order) or sales_order_name
		order_status = order.get("OrderStatus")
		fulfillment_channel = order.get("FulfillmentChannel")
		canceled = order_status == "Canceled"

		if not canceled and so_name:
			if sync_delivery_notes and order_status in ("Shipped", "PartiallyShipped"):
				create_delivery_note_from_amazon_order(self, order, so_name)
			if sync_sales_invoice or cint(self.amz_setting.get("sync_sales_invoice")):
				create_sales_invoice_from_sales_order(self, so_name)

		dn_names = frappe.get_all(
			"Delivery Note",
			filters={"amazon_order_id": amazon_order_id, "docstatus": ["!=", 2]},
			pluck="name",
		)
		si_names = frappe.get_all(
			"Sales Invoice",
			filters={"amazon_order_id": amazon_order_id, "is_return": 0, "docstatus": ["!=", 2]},
			pluck="name",
		)

		return {
			"amazon_order_id": amazon_order_id,
			"order_status": order_status,
			"fulfillment_channel": fulfillment_channel,
			"canceled": canceled,
			"delivery_notes": dn_names,
			"sales_invoices": si_names,
		}

	def get_listings(self) -> dict:
		return AmazonListingSync(self).sync()

	def sync_outbound_listing(self, item_code: str) -> dict:
		from amazon_connector.amazon_ext.outbound import AmazonOutboundSync

		return AmazonOutboundSync(self).create_or_update_listing(item_code)

	def sync_outbound_stock(self, item_code: str, warehouse: str | None = None) -> dict:
		from amazon_connector.amazon_ext.outbound import AmazonOutboundSync

		outbound = AmazonOutboundSync(self)
		stock_qty = outbound.get_stock_qty(item_code, warehouse=warehouse)
		return outbound.update_stock_and_price(item_code, stock_qty=stock_qty)

	def sync_outbound_price(self, item_code: str, price_list: str | None = None) -> dict:
		from amazon_connector.amazon_ext.outbound import AmazonOutboundSync

		outbound = AmazonOutboundSync(self)
		price = outbound.get_item_price(item_code, price_list=price_list)
		return outbound.update_stock_and_price(item_code, price=price)


AmazonRepository = CustomAmazonRepository


@frappe.whitelist()
def validate_amazon_sp_api_credentials(**args) -> None:
	frappe.has_permission("Amazon SP API Settings", "write", throw=True)
	custom_endpoint = resolve_custom_endpoint(args)
	api = SPAPI(
		iam_arn=args.get("iam_arn"),
		client_id=args.get("client_id"),
		client_secret=args.get("client_secret"),
		refresh_token=args.get("refresh_token"),
		aws_access_key=args.get("aws_access_key"),
		aws_secret_key=args.get("aws_secret_key"),
		country_code=args.get("country"),
		use_sandbox=bool(args.get("use_sandbox")),
		custom_endpoint=custom_endpoint,
	)

	try:
		api.get_access_token()
		api.get_auth()
	except SPAPIError as e:
		msg = f"<b>Error:</b> {e.error}<br/><b>Error Description:</b> {e.error_description}"
		frappe.throw(msg)
	except Exception as e:
		frappe.throw(f"<b>Error:</b> {e}")


@frappe.whitelist()
def get_orders(amz_setting_name, created_after=None, last_updated_after=None) -> list:
	frappe.has_permission("Amazon SP API Settings", "read", doc=amz_setting_name, throw=True)
	frappe.has_permission("Sales Order", "write", throw=True)
	ar = CustomAmazonRepository(amz_setting_name)
	return ar.get_orders(created_after=created_after, last_updated_after=last_updated_after)


def get_delivery_notes(amz_setting_name, created_after=None, last_updated_after=None) -> list:
	ar = CustomAmazonRepository(amz_setting_name)
	return ar.get_delivery_notes(created_after=created_after, last_updated_after=last_updated_after)


def get_listings(amz_setting_name: str) -> dict:
	ar = CustomAmazonRepository(amz_setting_name)
	return ar.get_listings()


def sync_outbound_listing(item_code: str, amz_setting_name: str | None = None) -> dict:
	if not amz_setting_name:
		amz_setting_name = frappe.db.get_value("Amazon SP API Settings", {"is_active": 1}, "name")
	ar = CustomAmazonRepository(amz_setting_name)
	return ar.sync_outbound_listing(item_code)


def sync_outbound_stock(
	item_code: str, warehouse: str | None = None, amz_setting_name: str | None = None
) -> dict:
	if not amz_setting_name:
		amz_setting_name = frappe.db.get_value("Amazon SP API Settings", {"is_active": 1}, "name")
	ar = CustomAmazonRepository(amz_setting_name)
	return ar.sync_outbound_stock(item_code, warehouse=warehouse)


def sync_outbound_price(
	item_code: str, price_list: str | None = None, amz_setting_name: str | None = None
) -> dict:
	if not amz_setting_name:
		amz_setting_name = frappe.db.get_value("Amazon SP API Settings", {"is_active": 1}, "name")
	ar = CustomAmazonRepository(amz_setting_name)
	return ar.sync_outbound_price(item_code, price_list=price_list)

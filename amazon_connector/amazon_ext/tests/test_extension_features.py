# Copyright (c) 2026, Frappe and Contributors
# See license.txt

from unittest.mock import MagicMock, patch
import uuid

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from amazon_connector.amazon_ext import fulfillment as amazon_fulfillment
from amazon_connector.amazon_ext.log_patch import _build_retry_enqueue_kwargs
from amazon_connector.amazon_ext.repository import CustomAmazonRepository
from amazon_connector.amazon_ext.sp_api import SPAPI, Orders, Tokens
from amazon_connector.overrides.amazon_sp_api_settings import CustomAmazonSPAPISettings
from amazon_connector.patches_runtime import apply_amazon_patches


def _uid(prefix: str) -> str:
	return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


def _make_fake_repo(
	company: str,
	warehouse: str,
	fba_warehouse: str | None = None,
	enable_rdt_pii: int = 0,
	sync_inventory: int = 1,
	price_list: str | None = None,
) -> CustomAmazonRepository:
	amz_setting = frappe._dict(
		name="EXT-TEST-SETTING",
		company=company,
		warehouse=warehouse,
		fba_warehouse=fba_warehouse,
		default_customer_group="All Customer Groups",
		default_territory="All Territories",
		price_list=price_list or "Standard Selling",
		marketplace_id="A21TJRUUN4KGV",
		country="IN",
		custom_endpoint="",
		use_sandbox=1,
		max_retry_limit=1,
		taxes_charges=0,
		calculate_taxes_via_erpnext=0,
		sync_orders=1,
		enable_sync=1,
		sync_inventory=sync_inventory,
		enable_rdt_pii=enable_rdt_pii,
		fulfillment_latency=2,
		create_item_if_not_exists=0,
		is_active=1,
		amazon_fields_map=[
			frappe._dict({"amazon_field": "ASIN", "item_field": "item_code", "use_to_find_item_code": 1})
		],
		get_password=lambda field: f"dummy-{field}",
	)
	repo = CustomAmazonRepository(amz_setting)
	repo.instance_params = {
		"iam_arn": "arn:aws:iam::123:role/test",
		"client_id": "TEST-CLIENT-ID",
		"client_secret": "TEST-CLIENT-SECRET",
		"refresh_token": "TEST-REFRESH-TOKEN",
		"aws_access_key": "AKIA_TEST",
		"aws_secret_key": "AWS_SECRET",
		"country_code": "IN",
		"use_sandbox": True,
		"custom_endpoint": "http://127.0.0.1:9099",
	}
	return repo


def _get_company_and_warehouses():
	company = frappe.db.get_value("Warehouse", "Finished Goods - OB", "company")
	if company:
		mfn_wh = "Finished Goods - OB"
	else:
		company = frappe.get_all("Company", limit=1, pluck="name")[0]
		mfn_wh = frappe.db.get_value("Warehouse", {"company": company, "is_group": 0}, "name")

	other_whs = frappe.get_all(
		"Warehouse",
		filters={"company": company, "is_group": 0, "name": ["!=", mfn_wh]},
		limit=1,
		pluck="name",
	)
	fba_wh = other_whs[0] if other_whs else None
	price_list = (
		frappe.db.get_value("Price List", {"selling": 1, "enabled": 1}, "name")
		or "Standard Selling"
	)
	return company, mfn_wh, fba_wh, price_list


def _create_stock_item(company: str, prefix: str = "EXT-ITEM") -> str:
	item_code = _uid(prefix)
	item_group = frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0]
	frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": item_code,
			"item_name": item_code,
			"item_group": item_group,
			"stock_uom": "Nos",
			"is_stock_item": 1,
			"include_item_in_manufacturing": 0,
		}
	).insert(ignore_permissions=True)
	return item_code


class TestAmazonExtensionWiringAndRDT(UnitTestCase):
	def test_runtime_patches_and_override_class(self):
		apply_amazon_patches()

		from ecommerce_integrations.amazon.doctype.amazon_sp_api_settings import (
			amazon_repository as upstream_repo_mod,
			amazon_sp_api as upstream_sp_api_mod,
			amazon_sp_api_settings as upstream_settings_mod,
		)

		self.assertIs(upstream_repo_mod.AmazonRepository, CustomAmazonRepository)
		self.assertIs(upstream_sp_api_mod.Tokens, Tokens)
		self.assertIsNone(upstream_settings_mod.schedule_get_order_details())

		doc = frappe.new_doc("Amazon SP API Settings")
		self.assertIsInstance(doc, CustomAmazonSPAPISettings)

		kwargs = _build_retry_enqueue_kwargs(
			"amazon_connector.amazon_ext.repository.get_orders",
			'{"amz_setting_name": "SET-1", "created_after": "2026-11-01"}',
			"LOG-1",
		)
		self.assertEqual(kwargs, {"amz_setting_name": "SET-1", "created_after": "2026-11-01"})

	def test_spapi_default_timeout(self):
		from amazon_connector.amazon_ext.sp_api import DEFAULT_SPAPI_TIMEOUT

		self.assertEqual(DEFAULT_SPAPI_TIMEOUT, (5, 30))
		base_kwargs = {
			"iam_arn": "arn:aws:iam::123:role/test",
			"client_id": "CLIENT-ID",
			"client_secret": "CLIENT-SECRET",
			"refresh_token": "REFRESH-TOKEN",
			"aws_access_key": "AKIA_TEST",
			"aws_secret_key": "AWS_SECRET",
			"country_code": "IN",
			"custom_endpoint": "http://127.0.0.1:9099",
		}
		sp = SPAPI(**base_kwargs)
		with patch("amazon_connector.amazon_ext.sp_api.request") as mock_req:
			mock_req.return_value.status_code = 200
			mock_req.return_value.json.return_value = {"access_token": "tok"}
			tok = sp.get_access_token()
			self.assertEqual(tok, "tok")
			self.assertEqual(mock_req.call_args.kwargs["timeout"], (5, 30))

		with (
			patch.object(sp, "get_access_token", return_value="tok"),
			patch("amazon_connector.amazon_ext.sp_api.request") as mock_req,
		):
			mock_req.return_value.status_code = 200
			mock_req.return_value.content = b'{"ok": true}'
			mock_req.return_value.json.return_value = {"ok": True}
			sp.make_request(method="GET", append_to_base_uri="/test")
			self.assertEqual(mock_req.call_args.kwargs["timeout"], (5, 30))

	def test_whitelisted_apis_require_permissions(self):
		from amazon_connector.amazon_ext.outbound import delete_amazon_listing, fetch_amazon_listing
		from amazon_connector.overrides.amazon_sp_api_settings import sync_outbound_item
		from amazon_connector.amazon_ext.repository import validate_amazon_sp_api_credentials

		current_user = frappe.session.user
		try:
			frappe.set_user("Guest")
			with self.assertRaises(frappe.PermissionError):
				delete_amazon_listing(item_code="NON-EXISTENT-ITEM")
			with self.assertRaises(frappe.PermissionError):
				fetch_amazon_listing(item_code="NON-EXISTENT-ITEM")
			with self.assertRaises(frappe.PermissionError):
				sync_outbound_item(item_code="NON-EXISTENT-ITEM")
			with self.assertRaises(frappe.PermissionError):
				validate_amazon_sp_api_credentials()
		finally:
			frappe.set_user(current_user)

	def test_sync_from_listview_permission_and_dispatch(self):
		from amazon_connector.overrides.amazon_sp_api_settings import sync_from_listview

		current_user = frappe.session.user
		try:
			frappe.set_user("Guest")
			with self.assertRaises(frappe.PermissionError):
				sync_from_listview("Sales Order")
		finally:
			frappe.set_user(current_user)

		with (
			patch("amazon_connector.overrides.amazon_sp_api_settings.frappe.db.exists", return_value=True),
			patch("amazon_connector.overrides.amazon_sp_api_settings.schedule_get_order_details") as mock_so_sync,
			patch("amazon_connector.overrides.amazon_sp_api_settings.schedule_get_delivery_notes") as mock_dn_sync,
		):
			res_so = sync_from_listview("Sales Order")
			self.assertEqual(res_so.get("status"), "success")
			mock_so_sync.assert_called_once()

			res_dn = sync_from_listview("Delivery Note")
			self.assertEqual(res_dn.get("status"), "success")
			mock_dn_sync.assert_called_once()

	def test_tokens_client_and_spapi_rdt_header_override(self):
		base_kwargs = {
			"iam_arn": "arn:aws:iam::123:role/test",
			"client_id": "CLIENT-ID",
			"client_secret": "CLIENT-SECRET",
			"refresh_token": "REFRESH-TOKEN",
			"aws_access_key": "AKIA_TEST",
			"aws_secret_key": "AWS_SECRET",
			"country_code": "IN",
			"use_sandbox": True,
			"custom_endpoint": "http://127.0.0.1:9099",
		}
		with patch.object(SPAPI, "get_access_token", return_value="lwa-token-1"):
			tokens = Tokens(**base_kwargs)
			self.assertEqual(tokens.get_headers()["x-amz-access-token"], "lwa-token-1")
			self.assertEqual(
				tokens.get_headers(restricted_data_token="rdt-secret-9")["x-amz-access-token"],
				"rdt-secret-9",
			)

			with patch.object(tokens, "make_request", return_value={"restrictedDataToken": "rdt-123"}) as m_req:
				res = tokens.create_restricted_data_token(
					restricted_resources=[{"method": "GET", "path": "/orders/v0/orders"}]
				)
				self.assertEqual(res["restrictedDataToken"], "rdt-123")
				m_req.assert_called_once_with(
					method="POST",
					append_to_base_uri="/restrictedDataToken",
					json_data={
						"restrictedResources": [{"method": "GET", "path": "/orders/v0/orders"}],
						"targetApplication": None,
					},
				)

			orders = Orders(**base_kwargs)
			with patch.object(orders, "make_request", return_value={"payload": {"Orders": []}}) as m_ord:
				orders.get_orders(created_after="2026-11-01", restricted_data_token="rdt-abc")
				self.assertEqual(m_ord.call_args.kwargs["restricted_data_token"], "rdt-abc")

				orders.get_order("402-1111111-1111111", restricted_data_token="rdt-single")
				self.assertEqual(m_ord.call_args.kwargs["restricted_data_token"], "rdt-single")

	def test_enable_rdt_pii_flag_controls_rdt_requests_and_orders_call(self):
		# Disabled: no Tokens call, no restricted_data_token passed
		repo_off = _make_fake_repo("Test Co", "WH-1", enable_rdt_pii=0)
		orders_api_off = MagicMock()
		orders_api_off.get_orders.return_value = {"payload": {"Orders": []}}

		with (
			patch.object(repo_off, "get_tokens_instance") as mock_tokens,
			patch.object(repo_off, "get_orders_instance", return_value=orders_api_off),
			patch("amazon_connector.amazon_ext.repository.update_setting_sync_timestamp"),
		):
			self.assertIsNone(repo_off._get_orders_rdt())
			mock_tokens.assert_not_called()
			repo_off.get_orders("2026-11-01")
			self.assertNotIn("restricted_data_token", orders_api_off.get_orders.call_args.kwargs)

		# Enabled: Tokens requested for bulk and single-order paths
		repo_on = _make_fake_repo("Test Co", "WH-1", enable_rdt_pii=1)
		tokens_api_on = MagicMock()
		tokens_api_on.create_restricted_data_token.side_effect = [
			{"restrictedDataToken": "rdt-bulk-1"},
			{"restrictedDataToken": "rdt-one-2"},
		]
		orders_api_on = MagicMock()
		orders_api_on.get_orders.return_value = {"payload": {"Orders": []}}

		with (
			patch.object(repo_on, "get_tokens_instance", return_value=tokens_api_on),
			patch.object(repo_on, "get_orders_instance", return_value=orders_api_on),
			patch("amazon_connector.amazon_ext.repository.update_setting_sync_timestamp"),
		):
			repo_on.get_orders("2026-11-01")
			first_resources = tokens_api_on.create_restricted_data_token.call_args_list[0].kwargs[
				"restricted_resources"
			]
			self.assertEqual(first_resources[0]["path"], "/orders/v0/orders")
			self.assertEqual(orders_api_on.get_orders.call_args.kwargs["restricted_data_token"], "rdt-bulk-1")

			single_rdt = repo_on._get_orders_rdt(order_id="402-2222222-2222222")
			self.assertEqual(single_rdt, "rdt-one-2")
			second_resources = tokens_api_on.create_restricted_data_token.call_args_list[1].kwargs[
				"restricted_resources"
			]
			paths = {entry["path"] for entry in second_resources}
			self.assertEqual(
				paths,
				{
					"/orders/v0/orders/402-2222222-2222222",
					"/orders/v0/orders/402-2222222-2222222/buyerInfo",
					"/orders/v0/orders/402-2222222-2222222/address",
				},
			)

	def test_sync_inventory_flag_gates_sle_job_and_dn_cancel(self):
		disabled_setting = frappe._dict(
			name="EXT-INV-OFF",
			company="Test Co",
			warehouse="WH-1",
			is_active=1,
			enable_sync=1,
			sync_inventory=0,
			get=lambda k, d=None: disabled_setting[k] if k in disabled_setting else d,
		)

		sle_doc = frappe._dict(item_code="ITEM-1", warehouse="WH-1", is_cancelled=0)
		with (
			patch("amazon_connector.amazon_ext.fulfillment.get_active_amazon_settings", return_value=[disabled_setting]),
			patch("amazon_connector.amazon_ext.fulfillment.is_item_mapped_to_amazon", return_value=True),
			patch("amazon_connector.amazon_ext.fulfillment.frappe.enqueue") as mock_enqueue,
		):
			amazon_fulfillment.handle_stock_ledger_entry(sle_doc)
			mock_enqueue.assert_not_called()

		with (
			patch("amazon_connector.amazon_ext.fulfillment.frappe.get_doc", return_value=disabled_setting),
			patch("amazon_connector.amazon_ext.fulfillment.AmazonOutboundSync") as mock_sync,
		):
			res = amazon_fulfillment.sync_stock_job("EXT-INV-OFF", "ITEM-1")
			self.assertIsNone(res)
			mock_sync.assert_not_called()

		mfn_dn = frappe._dict(
			name="DN-MFN-OFF",
			company="Test Co",
			amazon_order_id="402-3333333-3333333",
			imported_from_amazon=0,
			fulfillment_channel="MFN",
			items=[frappe._dict(item_code="ITEM-1", warehouse="WH-1", qty=1)],
		)
		with (
			patch("amazon_connector.amazon_ext.fulfillment.get_active_amazon_settings", return_value=[disabled_setting]),
			patch("amazon_connector.amazon_ext.fulfillment._update_dn_shipment_fields"),
			patch("amazon_connector.amazon_ext.fulfillment.sync_stock_job") as mock_stock_job,
		):
			amazon_fulfillment.handle_delivery_note_cancel(mfn_dn)
			mock_stock_job.assert_not_called()


class TestAmazonExtensionIntegration(IntegrationTestCase):
	def test_custom_fields_and_deprecated_channel_cleanup(self):
		self.assertFalse(frappe.db.exists("Custom Field", "Sales Order-amazon_fulfillment_channel"))
		self.assertFalse(frappe.db.exists("Custom Field", "Delivery Note-amazon_fulfillment_channel"))

		so_channel_meta = frappe.get_meta("Sales Order").get_field("fulfillment_channel")
		self.assertIsNotNone(so_channel_meta)
		self.assertEqual(so_channel_meta.fieldtype, "Select")
		self.assertIn("MFN", so_channel_meta.options or "")
		self.assertIn("AFN", so_channel_meta.options or "")

		so_prime_meta = frappe.get_meta("Sales Order").get_field("is_prime")
		self.assertIsNotNone(so_prime_meta)
		self.assertEqual(so_prime_meta.fieldtype, "Check")

		dn_channel_meta = frappe.get_meta("Delivery Note").get_field("fulfillment_channel")
		self.assertIsNotNone(dn_channel_meta)
		self.assertEqual(dn_channel_meta.fieldtype, "Select")

		settings_meta = frappe.get_meta("Amazon SP API Settings")
		for fieldname in ("fba_warehouse", "sync_inventory", "enable_rdt_pii", "custom_endpoint"):
			self.assertIsNotNone(settings_meta.get_field(fieldname), f"Missing Custom Field: {fieldname}")

	def test_afn_routes_to_fba_warehouse_and_persists_channel_and_is_prime(self):
		company, mfn_wh, existing_fba_wh, price_list = _get_company_and_warehouses()
		created_fba_wh = None
		if not existing_fba_wh:
			wh_doc = frappe.get_doc(
				{
					"doctype": "Warehouse",
					"warehouse_name": _uid("FBA-WH"),
					"company": company,
				}
			).insert(ignore_permissions=True)
			fba_wh = wh_doc.name
			created_fba_wh = fba_wh
		else:
			fba_wh = existing_fba_wh

		item_code = _create_stock_item(company, "EXT-AFN")
		ecom_item = frappe.get_doc(
			{
				"doctype": "Ecommerce Item",
				"integration": "Amazon",
				"erpnext_item_code": item_code,
				"integration_item_code": item_code,
				"sku": item_code,
			}
		).insert(ignore_permissions=True)

		afn_order_id = _uid("402-AFN")
		mfn_order_id = _uid("402-MFN")
		buyer_email = f"{_uid('buyer').lower()}@example.com"
		repo = _make_fake_repo(company, mfn_wh, fba_warehouse=fba_wh, price_list=price_list)

		def _make_order_payload(order_id: str, channel: str, is_prime: bool):
			return {
				"AmazonOrderId": order_id,
				"PurchaseDate": "2026-11-10T10:00:00Z",
				"LatestShipDate": "2026-11-12T10:00:00Z",
				"OrderStatus": "Unshipped",
				"FulfillmentChannel": channel,
				"IsPrime": is_prime,
				"MarketplaceId": "A21TJRUUN4KGV",
				"BuyerInfo": {"BuyerEmail": buyer_email, "BuyerName": "Extension Tester"},
				"ShippingAddress": {
					"Name": "Extension Tester",
					"AddressLine1": "12 Residency Road",
					"City": "Bengaluru",
					"StateOrRegion": "KA",
					"PostalCode": "560025",
					"CountryCode": "IN",
				},
			}

		def _make_items_payload(sku: str, order_item_id: str):
			return {
				"payload": {
					"OrderItems": [
						{
							"ASIN": sku,
							"OrderItemId": order_item_id,
							"SellerSKU": sku,
							"QuantityOrdered": 1,
							"ItemPrice": {"CurrencyCode": "INR", "Amount": "250.00"},
						}
					]
				}
			}

		created_customers = set()
		try:
			afn_order = _make_order_payload(afn_order_id, "AFN", True)
			mfn_order = _make_order_payload(mfn_order_id, "MFN", False)

			with (
				patch.object(
					repo,
					"get_orders_instance",
					return_value=frappe._dict(
						get_order_items=lambda order_id, next_token=None: _make_items_payload(
							item_code, f"OI-{order_id}"
						)
					),
				),
			):
				afn_so_name = repo.create_sales_order(afn_order)
				mfn_so_name = repo.create_sales_order(mfn_order)

			afn_so = frappe.get_doc("Sales Order", afn_so_name)
			mfn_so = frappe.get_doc("Sales Order", mfn_so_name)
			created_customers.update([afn_so.customer, mfn_so.customer])

			self.assertEqual(afn_so.set_warehouse, fba_wh)
			self.assertEqual(afn_so.items[0].warehouse, fba_wh)
			self.assertEqual(afn_so.fulfillment_channel, "AFN")
			self.assertEqual(afn_so.is_prime, 1)

			self.assertEqual(mfn_so.set_warehouse, mfn_wh)
			self.assertEqual(mfn_so.items[0].warehouse, mfn_wh)
			self.assertEqual(mfn_so.fulfillment_channel, "MFN")
			self.assertEqual(mfn_so.is_prime, 0)

			# Shipped AFN order creates Delivery Note routed to fba_warehouse
			afn_shipped_order = {**afn_order, "OrderStatus": "Shipped"}
			with patch.object(
				repo,
				"get_orders_instance",
				return_value=frappe._dict(
					get_order_items=lambda order_id, next_token=None: _make_items_payload(
					item_code, f"OI-{order_id}"
				)
				),
			):
				dn_name = amazon_fulfillment.create_delivery_note_from_amazon_order(
					repo, afn_shipped_order, sales_order_name=afn_so_name
				)

			self.assertIsNotNone(dn_name)
			dn_doc = frappe.get_doc("Delivery Note", dn_name)
			self.assertEqual(dn_doc.set_warehouse, fba_wh)
			self.assertEqual(dn_doc.items[0].warehouse, fba_wh)
			self.assertEqual(dn_doc.fulfillment_channel, "AFN")

			# Re-syncing existing order updates is_prime and fulfillment_channel
			updated_mfn_order = {**mfn_order, "IsPrime": True}
			with patch.object(
				repo,
				"get_orders_instance",
				return_value=frappe._dict(
					get_order_items=lambda order_id, next_token=None: _make_items_payload(
					item_code, f"OI-{order_id}"
				)
				),
			):
				repo.create_sales_order(updated_mfn_order)

			self.assertEqual(frappe.db.get_value("Sales Order", mfn_so_name, "is_prime"), 1)
		finally:
			for dn_id in frappe.get_all(
				"Delivery Note", filters={"amazon_order_id": ["in", [afn_order_id, mfn_order_id]]}, pluck="name"
			):
				dn = frappe.get_doc("Delivery Note", dn_id)
				if dn.docstatus == 1:
					dn.cancel()
				frappe.delete_doc("Delivery Note", dn_id, force=1, ignore_permissions=True)

			for so_id in frappe.get_all(
				"Sales Order", filters={"amazon_order_id": ["in", [afn_order_id, mfn_order_id]]}, pluck="name"
			):
				so = frappe.get_doc("Sales Order", so_id)
				if so.docstatus == 1:
					so.cancel()
				frappe.delete_doc("Sales Order", so_id, force=1, ignore_permissions=True)

			for cust_name in created_customers:
				if not cust_name:
					continue
				for link in frappe.get_all(
					"Dynamic Link",
					filters={"link_doctype": "Customer", "link_name": cust_name},
					fields=["parenttype", "parent"],
				):
					if frappe.db.exists(link.parenttype, link.parent):
						frappe.delete_doc(link.parenttype, link.parent, force=1, ignore_permissions=True)
				if frappe.db.exists("Customer", cust_name):
					frappe.delete_doc("Customer", cust_name, force=1, ignore_permissions=True)

			frappe.delete_doc("Ecommerce Item", ecom_item.name, force=1, ignore_permissions=True)
			frappe.delete_doc("Item", item_code, force=1, ignore_permissions=True)
			if created_fba_wh and frappe.db.exists("Warehouse", created_fba_wh):
				frappe.delete_doc("Warehouse", created_fba_wh, force=1, ignore_permissions=True)

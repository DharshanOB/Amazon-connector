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

	def test_settlement_reconciles_customer_ar_and_expense_root_type(self):
		from amazon_connector.amazon_ext.lifecycle import (
			create_sales_invoice_from_sales_order,
			get_or_create_leaf_account,
			sync_returns,
			sync_settlements,
		)
		from frappe.utils import flt

		company, mfn_wh, fba_wh, price_list = _get_company_and_warehouses()
		item1 = _create_stock_item(company, "EXT-FIN-1")
		item2 = _create_stock_item(company, "EXT-FIN-2")
		sku1 = _uid("SKU-FIN1")
		sku2 = _uid("SKU-FIN2")

		ecom1 = frappe.get_doc(
			{
				"doctype": "Ecommerce Item",
				"integration": "Amazon",
				"erpnext_item_code": item1,
				"integration_item_code": item1,
				"sku": sku1,
			}
		).insert(ignore_permissions=True)
		ecom2 = frappe.get_doc(
			{
				"doctype": "Ecommerce Item",
				"integration": "Amazon",
				"erpnext_item_code": item2,
				"integration_item_code": item2,
				"sku": sku2,
			}
		).insert(ignore_permissions=True)

		# Verify get_or_create_leaf_account with root_type="Expense" never returns an Asset/Receivable account
		ar_parent = frappe.db.get_value(
			"Account", {"company": company, "root_type": "Asset", "is_group": 1}, "name"
		)
		collision_name = _uid("Amazon Commission Collide")
		asset_acct = frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": collision_name,
				"parent_account": ar_parent,
				"company": company,
				"is_group": 0,
				"account_type": "Receivable",
			}
		).insert(ignore_permissions=True)

		exp_acct_name = get_or_create_leaf_account(company, collision_name, root_type="Expense")
		self.assertNotEqual(exp_acct_name, asset_acct.name)
		self.assertEqual(frappe.db.get_value("Account", exp_acct_name, "root_type"), "Expense")

		order_id = _uid("402-SETTLE")
		group_id = _uid("FIN-GRP")
		buyer_email = f"{_uid('finbuyer').lower()}@example.com"
		repo = _make_fake_repo(company, mfn_wh, fba_warehouse=fba_wh, price_list=price_list)
		repo.amz_setting.auto_create_sales_invoice = 1
		repo.amz_setting.auto_sync_returns = 1
		repo.amz_setting.auto_sync_settlements = 1

		order_payload = {
			"AmazonOrderId": order_id,
			"PurchaseDate": "2026-11-10T10:00:00Z",
			"LatestShipDate": "2026-11-12T10:00:00Z",
			"OrderStatus": "Shipped",
			"FulfillmentChannel": "MFN",
			"MarketplaceId": "A21TJRUUN4KGV",
			"BuyerInfo": {"BuyerEmail": buyer_email, "BuyerName": "Settlement Buyer"},
			"ShippingAddress": {
				"Name": "Settlement Buyer",
				"AddressLine1": "100 MG Road",
				"City": "Bengaluru",
				"StateOrRegion": "KA",
				"PostalCode": "560001",
				"CountryCode": "IN",
			},
		}
		items_payload = {
			"payload": {
				"OrderItems": [
					{
						"ASIN": item1,
						"OrderItemId": f"OI-1-{order_id}",
						"SellerSKU": sku1,
						"QuantityOrdered": 2,
						"QuantityShipped": 2,
						"ItemPrice": {"CurrencyCode": "INR", "Amount": "200.00"},
					},
					{
						"ASIN": item2,
						"OrderItemId": f"OI-2-{order_id}",
						"SellerSKU": sku2,
						"QuantityOrdered": 1,
						"QuantityShipped": 1,
						"ItemPrice": {"CurrencyCode": "INR", "Amount": "100.00"},
					},
				]
			}
		}

		finances_mock = MagicMock()
		# Partial price concession of 40.00 on sku1 (originally 200.00 for 2 units)
		finances_mock.list_financial_events.return_value = {
			"payload": {
				"FinancialEvents": {
					"RefundEventList": [
						{
							"AmazonOrderId": order_id,
							"PostedDate": "2026-11-11T10:00:00Z",
							"ShipmentItemAdjustmentList": [
								{
									"SellerSKU": sku1,
									"QuantityShipped": 1,
									"ItemChargeAdjustmentList": [
										{
											"ChargeType": "Principal",
											"ChargeAmount": {"CurrencyCode": "INR", "CurrencyAmount": -40.00},
										}
									],
								}
							],
						}
					]
				}
			}
		}
		finances_mock.list_financial_event_groups.return_value = {
			"payload": {
				"FinancialEventGroupList": [
					{
						"FinancialEventGroupId": group_id,
						"ProcessingStatus": "Closed",
						"FundTransferDate": "2026-11-12T10:00:00Z",
						"OriginalTotal": {"CurrencyCode": "INR", "CurrencyAmount": 410.00},
					}
				]
			}
		}
		finances_mock.list_financial_events_by_group_id.return_value = {
			"payload": {
				"FinancialEvents": {
					"ShipmentEventList": [
						{
							"AmazonOrderId": order_id,
							"ShipmentItemList": [
								{
									"SellerSKU": sku1,
									"ItemChargeList": [
										{
											"ChargeType": "Principal",
											"ChargeAmount": {"CurrencyCode": "INR", "CurrencyAmount": 200.00},
										}
									],
									"ItemFeeList": [
										{
											"FeeType": "Commission",
											"FeeAmount": {"CurrencyCode": "INR", "CurrencyAmount": -30.00},
										}
									],
								},
								{
									"SellerSKU": sku2,
									"ItemChargeList": [
										{
											"ChargeType": "Principal",
											"ChargeAmount": {"CurrencyCode": "INR", "CurrencyAmount": 100.00},
										}
									],
									"ItemFeeList": [],
								},
							],
						}
					],
					"RefundEventList": [
						{
							"AmazonOrderId": order_id,
							"ShipmentItemAdjustmentList": [
								{
									"SellerSKU": sku1,
									"ItemChargeAdjustmentList": [
										{
											"ChargeType": "Principal",
											"ChargeAmount": {"CurrencyCode": "INR", "CurrencyAmount": -40.00},
										}
									],
								}
							],
						}
					],
					"ReserveEventList": [
						{
							"ReserveType": "ReserveDebit",
							"ReserveAmount": {"CurrencyCode": "INR", "CurrencyAmount": -20.00},
						}
					],
				}
			}
		}

		so_name = None
		try:
			with (
				patch.object(
					repo,
					"get_orders_instance",
					return_value=frappe._dict(
						get_order_items=lambda order_id, next_token=None: items_payload
					),
				),
				patch.object(repo, "get_finances_instance", return_value=finances_mock),
			):
				so_name = repo.create_sales_order(order_payload)
				self.assertIsNotNone(so_name)

				si_List = frappe.get_all(
					"Sales Invoice",
					filters={"amazon_order_id": order_id, "is_return": 0, "docstatus": 1},
					pluck="name",
				)
				if not si_List:
					si_name = create_sales_invoice_from_sales_order(
						repo, so_name
					)
				else:
					si_name = si_List[0]
				self.assertIsNotNone(si_name)
				self.assertEqual(flt(frappe.db.get_value("Sales Invoice", si_name, "grand_total")), 300.0)
				self.assertEqual(flt(frappe.db.get_value("Sales Invoice", si_name, "outstanding_amount")), 300.0)

				# Sync returns: partial concession of 40.00 on sku1 (mapped to item1 via Ecommerce Item)
				ret_created = sync_returns(repo, posted_after="2026-11-01")
				self.assertEqual(len(ret_created), 1)
				cn_name = ret_created[0]
				self.assertIsNotNone(cn_name)
				cn_doc = frappe.get_doc("Sales Invoice", cn_name)
				# Must only include item1 (NOT item2!) and refund -40.00 (NOT -100.00 or -200.00!)
				self.assertEqual(len(cn_doc.items), 1)
				self.assertEqual(cn_doc.items[0].item_code, item1)
				self.assertAlmostEqual(abs(flt(cn_doc.grand_total)), 40.0, places=2)
				# Outstanding on original SI is now 300 - 40 = 260
				self.assertAlmostEqual(
					flt(frappe.db.get_value("Sales Invoice", si_name, "outstanding_amount")),
					260.0,
					places=2,
				)

				# Sync settlement: must clear the remaining 260.0 Customer AR on si_name!
				settle_created = sync_settlements(repo, started_after="2026-11-01")
				self.assertEqual(len(settle_created), 1)
				je_name = settle_created[0]
				self.assertIsNotNone(je_name)
				self.assertAlmostEqual(
					flt(frappe.db.get_value("Sales Invoice", si_name, "outstanding_amount")),
					0.0,
					places=2,
				)
		finally:
			for je_id in frappe.get_all("Journal Entry", filters={"cheque_no": group_id}, pluck="name"):
				je = frappe.get_doc("Journal Entry", je_id)
				if je.docstatus == 1:
					je.cancel()
				frappe.delete_doc("Journal Entry", je_id, force=1, ignore_permissions=True)

			for si_id in frappe.get_all(
				"Sales Invoice",
				filters={"amazon_order_id": order_id},
				order_by="is_return desc",
				pluck="name",
			):
				si = frappe.get_doc("Sales Invoice", si_id)
				if si.docstatus == 1:
					si.cancel()
				frappe.delete_doc("Sales Invoice", si_id, force=1, ignore_permissions=True)

			for dn_id in frappe.get_all("Delivery Note", filters={"amazon_order_id": order_id}, pluck="name"):
				dn = frappe.get_doc("Delivery Note", dn_id)
				if dn.docstatus == 1:
					dn.cancel()
				frappe.delete_doc("Delivery Note", dn_id, force=1, ignore_permissions=True)

			for so_id in frappe.get_all("Sales Order", filters={"amazon_order_id": order_id}, pluck="name"):
				so = frappe.get_doc("Sales Order", so_id)
				if so.docstatus == 1:
					so.cancel()
				frappe.delete_doc("Sales Order", so_id, force=1, ignore_permissions=True)

			frappe.delete_doc("Ecommerce Item", ecom1.name, force=1, ignore_permissions=True)
			frappe.delete_doc("Ecommerce Item", ecom2.name, force=1, ignore_permissions=True)
			frappe.delete_doc("Item", item1, force=1, ignore_permissions=True)
			frappe.delete_doc("Item", item2, force=1, ignore_permissions=True)

	def test_pending_order_upgrades_when_unshipped_and_charges_withheld_tax(self):
		from frappe.utils import flt

		company, mfn_wh, fba_wh, price_list = _get_company_and_warehouses()
		item_code = _create_stock_item(company, "EXT-PEND")
		ecom = frappe.get_doc(
			{
				"doctype": "Ecommerce Item",
				"integration": "Amazon",
				"erpnext_item_code": item_code,
				"integration_item_code": item_code,
				"sku": item_code,
			}
		).insert(ignore_permissions=True)

		order_id = _uid("402-PEND")
		repo = _make_fake_repo(company, mfn_wh, fba_warehouse=fba_wh, price_list=price_list)
		repo.amz_setting.taxes_charges = 1
		repo.amz_setting.calculate_taxes_via_erpnext = 0
		repo.amz_setting.auto_create_sales_invoice = 1

		pending_order = {
			"AmazonOrderId": order_id,
			"PurchaseDate": "2026-11-10T10:00:00Z",
			"LatestShipDate": "2026-11-12T10:00:00Z",
			"OrderStatus": "Pending",
			"FulfillmentChannel": "MFN",
			"MarketplaceId": "A21TJRUUN4KGV",
			"BuyerInfo": {},
			"ShippingAddress": {},
		}
		pending_items = {
			"payload": {
				"OrderItems": [
					{
						"ASIN": item_code,
						"OrderItemId": f"OI-{order_id}",
						"SellerSKU": item_code,
						"QuantityOrdered": 1,
						"ItemPrice": {"CurrencyCode": "INR", "Amount": "0.00"},
					}
				]
			}
		}

		confirmed_order = {
			**pending_order,
			"OrderStatus": "Unshipped",
			"BuyerInfo": {"BuyerEmail": f"{_uid('pend').lower()}@example.com", "BuyerName": "Confirmed Buyer"},
			"ShippingAddress": {
				"Name": "Confirmed Buyer",
				"AddressLine1": "22 Park Street",
				"City": "Kolkata",
				"StateOrRegion": "WB",
				"PostalCode": "700016",
				"CountryCode": "IN",
			},
		}
		confirmed_items = {
			"payload": {
				"OrderItems": [
					{
						"ASIN": item_code,
						"OrderItemId": f"OI-{order_id}",
						"SellerSKU": item_code,
						"QuantityOrdered": 1,
						"ItemPrice": {"CurrencyCode": "INR", "Amount": "500.00"},
						"ItemTax": {"CurrencyCode": "INR", "Amount": "45.00"},
						"GiftWrap": {"CurrencyCode": "INR", "Amount": "20.00"},
						"GiftWrapTax": {"CurrencyCode": "INR", "Amount": "3.60"},
						"PromotionDiscount": {"CurrencyCode": "INR", "Amount": "50.00"},
						"ItemTaxWithheldList": [
							{
								"TaxCollectionModel": "MarketplaceFacilitator",
								"TaxesWithheld": [
									{
										"ChargeType": "MarketplaceFacilitatorTax-Principal",
										"ChargeAmount": {"CurrencyCode": "INR", "Amount": "-48.60"},
									}
								],
							}
						],
					}
				]
			}
		}

		finances_empty = MagicMock()
		finances_empty.list_financial_events_by_order_id.return_value = {"payload": {"FinancialEvents": {}}}

		try:
			with (
				patch.object(
					repo,
					"get_orders_instance",
					return_value=frappe._dict(
						get_order_items=lambda order_id, next_token=None: pending_items
					),
				),
				patch.object(repo, "get_finances_instance", return_value=finances_empty),
			):
				so_pending_name = repo.create_sales_order(pending_order)
				self.assertIsNotNone(so_pending_name)
				self.assertEqual(flt(frappe.db.get_value("Sales Order", so_pending_name, "grand_total")), 0.0)
				# Pending order must NOT auto-create a Sales Invoice
				self.assertFalse(
					frappe.db.exists("Sales Invoice", {"amazon_order_id": order_id, "docstatus": 1})
				)

			# Now order transitions from Pending -> Unshipped with real price, GiftWrap, PromotionDiscount, and MFT withheld tax
			with (
				patch.object(
					repo,
					"get_orders_instance",
					return_value=frappe._dict(
						get_order_items=lambda order_id, next_token=None: confirmed_items
					),
				),
				patch.object(repo, "get_finances_instance", return_value=finances_empty),
			):
				so_confirmed_name = repo.create_sales_order(confirmed_order)
				self.assertIsNotNone(so_confirmed_name)
				so_doc = frappe.get_doc("Sales Order", so_confirmed_name)
				# Net = 500 (Item) + 45 (ItemTax) + 20 (GiftWrap) + 3.60 (GiftWrapTax) - 50 (Promo) - 48.60 (Withheld MFT) = 470.00
				self.assertAlmostEqual(flt(so_doc.grand_total), 470.0, places=2)
		finally:
			for si_id in frappe.get_all("Sales Invoice", filters={"amazon_order_id": order_id}, pluck="name"):
				si = frappe.get_doc("Sales Invoice", si_id)
				if si.docstatus == 1:
					si.cancel()
				frappe.delete_doc("Sales Invoice", si_id, force=1, ignore_permissions=True)

			for so_id in frappe.get_all("Sales Order", filters={"amazon_order_id": order_id}, pluck="name"):
				so = frappe.get_doc("Sales Order", so_id)
				if so.docstatus == 1:
					so.cancel()
				frappe.delete_doc("Sales Order", so_id, force=1, ignore_permissions=True)

			frappe.delete_doc("Ecommerce Item", ecom.name, force=1, ignore_permissions=True)
			frappe.delete_doc("Item", item_code, force=1, ignore_permissions=True)

	def test_customer_and_min_qty_prices_ignored_and_ssrf_blocked(self):
		from amazon_connector.amazon_ext.outbound import AmazonOutboundSync
		from amazon_connector.amazon_ext.repository import resolve_custom_endpoint
		from amazon_connector.amazon_ext.sp_api import Reports, SPAPIError

		# 1. SSRF / scheme validation on custom_endpoint and download_report_document
		with self.assertRaises(frappe.ValidationError):
			resolve_custom_endpoint({"custom_endpoint": "http://169.254.169.254/latest/meta-data/"})
		with self.assertRaises(frappe.ValidationError):
			resolve_custom_endpoint({"custom_endpoint": "file:///etc/passwd"})

		rep = Reports(
			iam_arn="arn:aws:iam::123:role/test",
			client_id="C",
			client_secret="S",
			refresh_token="R",
			aws_access_key="AK",
			aws_secret_key="SK",
			country_code="IN",
		)
		with self.assertRaises(SPAPIError):
			rep.download_report_document({"url": "http://169.254.169.254/latest/meta-data/"})

		# 2. Numeric packageReferenceId in _build_package_detail
		pkg = amazon_fulfillment._build_package_detail(
			delivery_note_name="MAT-DN-2026-00042",
			carrier_code="Blue Dart",
			carrier_name="Blue Dart",
			tracking_number="BD-999",
			ship_date="2026-11-12",
			order_items=[{"orderItemId": "OI-1", "quantity": 1}],
		)
		self.assertTrue(pkg["packageReferenceId"].isdigit())
		self.assertGreater(int(pkg["packageReferenceId"]), 0)

		# 3. Customer-specific and packing_unit > 1 Item Prices ignored by outbound price resolver
		company, mfn_wh, fba_wh, price_list = _get_company_and_warehouses()
		item_code = _create_stock_item(company, "EXT-PRC")
		customer = frappe.get_all("Customer", limit=1, pluck="name")[0]

		p_general = frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": item_code,
				"price_list": price_list,
				"price_list_rate": 500.0,
				"selling": 1,
			}
		).insert(ignore_permissions=True)
		p_cust = frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": item_code,
				"price_list": price_list,
				"price_list_rate": 199.0,
				"selling": 1,
				"customer": customer,
			}
		).insert(ignore_permissions=True)
		p_bulk = frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": item_code,
				"price_list": price_list,
				"price_list_rate": 299.0,
				"selling": 1,
				"packing_unit": 10,
			}
		).insert(ignore_permissions=True)

		try:
			repo = _make_fake_repo(company, mfn_wh, price_list=price_list)
			outbound = AmazonOutboundSync(repo)
			self.assertEqual(outbound.get_item_price(item_code, price_list=price_list), 500.0)
		finally:
			for p in (p_bulk, p_cust, p_general):
				frappe.delete_doc("Item Price", p.name, force=1, ignore_permissions=True)
			frappe.delete_doc("Item", item_code, force=1, ignore_permissions=True)


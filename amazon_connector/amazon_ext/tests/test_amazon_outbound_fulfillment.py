# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, nowdate, now_datetime

from amazon_connector.amazon_ext import fulfillment as amazon_fulfillment
from amazon_connector.amazon_ext import outbound as amazon_outbound
from amazon_connector.amazon_ext.sp_api import SPAPIError


def _uid(prefix: str) -> str:
	return f"{prefix}-{frappe.generate_hash(length=6).upper()}"


def _make_mock_repo(amz_setting=None):
	repo = MagicMock()
	repo.amz_setting = amz_setting or frappe._dict(
		name="AMZ-MOCK-001",
		seller_id="SELLER-XYZ",
		client_id="CLIENT-FALLBACK",
		country="IN",
		company="OneBook",
		warehouse="Finished Goods - OB",
		price_list="Standard Selling",
		is_active=1,
		enable_sync=1,
		enable_listings_sync=1,
	)
	repo.instance_params = {
		"iam_arn": "arn:aws:iam::123:role/test",
		"client_id": "CLIENT-FALLBACK",
		"client_secret": "secret",
		"refresh_token": "refresh",
		"aws_access_key": "AKIA_TEST",
		"aws_secret_key": "AWS_SECRET",
		"country_code": "IN",
		"use_sandbox": True,
	}
	repo.marketplace_id = "A21TJRUUN4KGV"
	mock_inst = MagicMock()
	mock_inst.marketplace_id = "A21TJRUUN4KGV"
	repo.get_listings_items_instance.return_value = mock_inst
	return repo


def _ensure_company_warehouse_setup():
	"""Find existing Company/Warehouse/Price List/Customer without leaking commits."""
	company = frappe.db.get_value("Warehouse", "Finished Goods - OB", "company")
	if company:
		warehouse = "Finished Goods - OB"
		group_wh = frappe.db.get_value("Warehouse", warehouse, "parent_warehouse")
	else:
		company = frappe.get_all("Company", limit=1, pluck="name")[0]
		warehouse = frappe.db.get_value("Warehouse", {"company": company, "is_group": 0}, "name")
		group_wh = frappe.db.get_value("Warehouse", warehouse, "parent_warehouse")

	price_list = (
		frappe.db.get_value("Price List", {"selling": 1, "enabled": 1}, "name")
		or "Standard Selling"
	)
	return company, warehouse, group_wh, price_list


def _create_test_item(prefix="OB-ITEM", is_stock_item=1, stock_uom="Nos", sales_uom=None, conv=None):
	item_code = _uid(prefix)
	doc = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": item_code,
			"item_name": item_code,
			"item_group": frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0],
			"stock_uom": stock_uom,
			"is_stock_item": is_stock_item,
			"include_item_in_manufacturing": 0,
		}
	)
	if sales_uom and sales_uom != stock_uom:
		if not frappe.db.exists("UOM", sales_uom):
			frappe.get_doc({"doctype": "UOM", "uom_name": sales_uom}).insert(ignore_permissions=True)
		doc.sales_uom = sales_uom
		doc.append("uoms", {"uom": sales_uom, "conversion_factor": conv or 1})
	doc.insert(ignore_permissions=True)
	return doc


def _create_ecommerce_item_mapping(item_code: str, sku: str | None = None, inventory_synced_on=None, item_synced_on=None):
	ecom_doc = frappe.get_doc(
		{
			"doctype": "Ecommerce Item",
			"integration": "Amazon",
			"erpnext_item_code": item_code,
			"integration_item_code": sku or item_code,
			"sku": sku or item_code,
			"inventory_synced_on": inventory_synced_on,
			"item_synced_on": item_synced_on,
		}
	)
	ecom_doc.insert(ignore_permissions=True)
	return ecom_doc


class TestAmazonOutbound(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.company, self.warehouse, self.group_wh, self.price_list = _ensure_company_warehouse_setup()
		self.patch_log = patch(
			"amazon_connector.amazon_ext.outbound.create_log",
			return_value=frappe._dict(name="MOCK-LOG"),
		)
		self.mock_create_log = self.patch_log.start()
		self.addCleanup(self.patch_log.stop)

	def test_seller_id_and_product_type_resolution(self):
		repo_with_seller = _make_mock_repo()
		outbound = amazon_outbound.AmazonOutboundSync(repo_with_seller)
		self.assertEqual(outbound.get_seller_id(), "SELLER-XYZ")

		repo_without_seller = _make_mock_repo(
			frappe._dict(name="AMZ-NOSELLER", seller_id="", client_id="CLIENT-ONLY", country="IN")
		)
		outbound_fb = amazon_outbound.AmazonOutboundSync(repo_without_seller)
		self.assertEqual(outbound_fb.get_seller_id(), "CLIENT-ONLY")

		item_with_pt = frappe._dict(item_code="X", amazon_product_type="SHIRT")
		item_without_pt = frappe._dict(item_code="Y", amazon_product_type="")
		self.assertEqual(outbound.get_product_type(item_with_pt), "SHIRT")
		self.assertEqual(outbound.get_product_type(item_without_pt), "PRODUCT")

		# In-memory settings without .name attribute must not fail create_or_update_listing
		item = _create_test_item("OB-NONAME")
		ephemeral_setting = frappe._dict(
			seller_id="S-EPHEM",
			client_id="C-EPHEM",
			country="IN",
			warehouse=self.warehouse,
			price_list=self.price_list,
		)
		repo_ephem = _make_mock_repo(ephemeral_setting)
		listings_client = MagicMock()
		listings_client.put_listings_item.return_value = {"status": "ACCEPTED", "issues": []}
		listings_client.marketplace_id = "A21TJRUUN4KGV"
		repo_ephem.get_listings_items_instance.return_value = listings_client
		put_res = amazon_outbound.AmazonOutboundSync(repo_ephem).create_or_update_listing(item.name)
		self.assertEqual(put_res["status"], "ACCEPTED")

	def test_get_product_type_definition_normalizes_schema(self):
		repo = _make_mock_repo()
		pt_client = MagicMock()
		pt_client.get_definitions_product_type.return_value = {
			"productType": "LUGGAGE",
			"requirements": "LISTING",
			"schema": {"link": {"resource": "https://schema.example/luggage.json"}},
			"propertyGroups": {"offer": {"propertyNames": ["purchasable_offer", "fulfillment_availability"]}},
		}
		repo.get_product_type_definitions_instance.return_value = pt_client

		outbound = amazon_outbound.AmazonOutboundSync(repo)
		res = outbound.get_product_type_definition("LUGGAGE")
		self.assertEqual(res["productType"], "LUGGAGE")
		self.assertEqual(res["schemaLink"], "https://schema.example/luggage.json")
		self.assertIn("purchasable_offer", res["propertyNames"])
		pt_client.get_definitions_product_type.assert_called_once()

	def test_build_identifier_attribute_strict_gtin_and_multi_barcode_ranking(self):
		outbound = amazon_outbound.AmazonOutboundSync(_make_mock_repo())
		# Valid EAN-13 check digit: 4006381333931 (check digit 1)
		# Numeric 12-digit UPC with dummy digits: 012345678901
		# Garbage alphanumeric code should be skipped, NOT mistagged as 'ean'
		barcodes = [
			frappe._dict(barcode="BAD-SKU-99", barcode_type=""),
			frappe._dict(barcode="012345678901", barcode_type="UPC"),
			frappe._dict(barcode="4006381333931", barcode_type="EAN"),
		]
		attrs = outbound.build_identifier_attribute(barcodes, "A21TJRUUN4KGV")
		self.assertEqual(len(attrs), 1)
		# Check-digit-valid EAN-13 ranks ahead of check-digit-invalid 12-digit UPC
		self.assertEqual(attrs[0]["type"], "ean")
		self.assertEqual(attrs[0]["value"], "4006381333931")

		garbage_only = outbound.build_identifier_attribute(
			[frappe._dict(barcode="CUSTOM_12345", barcode_type="")],
			"A21TJRUUN4KGV",
		)
		self.assertEqual(garbage_only, [])

	def test_get_item_price_filters_selling_uom_and_validity_dates(self):
		item = _create_test_item("OB-PRICE")
		pl = self.price_list
		buying_pl = frappe.db.get_value("Price List", {"buying": 1, "selling": 0, "enabled": 1}, "name")
		if not buying_pl:
			buying_pl = frappe.get_doc(
				{
					"doctype": "Price List",
					"price_list_name": _uid("BUY-PL"),
					"currency": frappe.db.get_value("Price List", pl, "currency") or "INR",
					"buying": 1,
					"selling": 0,
					"enabled": 1,
				}
			).insert(ignore_permissions=True).name

		# Buying price should NEVER leak into selling price lookup (even if not filtered by selling=1, e.g. cross-check)
		frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": item.name,
				"price_list": buying_pl,
				"price_list_rate": 999.0,
				"buying": 1,
				"selling": 0,
			}
		).insert(ignore_permissions=True)

		outbound = amazon_outbound.AmazonOutboundSync(
			_make_mock_repo(frappe._dict(name="AMZ-P", price_list=buying_pl, warehouse=self.warehouse, country="IN"))
		)
		self.assertEqual(outbound.get_item_price(item.name, price_list=buying_pl), 0.0)

		outbound = amazon_outbound.AmazonOutboundSync(
			_make_mock_repo(frappe._dict(name="AMZ-P", price_list=pl, warehouse=self.warehouse, country="IN"))
		)
		self.assertEqual(outbound.get_item_price(item.name, price_list=pl), 0.0)

		# Expired selling price should also be ignored
		frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": item.name,
				"price_list": pl,
				"price_list_rate": 150.0,
				"selling": 1,
				"valid_from": add_days(nowdate(), -10),
				"valid_upto": add_days(nowdate(), -2),
			}
		).insert(ignore_permissions=True)
		self.assertEqual(outbound.get_item_price(item.name, price_list=pl), 0.0)

		# Valid active selling price should be returned
		frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": item.name,
				"price_list": pl,
				"price_list_rate": 249.5,
				"selling": 1,
				"valid_from": add_days(nowdate(), -1),
				"valid_upto": add_days(nowdate(), 10),
			}
		).insert(ignore_permissions=True)
		self.assertEqual(outbound.get_item_price(item.name, price_list=pl), 249.5)

	def test_update_stock_and_price_never_overwrites_zero_price(self):
		item = _create_test_item("OB-NOPRICE")
		repo = _make_mock_repo(
			frappe._dict(
				name="AMZ-NP",
				seller_id="SELLER-1",
				client_id="C1",
				country="IN",
				warehouse=self.warehouse,
				price_list=self.price_list,
			)
		)
		listings_client = MagicMock()
		listings_client.patch_listings_item.return_value = {"status": "ACCEPTED", "issues": []}
		repo.get_listings_items_instance.return_value = listings_client

		outbound = amazon_outbound.AmazonOutboundSync(repo)
		res = outbound.update_stock_and_price(item.name, stock_qty=5, price=None)
		self.assertEqual(res["status"], "ACCEPTED")

		sent_patches = listings_client.patch_listings_item.call_args.kwargs["body"]["patches"]
		paths = [p["path"] for p in sent_patches]
		self.assertIn("/attributes/fulfillment_availability", paths)
		self.assertNotIn("/attributes/purchasable_offer", paths)

		# Explicit 0.0 must also NOT push purchasable_offer=0.0
		res_skip = outbound.update_stock_and_price(item.name, stock_qty=None, price=0.0)
		self.assertEqual(res_skip["status"], "SKIPPED")

	def test_get_stock_qty_subtracts_reserved_aggregates_group_wh_and_applies_uom(self):
		item = _create_test_item("OB-UOM-STK", sales_uom="Box", conv=4)
		wh_parent = self.group_wh or self.warehouse
		child_whs = (
			frappe.get_all(
				"Warehouse",
				filters={"parent_warehouse": wh_parent, "is_group": 0, "company": self.company},
				pluck="name",
				limit=2,
			)
			if self.group_wh
			else [self.warehouse]
		)
		wh1 = child_whs[0]
		wh2 = child_whs[1] if len(child_whs) > 1 else None

		# Put actual & reserved qtys cleanly inside test rollback transaction
		bin1 = frappe.get_doc(
			{"doctype": "Bin", "item_code": item.name, "warehouse": wh1, "actual_qty": 10.0, "reserved_qty": 3.0}
		).insert(ignore_permissions=True)
		self.assertTrue(bin1.name)
		if wh2:
			frappe.get_doc(
				{"doctype": "Bin", "item_code": item.name, "warehouse": wh2, "actual_qty": 7.0, "reserved_qty": 2.0}
			).insert(ignore_permissions=True)
			expected_stock_uom_net = (10.0 - 3.0) + (7.0 - 2.0)  # 12
		else:
			expected_stock_uom_net = 7.0

		outbound = amazon_outbound.AmazonOutboundSync(
			_make_mock_repo(frappe._dict(name="AMZ-WH", warehouse=wh_parent, company=self.company, country="IN"))
		)
		net_sales_uom_qty = outbound.get_stock_qty(item.name)
		self.assertAlmostEqual(net_sales_uom_qty, expected_stock_uom_net / 4.0)

	def test_invalid_response_retries_and_missing_item_guard(self):
		item = _create_test_item("OB-INV")
		_create_ecommerce_item_mapping(item.name, sku="SKU-INV-1")

		repo = _make_mock_repo(
			frappe._dict(
				name="AMZ-INV",
				seller_id="S-1",
				client_id="C-1",
				country="IN",
				warehouse=self.warehouse,
				price_list=self.price_list,
				enable_sync=1,
			)
		)
		listings_client = MagicMock()
		err_429 = SPAPIError({"errors": [{"code": "QuotaExceeded", "message": "429"}]})
		err_429.error_response = frappe._dict(status_code=429)
		listings_client.patch_listings_item.side_effect = [
			err_429,
			{"status": "INVALID", "issues": [{"code": "90220", "message": "Invalid attribute value"}]},
		]
		repo.get_listings_items_instance.return_value = listings_client

		outbound = amazon_outbound.AmazonOutboundSync(repo)
		res = outbound.update_stock_and_price(item.name, stock_qty=4, price=None)
		self.assertEqual(res["status"], "INVALID")
		self.assertEqual(listings_client.patch_listings_item.call_count, 2)
		self.assertTrue(res["issues"])
		# Plain-text log with non-null method and no <strong> HTML
		for call in self.mock_create_log.call_args_list:
			self.assertTrue(call.kwargs.get("method"))
			msg = call.kwargs.get("message") or ""
			self.assertNotIn("<strong>", msg)

		# Stale RQ job on deleted Item must not raise LinkValidationError
		outbound.record_status_on_ecommerce_item("ITEM-ALREADY-DELETED-XYZ", "SKU-GONE", "ACCEPTED")

	def test_get_and_delete_listings_item_and_item_hooks(self):
		item = _create_test_item("OB-CRUD")
		_create_ecommerce_item_mapping(item.name, sku="SKU-CRUD-1")

		repo = _make_mock_repo(
			frappe._dict(
				name="AMZ-CRUD",
				seller_id="S-CRUD",
				client_id="C-CRUD",
				country="IN",
				warehouse=self.warehouse,
				price_list=self.price_list,
				is_active=1,
				enable_sync=1,
				enable_listings_sync=1,
			)
		)
		listings_client = MagicMock()
		listings_client.get_listings_item.return_value = {"sku": "SKU-CRUD-1", "summaries": [{"status": ["BUYABLE"]}]}
		listings_client.delete_listings_item.return_value = {"status": "ACCEPTED", "sku": "SKU-CRUD-1"}
		repo.get_listings_items_instance.return_value = listings_client

		outbound = amazon_outbound.AmazonOutboundSync(repo)
		fetched = outbound.get_listings_item(item.name)
		self.assertEqual(fetched["sku"], "SKU-CRUD-1")
		listings_client.get_listings_item.assert_called_once()

		deleted = outbound.delete_listing(item.name, remove_mapping=True)
		self.assertEqual(deleted["status"], "ACCEPTED")
		listings_client.delete_listings_item.assert_called_once()
		self.assertFalse(
			frappe.db.exists("Ecommerce Item", {"integration": "amazon", "erpnext_item_code": item.name})
		)

	def test_product_type_requirements_offer_only_vs_specific_listing(self):
		item = _create_test_item("OB-PTYPE")
		repo = _make_mock_repo(
			frappe._dict(
				name="AMZ-PTYPE",
				seller_id="S-PTYPE",
				client_id="C-PTYPE",
				country="IN",
				warehouse=self.warehouse,
				price_list=self.price_list,
			)
		)
		listings_client = MagicMock()
		listings_client.put_listings_item.return_value = {"status": "ACCEPTED", "issues": []}
		listings_client.marketplace_id = "A21TJRUUN4KGV"
		repo.get_listings_items_instance.return_value = listings_client

		pt_client = MagicMock()
		pt_client.get_definitions_product_type.return_value = {"productType": "PRODUCT", "requirements": "LISTING_OFFER_ONLY"}
		repo.get_product_type_definitions_instance.return_value = pt_client

		outbound = amazon_outbound.AmazonOutboundSync(repo)
		outbound.create_or_update_listing(item.name, product_type="PRODUCT", price_override=199.0)
		offer_body = listings_client.put_listings_item.call_args.kwargs["body"]
		self.assertEqual(offer_body["requirements"], "LISTING_OFFER_ONLY")
		self.assertIn("condition_type", offer_body["attributes"])
		self.assertNotIn("item_name", offer_body["attributes"])

		outbound.create_or_update_listing(item.name, product_type="HEADPHONES", price_override=199.0)
		full_body = listings_client.put_listings_item.call_args.kwargs["body"]
		self.assertEqual(full_body["requirements"], "LISTING")
		self.assertIn("item_name", full_body["attributes"])
		self.assertIn("brand", full_body["attributes"])

		outbound.get_product_type_definition("PRODUCT")
		self.assertEqual(
			pt_client.get_definitions_product_type.call_args.kwargs["requirements"],
			"LISTING_OFFER_ONLY",
		)

	def test_india_purchasable_offer_includes_audience_and_mrp_in_put_and_patch(self):
		item = _create_test_item("OB-IN-MRP")
		item.amazon_mrp = 799.0
		item.save(ignore_permissions=True)

		in_repo = _make_mock_repo(
			frappe._dict(
				name="AMZ-IN-MRP",
				seller_id="S-IN",
				client_id="C-IN",
				country="IN",
				warehouse=self.warehouse,
				price_list=self.price_list,
			)
		)
		listings_client = MagicMock()
		listings_client.put_listings_item.return_value = {"status": "ACCEPTED", "issues": []}
		listings_client.patch_listings_item.return_value = {"status": "ACCEPTED", "issues": []}
		listings_client.marketplace_id = "A21TJRUUN4KGV"
		in_repo.get_listings_items_instance.return_value = listings_client

		outbound_in = amazon_outbound.AmazonOutboundSync(in_repo)
		outbound_in.create_or_update_listing(item.name, product_type="HEADPHONES", price_override=599.0)
		put_offer = listings_client.put_listings_item.call_args.kwargs["body"]["attributes"]["purchasable_offer"][0]
		self.assertEqual(put_offer["audience"], "ALL")
		self.assertEqual(put_offer["currency"], "INR")
		self.assertEqual(put_offer["our_price"][0]["schedule"][0]["value_with_tax"], 599.0)
		self.assertEqual(put_offer["maximum_retail_price"][0]["schedule"][0]["value_with_tax"], 799.0)

		outbound_in.update_stock_and_price(item.name, stock_qty=10, price=549.0)
		patches = listings_client.patch_listings_item.call_args.kwargs["body"]["patches"]
		offer_patch = next(p for p in patches if p["path"] == "/attributes/purchasable_offer")
		self.assertEqual(offer_patch["value"][0]["audience"], "ALL")
		self.assertEqual(offer_patch["value"][0]["maximum_retail_price"][0]["schedule"][0]["value_with_tax"], 799.0)

		# Fallback to selling price when amazon_mrp is zero
		item.amazon_mrp = 0
		item.save(ignore_permissions=True)
		outbound_in.create_or_update_listing(item.name, product_type="PRODUCT", price_override=499.0)
		fb_offer = listings_client.put_listings_item.call_args.kwargs["body"]["attributes"]["purchasable_offer"][0]
		self.assertEqual(fb_offer["maximum_retail_price"][0]["schedule"][0]["value_with_tax"], 499.0)

		# Non-India marketplaces should omit maximum_retail_price
		us_pl = frappe.db.get_value("Price List", {"selling": 1, "currency": "USD"}, "name")
		us_repo = _make_mock_repo(
			frappe._dict(
				name="AMZ-US",
				seller_id="S-US",
				client_id="C-US",
				country="US",
				warehouse=self.warehouse,
				price_list=us_pl,
			)
		)
		us_repo.get_listings_items_instance.return_value = listings_client
		outbound_us = amazon_outbound.AmazonOutboundSync(us_repo)
		with patch.object(outbound_us, "get_currency", return_value="USD"):
			outbound_us.create_or_update_listing(item.name, product_type="PRODUCT", price_override=29.0)
		us_offer = listings_client.put_listings_item.call_args.kwargs["body"]["attributes"]["purchasable_offer"][0]
		self.assertEqual(us_offer["audience"], "ALL")
		self.assertEqual(us_offer["currency"], "USD")
		self.assertNotIn("maximum_retail_price", us_offer)

	def test_compliance_defaults_and_standard_item_field_mappings(self):
		if not frappe.db.exists("Customs Tariff Number", "85183000"):
			frappe.get_doc({"doctype": "Customs Tariff Number", "tariff_number": "85183000"}).insert(
				ignore_permissions=True
			)
		if not frappe.db.exists("UOM", "Kg"):
			frappe.get_doc({"doctype": "UOM", "uom_name": "Kg"}).insert(ignore_permissions=True)
		if not frappe.db.exists("Manufacturer", "Acme Mfg"):
			frappe.get_doc({"doctype": "Manufacturer", "short_name": "Acme Mfg"}).insert(ignore_permissions=True)
		if not frappe.db.exists("Amazon Product Type", "HEADPHONES"):
			frappe.get_doc({
				"doctype": "Amazon Product Type",
				"product_type": "HEADPHONES",
				"display_name": "Headphones",
				"marketplace_id": "A21TJRUUN4KGV",
			}).insert(ignore_permissions=True)

		item = _create_test_item("OB-COMPLY")
		item.country_of_origin = "India"
		item.customs_tariff_number = "85183000"
		item.weight_per_unit = 0.25
		item.weight_uom = "Kg"
		item.warranty_period = "365"
		item.default_item_manufacturer = "Acme Mfg"
		item.amazon_asin = "b09test123"
		item.amazon_product_type = "HEADPHONES"
		item.save(ignore_permissions=True)

		setting = frappe._dict(
			name="AMZ-IN-COMPLY",
			seller_id="S-IN",
			client_id="C-IN",
			country="IN",
			warehouse=self.warehouse,
			price_list=self.price_list,
			default_country_of_origin="India",
			default_dg_regulation="not_applicable",
			packer_contact_info="Acme Pack, Bengaluru 560001",
			importer_contact_info="Acme Imp, Mumbai 400001",
			manufacturer_contact_info="support@acme.example",
		)
		repo = _make_mock_repo(setting)
		listings_client = MagicMock()
		listings_client.put_listings_item.return_value = {"status": "ACCEPTED", "issues": []}
		listings_client.marketplace_id = "A21TJRUUN4KGV"
		repo.get_listings_items_instance.return_value = listings_client

		outbound = amazon_outbound.AmazonOutboundSync(repo)
		outbound.create_or_update_listing(item.name, price_override=999.0)
		attrs = listings_client.put_listings_item.call_args.kwargs["body"]["attributes"]

		self.assertEqual(attrs["country_of_origin"], [{"value": "IN", "marketplace_id": "A21TJRUUN4KGV"}])
		self.assertEqual(
			attrs["external_product_information"],
			[{"entity": "HSN Code", "value": "85183000", "marketplace_id": "A21TJRUUN4KGV"}],
		)
		self.assertEqual(
			attrs["item_weight"],
			[{"unit": "kilograms", "value": 0.25, "marketplace_id": "A21TJRUUN4KGV"}],
		)
		self.assertEqual(
			attrs["warranty_description"],
			[{"language_tag": "en_IN", "value": "365 days manufacturer warranty", "marketplace_id": "A21TJRUUN4KGV"}],
		)
		self.assertEqual(
			attrs["merchant_suggested_asin"],
			[{"value": "B09TEST123", "marketplace_id": "A21TJRUUN4KGV"}],
		)
		self.assertEqual(
			attrs["supplier_declared_dg_hz_regulation"],
			[{"value": "not_applicable", "marketplace_id": "A21TJRUUN4KGV"}],
		)
		self.assertEqual(
			attrs["packer_contact_information"],
			[{"language_tag": "en_IN", "value": "Acme Pack, Bengaluru 560001", "marketplace_id": "A21TJRUUN4KGV"}],
		)
		self.assertEqual(
			attrs["importer_contact_information"],
			[{"language_tag": "en_IN", "value": "Acme Imp, Mumbai 400001", "marketplace_id": "A21TJRUUN4KGV"}],
		)
		self.assertEqual(
			attrs["rtip_manufacturer_contact_information"],
			[{"value": "support@acme.example"}],
		)
		self.assertEqual(
			attrs["manufacturer"],
			[{"language_tag": "en_IN", "value": "Acme Mfg", "marketplace_id": "A21TJRUUN4KGV"}],
		)

		# Fallback country_of_origin from Settings and ASIN from Ecommerce Item when Item fields are blank;
		# LISTING_OFFER_ONLY omits warranty_description while retaining compliance attributes.
		item_fb = _create_test_item("OB-FB")
		item_fb.country_of_origin = ""
		item_fb.customs_tariff_number = "85183000"
		item_fb.warranty_period = "180"
		item_fb.save(ignore_permissions=True)
		ecom = _create_ecommerce_item_mapping(item_fb.name, sku="SKU-FB-ONLY")
		frappe.db.set_value("Ecommerce Item", ecom.name, "integration_item_code", "B08FALLBK1")

		outbound.create_or_update_listing(item_fb.name, product_type="PRODUCT", price_override=499.0)
		fb_attrs = listings_client.put_listings_item.call_args.kwargs["body"]["attributes"]
		self.assertEqual(fb_attrs["country_of_origin"][0]["value"], "IN")
		self.assertEqual(fb_attrs["merchant_suggested_asin"][0]["value"], "B08FALLBK1")
		self.assertIn("external_product_information", fb_attrs)
		self.assertNotIn("warranty_description", fb_attrs)

	def test_amazon_attributes_json_override_normalization_and_validation(self):
		item = _create_test_item("OB-JSON")
		item.amazon_product_type = "HEADPHONES"
		item.country_of_origin = "India"
		item.amazon_attributes = (
			'{"color": "Matte Black", '
			'"bullet_point": ["ANC 30dB", {"language_tag": "en_IN", "value": "30h battery"}], '
			'"model_number": {"value": "BUDS-PRO-1"}, '
			'"country_of_origin": "VN"}'
		)
		item.save(ignore_permissions=True)

		repo = _make_mock_repo()
		listings_client = MagicMock()
		listings_client.put_listings_item.return_value = {"status": "ACCEPTED", "issues": []}
		listings_client.marketplace_id = "A21TJRUUN4KGV"
		repo.get_listings_items_instance.return_value = listings_client

		outbound = amazon_outbound.AmazonOutboundSync(repo)
		outbound.create_or_update_listing(item.name, price_override=1299.0)
		attrs = listings_client.put_listings_item.call_args.kwargs["body"]["attributes"]

		self.assertEqual(attrs["color"], [{"value": "Matte Black", "marketplace_id": "A21TJRUUN4KGV"}])
		self.assertEqual(
			attrs["bullet_point"],
			[
				{"value": "ANC 30dB", "marketplace_id": "A21TJRUUN4KGV"},
				{"language_tag": "en_IN", "value": "30h battery", "marketplace_id": "A21TJRUUN4KGV"},
			],
		)
		self.assertEqual(attrs["model_number"], [{"value": "BUDS-PRO-1", "marketplace_id": "A21TJRUUN4KGV"}])
		self.assertEqual(attrs["country_of_origin"], [{"value": "VN", "marketplace_id": "A21TJRUUN4KGV"}])

		item.amazon_attributes = "{bad-json"
		item.flags.from_integration = True
		item.save(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError):
			outbound.create_or_update_listing(item.name, price_override=1299.0)

	def test_amazon_product_type_custom_query_and_sync(self):
		from amazon_connector.amazon_connector.doctype.amazon_product_type.amazon_product_type import (
			get_product_types_list,
		)

		if not frappe.db.exists("Amazon Product Type", "TEST_HEADPHONES"):
			frappe.get_doc({
				"doctype": "Amazon Product Type",
				"product_type": "TEST_HEADPHONES",
				"display_name": "Test Wireless Headphones",
				"marketplace_id": "A21TJRUUN4KGV",
			}).insert(ignore_permissions=True)

		results = get_product_types_list("Amazon Product Type", "wireless", "name", 0, 10, {})
		found = [r for r in results if r[0] == "TEST_HEADPHONES"]
		self.assertTrue(found, "Custom query should find TEST_HEADPHONES by display name keyword 'wireless'")
		self.assertIn("Test Wireless Headphones (TEST_HEADPHONES)", found[0][1])

		repo = _make_mock_repo()
		ptd = MagicMock()
		ptd.make_request.return_value = {
			"productTypes": [
				{"name": "SYNC_TEST_PT", "displayName": "Sync Test Category"}
			]
		}
		repo.get_product_type_definitions_instance.return_value = ptd

		with patch("amazon_connector.amazon_ext.repository.CustomAmazonRepository", return_value=repo):
			res = amazon_outbound.sync_amazon_product_types(amz_setting_name="AMZ-MOCK-001")
			self.assertGreaterEqual(res.get("synced", 0), 1)
			self.assertTrue(frappe.db.exists("Amazon Product Type", "SYNC_TEST_PT"))

	def test_build_attributes_template_and_category_whitelisted_api(self):
		repo = _make_mock_repo()
		outbound = amazon_outbound.AmazonOutboundSync(repo)
		mock_defn = {
			"productType": "HEADPHONES",
			"schemaLink": None,
			"raw": {
				"schema": {
					"required": ["brand", "item_name", "color", "model_name"],
					"properties": {
						"color": {
							"type": "array",
							"items": {
								"type": "object",
								"properties": {
									"language_tag": {"type": "string"},
									"value": {"type": "string", "examples": ["Matte Black"]},
								},
							},
						},
						"model_name": {
							"type": "array",
							"items": {
								"type": "object",
								"properties": {
									"language_tag": {"type": "string"},
									"value": {"type": "string", "examples": ["Buds Pro"]},
								},
							},
						},
						"unit_count": {
							"type": "array",
							"items": {
								"type": "object",
								"properties": {"value": {"type": "number"}, "type": {"type": "object"}},
							},
						},
						"item_dimensions": {
							"type": "array",
							"items": {
								"type": "object",
								"properties": {
									"length": {"type": "object"},
									"width": {"type": "object"},
									"height": {"type": "object"},
								},
							},
						},
					},
				}
			},
		}
		with patch.object(outbound, "get_product_type_definition", return_value=mock_defn):
			tmpl = outbound.build_attributes_template("HEADPHONES")
			self.assertIn("color", tmpl)
			self.assertEqual(tmpl["color"], {"language_tag": "en_IN", "value": "Matte Black"})
			self.assertIn("model_name", tmpl)
			self.assertIn("unit_count", tmpl)
			self.assertIn("item_dimensions", tmpl)

		with patch("amazon_connector.amazon_ext.repository.CustomAmazonRepository", return_value=repo):
			with patch.object(amazon_outbound.AmazonOutboundSync, "build_attributes_template", return_value={"color": "Black"}):
				api_res = amazon_outbound.get_category_attributes_template("HEADPHONES", amz_setting_name="AMZ-MOCK-001")
				self.assertEqual(api_res["product_type"], "HEADPHONES")
				self.assertEqual(api_res["template"], {"color": "Black"})
				self.assertIn('"color": "Black"', api_res["json"])

	def test_item_group_amazon_product_type_and_template_custom_fields(self):
		if not frappe.db.exists("Amazon Product Type", "HEADPHONES"):
			frappe.get_doc({
				"doctype": "Amazon Product Type",
				"product_type": "HEADPHONES",
				"display_name": "Headphones",
				"marketplace_id": "A21TJRUUN4KGV",
			}).insert(ignore_permissions=True)

		ig_name = _uid("IG-AMZ")
		ig = frappe.get_doc({
			"doctype": "Item Group",
			"item_group_name": ig_name,
			"parent_item_group": "All Item Groups",
			"amazon_product_type": "HEADPHONES",
			"amazon_attributes_template": '{"color": "Obsidian Black"}',
		}).insert(ignore_permissions=True)

		self.assertEqual(frappe.db.get_value("Item Group", ig.name, "amazon_product_type"), "HEADPHONES")
		self.assertIn("Obsidian Black", frappe.db.get_value("Item Group", ig.name, "amazon_attributes_template"))
		frappe.delete_doc("Item Group", ig.name, force=1)

	def test_variant_item_inherits_product_type_and_maps_variant_attributes(self):
		repo = _make_mock_repo()
		outbound = amazon_outbound.AmazonOutboundSync(repo)

		# 1. Product type inheritance from template
		template_item = frappe._dict(name="ITEM-TMPL", amazon_product_type="HEADPHONES")
		variant_item = frappe._dict(
			name="ITEM-VAR-1",
			variant_of="ITEM-TMPL",
			amazon_product_type=None,
			attributes=[
				frappe._dict(attribute="Colour", attribute_value="Midnight Blue"),
				frappe._dict(attribute="Size", attribute_value="Medium"),
			],
		)
		with patch.object(frappe.db, "get_value", return_value="HEADPHONES"):
			pt = outbound.get_product_type(variant_item)
			self.assertEqual(pt, "HEADPHONES")

		# 2. Variant attributes mapping with aliases (Colour -> color)
		attrs = outbound.build_listing_attributes(
			item=variant_item,
			item_code="ITEM-VAR-1",
			sku="SKU-VAR-1",
			product_type="HEADPHONES",
			stock_qty=10,
			price=1499.0,
			currency="INR",
			marketplace_id="A21TJRUUN4KGV",
			barcodes=[],
		)
		self.assertIn("color", attrs)
		self.assertEqual(attrs["color"][0]["value"], "Midnight Blue")
		self.assertIn("size", attrs)
		self.assertEqual(attrs["size"][0]["value"], "Medium")

		# 3. Metadata extraction with strict fieldtypes & structured widgets
		meta = outbound.build_attributes_metadata(
			"HEADPHONES",
			tmpl={
				"color": {"language_tag": "en_IN", "value": "Blue"},
				"item_type_name": {"language_tag": "en_IN", "value": "Headphones"},
				"batteries_required": False,
				"publication_date": "2024-01-01",
				"bullet_point": [{"language_tag": "en_IN", "value": "ANC"}],
				"item_dimensions": {"length": {"unit": "centimeters", "value": 10.0}},
				"unit_count": {"value": 1, "type": {"language_tag": "en_IN", "value": "count"}},
			},
		)
		field_map = {m["fieldname"]: m for m in meta}
		self.assertEqual(field_map["color"]["fieldtype"], "Data")
		self.assertEqual(field_map["item_type_name"]["fieldtype"], "Data")
		self.assertEqual(field_map["batteries_required"]["fieldtype"], "Check")
		self.assertEqual(field_map["publication_date"]["fieldtype"], "Date")
		self.assertEqual(field_map["bullet_point"]["widget_type"], "bullet_list")
		self.assertEqual(field_map["bullet_point"]["placeholder"], "ANC")
		self.assertEqual(field_map["item_dimensions"]["widget_type"], "dimensions")
		self.assertEqual(field_map["unit_count"]["widget_type"], "unit_count")

	def test_seed_default_item_attributes_is_idempotent(self):
		from amazon_connector.setup_fixtures import DEFAULT_ITEM_ATTRIBUTES, _seed_default_item_attributes

		# Ensure custom existing attribute is never overwritten by seeding
		if not frappe.db.exists("Item Attribute", "Color"):
			frappe.get_doc({
				"doctype": "Item Attribute",
				"attribute_name": "Color",
				"numeric_values": 0,
				"item_attribute_values": [{"attribute_value": "Custom Crimson", "abbr": "CCRM"}],
			}).insert(ignore_permissions=True)

		color_before = [
			(r.attribute_value, r.abbr)
			for r in frappe.get_doc("Item Attribute", "Color").item_attribute_values
		]

		_seed_default_item_attributes()

		for attr_def in DEFAULT_ITEM_ATTRIBUTES:
			self.assertTrue(
				frappe.db.exists("Item Attribute", attr_def["attribute_name"]),
				f"Expected Item Attribute {attr_def['attribute_name']} to be seeded",
			)

		color_after = [
			(r.attribute_value, r.abbr)
			for r in frappe.get_doc("Item Attribute", "Color").item_attribute_values
		]
		self.assertEqual(color_before, color_after)

	def test_attribute_inheritance_hierarchy_item_group_template_variant(self):
		if not frappe.db.exists("Amazon Product Type", "HEADPHONES"):
			frappe.get_doc({
				"doctype": "Amazon Product Type",
				"product_type": "HEADPHONES",
				"display_name": "Headphones",
				"marketplace_id": "A21TJRUUN4KGV",
			}).insert(ignore_permissions=True)

		ig_name = _uid("IG-INH")
		ig = frappe.get_doc({
			"doctype": "Item Group",
			"item_group_name": ig_name,
			"parent_item_group": "All Item Groups",
			"amazon_product_type": "HEADPHONES",
			"amazon_attributes_template": '{"product_tax_code": "A_GEN_NOTAX", "model_name": "IG-Default", "color": "Grey"}',
		}).insert(ignore_permissions=True)

		tmpl_item = _create_test_item("TMPL-INH")
		tmpl_item.item_group = ig.name
		tmpl_item.amazon_product_type = "HEADPHONES"
		tmpl_item.amazon_attributes = '{"model_name": "Buds X1", "color": "Black", "batteries_required": false}'
		tmpl_item.save(ignore_permissions=True)

		# Variant has copied template attributes (including color: Black) plus its own Variant Attributes row
		variant_item = frappe._dict(
			name=_uid("VAR-INH"),
			item_code=_uid("VAR-INH"),
			item_name="Buds X1 Midnight Blue",
			item_group=ig.name,
			variant_of=tmpl_item.name,
			amazon_product_type=None,
			amazon_attributes='{"color": "Black", "warranty_description": "1 Year"}',
			attributes=[
				frappe._dict(attribute="Colour", attribute_value="Midnight Blue"),
				frappe._dict(attribute="Storage Capacity", attribute_value="128 GB"),
				frappe._dict(attribute="Pack Size", attribute_value="2"),
			],
		)

		repo = _make_mock_repo()
		outbound = amazon_outbound.AmazonOutboundSync(repo)
		attrs = outbound.build_listing_attributes(
			item=variant_item,
			item_code=variant_item.item_code,
			sku=variant_item.item_code,
			product_type="HEADPHONES",
			stock_qty=5,
			price=1999.0,
			currency="INR",
			marketplace_id="A21TJRUUN4KGV",
			barcodes=[],
		)

		# Inherited from Item Group
		self.assertEqual(attrs["product_tax_code"][0]["value"], "A_GEN_NOTAX")
		# Inherited from Template (overriding Item Group)
		self.assertEqual(attrs["model_name"][0]["value"], "Buds X1")
		self.assertEqual(attrs["batteries_required"][0]["value"], False)
		# Item-level override
		self.assertEqual(attrs["warranty_description"][0]["value"], "1 Year")
		# Variant Attributes tab overrides copied/template color & maps expanded aliases
		self.assertEqual(attrs["color"][0]["value"], "Midnight Blue")
		self.assertEqual(attrs["memory_storage_capacity"][0]["value"], "128 GB")
		self.assertEqual(attrs["number_of_items"][0]["value"], 2)

		frappe.delete_doc("Item Group", ig.name, force=1)


class TestAmazonFulfillment(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.company, self.warehouse, self.group_wh, self.price_list = _ensure_company_warehouse_setup()
		self.patch_log = patch(
			"amazon_connector.amazon_ext.outbound.create_log",
			return_value=frappe._dict(name="MOCK-LOG"),
		)
		self.mock_create_log = self.patch_log.start()
		self.addCleanup(self.patch_log.stop)

		self.customer = frappe.get_all("Customer", limit=1, pluck="name")[0]

	def _active_setting_dict(self, warehouse=None, enable_sync=1):
		return frappe._dict(
			name=frappe.db.get_value("Amazon SP API Settings", {}, "name") or "AMZ-TEST",
			company=self.company,
			warehouse=warehouse or self.warehouse,
			price_list=self.price_list,
			country="IN",
			use_sandbox=1,
			is_active=1,
			enable_sync=enable_sync,
			enable_listings_sync=1,
		)

	def _create_mfn_sales_order(self, item_code: str, qty: float = 5.0, rate: float = 100.0, order_id: str | None = None):
		so_data = {
			"doctype": "Sales Order",
			"customer": self.customer,
			"company": self.company,
			"delivery_date": add_days(nowdate(), 2),
			"items": [
				{
					"item_code": item_code,
					"qty": qty,
					"rate": rate,
					"warehouse": self.warehouse,
				}
			],
		}
		if frappe.db.has_column("Sales Order", "amazon_order_id"):
			so_data["amazon_order_id"] = order_id or _uid("402-1111111")
		if frappe.db.has_column("Sales Order", "fulfillment_channel"):
			so_data["fulfillment_channel"] = "MFN"
		so = frappe.get_doc(so_data).insert(ignore_permissions=True)
		so.submit()
		return so

	def test_real_delivery_note_sle_pushes_post_sle_qty_minus_reserved(self):
		item = _create_test_item("OB-SLE")
		sku = _uid("SKU-SLE")
		_create_ecommerce_item_mapping(item.name, sku=sku)

		# Stock Receipt: +10 units in active warehouse
		se = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Receipt",
				"company": self.company,
				"items": [
					{
						"item_code": item.name,
						"t_warehouse": self.warehouse,
						"qty": 10,
						"basic_rate": 50,
					}
				],
			}
		).insert(ignore_permissions=True)
		se.submit()

		# Submit a Sales Order for 6 units -> reserved_qty = 6 (actual=10 -> net=4)
		so = self._create_mfn_sales_order(item.name, qty=6.0)

		from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

		dn = make_delivery_note(so.name)
		dn.items[0].qty = 4.0
		dn.flags.ignore_permissions = True
		dn.insert(ignore_permissions=True)

		pushed_qtys = []

		def _capture_sync(amz_setting_name, item_code, warehouse=None):
			pending = getattr(frappe.flags, "_amazon_pending_sle_qty", {}) or {}
			if (amz_setting_name, item_code) in pending:
				pushed_qtys.append(pending[(amz_setting_name, item_code)])

		setting_dict = self._active_setting_dict(warehouse=self.warehouse)
		with (
			patch.object(amazon_fulfillment, "get_active_amazon_settings", return_value=[setting_dict]),
			patch.object(amazon_fulfillment, "get_active_amazon_setting", return_value=setting_dict.name),
			patch.object(amazon_fulfillment, "sync_stock_job", side_effect=_capture_sync),
			patch.object(amazon_fulfillment, "handle_delivery_note_submit"),
		):
			dn.submit()

		# After DN submits 4 against SO of 6: actual = 10 - 4 = 6, reserved = 6 - 4 = 2 => net available = 4 (NEVER stale 10!)
		self.assertTrue(pushed_qtys, "Expected SLE hook to trigger sync_stock_job on Delivery Note submit")
		self.assertEqual(flt(pushed_qtys[-1]), 4.0)

	def test_sle_filters_unconfigured_warehouse(self):
		item = _create_test_item("OB-WHF")
		_create_ecommerce_item_mapping(item.name, sku=_uid("SKU-WHF"))
		other_wh = frappe.db.get_value(
			"Warehouse",
			{"company": self.company, "is_group": 0, "name": ["!=", self.warehouse]},
			"name",
		)
		if not other_wh:
			self.skipTest("Need a second leaf warehouse to test warehouse filtering")

		sle_mock = frappe._dict(item_code=item.name, warehouse=other_wh, actual_qty=5, is_cancelled=0)
		with (
			patch.object(
				amazon_fulfillment,
				"get_active_amazon_settings",
				return_value=[self._active_setting_dict(warehouse=self.warehouse)],
			),
			patch.object(amazon_fulfillment, "sync_stock_job") as mock_sync,
		):
			amazon_fulfillment.handle_stock_ledger_entry(sle_mock)
		mock_sync.assert_not_called()

	def test_persisted_afn_fba_guard_blocks_stock_push_and_shipment_confirm(self):
		item = _create_test_item("OB-AFN")
		_create_ecommerce_item_mapping(item.name, sku=_uid("SKU-AFN"))

		dn_afn = frappe._dict(
			name="DN-AFN-PERSISTED",
			company=self.company,
			amazon_order_id="402-9999999-1111111",
			imported_from_amazon=1,
			fulfillment_channel="AFN",
			items=[frappe._dict(item_code=item.name, qty=1)],
		)
		with (
			patch.object(amazon_fulfillment, "has_column", create=True, return_value=True),
			patch("frappe.db.has_column", return_value=True),
			patch("frappe.db.exists", return_value=False),
			patch.object(amazon_fulfillment, "confirm_delivery_note_shipment") as mock_confirm,
		):
			amazon_fulfillment.handle_delivery_note_submit(dn_afn)
		mock_confirm.assert_not_called()

		sle_afn = frappe._dict(
			item_code=item.name,
			warehouse=self.warehouse,
			actual_qty=-1,
			voucher_type="Delivery Note",
			voucher_no="DN-AFN-PERSISTED",
			is_cancelled=0,
		)
		with (
			patch("frappe.db.has_column", return_value=True),
			patch("frappe.db.exists", side_effect=lambda dt, n: dt == "Delivery Note" and n == "DN-AFN-PERSISTED"),
			patch(
				"frappe.db.get_value",
				side_effect=lambda dt, n, *a, **kw: {"fulfillment_channel": "AFN", "imported_from_amazon": 1}
				if dt == "Delivery Note"
				else 0,
			),
			patch.object(amazon_fulfillment, "sync_stock_job") as mock_sync,
		):
			amazon_fulfillment.handle_stock_ledger_entry(sle_afn)
		mock_sync.assert_not_called()

	def test_blank_tracking_delivery_note_submit_never_calls_sp_api_or_commit_rollback(self):
		item = _create_test_item("OB-NOTRK")
		fake_dn = frappe._dict(
			name="DN-BLANK-TRK",
			company=self.company,
			amazon_order_id="402-8888888-2222222",
			imported_from_amazon=0,
			fulfillment_channel="MFN",
			transporter_name="FedEx",
			lr_no="",
			amazon_tracking_number="",
			items=[frappe._dict(item_code=item.name, qty=1)],
		)
		with (
			patch.object(amazon_fulfillment, "get_active_amazon_setting", return_value="AMZ-MOCK"),
			patch.object(amazon_fulfillment, "confirm_delivery_note_shipment") as mock_confirm,
			patch("frappe.db.rollback") as mock_rollback,
			patch("frappe.db.commit") as mock_commit,
		):
			amazon_fulfillment.handle_delivery_note_submit(fake_dn)

		mock_confirm.assert_not_called()
		mock_rollback.assert_not_called()
		mock_commit.assert_not_called()

		# Direct confirm_delivery_note_shipment with blank tracking must also refuse SP-API call
		with (
			patch.object(amazon_fulfillment, "AmazonRepository") as mock_repo_cls,
		):
			res = amazon_fulfillment.confirm_delivery_note_shipment(
				delivery_note_name="DN-BLANK-TRK",
				amazon_order_id="402-8888888-2222222",
				package_detail={"carrierCode": "FedEx", "trackingNumber": "", "orderItems": [{"orderItemId": "OI1", "quantity": 1}]},
				marketplace_id="A21TJRUUN4KGV",
				amz_setting_name="AMZ-MOCK",
			)
		self.assertEqual(res["status"], "Failed")
		mock_repo_cls.assert_not_called()

	def test_confirm_shipment_handles_204_empty_response_and_429_retry(self):
		mock_orders = MagicMock()
		err_429 = SPAPIError({"errors": [{"code": "QuotaExceeded", "message": "429"}]})
		err_429.error_response = frappe._dict(status_code=429)
		mock_orders.confirm_shipment.side_effect = [err_429, None]

		mock_repo = _make_mock_repo()
		mock_repo.get_orders_instance.return_value = mock_orders

		with (
			patch("frappe.get_doc", return_value=mock_repo.amz_setting),
			patch.object(amazon_fulfillment, "AmazonRepository", return_value=mock_repo),
		):
			res = amazon_fulfillment.confirm_delivery_note_shipment(
				delivery_note_name="DN-204-OK",
				amazon_order_id="402-5555555-3333333",
				package_detail={
					"packageReferenceId": "DN-204-OK",
					"carrierCode": "FedEx",
					"carrierName": "FedEx",
					"trackingNumber": "FDX123456789",
					"shipDate": "2026-11-12T10:00:00Z",
					"orderItems": [{"orderItemId": "OI-1", "quantity": 2}],
				},
				marketplace_id="A21TJRUUN4KGV",
				amz_setting_name="AMZ-MOCK-001",
			)
		self.assertEqual(res["status"], "Confirmed")
		self.assertEqual(mock_orders.confirm_shipment.call_count, 2)

	def test_resolve_order_item_ids_paginates_next_token(self):
		item1 = _create_test_item("OB-PG1")
		item2 = _create_test_item("OB-PG2")
		_create_ecommerce_item_mapping(item1.name, sku="SKU-PAGE-1")
		_create_ecommerce_item_mapping(item2.name, sku="SKU-PAGE-2")

		orders_client = MagicMock()
		orders_client.get_order_items.side_effect = [
			{
				"payload": {
					"OrderItems": [{"OrderItemId": "OI-P1", "SellerSKU": "SKU-PAGE-1", "QuantityOrdered": 1}],
					"NextToken": "NEXT-PAGE-2",
				}
			},
			{
				"payload": {
					"OrderItems": [{"OrderItemId": "OI-P2", "SellerSKU": "SKU-PAGE-2", "QuantityOrdered": 3}],
				}
			},
		]

		resolved = amazon_fulfillment.resolve_order_item_ids(
			orders_client=orders_client,
			amazon_order_id="402-7777777-4444444",
			dn_items=[
				frappe._dict(item_code=item1.name, qty=1),
				frappe._dict(item_code=item2.name, qty=3),
			],
		)
		self.assertEqual(orders_client.get_order_items.call_count, 2)
		self.assertEqual(
			resolved,
			[
				{"orderItemId": "OI-P1", "quantity": 1},
				{"orderItemId": "OI-P2", "quantity": 3},
			],
		)

	def test_create_delivery_note_partially_shipped_updates_draft_and_shipped_idempotent(self):
		item1 = _create_test_item("OB-PS1")
		item2 = _create_test_item("OB-PS2")
		_create_ecommerce_item_mapping(item1.name, sku="SKU-PS-1")
		_create_ecommerce_item_mapping(item2.name, sku="SKU-PS-2")

		order_id = _uid("402-PS")
		so = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"customer": self.customer,
				"company": self.company,
				"delivery_date": add_days(nowdate(), 2),
				"amazon_order_id": order_id,
				"items": [
					{"item_code": item1.name, "qty": 2.0, "rate": 100.0, "warehouse": self.warehouse},
					{"item_code": item2.name, "qty": 1.0, "rate": 200.0, "warehouse": self.warehouse},
				],
			}
		).insert(ignore_permissions=True)
		so.submit()

		repo = _make_mock_repo()
		repo.get_item_code.side_effect = lambda raw: item1.name if raw.get("SellerSKU") == "SKU-PS-1" else item2.name

		# Force negative stock disabled so initial DN stays in Draft (docstatus=0)
		orig_neg = frappe.db.get_single_value("Stock Settings", "allow_negative_stock")
		frappe.db.set_single_value("Stock Settings", "allow_negative_stock", 0)
		try:
			repo.get_raw_order_items.return_value = [
				{"OrderItemId": "OI-PS-1", "SellerSKU": "SKU-PS-1", "QuantityOrdered": 2, "QuantityShipped": 1},
				{"OrderItemId": "OI-PS-2", "SellerSKU": "SKU-PS-2", "QuantityOrdered": 1, "QuantityShipped": 0},
			]
			# Malformed LastUpdateDate should safely fall back to nowdate()
			order_step1 = {
				"AmazonOrderId": order_id,
				"OrderStatus": "PartiallyShipped",
				"LastUpdateDate": "not-a-valid-timestamp",
				"CarrierCode": "Blue Dart",
				"TrackingNumber": "AMZ-TRK-PART-1",
			}
			dn_name = amazon_fulfillment.create_delivery_note_from_amazon_order(repo, order_step1, so.name)
			self.assertTrue(dn_name)
			dn_doc = frappe.get_doc("Delivery Note", dn_name)
			self.assertEqual(dn_doc.docstatus, 0)
			self.assertEqual(len(dn_doc.items), 1)
			self.assertEqual(flt(dn_doc.items[0].qty), 1.0)
			self.assertEqual(dn_doc.transporter_name, "Blue Dart")
			self.assertEqual(dn_doc.lr_no, "AMZ-TRK-PART-1")
			self.assertEqual(dn_doc.get("amazon_carrier"), "Blue Dart")
			self.assertEqual(dn_doc.get("amazon_tracking_number"), "AMZ-TRK-PART-1")

			# Unchanged repeat call must return None (idempotent!)
			repeat = amazon_fulfillment.create_delivery_note_from_amazon_order(repo, order_step1, so.name)
			self.assertIsNone(repeat)

			# Second PartiallyShipped update ships 2nd unit of item1 + 1 unit of item2 -> Draft DN updates cleanly
			repo.get_raw_order_items.return_value = [
				{"OrderItemId": "OI-PS-1", "SellerSKU": "SKU-PS-1", "QuantityOrdered": 2, "QuantityShipped": 2},
				{"OrderItemId": "OI-PS-2", "SellerSKU": "SKU-PS-2", "QuantityOrdered": 1, "QuantityShipped": 1},
			]
			updated_dn = amazon_fulfillment.create_delivery_note_from_amazon_order(
				repo,
				{"AmazonOrderId": order_id, "OrderStatus": "Shipped", "LastUpdateDate": "2026-11-12T08:00:00Z"},
				so.name,
			)
			self.assertEqual(updated_dn, dn_name)
			dn_refreshed = frappe.get_doc("Delivery Note", dn_name)
			qty_by_item = {row.item_code: flt(row.qty) for row in dn_refreshed.items}
			self.assertEqual(qty_by_item, {item1.name: 2.0, item2.name: 1.0})
		finally:
			frappe.db.set_single_value("Stock Settings", "allow_negative_stock", orig_neg or 0)

	def test_reconciler_skips_unchanged_and_afn_only_items(self):
		item_unchanged = _create_test_item("OB-REC-SAME")
		_create_ecommerce_item_mapping(
			item_unchanged.name,
			sku=_uid("SKU-SAME"),
			inventory_synced_on=add_days(now_datetime(), 1),
			item_synced_on=add_days(now_datetime(), 1),
		)
		frappe.get_doc(
			{"doctype": "Bin", "item_code": item_unchanged.name, "warehouse": self.warehouse, "actual_qty": 5}
		).insert(ignore_permissions=True)

		item_afn = _create_test_item("OB-REC-AFN")
		_create_ecommerce_item_mapping(item_afn.name, sku=_uid("SKU-AFN-ONLY"))

		setting_dict = self._active_setting_dict(enable_sync=1)
		mock_outbound = MagicMock()

		with (
			patch.object(amazon_fulfillment, "get_active_amazon_settings", return_value=[setting_dict]),
			patch("frappe.get_doc", return_value=setting_dict),
			patch.object(amazon_fulfillment, "AmazonRepository"),
			patch.object(amazon_fulfillment, "AmazonOutboundSync", return_value=mock_outbound),
			patch.object(amazon_fulfillment, "_get_afn_only_items", return_value={item_afn.name}),
			patch(
				"frappe.get_all",
				side_effect=lambda dt, **kw: [
					frappe._dict(
						name="EI-1",
						erpnext_item_code=item_unchanged.name,
						sku="SKU-SAME",
						inventory_synced_on=add_days(now_datetime(), 1),
						item_synced_on=add_days(now_datetime(), 1),
					),
					frappe._dict(
						name="EI-2",
						erpnext_item_code=item_afn.name,
						sku="SKU-AFN-ONLY",
						inventory_synced_on=None,
						item_synced_on=None,
					),
				]
				if dt == "Ecommerce Item"
				else [],
			),
		):
			amazon_fulfillment.reconcile_amazon_stock_and_prices()

		mock_outbound.update_stock_and_price.assert_not_called()

	def test_item_price_buying_ignored_and_selling_triggered(self):
		item = _create_test_item("OB-PRHOOK")
		_create_ecommerce_item_mapping(item.name, sku=_uid("SKU-PRH"))

		buying_doc = frappe._dict(item_code=item.name, selling=0, buying=1, price_list=self.price_list)
		with (
			patch.object(
				amazon_fulfillment,
				"get_active_amazon_settings",
				return_value=[self._active_setting_dict()],
			),
			patch.object(amazon_fulfillment, "sync_price_job") as mock_sync_price,
		):
			amazon_fulfillment.handle_item_price_change(buying_doc)
			mock_sync_price.assert_not_called()

			selling_doc = frappe._dict(item_code=item.name, selling=1, buying=0, price_list=self.price_list)
			amazon_fulfillment.handle_item_price_change(selling_doc)
			mock_sync_price.assert_called_once()

	def test_map_carrier_exact_and_substring_fallback(self):
		self.assertEqual(amazon_fulfillment.map_carrier("Blue Dart"), ("Blue Dart", "Blue Dart"))
		self.assertEqual(amazon_fulfillment.map_carrier("bluedart express"), ("Blue Dart", "bluedart express"))
		self.assertEqual(amazon_fulfillment.map_carrier("Delhivery Domestic"), ("Delhivery", "Delhivery Domestic"))
		self.assertEqual(amazon_fulfillment.map_carrier("Custom Express"), ("Other", "Custom Express"))
		self.assertEqual(amazon_fulfillment.map_carrier(""), ("Other", "Other"))
		self.assertEqual(amazon_fulfillment.map_carrier(None), ("Other", "Other"))

	def test_handle_delivery_note_cancel_updates_status_and_triggers_stock_sync(self):
		item = _create_test_item("OB-CANCEL")
		_create_ecommerce_item_mapping(item.name, sku=_uid("SKU-CNC"))

		mfn_dn = frappe._dict(
			name="DN-MFN-CNC",
			company=self.company,
			amazon_order_id="402-6666666-0000001",
			imported_from_amazon=0,
			fulfillment_channel="MFN",
			amazon_shipment_status="Confirmed",
			items=[frappe._dict(item_code=item.name, warehouse=self.warehouse, qty=2)],
		)
		with (
			patch.object(amazon_fulfillment, "get_active_amazon_settings", return_value=[self._active_setting_dict()]),
			patch.object(amazon_fulfillment, "_update_dn_shipment_fields") as mock_upd,
			patch.object(amazon_fulfillment, "sync_stock_job") as mock_sync,
			patch("frappe.db.rollback") as mock_rb,
			patch("frappe.db.commit") as mock_cm,
		):
			amazon_fulfillment.handle_delivery_note_cancel(mfn_dn)
			mock_upd.assert_called_once()
			self.assertEqual(mock_upd.call_args.kwargs["status"], "Cancelled")
			mock_sync.assert_called_once()
			mock_rb.assert_not_called()
			mock_cm.assert_not_called()

		fba_dn = frappe._dict(
			name="DN-FBA-CNC",
			company=self.company,
			amazon_order_id="402-6666666-0000002",
			imported_from_amazon=1,
			fulfillment_channel="AFN",
			items=[frappe._dict(item_code=item.name, warehouse=self.warehouse, qty=2)],
		)
		with (
			patch.object(amazon_fulfillment, "get_active_amazon_settings", return_value=[self._active_setting_dict()]),
			patch.object(amazon_fulfillment, "_update_dn_shipment_fields") as mock_upd,
			patch.object(amazon_fulfillment, "sync_stock_job") as mock_sync,
		):
			amazon_fulfillment.handle_delivery_note_cancel(fba_dn)
			mock_upd.assert_called_once()
			self.assertEqual(mock_upd.call_args.kwargs["status"], "Skipped")
			mock_sync.assert_not_called()

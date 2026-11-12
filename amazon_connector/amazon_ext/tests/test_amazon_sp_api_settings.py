# Copyright (c) 2022, Frappe and Contributors
# See license.txt


import gzip
import json
import os
import time
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import patch

import frappe
import responses
from frappe.exceptions import ValidationError
from frappe.tests import IntegrationTestCase, UnitTestCase
from requests import Request, request
from requests.exceptions import HTTPError

from amazon_connector.amazon_ext.fulfillment import (
	confirm_delivery_note_shipment,
	format_iso_datetime,
	handle_delivery_note_submit,
	handle_item_price_change,
	handle_stock_ledger_entry,
	is_item_mapped_to_amazon,
	map_carrier,
)
from amazon_connector.amazon_ext.lifecycle import (
	handle_sales_order_cancel,
	sync_returns,
	sync_settlements,
)
from amazon_connector.amazon_ext.listings import (
	SANDBOX_REPORT_DOCUMENT_ID,
	AmazonListingSync,
	normalize_catalog_v2_item,
	parse_listings_tsv,
	resolve_safe_catalog_image_url,
	upsert_item_price,
)
from amazon_connector.amazon_ext.log_patch import _retry_job, create_log
from amazon_connector.amazon_ext.outbound import (
	AmazonOutboundSync,
)
from amazon_connector.amazon_ext.repository import (
	CustomAmazonRepository as AmazonRepository,
	resolve_custom_endpoint,
	validate_amazon_sp_api_credentials,
)
from amazon_connector.amazon_ext.sp_api import (
	SPAPI,
	AWSSigV4,
	CatalogItems,
	CatalogItemsV2,
	Finances,
	ListingsItems,
	Orders,
	ProductTypeDefinitions,
	Reports,
	SPAPIError,
	Util,
)
from amazon_connector.overrides.amazon_sp_api_settings import (
	sync_amazon_order,
)

file_path = os.path.join(os.path.dirname(__file__), "test_data.json")
with open(file_path) as json_file:
	try:
		DATA = json.load(json_file)
	except json.decoder.JSONDecodeError as e:
		frappe.throw(e)


class TestSPAPI(SPAPI):
	# Expected response after hitting the URL.
	expected_response: ClassVar = {}

	@responses.activate
	def make_request(
		self,
		method: str = "GET",
		append_to_base_uri: str = "",
		params: dict | None = None,
		data: dict | None = None,
		json_data: dict | None = None,
	) -> object:
		if isinstance(params, dict):
			params = Util.remove_empty(params)
		if isinstance(data, dict):
			data = Util.remove_empty(data)
		if isinstance(json_data, dict):
			json_data = Util.remove_empty(json_data)

		if method == "GET":
			responses_method = responses.GET
		elif method == "POST":
			responses_method = responses.POST
		elif method == "PUT":
			responses_method = responses.PUT
		elif method == "PATCH":
			responses_method = responses.PATCH
		elif method == "DELETE":
			responses_method = responses.DELETE
		else:
			raise HTTPError("Method not supported!")

		url = self.endpoint + self.BASE_URI + append_to_base_uri

		responses.add(
			responses_method,
			url,
			status=self.expected_response.get("status", 200),
			json=self.expected_response.get("json", {}),
		)

		try:
			response = request(
				method=method,
				url=url,
				params=params,
				data=data,
				json=json_data,
			)
			return response.json() if response.content else {}
		except HTTPError as e:
			error = SPAPIError(str(e))
			error.response = e.response
			raise error


class TestFinances(Finances, TestSPAPI):
	def list_financial_events_by_order_id(
		self, order_id: str, max_results: int | None = None, next_token: str | None = None
	) -> object:
		self.expected_response = DATA.get("list_financial_events_by_order_id_200")
		return super().list_financial_events_by_order_id(order_id, max_results, next_token)


class TestOrders(Orders, TestSPAPI):
	def get_orders(
		self,
		created_after: str | None = None,
		created_before: str | None = None,
		last_updated_after: str | None = None,
		last_updated_before: str | None = None,
		order_statuses: list | None = None,
		marketplace_ids: list | None = None,
		fulfillment_channels: list | None = None,
		payment_methods: list | None = None,
		buyer_email: str | None = None,
		seller_order_id: str | None = None,
		max_results: int = 100,
		easyship_shipment_statuses: list | None = None,
		next_token: str | None = None,
		amazon_order_ids: list | None = None,
		actual_fulfillment_supply_source_id: str | None = None,
		is_ispu: bool | None = None,
		store_chain_store_id: str | None = None,
	) -> object:
		self.expected_response = DATA.get("get_orders_200")
		return super().get_orders(
			created_after=created_after,
			created_before=created_before,
			last_updated_after=last_updated_after,
			last_updated_before=last_updated_before,
			order_statuses=order_statuses,
			marketplace_ids=marketplace_ids,
			fulfillment_channels=fulfillment_channels,
			payment_methods=payment_methods,
			buyer_email=buyer_email,
			seller_order_id=seller_order_id,
			max_results=max_results,
			easyship_shipment_statuses=easyship_shipment_statuses,
			next_token=next_token,
			amazon_order_ids=amazon_order_ids,
			actual_fulfillment_supply_source_id=actual_fulfillment_supply_source_id,
			is_ispu=is_ispu,
			store_chain_store_id=store_chain_store_id,
		)

	def get_order_items(self, order_id: str, next_token: str | None = None) -> object:
		self.expected_response = DATA.get("get_order_items_200")
		return super().get_order_items(order_id, next_token)

	def confirm_shipment(
		self,
		order_id: str,
		marketplace_id: str | None = None,
		package_detail: dict | None = None,
	) -> object:
		self.last_confirm_order_id = order_id
		self.last_confirm_marketplace_id = marketplace_id
		self.last_confirm_package_detail = package_detail
		self.expected_response = getattr(
			self, "confirm_shipment_response", {"status": 200, "json": {}}
		)
		return super().confirm_shipment(order_id, marketplace_id, package_detail)


class TestCatalogItems(CatalogItems, TestSPAPI):
	def get_catalog_item(
		self,
		asin: str,
		marketplace_id: str | None = None,
	) -> object:
		self.expected_response = DATA.get("get_catalog_item_200")
		return super().get_catalog_item(asin, marketplace_id)


class TestCatalogItemsV2(CatalogItemsV2, TestSPAPI):
	mock_items: ClassVar = {}

	def get_catalog_item(
		self,
		asin: str,
		marketplace_ids: list | None = None,
		included_data: list | None = None,
	) -> object:
		if asin in self.mock_items:
			self.expected_response = {"status": 200, "json": self.mock_items[asin]}
			return super().get_catalog_item(asin, marketplace_ids, included_data)
		raise SPAPIError(error="InvalidInput", error_description="Mocked catalog miss")


class TestReports(Reports, TestSPAPI):
	report_status_override: str | None = None
	report_tsv: ClassVar[bytes | str] = ""

	def create_report(
		self,
		report_type: str,
		marketplace_ids: list | None = None,
		data_start_time: str | None = None,
		data_end_time: str | None = None,
		report_options: dict | None = None,
	) -> dict:
		self.expected_response = DATA.get("create_report_200")
		return super().create_report(
			report_type, marketplace_ids, data_start_time, data_end_time, report_options
		)

	def get_report(self, report_id: str) -> dict:
		report_resp = json.loads(json.dumps(DATA.get("get_report_200")))
		if self.report_status_override == "IN_PROGRESS":
			report_resp["json"]["processingStatus"] = "IN_PROGRESS"
			report_resp["json"].pop("reportDocumentId", None)
		self.expected_response = report_resp
		return super().get_report(report_id)

	def get_report_document(self, report_document_id: str) -> dict:
		self.last_report_document_id = report_document_id
		self.expected_response = DATA.get("get_report_document_200")
		return super().get_report_document(report_document_id)

	@responses.activate
	def download_report_document(self, report_document: dict) -> str:
		url = report_document["url"]
		body = self.report_tsv
		if isinstance(body, str):
			body = body.encode("utf-8")
		responses.add(responses.GET, url, status=200, body=body)
		return super().download_report_document(report_document)


class TestListingsItems(ListingsItems, TestSPAPI):
	put_response: ClassVar = {
		"status": 200,
		"json": {"status": "ACCEPTED", "submissionId": "sub-test-put-1"},
	}
	patch_response: ClassVar = {
		"status": 200,
		"json": {"status": "ACCEPTED", "submissionId": "sub-test-patch-1"},
	}
	get_response: ClassVar = {
		"status": 200,
		"json": {"sku": "TEST-SKU", "status": "BUYABLE"},
	}
	delete_response: ClassVar = {
		"status": 200,
		"json": {"status": "ACCEPTED", "submissionId": "sub-test-del-1"},
	}

	def put_listings_item(
		self,
		seller_id: str,
		sku: str,
		marketplace_ids: list | str | None = None,
		body: dict | None = None,
		issue_locale: str | None = None,
	) -> dict:
		self.last_put_seller_id = seller_id
		self.last_put_sku = sku
		self.last_put_body = body
		self.expected_response = getattr(self, "put_response_override", self.put_response)
		return super().put_listings_item(seller_id, sku, marketplace_ids, body, issue_locale)

	def patch_listings_item(
		self,
		seller_id: str,
		sku: str,
		marketplace_ids: list | str | None = None,
		body: dict | None = None,
		issue_locale: str | None = None,
	) -> dict:
		self.last_patch_seller_id = seller_id
		self.last_patch_sku = sku
		self.last_patch_body = body
		self.expected_response = getattr(self, "patch_response_override", self.patch_response)
		return super().patch_listings_item(seller_id, sku, marketplace_ids, body, issue_locale)

	def get_listings_item(
		self,
		seller_id: str,
		sku: str,
		marketplace_ids: list | str | None = None,
		issue_locale: str | None = None,
		included_data: list | str | None = None,
	) -> dict:
		self.expected_response = self.get_response
		return super().get_listings_item(seller_id, sku, marketplace_ids, issue_locale, included_data)

	def delete_listings_item(
		self,
		seller_id: str,
		sku: str,
		marketplace_ids: list | str | None = None,
		issue_locale: str | None = None,
	) -> dict:
		self.expected_response = self.delete_response
		return super().delete_listings_item(seller_id, sku, marketplace_ids, issue_locale)


class TestProductTypeDefinitions(ProductTypeDefinitions, TestSPAPI):
	def get_definitions_product_type(
		self,
		product_type: str,
		marketplace_ids: list | str | None = None,
		seller_id: str | None = None,
		product_type_version: str = "LATEST",
		requirements: str = "LISTING",
	) -> dict:
		self.expected_response = {
			"status": 200,
			"json": {
				"productType": product_type,
				"productTypeVersion": {"version": product_type_version, "latest": True},
			},
		}
		return super().get_definitions_product_type(
			product_type, marketplace_ids, seller_id, product_type_version, requirements
		)


class TestAmazonSettings:
	def __init__(self) -> None:
		def get_company():
			company_name = frappe.db.get_value(
				"Company",
				{"company_name": "Amazon Test Company", "country": "India", "default_currency": "INR"},
				"company_name",
			)

			if not company_name:
				company = frappe.get_doc(
					{
						"doctype": "Company",
						"company_name": "Amazon Test Company",
						"abbr": "ATC",
						"country": "India",
						"default_currency": "INR",
					}
				)
				company.insert(ignore_permissions=True)
				company_name = company.company_name

			return company_name

		def get_warehouse():
			warehouse_name = frappe.db.get_value(
				"Warehouse",
				{
					"warehouse_name": "Amazon Test Warehouse",
				},
				"warehouse_name",
			)

			if not warehouse_name:
				warehouse = frappe.get_doc(
					{
						"doctype": "Warehouse",
						"warehouse_name": "Amazon Test Warehouse",
						"company": "Amazon Test Company",
					}
				)
				warehouse.insert(ignore_permissions=True)
				warehouse_name = warehouse.warehouse_name

			return warehouse_name + " - ATC"

		def get_item_group():
			item_group_name = frappe.db.get_value(
				"Item Group",
				{
					"item_group_name": "Amazon Test Warehouse",
				},
				"item_group_name",
			)

			if not item_group_name:
				item_group = frappe.get_doc(
					{
						"doctype": "Item Group",
						"item_group_name": "Amazon Test Warehouse",
					}
				)
				item_group.insert(ignore_permissions=True)
				item_group_name = item_group.item_group_name

			return item_group_name

		self.name = None
		self.is_active = 1
		self.iam_arn = "********************"
		self.refresh_token = "********************"
		self.client_id = "********************"
		self.client_secret = "********************"
		self.aws_access_key = "********************"
		self.aws_secret_key = "********************"
		self.country = "US"
		self.company = get_company()
		self.warehouse = get_warehouse()
		self.parent_item_group = get_item_group()
		self.price_list = "Standard Selling"
		self.customer_group = "Individual"
		self.territory = "All Territories"
		self.customer_type = "Individual"
		self.market_place_account_group = "Accounts Receivable - ATC"
		self.after_date = "2000-07-23"
		self.taxes_charges = 1
		self.enable_sync = 1
		self.max_retry_limit = 3
		self.create_item_if_not_exists = 1
		self.use_sandbox = 0
		self.amazon_fields_map = [
			frappe._dict({"amazon_field": "ASIN", "item_field": "item_code", "use_to_find_item_code": 1})
		]

	def get(self, key, default=None):
		return getattr(self, key, default)


class TestAmazonRepository(AmazonRepository):
	def __init__(self, use_sandbox: int = 0, custom_endpoint: str | None = None) -> None:
		self.amz_setting = TestAmazonSettings()
		self.amz_setting.use_sandbox = use_sandbox
		self.amz_setting.custom_endpoint = custom_endpoint
		self.instance_params = dict(
			iam_arn=self.amz_setting.iam_arn,
			client_id=self.amz_setting.client_id,
			client_secret=self.amz_setting.client_secret,
			refresh_token=self.amz_setting.refresh_token,
			aws_access_key=self.amz_setting.aws_access_key,
			aws_secret_key=self.amz_setting.aws_secret_key,
			country_code=self.amz_setting.country,
			use_sandbox=bool(self.amz_setting.use_sandbox),
			custom_endpoint=custom_endpoint,
		)

	def call_sp_api_method(self, sp_api_method, **kwargs):
		max_retries = self.amz_setting.max_retry_limit

		for _x in range(max_retries):
			try:
				result = sp_api_method(**kwargs)
				if isinstance(result, dict) and "payload" in result:
					return result.get("payload")
				return result
			except Exception:
				time.sleep(0.01)
				continue

	def get_finances_instance(self):
		return TestFinances(**self.instance_params)

	def get_orders_instance(self):
		return TestOrders(**self.instance_params)

	def get_catalog_items_instance(self):
		return TestCatalogItems(**self.instance_params)

	def get_catalog_items_v2_instance(self):
		return TestCatalogItemsV2(**self.instance_params)

	def get_reports_instance(self):
		return getattr(self, "_reports_instance", None) or TestReports(**self.instance_params)

	def get_listings_items_instance(self):
		return getattr(self, "_listings_items_instance", None) or TestListingsItems(
			**self.instance_params
		)

	def get_product_type_definitions_instance(self):
		return getattr(self, "_product_type_definitions_instance", None) or TestProductTypeDefinitions(
			**self.instance_params
		)


class TestAmazonUnit(UnitTestCase):
	def test_sandbox_marketplace_endpoint(self):
		region, endpoint, marketplace_id = Util.get_marketplace_data("IN", use_sandbox=True)
		self.assertEqual(region, "eu-west-1")
		self.assertEqual(endpoint, "https://sandbox.sellingpartnerapi-eu.amazon.com")
		self.assertEqual(marketplace_id, "A21TJRUUN4KGV")

	def test_parse_listings_tsv_handles_quotes_and_commas(self):
		tsv = (
			"seller-sku\tasin1\titem-name\titem-description\tlisting-id\tprice\tquantity\n"
			'ARRIS,SB6141\tB00AJHDZSI\t24" Monitor Case, Black\t\t1202QA98BWP\t99.99\t2\n'
			"vb554a-bl1915\tB00H3LHY6C\t\t\t0829N45OKCE\t247.05\t3\n"
			"\t\t\t\t\t\t\n"
		)
		listings = parse_listings_tsv(tsv)
		self.assertEqual(len(listings), 2)
		self.assertEqual(listings[0]["SellerSKU"], "ARRIS,SB6141")
		self.assertEqual(listings[0]["Title"], '24" Monitor Case, Black')
		self.assertEqual(listings[0]["Price"], 99.99)
		self.assertEqual(listings[1]["ASIN"], "B00H3LHY6C")
		self.assertEqual(listings[1]["Title"], "")

	def test_validate_after_date_accepts_date_and_str(self):
		from types import SimpleNamespace
		from frappe.utils import add_days, getdate, today
		from amazon_connector.overrides.amazon_sp_api_settings import (
			CustomAmazonSPAPISettings as AmazonSPAPISettings,
		)

		AmazonSPAPISettings.validate_after_date(SimpleNamespace(after_date=getdate(today())))
		AmazonSPAPISettings.validate_after_date(SimpleNamespace(after_date=str(today())))
		with self.assertRaises(frappe.ValidationError):
			AmazonSPAPISettings.validate_after_date(
				SimpleNamespace(after_date=getdate(add_days(today(), -31)))
			)

	def test_listings_items_api_methods(self):
		repo = TestAmazonRepository()
		client = repo.get_listings_items_instance()

		# PUT listings item
		put_payload = {
			"productType": "PRODUCT",
			"requirements": "LISTING",
			"attributes": {"item_name": [{"value": "Test Unit Item"}]},
		}
		res_put = client.put_listings_item(
			seller_id="TEST_SELLER", sku="TEST_SKU_001", body=put_payload
		)
		self.assertEqual(res_put.get("status"), "ACCEPTED")
		self.assertEqual(client.last_put_seller_id, "TEST_SELLER")
		self.assertEqual(client.last_put_sku, "TEST_SKU_001")
		self.assertEqual(client.last_put_body.get("requirements"), "LISTING")

		# PATCH listings item
		patch_payload = {
			"productType": "PRODUCT",
			"patches": [{"op": "replace", "path": "/attributes/purchasable_offer", "value": []}],
		}
		res_patch = client.patch_listings_item(
			seller_id="TEST_SELLER", sku="TEST_SKU_001", body=patch_payload
		)
		self.assertEqual(res_patch.get("status"), "ACCEPTED")
		self.assertEqual(client.last_patch_sku, "TEST_SKU_001")
		self.assertEqual(len(client.last_patch_body.get("patches")), 1)

		# GET listings item
		res_get = client.get_listings_item(seller_id="TEST_SELLER", sku="TEST_SKU_001")
		self.assertEqual(res_get.get("sku"), "TEST-SKU")

		# DELETE listings item
		res_del = client.delete_listings_item(seller_id="TEST_SELLER", sku="TEST_SKU_001")
		self.assertEqual(res_del.get("status"), "ACCEPTED")

	def test_product_type_definitions_api(self):
		repo = TestAmazonRepository()
		client = repo.get_product_type_definitions_instance()
		res = client.get_definitions_product_type("LUGGAGE", marketplace_ids=["ATVPDKIKX0DER"])
		self.assertEqual(res.get("productType"), "LUGGAGE")

	def test_orders_confirm_shipment_api(self):
		repo = TestAmazonRepository()
		client = repo.get_orders_instance()
		package_detail = {
			"packageReferenceId": "1",
			"carrierCode": "FedEx",
			"trackingNumber": "TRK12345",
			"shipDate": "2026-11-11T12:00:00Z",
			"orderItems": [{"orderItemId": "ITEM001", "quantity": 1}],
		}
		res = client.confirm_shipment(
			order_id="123-4567890-1234567",
			marketplace_id="ATVPDKIKX0DER",
			package_detail=package_detail,
		)
		self.assertEqual(client.last_confirm_order_id, "123-4567890-1234567")
		self.assertEqual(client.last_confirm_marketplace_id, "ATVPDKIKX0DER")
		self.assertEqual(client.last_confirm_package_detail["carrierCode"], "FedEx")

	def test_carrier_mapping_and_datetime(self):
		self.assertEqual(map_carrier("Blue Dart"), ("Blue Dart", "Blue Dart"))
		self.assertEqual(map_carrier("bluedart express"), ("Blue Dart", "bluedart express"))
		self.assertEqual(map_carrier("FedEx"), ("FedEx", "FedEx"))
		self.assertEqual(map_carrier("Delhivery Domestic"), ("Delhivery", "Delhivery Domestic"))
		self.assertEqual(map_carrier("DHL"), ("DHL", "DHL"))
		self.assertEqual(map_carrier("Custom Express Logistics"), ("Other", "Custom Express Logistics"))
		self.assertEqual(map_carrier(""), ("Other", "Other"))
		self.assertEqual(map_carrier(None), ("Other", "Other"))

		iso_str = format_iso_datetime("2026-11-11")
		self.assertTrue(iso_str.startswith("2026-11-11"))
		self.assertTrue(iso_str.endswith("Z"))

	def test_outbound_identifier_builder(self):
		repo = TestAmazonRepository()
		outbound = AmazonOutboundSync(repo)
		# EAN 13
		ean_attr = outbound.build_identifier_attribute(
			[{"barcode": "8901234567890", "barcode_type": "EAN"}], "ATVPDKIKX0DER"
		)
		self.assertEqual(len(ean_attr), 1)
		self.assertEqual(ean_attr[0]["type"], "ean")
		self.assertEqual(ean_attr[0]["value"], "8901234567890")

		# UPC 12
		upc_attr = outbound.build_identifier_attribute(
			[{"barcode": "012345678901", "barcode_type": "UPC"}], "ATVPDKIKX0DER"
		)
		self.assertEqual(len(upc_attr), 1)
		self.assertEqual(upc_attr[0]["type"], "upc")

		# Empty
		empty_attr = outbound.build_identifier_attribute([], "ATVPDKIKX0DER")
		self.assertEqual(empty_attr, [])

	@responses.activate
	def test_custom_endpoint_spapi_routing_and_sts_bypass(self):
		api = SPAPI(
			iam_arn="arn:aws:iam::123:role/mock",
			client_id="mock-client",
			client_secret="mock-secret",
			refresh_token="mock-refresh",
			aws_access_key="",
			aws_secret_key="",
			country_code="IN",
			use_sandbox=True,
			custom_endpoint="http://127.0.0.1:9099/",
		)
		self.assertEqual(api.custom_endpoint, "http://127.0.0.1:9099")
		self.assertEqual(api.endpoint, "http://127.0.0.1:9099")
		self.assertEqual(api.region, "eu-west-1")
		self.assertEqual(api.marketplace_id, "A21TJRUUN4KGV")

		responses.add(
			responses.POST,
			"http://127.0.0.1:9099/auth/o2/token",
			status=200,
			json={"access_token": "Atza|mock-local-token", "token_type": "bearer"},
		)
		token = api.get_access_token()
		self.assertEqual(token, "Atza|mock-local-token")
		self.assertEqual(len(responses.calls), 1)
		self.assertEqual(responses.calls[0].request.url, "http://127.0.0.1:9099/auth/o2/token")

		with patch(
			"amazon_connector.amazon_ext.sp_api.boto3.client"
		) as mock_boto:
			auth = api.get_auth()
			self.assertFalse(mock_boto.called)
			self.assertEqual(auth.aws_access_key_id, "mock-access-key")
			self.assertEqual(auth.aws_secret_access_key, "mock-secret-key")

		# Standard endpoint when custom_endpoint is None
		prod_api = SPAPI(
			iam_arn="arn:aws:iam::123:role/mock",
			client_id="mock-client",
			client_secret="mock-secret",
			refresh_token="mock-refresh",
			aws_access_key="ak",
			aws_secret_key="sk",
			country_code="IN",
			use_sandbox=False,
			custom_endpoint=None,
		)
		self.assertIsNone(prod_api.custom_endpoint)
		self.assertEqual(prod_api.endpoint, "https://sellingpartnerapi-eu.amazon.com")

	def test_resolve_custom_endpoint(self):
		self.assertEqual(
			resolve_custom_endpoint({"custom_endpoint": " http://127.0.0.1:9099 "}),
			"http://127.0.0.1:9099",
		)
		with patch.object(frappe, "conf", {"amazon_sp_api_endpoint": "http://127.0.0.1:9098"}):
			self.assertEqual(resolve_custom_endpoint({}), "http://127.0.0.1:9098")
			self.assertEqual(
				resolve_custom_endpoint({"custom_endpoint": "http://127.0.0.1:9099"}),
				"http://127.0.0.1:9099",
			)

	def test_dynamic_report_flow_with_custom_endpoint(self):
		repo = TestAmazonRepository(use_sandbox=1, custom_endpoint="http://127.0.0.1:9099")
		reports = TestReports(**repo.instance_params)
		reports.report_tsv = "seller-sku\tasin1\titem-name\titem-description\tlisting-id\tprice\tquantity\nSKU1\tASIN1\tTest\t\tL1\t10.0\t2\n"
		repo._reports_instance = reports

		syncer = AmazonListingSync(repo)
		with patch.object(
			syncer, "_wait_for_report_document_id", return_value="dynamic-mock-doc-id-123"
		) as mock_wait:
			text = syncer._fetch_report_text()
			self.assertTrue(mock_wait.called)
			self.assertEqual(reports.last_report_document_id, "dynamic-mock-doc-id-123")
			self.assertNotEqual(reports.last_report_document_id, SANDBOX_REPORT_DOCUMENT_ID)
			self.assertIn("SKU1", text)

		# Without custom_endpoint, use_sandbox=1 skips _wait_for_report_document_id
		repo_sbx = TestAmazonRepository(use_sandbox=1, custom_endpoint=None)
		reports_sbx = TestReports(**repo_sbx.instance_params)
		reports_sbx.report_status_override = "IN_PROGRESS"
		reports_sbx.report_tsv = reports.report_tsv
		repo_sbx._reports_instance = reports_sbx

		syncer_sbx = AmazonListingSync(repo_sbx)
		with patch.object(syncer_sbx, "_wait_for_report_document_id") as mock_wait_sbx:
			syncer_sbx._fetch_report_text()
			self.assertFalse(mock_wait_sbx.called)
			self.assertEqual(reports_sbx.last_report_document_id, SANDBOX_REPORT_DOCUMENT_ID)

	def test_awssigv4_preserves_non_default_port_and_ipv6(self):
		auth = AWSSigV4(
			service="execute-api",
			aws_access_key_id="AKIA-TEST",
			aws_secret_access_key="SECRET-TEST",
			region="eu-west-1",
		)
		cases = [
			("http://127.0.0.1:9099/reports/2021-06-30/documents/DOC1", "127.0.0.1:9099"),
			("http://[::1]:9099/reports/2021-06-30/documents/DOC1", "[::1]:9099"),
			("http://127.0.0.1:80/reports/2021-06-30/documents/DOC1", "127.0.0.1"),
			("https://sellingpartnerapi-eu.amazon.com:443/reports/2021-06-30/reports", "sellingpartnerapi-eu.amazon.com"),
		]
		for url, expected_host in cases:
			prepared = Request("GET", url).prepare()
			auth(prepared)
			self.assertEqual(prepared.headers["Host"], expected_host)
			self.assertIn("host", prepared.headers["Authorization"])

	def test_util_remove_empty_preserves_falsy_numbers_and_booleans(self):
		cleaned = Util.remove_empty(
			{
				"qty": 0,
				"zero_float": 0.0,
				"is_ispu": False,
				"empty_str": "",
				"none_val": None,
				"empty_list": [],
				"empty_dict": {},
				"keep": "value",
			}
		)
		self.assertEqual(cleaned, {"qty": 0, "zero_float": 0.0, "is_ispu": False, "keep": "value"})

	@responses.activate
	def test_spapi_204_no_content_returns_empty_dict(self):
		api = SPAPI(
			iam_arn="arn:aws:iam::123:role/mock",
			client_id="mock-client",
			client_secret="mock-secret",
			refresh_token="mock-refresh",
			aws_access_key="ak",
			aws_secret_key="sk",
			country_code="IN",
			use_sandbox=True,
			custom_endpoint="http://127.0.0.1:9099",
		)
		responses.add(
			responses.POST,
			"http://127.0.0.1:9099/auth/o2/token",
			status=200,
			json={"access_token": "Atza|mock-token", "token_type": "bearer"},
		)
		responses.add(
			responses.POST,
			"http://127.0.0.1:9099/orders/v0/orders/123-456/shipmentConfirmation",
			status=204,
			body="",
		)
		orders = Orders(
			iam_arn="arn:aws:iam::123:role/mock",
			client_id="mock-client",
			client_secret="mock-secret",
			refresh_token="mock-refresh",
			aws_access_key="ak",
			aws_secret_key="sk",
			country_code="IN",
			use_sandbox=True,
			custom_endpoint="http://127.0.0.1:9099",
		)
		res = orders.confirm_shipment(
			order_id="123-456",
			marketplace_id="A21TJRUUN4KGV",
			package_detail={
				"packageReferenceId": "1",
				"carrierCode": "Blue Dart",
				"trackingNumber": "TRK-204",
				"shipDate": "2026-11-12T10:00:00Z",
				"orderItems": [{"orderItemId": "OI-1", "quantity": 1}],
			},
		)
		self.assertEqual(res, {})
		self.assertIsNotNone(api)

	def test_normalize_catalog_v2_extracts_list_price_and_product_type(self):
		payload = {
			"asin": "B0V2LP001",
			"summaries": [
				{
					"itemName": "V2 Headphones",
					"brand": "Sony",
					"manufacturer": "Sony Corp",
					"websiteDisplayGroupName": "Electronics",
				}
			],
			"productTypes": [{"productType": "HEADPHONES"}],
			"attributes": {"list_price": [{"value": 2499.0, "currency": "INR"}]},
		}
		normalized = normalize_catalog_v2_item(payload)
		self.assertEqual(normalized["list_price"], 2499.0)
		self.assertEqual(normalized["product_type"], "HEADPHONES")
		self.assertEqual(normalized["brand"], "Sony")

	def test_amazon_fields_map_allows_shared_field_names(self):
		meta = frappe.get_meta("Amazon Fields Map")
		self.assertFalse(meta.get_field("amazon_field").unique)
		self.assertFalse(meta.get_field("item_field").unique)


class TestAmazon(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		for code in ("B07N4M94X4", "B001SBX001", "AMZ-OUT-ITEM-1"):
			frappe.db.delete("Item", {"name": code})
			frappe.db.delete("Ecommerce Item", {"erpnext_item_code": code})
			frappe.db.delete("Item Price", {"item_code": code})
			frappe.db.delete("Item Barcode", {"parent": code})

	def tearDown(self):
		for code in ("B07N4M94X4", "B001SBX001", "AMZ-OUT-ITEM-1"):
			frappe.db.delete("Item", {"name": code})
			frappe.db.delete("Ecommerce Item", {"erpnext_item_code": code})
			frappe.db.delete("Item Price", {"item_code": code})
			frappe.db.delete("Item Barcode", {"parent": code})

	def test_get_orders(self):
		amazon_repository = TestAmazonRepository()
		sales_orders = amazon_repository.get_orders("2000-07-23")
		self.assertEqual(len(sales_orders), 2)

	def test_validate_credentials(self):
		credentials = dict(
			iam_arn="********************",
			client_id="********************",
			client_secret="********************",
			refresh_token="********************",
			aws_access_key="********************",
			aws_secret_key="********************",
			country="US",
		)

		self.assertRaises(ValidationError, validate_amazon_sp_api_credentials, **credentials)

	def test_get_listings_production_flow_gzip_and_idempotency(self):
		tsv = (
			"seller-sku\tasin1\titem-name\titem-description\tlisting-id\tprice\tquantity\n"
			"SKU-TV-1\tB07N4M94X4\t\t\tL1\t1499.50\t4\n"
		)
		repo = TestAmazonRepository(use_sandbox=0)
		reports = TestReports(**repo.instance_params)
		reports.report_tsv = gzip.compress(tsv.encode("utf-8"))
		repo._reports_instance = reports

		TestCatalogItemsV2.mock_items = {
			"B07N4M94X4": {
				"asin": "B07N4M94X4",
				"summaries": [
					{
						"itemName": "Samsung QLED 4K Smart TV",
						"brand": "SAMSUNG",
						"manufacturer": "Samsung",
						"websiteDisplayGroupName": "Home Theater",
					}
				],
				"images": [{"images": [{"variant": "MAIN", "link": "https://example.com/tv.jpg"}]}],
			}
		}

		try:
			with patch(
				"amazon_connector.amazon_ext.listings.create_log"
			) as mock_log:
				summary = repo.get_listings()
			self.assertEqual(mock_log.call_args.kwargs["status"], "Success")
			self.assertEqual(summary["created"], 1)
			self.assertEqual(summary["updated"], 0)
			self.assertEqual(summary["failed"], 0)

			item = frappe.get_doc("Item", "B07N4M94X4")
			self.assertEqual(item.item_name, "Samsung QLED 4K Smart TV")
			self.assertEqual(item.brand, "SAMSUNG")
			self.assertEqual(item.default_item_manufacturer, "Samsung")
			self.assertEqual(item.item_group, "Home Theater")
			self.assertEqual(item.image, "https://example.com/tv.jpg")

			price = frappe.db.get_value(
				"Item Price",
				{"item_code": "B07N4M94X4", "price_list": "Standard Selling"},
				"price_list_rate",
			)
			self.assertEqual(price, 1499.50)

			ecomm_item = frappe.get_doc(
				"Ecommerce Item", {"integration": "Amazon", "erpnext_item_code": "B07N4M94X4"}
			)
			self.assertEqual(ecomm_item.integration_item_code, "B07N4M94X4")
			self.assertEqual(ecomm_item.sku, "SKU-TV-1")

			# Second sync should update price without duplicating Item or Ecommerce Item.
			tsv_updated = (
				"seller-sku\tasin1\titem-name\titem-description\tlisting-id\tprice\tquantity\n"
				"SKU-TV-1\tB07N4M94X4\t\t\tL1\t1399.00\t4\n"
			)
			reports.report_tsv = gzip.compress(tsv_updated.encode("utf-8"))
			with patch(
				"amazon_connector.amazon_ext.listings.create_log"
			):
				summary2 = repo.get_listings()
			self.assertEqual(summary2["created"], 0)
			self.assertEqual(summary2["updated"], 1)
			updated_price = frappe.db.get_value(
				"Item Price",
				{"item_code": "B07N4M94X4", "price_list": "Standard Selling"},
				"price_list_rate",
			)
			self.assertEqual(updated_price, 1399.00)
		finally:
			TestCatalogItemsV2.mock_items = {}

	def test_get_listings_sandbox_fallback_when_in_progress(self):
		tsv = (
			"seller-sku\tasin1\titem-name\titem-description\tlisting-id\tprice\tquantity\n"
			"SBX-SKU-1\tB001SBX001\tSandbox Sample Product\tSample desc\tL2\t29.99\t5\n"
		)
		repo = TestAmazonRepository(use_sandbox=1)
		reports = TestReports(**repo.instance_params)
		reports.report_status_override = "IN_PROGRESS"
		reports.report_tsv = tsv
		repo._reports_instance = reports

		# Force Catalog v0 fallback off to test report-only fallback cleanly.
		repo.get_catalog_items_instance = lambda: None
		TestCatalogItemsV2.mock_items = {}

		with patch(
			"amazon_connector.amazon_ext.listings.create_log"
		):
			summary = repo.get_listings()
		self.assertEqual(reports.last_report_document_id, SANDBOX_REPORT_DOCUMENT_ID)
		self.assertEqual(summary["created"], 1)
		self.assertEqual(summary["failed"], 0)
		item = frappe.get_doc("Item", "B001SBX001")
		self.assertEqual(item.item_name, "Sandbox Sample Product")
		self.assertEqual(item.description, "Sample desc")

	def _setup_test_item(self, item_code="AMZ-OUT-ITEM-1"):
		if not frappe.db.exists("Brand", "Generic"):
			b = frappe.new_doc("Brand")
			b.brand = "Generic"
			b.insert(ignore_permissions=True)

		item_group_name = frappe.db.get_value(
			"Item Group", {"item_group_name": "Amazon Test Warehouse"}, "name"
		)
		if not item_group_name:
			ig = frappe.new_doc("Item Group")
			ig.item_group_name = "Amazon Test Warehouse"
			ig.parent_item_group = "All Item Groups"
			ig.insert(ignore_permissions=True)
			item_group_name = ig.name

		if not frappe.db.exists("Item", item_code):
			item = frappe.new_doc("Item")
			item.item_code = item_code
			item.item_name = "Outbound Test Item"
			item.item_group = item_group_name
			item.standard_rate = 250.0
			item.brand = "Generic"
			item.insert(ignore_permissions=True)

		if not frappe.db.exists("Item Barcode", {"parent": item_code, "barcode": "8901234567890"}):
			bc = frappe.new_doc("Item Barcode")
			bc.parent = item_code
			bc.parenttype = "Item"
			bc.parentfield = "barcodes"
			bc.barcode = "8901234567890"
			bc.barcode_type = "EAN"
			bc.insert(ignore_permissions=True)

		if not frappe.db.exists(
			"Item Price", {"item_code": item_code, "price_list": "Standard Selling"}
		):
			ip = frappe.new_doc("Item Price")
			ip.item_code = item_code
			ip.price_list = "Standard Selling"
			ip.price_list_rate = 250.0
			ip.insert(ignore_permissions=True)

		if not frappe.db.exists(
			"Ecommerce Item", {"erpnext_item_code": item_code, "integration": "Amazon"}
		):
			ecom = frappe.new_doc("Ecommerce Item")
			ecom.integration = "Amazon"
			ecom.erpnext_item_code = item_code
			ecom.integration_item_code = item_code
			ecom.sku = item_code
			ecom.has_variants = 0
			ecom.insert(ignore_permissions=True)

	def test_outbound_listing_creation_and_stock_price_patch(self):
		item_code = "AMZ-OUT-ITEM-1"
		self._setup_test_item(item_code)

		repo = TestAmazonRepository()
		mock_listings = TestListingsItems(**repo.instance_params)
		repo._listings_items_instance = mock_listings

		res = repo.sync_outbound_listing(item_code)
		self.assertEqual(res.get("status"), "ACCEPTED")
		self.assertEqual(mock_listings.last_put_sku, item_code)
		self.assertIn("attributes", mock_listings.last_put_body)
		attrs = mock_listings.last_put_body["attributes"]
		self.assertIn("purchasable_offer", attrs)
		self.assertEqual(
			attrs["purchasable_offer"][0]["our_price"][0]["schedule"][0]["value_with_tax"],
			250.0,
		)
		self.assertIn("externally_assigned_product_identifier", attrs)
		self.assertEqual(
			attrs["externally_assigned_product_identifier"][0]["value"], "8901234567890"
		)

		ecom_item = frappe.get_doc(
			"Ecommerce Item", {"integration": "Amazon", "erpnext_item_code": item_code}
		)
		self.assertIsNotNone(ecom_item)
		if frappe.db.has_column("Ecommerce Item", "status"):
			self.assertEqual(ecom_item.status, "ACCEPTED")

		# Patch price
		res_price = repo.sync_outbound_price(item_code, price_list="Standard Selling")
		self.assertEqual(res_price.get("status"), "ACCEPTED")
		self.assertEqual(mock_listings.last_patch_sku, item_code)
		patches = mock_listings.last_patch_body.get("patches", [])
		self.assertTrue(any(p["path"] == "/attributes/purchasable_offer" for p in patches))

		# Patch stock
		res_stock = repo.sync_outbound_stock(item_code)
		self.assertEqual(res_stock.get("status"), "ACCEPTED")
		patches_stock = mock_listings.last_patch_body.get("patches", [])
		self.assertTrue(
			any(p["path"] == "/attributes/fulfillment_availability" for p in patches_stock)
		)

	def test_outbound_listing_error_handling_and_logging(self):
		item_code = "AMZ-OUT-ITEM-1"
		self._setup_test_item(item_code)
		repo = TestAmazonRepository()
		mock_listings = TestListingsItems(**repo.instance_params)
		mock_listings.put_response_override = {
			"status": 200,
			"json": {
				"status": "INVALID",
				"issues": [
					{"severity": "ERROR", "code": "InvalidInput", "message": "Brand is missing"}
				],
			},
		}
		repo._listings_items_instance = mock_listings

		res = repo.sync_outbound_listing(item_code)
		self.assertEqual(res.get("status"), "INVALID")

		ecom_item = frappe.get_doc(
			"Ecommerce Item", {"integration": "Amazon", "erpnext_item_code": item_code}
		)
		if frappe.db.has_column("Ecommerce Item", "status"):
			self.assertEqual(ecom_item.status, "Error")

		recent_log = frappe.get_all(
			"Ecommerce Integration Log",
			filters={"integration": "Amazon", "status": "Error"},
			fields=["message"],
			order_by="creation desc",
			limit=1,
		)
		self.assertTrue(len(recent_log) > 0)
		self.assertIn("Brand is missing", recent_log[0].message)

	def test_delivery_note_shipment_confirmation(self):
		package_detail = {
			"packageReferenceId": "DN-TEST-001",
			"carrierCode": "Blue Dart",
			"trackingNumber": "TRK-BLUEDART-99",
			"shipDate": "2026-11-11T12:00:00Z",
			"orderItems": [{"orderItemId": "ORD-ITEM-001", "quantity": 1}],
		}

		with patch.object(
			Orders, "confirm_shipment", return_value={"status": "Shipment Confirmed"}
		) as mock_confirm:
			res = confirm_delivery_note_shipment(
				delivery_note_name="DN-TEST-001",
				amazon_order_id="114-1234567-8901234",
				package_detail=package_detail,
				marketplace_id="A21TJRUUN4KGV",
			)
			self.assertEqual(res.get("status"), "Confirmed")
			self.assertEqual((res.get("response") or {}).get("status"), "Shipment Confirmed")
			mock_confirm.assert_called_once()
			call_kwargs = mock_confirm.call_args.kwargs
			self.assertEqual(call_kwargs["order_id"], "114-1234567-8901234")
			self.assertEqual(call_kwargs["package_detail"]["carrierCode"], "Blue Dart")
			self.assertEqual(call_kwargs["package_detail"]["trackingNumber"], "TRK-BLUEDART-99")

		# Test handle_delivery_note_submit
		fake_dn = frappe._dict(
			name="DN-TEST-002",
			amazon_order_id="114-9999999-8888888",
			transporter_name="Delhivery",
			lr_no="LR-DELHIVERY-01",
			lr_date="2026-11-11",
			items=[frappe._dict(item_code="AMZ-OUT-ITEM-1", qty=2, amazon_order_item_id="AMZ-SUB-01")],
		)

		with patch.object(Orders, "confirm_shipment", return_value={"status": "ACCEPTED"}):
			handle_delivery_note_submit(fake_dn)

		recent_log = frappe.get_all(
			"Ecommerce Integration Log",
			filters={"integration": "Amazon", "status": "Success"},
			fields=["message"],
			order_by="creation desc",
			limit=1,
		)
		self.assertTrue(len(recent_log) > 0)
		self.assertIn("114-9999999-8888888", recent_log[0].message)

	def test_stock_ledger_and_item_price_event_hooks(self):
		item_code = "AMZ-OUT-ITEM-1"
		self._setup_test_item(item_code)
		self.assertTrue(is_item_mapped_to_amazon(item_code))

		active_warehouse = (
			frappe.db.get_value("Amazon SP API Settings", {"is_active": 1}, "warehouse")
			or "Amazon Test Warehouse - ATC"
		)
		sle = frappe._dict(item_code=item_code, warehouse=active_warehouse, actual_qty=10, qty_after_transaction=10)
		with patch(
			"amazon_connector.amazon_ext.fulfillment.sync_stock_job"
		) as mock_sync_stock:
			handle_stock_ledger_entry(sle)
			self.assertTrue(mock_sync_stock.called)

		sle_unrelated = frappe._dict(
			item_code=item_code, warehouse="Unrelated Other Warehouse - XX", actual_qty=10, qty_after_transaction=10
		)
		with patch(
			"amazon_connector.amazon_ext.fulfillment.sync_stock_job"
		) as mock_sync_stock_unrelated:
			handle_stock_ledger_entry(sle_unrelated)
			self.assertFalse(mock_sync_stock_unrelated.called)

		active_price_list = (
			frappe.db.get_value("Amazon SP API Settings", {"is_active": 1}, "price_list")
			or "Standard Selling"
		)
		item_price = frappe._dict(item_code=item_code, price_list=active_price_list, selling=1)
		with patch(
			"amazon_connector.amazon_ext.fulfillment.sync_price_job"
		) as mock_sync_price:
			handle_item_price_change(item_price)
			self.assertTrue(mock_sync_price.called)

		buying_price = frappe._dict(item_code=item_code, price_list=active_price_list, selling=0)
		with patch(
			"amazon_connector.amazon_ext.fulfillment.sync_price_job"
		) as mock_sync_buying:
			handle_item_price_change(buying_price)
			self.assertFalse(mock_sync_buying.called)

	def test_handle_delivery_note_submit_with_sales_order_lookup(self):
		sales_orders = TestAmazonRepository().get_orders("2000-07-23")
		self.assertTrue(len(sales_orders) >= 1)
		so_doc = frappe.get_doc("Sales Order", sales_orders[0])

		fake_dn = frappe._dict(
			name="DN-SO-LOOKUP-001",
			transporter_name="Blue Dart",
			lr_no="BD-LOOKUP-123",
			lr_date="2026-11-11",
			items=[
				frappe._dict(
					item_code=so_doc.items[0].item_code,
					qty=so_doc.items[0].qty,
					against_sales_order=so_doc.name,
					so_detail=so_doc.items[0].name,
				)
			],
		)

		remote_items_resp = {
			"payload": {
				"OrderItems": [
					{
						"OrderItemId": "RESOLVED-ORD-ITEM-1",
						"SellerSKU": so_doc.items[0].item_code,
						"ASIN": so_doc.items[0].item_code,
					}
				]
			}
		}

		with (
			patch.object(Orders, "get_order_items", return_value=remote_items_resp),
			patch.object(Orders, "confirm_shipment", return_value={"status": "ACCEPTED"}) as mock_confirm,
		):
			handle_delivery_note_submit(fake_dn)
			self.assertTrue(mock_confirm.called)
			kwargs = mock_confirm.call_args.kwargs
			self.assertEqual(kwargs["order_id"], so_doc.amazon_order_id)
			self.assertEqual(
				kwargs["package_detail"]["orderItems"][0]["orderItemId"],
				"RESOLVED-ORD-ITEM-1",
			)

	def _cleanup_test_order_docs(self, amazon_order_id: str, buyer_email: str | None = None):
		with patch(
			"amazon_connector.amazon_ext.fulfillment.sync_stock_job"
		):
			for dn_name in frappe.get_all(
				"Delivery Note", filters={"amazon_order_id": amazon_order_id}, pluck="name"
			):
				dn = frappe.get_doc("Delivery Note", dn_name)
				if dn.docstatus == 1:
					dn.cancel()
				frappe.delete_doc("Delivery Note", dn_name, force=1, ignore_permissions=True)
			for so_name in frappe.get_all(
				"Sales Order", filters={"amazon_order_id": amazon_order_id}, pluck="name"
			):
				so = frappe.get_doc("Sales Order", so_name)
				if so.docstatus == 1:
					so.cancel()
				frappe.delete_doc("Sales Order", so_name, force=1, ignore_permissions=True)
		if buyer_email:
			for ct in frappe.get_all("Contact", filters={"first_name": buyer_email}, pluck="name"):
				frappe.delete_doc("Contact", ct, force=1, ignore_permissions=True)
			if frappe.db.exists("Customer", buyer_email):
				frappe.delete_doc("Customer", buyer_email, force=1, ignore_permissions=True)

	def test_get_delivery_notes_shipped_idempotency_and_loop_guard(self):
		self._cleanup_test_order_docs("402-7777777-1111111", "dn-shipped@example.com")
		self._cleanup_test_order_docs("402-7777777-3333333", "dn-shipped@example.com")
		item_code = "AMZ-OUT-ITEM-1"
		self._setup_test_item(item_code)
		if not frappe.db.exists("Ecommerce Item", {"integration": "Amazon", "erpnext_item_code": item_code}):
			ecom = frappe.new_doc("Ecommerce Item")
			ecom.integration = "Amazon"
			ecom.erpnext_item_code = item_code
			ecom.integration_item_code = item_code
			ecom.sku = item_code
			ecom.insert(ignore_permissions=True)

		repo = TestAmazonRepository()
		shipped_order = {
			"AmazonOrderId": "402-7777777-1111111",
			"PurchaseDate": "2026-11-12T05:00:00Z",
			"LastUpdateDate": "2026-11-12T06:00:00Z",
			"LatestShipDate": "2026-11-13T06:00:00Z",
			"OrderStatus": "Shipped",
			"FulfillmentChannel": "AFN",
			"MarketplaceId": "A21TJRUUN4KGV",
			"CarrierCode": "Blue Dart",
			"TrackingNumber": "AMZ-TRK-999",
			"BuyerInfo": {"BuyerEmail": "dn-shipped@example.com"},
		}
		order_items_resp = {
			"payload": {
				"OrderItems": [
					{
						"ASIN": item_code,
						"OrderItemId": "ORD-ITEM-SHIP-1",
						"SellerSKU": item_code,
						"Title": "Amazon Outbound Test Item",
						"QuantityOrdered": 2,
						"QuantityShipped": 2,
						"ItemPrice": {"CurrencyCode": "INR", "Amount": "500.00"},
					}
				]
			}
		}

		# 1. Without warehouse stock (allow_negative_stock=0): saves Draft DN without duplicating.
		orig_neg = frappe.db.get_single_value("Stock Settings", "allow_negative_stock")
		frappe.db.set_single_value("Stock Settings", "allow_negative_stock", 0)
		try:
			with (
				patch.object(TestOrders, "get_orders", return_value={"payload": {"Orders": [shipped_order]}}),
				patch.object(TestOrders, "get_order_items", return_value=order_items_resp),
				patch.object(Orders, "confirm_shipment") as mock_confirm,
				patch(
					"amazon_connector.amazon_ext.fulfillment.sync_stock_job"
				),
			):
				draft_dns = repo.get_delivery_notes("2026-11-01")
				self.assertEqual(len(draft_dns), 1)
				draft_dn = frappe.get_doc("Delivery Note", draft_dns[0])
				self.assertEqual(draft_dn.docstatus, 0)
				self.assertEqual(draft_dn.amazon_order_id, "402-7777777-1111111")
				self.assertEqual(draft_dn.imported_from_amazon, 1)
				self.assertEqual(draft_dn.transporter_name, "Blue Dart")
				self.assertEqual(draft_dn.lr_no, "AMZ-TRK-999")
				self.assertEqual(draft_dn.items[0].qty, 2)
				self.assertFalse(mock_confirm.called)

				# Re-running does not create duplicate Draft DN
				self.assertEqual(repo.get_delivery_notes("2026-11-01"), [])

			# 2. With allow_negative_stock=1 (simulating stocked warehouse): submits DN cleanly & never calls confirm_shipment
			shipped_order_2 = {**shipped_order, "AmazonOrderId": "402-7777777-3333333"}
			frappe.db.set_single_value("Stock Settings", "allow_negative_stock", 1)
			with (
				patch.object(TestOrders, "get_orders", return_value={"payload": {"Orders": [shipped_order_2]}}),
				patch.object(TestOrders, "get_order_items", return_value=order_items_resp),
				patch.object(Orders, "confirm_shipment") as mock_confirm,
				patch(
					"amazon_connector.amazon_ext.fulfillment.sync_stock_job"
				),
			):
				submitted_dns = repo.get_delivery_notes("2026-11-01")
				self.assertEqual(len(submitted_dns), 1)
				submitted_dn = frappe.get_doc("Delivery Note", submitted_dns[0])
				self.assertEqual(submitted_dn.docstatus, 1)
				self.assertEqual(submitted_dn.items[0].qty, 2)
				self.assertFalse(mock_confirm.called)

				# Already 100% delivered -> idempotent on repeat sync
				self.assertEqual(repo.get_delivery_notes("2026-11-01"), [])
		finally:
			frappe.db.set_single_value("Stock Settings", "allow_negative_stock", orig_neg or 0)
			self._cleanup_test_order_docs("402-7777777-1111111", "dn-shipped@example.com")
			self._cleanup_test_order_docs("402-7777777-3333333", "dn-shipped@example.com")

	def test_get_delivery_notes_partial_and_auto_sync_in_get_orders(self):
		self._cleanup_test_order_docs("402-8888888-2222222", "dn-partial@example.com")
		item_code = "AMZ-OUT-ITEM-1"
		self._setup_test_item(item_code)
		repo = TestAmazonRepository()
		partial_order = {
			"AmazonOrderId": "402-8888888-2222222",
			"PurchaseDate": "2026-11-12T05:00:00Z",
			"LastUpdateDate": "2026-11-12T06:00:00Z",
			"LatestShipDate": "2026-11-13T06:00:00Z",
			"OrderStatus": "PartiallyShipped",
			"FulfillmentChannel": "MFN",
			"MarketplaceId": "A21TJRUUN4KGV",
			"BuyerInfo": {"BuyerEmail": "dn-partial@example.com"},
		}
		partial_items_resp = {
			"payload": {
				"OrderItems": [
					{
						"ASIN": item_code,
						"OrderItemId": "ORD-ITEM-PART-1",
						"SellerSKU": item_code,
						"Title": "Amazon Outbound Test Item",
						"QuantityOrdered": 3,
						"QuantityShipped": 2,
						"ItemPrice": {"CurrencyCode": "INR", "Amount": "300.00"},
					}
				]
			}
		}

		orig_neg = frappe.db.get_single_value("Stock Settings", "allow_negative_stock")
		frappe.db.set_single_value("Stock Settings", "allow_negative_stock", 1)
		try:
			repo.amz_setting.sync_delivery_notes = 1
			with (
				patch.object(TestOrders, "get_orders", return_value={"payload": {"Orders": [partial_order]}}),
				patch.object(TestOrders, "get_order_items", return_value=partial_items_resp),
				patch.object(Orders, "confirm_shipment") as mock_confirm,
				patch(
					"amazon_connector.amazon_ext.fulfillment.sync_stock_job"
				),
			):
				sos = repo.get_orders("2026-11-01")
				self.assertEqual(len(sos), 1)
				self.assertFalse(mock_confirm.called)

				dn_name = frappe.db.get_value(
					"Delivery Note",
					{"amazon_order_id": "402-8888888-2222222", "docstatus": 1},
					"name",
				)
				self.assertTrue(dn_name)
				dn = frappe.get_doc("Delivery Note", dn_name)
				self.assertEqual(dn.items[0].qty, 2)

				so = frappe.get_doc("Sales Order", sos[0])
				self.assertEqual(so.items[0].qty, 3)
				self.assertEqual(so.items[0].delivered_qty, 2)
		finally:
			frappe.db.set_single_value("Stock Settings", "allow_negative_stock", orig_neg or 0)
			self._cleanup_test_order_docs("402-8888888-2222222", "dn-partial@example.com")

	def test_unit_rate_uom_item_id_address_country_and_fee_separation(self):
		order_id = "402-1111111-2222222"
		self._cleanup_test_order_docs(order_id, "multi-qty@example.com")
		item_code = "AMZ-OUT-ITEM-1"
		self._setup_test_item(item_code)
		if not frappe.db.exists("UOM", "Kg"):
			frappe.get_doc({"doctype": "UOM", "uom_name": "Kg"}).insert(ignore_permissions=True)
		frappe.db.set_value("Item", item_code, "stock_uom", "Kg")

		repo = TestAmazonRepository()
		repo.amz_setting.amazon_fields_map = [
			frappe._dict({"amazon_field": "SellerSKU", "item_field": "item_code", "use_to_find_item_code": 1})
		]
		order_data = {
			"AmazonOrderId": order_id,
			"PurchaseDate": "2026-11-12T05:00:00Z",
			"LastUpdateDate": "2026-11-12T06:00:00Z",
			"LatestShipDate": None,
			"OrderStatus": "Unshipped",
			"FulfillmentChannel": "MFN",
			"MarketplaceId": "A21TJRUUN4KGV",
			"BuyerInfo": {"BuyerEmail": "multi-qty@example.com"},
			"ShippingAddress": {
				"Name": "Priya Nair",
				"AddressLine1": "12 MG Road",
				"City": "Bengaluru",
				"StateOrRegion": "Karnataka",
				"PostalCode": "560001",
				"CountryCode": "IN",
			},
		}
		items_resp = {
			"payload": {
				"OrderItems": [
					{
						"ASIN": "B0MULTI001",
						"OrderItemId": "OI-MULTI-001",
						"SellerSKU": item_code,
						"Title": "Multi Qty Pack",
						"QuantityOrdered": 3,
						"ItemPrice": {"CurrencyCode": "INR", "Amount": "900.00"},
						"ItemTax": {"CurrencyCode": "INR", "Amount": "162.00"},
					}
				]
			}
		}
		# Finances contains both customer tax charge AND seller fees (with SellerSKU=None guard check).
		finances_resp = {
			"payload": {
				"FinancialEvents": {
					"ShipmentEventList": [
						{
							"AmazonOrderId": order_id,
							"ShipmentItemList": [
								{
									"SellerSKU": None,
									"ItemChargeList": [
										{"ChargeType": "Principal", "ChargeAmount": {"CurrencyAmount": 900.0}},
										{"ChargeType": "Tax", "ChargeAmount": {"CurrencyAmount": 162.0}},
									],
									"ItemFeeList": [
										{"FeeType": "Commission", "FeeAmount": {"CurrencyAmount": -135.0}},
										{"FeeType": "FBAPerUnitFulfillmentFee", "FeeAmount": {"CurrencyAmount": -45.0}},
									],
								}
							],
						}
					]
				}
			}
		}

		try:
			with (
				patch.object(TestOrders, "get_orders", return_value={"payload": {"Orders": [order_data]}}),
				patch.object(TestOrders, "get_order_items", return_value=items_resp),
				patch.object(TestFinances, "list_financial_events_by_order_id", return_value=finances_resp),
			):
				sos = repo.get_orders("2026-11-01")

			self.assertEqual(len(sos), 1)
			so = frappe.get_doc("Sales Order", sos[0])
			self.assertEqual(so.items[0].qty, 3)
			self.assertEqual(so.items[0].rate, 300.0)
			self.assertEqual(so.items[0].stock_uom, "Kg")
			self.assertEqual(so.items[0].amazon_order_item_id, "OI-MULTI-001")
			self.assertEqual(so.amazon_order_status, "Unshipped")
			self.assertEqual(so.fulfillment_channel, "MFN")
			self.assertTrue(so.shipping_address_name)
			self.assertEqual(frappe.db.get_value("Address", so.shipping_address_name, "country"), "India")

			tax_accounts = [t.account_head for t in so.taxes]
			self.assertTrue(any("Amazon Tax" in acc for acc in tax_accounts))
			self.assertFalse(any("Commission" in acc or "FBAPerUnit" in acc for acc in tax_accounts))
			self.assertEqual(len(so.taxes), 1)
		finally:
			self._cleanup_test_order_docs(order_id, "multi-qty@example.com")

	def test_unshipped_empty_shipment_events_and_iter_orders_pagination(self):
		order_id = "402-3333333-4444444"
		self._cleanup_test_order_docs(order_id, "empty-fin@example.com")
		item_code = "AMZ-OUT-ITEM-1"
		self._setup_test_item(item_code)
		repo = TestAmazonRepository()

		page_1 = {
			"payload": {
				"Orders": [
					{
						"AmazonOrderId": order_id,
						"PurchaseDate": "2026-11-12T05:00:00Z",
						"LatestShipDate": "2026-11-13T05:00:00Z",
						"OrderStatus": "Unshipped",
						"FulfillmentChannel": "MFN",
						"MarketplaceId": "A21TJRUUN4KGV",
						"BuyerInfo": {"BuyerEmail": "empty-fin@example.com"},
					}
				],
				"NextToken": "TOK-PAGE-2",
			}
		}
		page_2 = {"payload": {"Orders": []}}
		items_resp = {
			"payload": {
				"OrderItems": [
					{
						"ASIN": item_code,
						"OrderItemId": "OI-UNSHIP-1",
						"SellerSKU": item_code,
						"Title": "Unshipped Item",
						"QuantityOrdered": 1,
						"ItemPrice": {"CurrencyCode": "INR", "Amount": "400.00"},
						"ItemTax": {"CurrencyCode": "INR", "Amount": "72.00"},
						"ShippingPrice": {"CurrencyCode": "INR", "Amount": "40.00"},
					}
				]
			}
		}
		empty_fin_resp = {"payload": {"FinancialEvents": {"ShipmentEventList": []}}}

		try:
			with (
				patch.object(TestOrders, "get_orders", side_effect=[page_1, page_2]) as mock_get_orders,
				patch.object(TestOrders, "get_order_items", return_value=items_resp),
				patch.object(TestFinances, "list_financial_events_by_order_id", return_value=empty_fin_resp),
			):
				sos = repo.get_orders("2026-11-01")

			self.assertEqual(len(sos), 1)
			first_call_kwargs = mock_get_orders.call_args_list[0].kwargs
			self.assertEqual(first_call_kwargs["fulfillment_channels"], ["AFN", "MFN"])
			second_call_kwargs = mock_get_orders.call_args_list[1].kwargs
			self.assertEqual(second_call_kwargs["next_token"], "TOK-PAGE-2")
			self.assertEqual(second_call_kwargs["fulfillment_channels"], ["AFN", "MFN"])

			so = frappe.get_doc("Sales Order", sos[0])
			self.assertEqual(len(so.taxes), 2)
			self.assertAlmostEqual(sum(t.tax_amount for t in so.taxes), 112.0)
		finally:
			self._cleanup_test_order_docs(order_id, "empty-fin@example.com")

	def test_canceled_order_skips_new_and_cancels_existing_sales_order(self):
		new_cancel_id = "402-5555555-1111111"
		existing_cancel_id = "402-5555555-2222222"
		self._cleanup_test_order_docs(new_cancel_id, "cancel-new@example.com")
		self._cleanup_test_order_docs(existing_cancel_id, "cancel-existing@example.com")
		item_code = "AMZ-OUT-ITEM-1"
		self._setup_test_item(item_code)
		repo = TestAmazonRepository()
		repo.amz_setting.taxes_charges = 0

		active_order = {
			"AmazonOrderId": existing_cancel_id,
			"PurchaseDate": "2026-11-12T05:00:00Z",
			"LatestShipDate": "2026-11-13T05:00:00Z",
			"OrderStatus": "Unshipped",
			"FulfillmentChannel": "MFN",
			"BuyerInfo": {"BuyerEmail": "cancel-existing@example.com"},
		}
		canceled_new_order = {
			**active_order,
			"AmazonOrderId": new_cancel_id,
			"OrderStatus": "Canceled",
			"BuyerInfo": {"BuyerEmail": "cancel-new@example.com"},
		}
		items_resp = {
			"payload": {
				"OrderItems": [
					{
						"ASIN": item_code,
						"OrderItemId": "OI-CANC-1",
						"SellerSKU": item_code,
						"Title": "Cancelable Item",
						"QuantityOrdered": 1,
						"ItemPrice": {"CurrencyCode": "INR", "Amount": "250.00"},
					}
				]
			}
		}

		try:
			with (
				patch.object(
					TestOrders,
					"get_orders",
					return_value={"payload": {"Orders": [canceled_new_order, active_order]}},
				),
				patch.object(TestOrders, "get_order_items", return_value=items_resp),
			):
				created = repo.get_orders("2026-11-01")

			self.assertIsNone(frappe.db.get_value("Sales Order", {"amazon_order_id": new_cancel_id}, "name"))
			self.assertEqual(len(created), 1)
			so_name = created[0]

			canceled_existing = {**active_order, "OrderStatus": "Canceled"}
			with patch.object(
				TestOrders,
				"get_orders",
				return_value={"payload": {"Orders": [canceled_existing]}},
			):
				repo.get_orders("2026-11-01")

			so = frappe.get_doc("Sales Order", so_name)
			self.assertEqual(so.docstatus, 2)
			self.assertEqual(so.amazon_canceled, 1)
			self.assertEqual(so.amazon_order_status, "Canceled")
		finally:
			self._cleanup_test_order_docs(new_cancel_id, "cancel-new@example.com")
			self._cleanup_test_order_docs(existing_cancel_id, "cancel-existing@example.com")

	def test_auto_sales_invoice_returns_and_settlement_lifecycle(self):
		order_id = "402-6666666-9999999"
		group_id = "FEG-TEST-SETTLE-001"
		self._cleanup_test_order_docs(order_id, "lifecycle@example.com")
		for si_name in frappe.get_all("Sales Invoice", filters={"amazon_order_id": order_id}, pluck="name"):
			si = frappe.get_doc("Sales Invoice", si_name)
			if si.docstatus == 1:
				si.cancel()
			frappe.delete_doc("Sales Invoice", si_name, force=1, ignore_permissions=True)
		for je_name in frappe.get_all(
			"Journal Entry", filters={"amazon_financial_event_group_id": group_id}, pluck="name"
		):
			je = frappe.get_doc("Journal Entry", je_name)
			if je.docstatus == 1:
				je.cancel()
			frappe.delete_doc("Journal Entry", je_name, force=1, ignore_permissions=True)

		item_code = "AMZ-OUT-ITEM-1"
		self._setup_test_item(item_code)
		repo = TestAmazonRepository()
		repo.amz_setting.taxes_charges = 0
		repo.amz_setting.sync_sales_invoice = 1

		order_data = {
			"AmazonOrderId": order_id,
			"PurchaseDate": "2026-11-12T05:00:00Z",
			"LatestShipDate": "2026-11-13T05:00:00Z",
			"OrderStatus": "Shipped",
			"FulfillmentChannel": "MFN",
			"MarketplaceId": "A21TJRUUN4KGV",
			"BuyerInfo": {"BuyerEmail": "lifecycle@example.com"},
		}
		items_resp = {
			"payload": {
				"OrderItems": [
					{
						"ASIN": item_code,
						"OrderItemId": "OI-LIFE-1",
						"SellerSKU": item_code,
						"Title": "Lifecycle Item",
						"QuantityOrdered": 2,
						"QuantityShipped": 2,
						"ItemPrice": {"CurrencyCode": "INR", "Amount": "1000.00"},
					}
				]
			}
		}

		try:
			with (
				patch.object(TestOrders, "get_orders", return_value={"payload": {"Orders": [order_data]}}),
				patch.object(TestOrders, "get_order_items", return_value=items_resp),
			):
				sos = repo.get_orders("2026-11-01")
				self.assertEqual(len(sos), 1)
				# Idempotent repeat does not duplicate Sales Invoice
				repo.create_sales_order(order_data)

			sis = frappe.get_all(
				"Sales Invoice",
				filters={"amazon_order_id": order_id, "is_return": 0, "docstatus": 1},
				pluck="name",
			)
			self.assertEqual(len(sis), 1)

			# Returns / Credit Note sync
			refund_payload = {
				"payload": {
					"FinancialEvents": {
						"RefundEventList": [
							{
								"AmazonOrderId": order_id,
								"PostedDate": "2026-11-12T08:00:00Z",
								"ShipmentItemAdjustmentList": [
									{
										"SellerSKU": item_code,
										"OrderAdjustmentItemId": "ADJ-001",
										"QuantityShipped": 1,
										"ItemChargeAdjustmentList": [
											{"ChargeType": "Principal", "ChargeAmount": {"CurrencyAmount": -500.0}}
										],
									}
								],
							}
						]
					}
				}
			}
			with (
				patch(
					"amazon_connector.amazon_ext.repository.CustomAmazonRepository",
					return_value=repo,
				),
				patch.object(Finances, "list_financial_events", return_value=refund_payload),
			):
				cns_1 = sync_returns("test-setting", posted_after="2026-11-01")
				cns_2 = sync_returns("test-setting", posted_after="2026-11-01")

			self.assertEqual(len(cns_1), 1)
			self.assertEqual(cns_1, cns_2)
			cn = frappe.get_doc("Sales Invoice", cns_1[0])
			self.assertEqual(cn.is_return, 1)
			self.assertEqual(cn.items[0].qty, -1)

			# Settlement Journal Entry sync
			groups_payload = {
				"payload": {
					"FinancialEventGroupList": [
						{
							"FinancialEventGroupId": group_id,
							"ProcessingStatus": "Closed",
							"FundTransferStatus": "Succeeded",
							"FinancialEventGroupStart": "2026-11-01T00:00:00Z",
							"FinancialEventGroupEnd": "2026-11-12T00:00:00Z",
							"OriginalTotal": {"CurrencyCode": "INR", "CurrencyAmount": 350.0},
						}
					]
				}
			}
			group_events_payload = {
				"payload": {
					"FinancialEvents": {
						"ShipmentEventList": [
							{
								"AmazonOrderId": order_id,
								"ShipmentItemList": [
									{
										"SellerSKU": item_code,
										"ItemChargeList": [
											{"ChargeType": "Principal", "ChargeAmount": {"CurrencyAmount": 1000.0}}
										],
										"ItemFeeList": [
											{"FeeType": "Commission", "FeeAmount": {"CurrencyAmount": -150.0}}
										],
									}
								],
							}
						],
						"RefundEventList": refund_payload["payload"]["FinancialEvents"]["RefundEventList"],
					}
				}
			}
			with (
				patch(
					"amazon_connector.amazon_ext.repository.CustomAmazonRepository",
					return_value=repo,
				),
				patch.object(Finances, "list_financial_event_groups", return_value=groups_payload),
				patch.object(Finances, "list_financial_events_by_group_id", return_value=group_events_payload),
			):
				jes_1 = sync_settlements("test-setting", started_after="2026-11-01")
				jes_2 = sync_settlements("test-setting", started_after="2026-11-01")

			self.assertEqual(len(jes_1), 1)
			self.assertEqual(jes_1, jes_2)
			je = frappe.get_doc("Journal Entry", jes_1[0])
			self.assertEqual(je.docstatus, 1)
			self.assertEqual(je.amazon_financial_event_group_id, group_id)
			self.assertAlmostEqual(
				sum(r.debit_in_account_currency for r in je.accounts),
				sum(r.credit_in_account_currency for r in je.accounts),
			)
		finally:
			for je_name in frappe.get_all(
				"Journal Entry", filters={"amazon_financial_event_group_id": group_id}, pluck="name"
			):
				je = frappe.get_doc("Journal Entry", je_name)
				if je.docstatus == 1:
					je.cancel()
				frappe.delete_doc("Journal Entry", je_name, force=1, ignore_permissions=True)
			for si_name in frappe.get_all("Sales Invoice", filters={"amazon_order_id": order_id}, pluck="name"):
				si = frappe.get_doc("Sales Invoice", si_name)
				if si.docstatus == 1:
					si.cancel()
				frappe.delete_doc("Sales Invoice", si_name, force=1, ignore_permissions=True)
			self._cleanup_test_order_docs(order_id, "lifecycle@example.com")

	def test_retry_job_handles_null_method_and_kwargs_dispatch(self):
		null_log = create_log(module_def="Amazon", status="Error", method=None, message="No method", make_new=True)
		_retry_job(null_log.name)  # Should not raise AttributeError

		kw_log = create_log(
			module_def="Amazon",
			status="Error",
			method="amazon_connector.amazon_ext.repository.get_orders",
			request_data={"amz_setting_name": "SET-1", "created_after": "2026-11-01", "extra": "ignored"},
			message="<b>Failed</b>",
			make_new=True,
		)
		self.assertEqual(kw_log.message, "Failed")
		with patch("frappe.enqueue") as mock_enq:
			_retry_job(kw_log.name)
			self.assertTrue(mock_enq.called)
			self.assertTrue(mock_enq.call_args.kwargs["now"])
			self.assertEqual(mock_enq.call_args.kwargs["amz_setting_name"], "SET-1")
			self.assertEqual(mock_enq.call_args.kwargs["created_after"], "2026-11-01")
			self.assertNotIn("extra", mock_enq.call_args.kwargs)

		sync_log = create_log(
			module_def="Amazon",
			status="Error",
			method="amazon_connector.amazon_ext.repository.get_orders",
			request_data={"amz_setting_name": "SET-SYNC", "created_after": "2026-11-02"},
			message="Sync retry",
			make_new=True,
		)
		with patch(
			"amazon_connector.amazon_ext.repository.get_orders"
		) as mock_get_orders:
			_retry_job(sync_log.name)
			mock_get_orders.assert_called_once_with(
				amz_setting_name="SET-SYNC",
				created_after="2026-11-02",
				request_id=sync_log.name,
			)

	def test_listings_opening_stock_idempotency_and_zero_price_guard(self):
		asin = "B0OPENSTK01"
		frappe.db.delete("Item Price", {"item_code": asin})
		for se_name in frappe.get_all(
			"Stock Entry",
			filters={"remarks": f"Amazon Opening Stock: {asin}"},
			pluck="name",
		):
			se = frappe.get_doc("Stock Entry", se_name)
			if se.docstatus == 1:
				se.cancel()
			frappe.delete_doc("Stock Entry", se_name, force=1, ignore_permissions=True)
		frappe.db.delete("Ecommerce Item", {"erpnext_item_code": asin})
		frappe.db.delete("Item", {"name": asin})

		tsv = (
			"seller-sku\tasin1\titem-name\titem-description\tlisting-id\tprice\tquantity\n"
			f"SKU-OPEN-1\t{asin}\tInitial Opening Title\tInitial Desc\tL-OP-1\t\t5\n"
		)
		repo = TestAmazonRepository(use_sandbox=0)
		reports = TestReports(**repo.instance_params)
		reports.report_tsv = gzip.compress(tsv.encode("utf-8"))
		repo._reports_instance = reports
		repo.get_catalog_items_instance = lambda: None
		TestCatalogItemsV2.mock_items = {
			asin: {
				"asin": asin,
				"summaries": [{"itemName": "Initial Opening Title"}],
				"attributes": {"list_price": [{"value": 750.0}]},
			}
		}

		try:
			upsert_item_price(asin, "Standard Selling", 0.0)
			# Zero rate should NOT insert a 0 price on a new Item Price
			self.assertIsNone(
				frappe.db.get_value("Item Price", {"item_code": asin, "price_list": "Standard Selling"}, "name")
			)

			with patch(
				"amazon_connector.amazon_ext.fulfillment.sync_stock_job"
			):
				summary1 = repo.get_listings()
				self.assertEqual(summary1["created"], 1)
				qty_after_first = frappe.db.get_value(
					"Bin", {"item_code": asin, "warehouse": "Amazon Test Warehouse - ATC"}, "actual_qty"
				)
				self.assertEqual(qty_after_first, 5.0)

				# Re-run with updated title: should update Item and NOT create a duplicate Stock Entry
				tsv2 = (
					"seller-sku\tasin1\titem-name\titem-description\tlisting-id\tprice\tquantity\n"
					f"SKU-OPEN-1\t{asin}\tUpdated Opening Title\tUpdated Desc\tL-OP-1\t799.0\t5\n"
				)
				reports.report_tsv = gzip.compress(tsv2.encode("utf-8"))
				summary2 = repo.get_listings()
				self.assertEqual(summary2["updated"], 1)
				self.assertEqual(frappe.db.get_value("Item", asin, "item_name"), "Updated Opening Title")
				ses = frappe.get_all(
					"Stock Entry",
					filters={"remarks": f"Amazon Opening Stock: {asin}", "docstatus": 1},
					pluck="name",
				)
				self.assertEqual(len(ses), 1)
		finally:
			TestCatalogItemsV2.mock_items = {}
			for se_name in frappe.get_all(
				"Stock Entry",
				filters={"remarks": f"Amazon Opening Stock: {asin}"},
				pluck="name",
			):
				se = frappe.get_doc("Stock Entry", se_name)
				if se.docstatus == 1:
					se.cancel()
				frappe.delete_doc("Stock Entry", se_name, force=1, ignore_permissions=True)
			frappe.db.delete("Item Price", {"item_code": asin})
			frappe.db.delete("Ecommerce Item", {"erpnext_item_code": asin})
			frappe.db.delete("Item", {"name": asin})

	def test_sync_amazon_order_whitelisted_and_after_date_programmatic_save(self):
		from amazon_connector.overrides.amazon_sp_api_settings import (
			CustomAmazonSPAPISettings as AmazonSPAPISettings,
		)

		order_id = "402-9898989-1231231"
		self._cleanup_test_order_docs(order_id, "single-sync@example.com")
		item_code = "AMZ-OUT-ITEM-1"
		self._setup_test_item(item_code)
		repo = TestAmazonRepository()
		repo.amz_setting.taxes_charges = 0

		order_data = {
			"AmazonOrderId": order_id,
			"PurchaseDate": "2026-11-12T05:00:00Z",
			"LatestShipDate": "2026-11-13T05:00:00Z",
			"OrderStatus": "Unshipped",
			"FulfillmentChannel": "MFN",
			"MarketplaceId": "A21TJRUUN4KGV",
			"BuyerInfo": {"BuyerEmail": "single-sync@example.com"},
		}
		items_resp = {
			"payload": {
				"OrderItems": [
					{
						"ASIN": item_code,
						"OrderItemId": "OI-SGL-1",
						"SellerSKU": item_code,
						"QuantityOrdered": 1,
						"ItemPrice": {"CurrencyCode": "INR", "Amount": "150.00"},
					}
				]
			}
		}

		try:
			with (
				patch.object(TestOrders, "get_orders", return_value={"payload": {"Orders": [order_data]}}),
				patch.object(TestOrders, "get_order_items", return_value=items_resp),
			):
				so_name = repo.get_orders("2026-11-01")[0]

			updated_order = {**order_data, "OrderStatus": "Shipped"}
			with (
				patch(
					"amazon_connector.overrides.amazon_sp_api_settings.CustomAmazonRepository",
					return_value=repo,
				),
				patch.object(TestOrders, "get_order", return_value={"payload": updated_order}),
				patch.object(TestOrders, "get_order_items", return_value=items_resp),
				patch(
					"amazon_connector.amazon_ext.fulfillment.sync_stock_job"
				),
			):
				result = sync_amazon_order(so_name, sync_delivery_notes=0, sync_sales_invoice=1)

			self.assertEqual(result["amazon_order_id"], order_id)
			self.assertEqual(result["order_status"], "Shipped")
			self.assertFalse(result["canceled"])
			self.assertEqual(len(result["sales_invoices"]), 1)

			so_doc = frappe.get_doc("Sales Order", so_name)
			so_doc.flags.from_amazon_sync = False
			handle_sales_order_cancel(so_doc)
			self.assertEqual(frappe.db.get_value("Sales Order", so_name, "amazon_canceled"), 1)

			# Unchanged after_date on non-new Document should not block save validation
			fake_doc = frappe._dict(
				after_date="2000-01-01",
				flags=frappe._dict(),
				is_new=lambda: False,
				has_value_changed=lambda field: False,
			)
			AmazonSPAPISettings.validate_after_date(fake_doc)
		finally:
			for si_name in frappe.get_all("Sales Invoice", filters={"amazon_order_id": order_id}, pluck="name"):
				si = frappe.get_doc("Sales Invoice", si_name)
				if si.docstatus == 1:
					si.cancel()
				frappe.delete_doc("Sales Invoice", si_name, force=1, ignore_permissions=True)
			self._cleanup_test_order_docs(order_id, "single-sync@example.com")

	def test_catalog_image_guard_skips_mock_local_and_broken_request_context(self):
		self.assertIsNone(resolve_safe_catalog_image_url("https://mock-cdn.local/items/B0GUARD01.jpg"))
		self.assertIsNone(resolve_safe_catalog_image_url("http://localhost:9099/img.jpg"))
		self.assertEqual(resolve_safe_catalog_image_url("https://example.com/tv.jpg"), "https://example.com/tv.jpg")

		asin = "B0IMGGUARD1"
		frappe.db.delete("Ecommerce Item", {"erpnext_item_code": asin})
		frappe.db.delete("Item", {"name": asin})
		orig_request = getattr(frappe.local, "request", None)
		before_errors = frappe.db.count("Error Log", {"method": "Error Attaching File"})
		try:
			frappe.local.request = SimpleNamespace(host="", headers=None)
			self.assertIsNone(resolve_safe_catalog_image_url("https://example.com/tv.jpg"))

			repo = TestAmazonRepository(use_sandbox=0)
			sync = AmazonListingSync(repo)
			item_code = sync._create_erpnext_item(
				{"ASIN": asin, "SellerSKU": "SKU-IMG-1", "Title": "Image Guard Item"},
				{"image": "https://example.com/tv.jpg", "product_group": "Electronics"},
			)
			self.assertEqual(item_code, asin)
			self.assertFalse(frappe.db.get_value("Item", asin, "image"))

			frappe.local.request = orig_request
			sync._update_erpnext_item(
				asin,
				{"Title": "Updated Image Guard Item"},
				{"image": "https://mock-cdn.local/items/B0IMGGUARD1.jpg"},
			)
			self.assertFalse(frappe.db.get_value("Item", asin, "image"))
			after_errors = frappe.db.count("Error Log", {"method": "Error Attaching File"})
			self.assertEqual(before_errors, after_errors)
		finally:
			frappe.local.request = orig_request
			frappe.db.delete("Ecommerce Item", {"erpnext_item_code": asin})
			frappe.db.delete("Item", {"name": asin})

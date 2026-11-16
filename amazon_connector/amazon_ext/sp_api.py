# Copyright (c) 2022, Frappe and contributors
# For license information, please see license.txt

import datetime
import gzip
import hashlib
import hmac

import boto3
from requests import request
from requests.auth import AuthBase
from requests.compat import quote, urlparse

__all__ = [
	"SPAPIError",
	"Tokens",
	"Finances",
	"Orders",
	"CatalogItems",
	"CatalogItemsV2",
	"Reports",
	"ListingsItems",
	"ProductTypeDefinitions",
]

DEFAULT_SPAPI_TIMEOUT = (5, 30)

MARKETPLACES = {
	"North America": {
		"CA": "A2EUQ1WTGCTBG2",
		"US": "ATVPDKIKX0DER",
		"MX": "A1AM78C64UM0Y8",
		"BR": "A2Q3Y263D00KWC",
		"AWS Region": "us-east-1",
		"Endpoint": "https://sellingpartnerapi-na.amazon.com",
	},
	"Europe": {
		"ES": "A1RKKUPIHCS9HS",
		"GB": "A1F83G8C2ARO7P",
		"FR": "A13V1IB3VIYZZH",
		"NL": "A1805IZSGTT6HS",
		"DE": "A1PA6795UKMFR9",
		"IT": "APJ6JRA9NG5V4",
		"SE": "A2NODRKZP88ZB9",
		"PL": "A1C3SOZRARQ6R3",
		"EG": "ARBP9OOSHTCHU",
		"TR": "A33AVAJ2PDY3EV",
		"SA": "A17E79C6D8DWNP",
		"AE": "A2VIGQ35RCS4UG",
		"IN": "A21TJRUUN4KGV",
		"AWS Region": "eu-west-1",
		"Endpoint": "https://sellingpartnerapi-eu.amazon.com",
	},
	"Far East": {
		"SG": "A19VAU5U5O7RUS",
		"AU": "A39IBJ37TRP1C6",
		"JP": "A1VC38T7YXB528",
		"AWS Region": "us-west-2",
		"Endpoint": "https://sellingpartnerapi-fe.amazon.com",
	},
}


class AWSSigV4(AuthBase):
	def __init__(self, service, **kwargs):
		self.service = service
		self.region = kwargs.get("region")
		self.aws_access_key_id = kwargs.get("aws_access_key_id")
		self.aws_session_token = kwargs.get("aws_session_token")
		self.aws_secret_access_key = kwargs.get("aws_secret_access_key")

		if not self.aws_access_key_id or not self.aws_secret_access_key:
			raise KeyError("AWS Access Key ID and Secret Access Key are required.")

		if self.region is None:
			raise KeyError("Region is required.")

	def __call__(self, request):
		time = datetime.datetime.now(datetime.timezone.utc)
		self.amzdate = time.strftime("%Y%m%dT%H%M%SZ")
		self.datestamp = time.strftime("%Y%m%d")

		parsed_url = urlparse(request.url)
		host = parsed_url.hostname or ""
		if ":" in host and not host.startswith("["):
			host = f"[{host}]"
		if parsed_url.port and (parsed_url.scheme.lower(), parsed_url.port) not in (
			("http", 80),
			("https", 443),
		):
			host = f"{host}:{parsed_url.port}"
		uri = parsed_url.path

		if len(parsed_url.query) > 0:
			query_string = dict(
				item.split("=", 1) if "=" in item else (item, "") for item in parsed_url.query.split("&")
			)
		else:
			query_string = {}

		if "Host" not in request.headers:
			request.headers["Host"] = host
		if "Content-Type" not in request.headers:
			request.headers["Content-Type"] = "application/x-www-form-urlencoded; charset=utf-8"
		if "User-Agent" not in request.headers:
			request.headers["User-Agent"] = "python-amazon-mws/0.0.1 (Language=Python)"
		if self.aws_session_token:
			request.headers["x-amz-security-token"] = self.aws_session_token
		request.headers["X-AMZ-Date"] = self.amzdate

		canonical_query_string = "&".join(map(lambda p: "=".join(p), sorted(query_string.items())))

		if request.method == "GET":
			payload_hash = hashlib.sha256(b"").hexdigest()
		elif request.body:
			raw_body = request.body if isinstance(request.body, bytes) else request.body.encode("utf-8")
			payload_hash = hashlib.sha256(raw_body).hexdigest()
		else:
			payload_hash = hashlib.sha256(b"").hexdigest()
		request.headers["x-amz-content-sha256"] = payload_hash

		headers_to_sign = sorted(
			filter(
				lambda h: h.startswith("x-amz-") or h == "host",
				map(lambda h: h.lower(), request.headers.keys()),
			)
		)
		canonical_headers = "".join(f"{h}:{request.headers[h]}\n" for h in headers_to_sign)
		signed_headers = ";".join(headers_to_sign)

		canonical_request = "\n".join(
			[request.method, uri, canonical_query_string, canonical_headers, signed_headers, payload_hash]
		)

		credential_scope = "/".join([self.datestamp, self.region, self.service, "aws4_request"])
		string_to_sign = "\n".join(
			[
				"AWS4-HMAC-SHA256",
				self.amzdate,
				credential_scope,
				hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
			]
		)

		def sign(key, msg):
			return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

		key_date = sign(("AWS4" + self.aws_secret_access_key).encode("utf-8"), self.datestamp)
		key_region = sign(key_date, self.region)
		k_service = sign(key_region, self.service)
		key_signing = sign(k_service, "aws4_request")
		signature = hmac.new(key_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

		request.headers["Authorization"] = (
			f"AWS4-HMAC-SHA256 Credential={self.aws_access_key_id}/{credential_scope},"
			f" SignedHeaders={signed_headers}, Signature={signature}"
		)
		return request


class SPAPIError(Exception):
	"""Main SP-API Exception class."""

	def __init__(self, *args, **kwargs) -> None:
		self.error = kwargs.get("error", "-")
		self.error_description = kwargs.get("error_description", "-")
		if not args and ("error" in kwargs or "error_description" in kwargs):
			msg = (
				f"{self.error}: {self.error_description}"
				if self.error_description != "-"
				else str(self.error)
			)
			args = (msg,)
		super().__init__(*args)


class SPAPI:
	"""Base Amazon SP-API class."""

	AUTH_URL = "https://api.amazon.com/auth/o2/token"
	BASE_URI = "/"

	def __init__(
		self,
		iam_arn: str,
		client_id: str,
		client_secret: str,
		refresh_token: str,
		aws_access_key: str,
		aws_secret_key: str,
		country_code: str = "US",
		use_sandbox: bool = False,
		custom_endpoint: str | None = None,
	) -> None:
		self.iam_arn = iam_arn
		self.client_id = client_id
		self.client_secret = client_secret
		self.refresh_token = refresh_token
		self.aws_access_key = aws_access_key
		self.aws_secret_key = aws_secret_key
		self.country_code = country_code
		self.use_sandbox = bool(use_sandbox)
		self.custom_endpoint = (
			custom_endpoint.strip().rstrip("/")
			if isinstance(custom_endpoint, str) and custom_endpoint.strip()
			else None
		)
		self.region, self.endpoint, self.marketplace_id = Util.get_marketplace_data(
			country_code, use_sandbox=self.use_sandbox
		)
		if self.custom_endpoint:
			self.endpoint = self.custom_endpoint

	def get_access_token(self) -> str:
		data = {
			"grant_type": "refresh_token",
			"client_id": self.client_id,
			"client_secret": self.client_secret,
			"refresh_token": self.refresh_token,
		}
		auth_url = f"{self.custom_endpoint}/auth/o2/token" if self.custom_endpoint else self.AUTH_URL
		response = request(method="POST", url=auth_url, data=data, timeout=DEFAULT_SPAPI_TIMEOUT)
		result = response.json()
		if response.status_code == 200:
			return result.get("access_token")
		raise SPAPIError(error=result.get("error"), error_description=result.get("error_description"))

	def get_auth(self) -> AWSSigV4:
		if self.custom_endpoint:
			return AWSSigV4(
				service="execute-api",
				aws_access_key_id=self.aws_access_key or "mock-access-key",
				aws_secret_access_key=self.aws_secret_key or "mock-secret-key",
				region=self.region,
			)

		try:
			client = boto3.client(
				"sts",
				aws_access_key_id=self.aws_access_key,
				aws_secret_access_key=self.aws_secret_key,
				region_name=self.region,
			)
			response = client.assume_role(RoleArn=self.iam_arn, RoleSessionName="SellingPartnerAPI")
			credentials = response["Credentials"]
			return AWSSigV4(
				service="execute-api",
				aws_access_key_id=credentials["AccessKeyId"],
				aws_secret_access_key=credentials["SecretAccessKey"],
				aws_session_token=credentials["SessionToken"],
				region=self.region,
			)
		except Exception as e:
			raise SPAPIError(error="invalid_aws_credentials", error_description=e)

	def get_headers(self, restricted_data_token: str | None = None) -> dict:
		return {"x-amz-access-token": restricted_data_token or self.get_access_token()}

	def make_request(
		self,
		method: str = "GET",
		append_to_base_uri: str = "",
		params: dict | None = None,
		data: dict | None = None,
		json_data: dict | None = None,
		restricted_data_token: str | None = None,
		timeout: tuple[int, int] | int = DEFAULT_SPAPI_TIMEOUT,
	) -> dict:
		if isinstance(params, dict):
			params = Util.remove_empty(params)
		if isinstance(data, dict):
			data = Util.remove_empty(data)
		if isinstance(json_data, dict):
			json_data = Util.remove_empty(json_data)

		url = self.endpoint + self.BASE_URI + append_to_base_uri
		headers = (
			self.get_headers(restricted_data_token=restricted_data_token)
			if restricted_data_token
			else self.get_headers()
		)
		if method.upper() == "DELETE" and "Content-Type" not in headers:
			headers = {**headers, "Content-Type": "application/json; charset=utf-8"}

		response = request(
			method=method,
			url=url,
			params=params,
			data=data,
			json=json_data,
			headers=headers,
			auth=self.get_auth(),
			timeout=timeout,
		)

		if response.status_code == 204 or not response.content or not response.content.strip():
			result = {}
		else:
			try:
				result = response.json()
			except ValueError:
				result = {}

		if response.status_code >= 400:
			errors = result.get("errors") if isinstance(result, dict) else None
			if errors and isinstance(errors, list):
				first_error = errors[0]
				raise SPAPIError(
					error=first_error.get("code", str(response.status_code)),
					error_description=first_error.get("details") or first_error.get("message", response.text),
				)
			raise SPAPIError(error=str(response.status_code), error_description=response.text)

		return result

	def list_to_dict(self, key: str, values: list | tuple | set | str | None, data: dict) -> None:
		if not values:
			return
		if isinstance(values, (list, tuple, set)):
			items = [str(v) for v in values if v is not None and str(v) != ""]
			if items:
				data[key] = ",".join(items)
		else:
			data[key] = str(values)


class Tokens(SPAPI):
	"""Amazon SP-API Tokens API (v2021-03-01) for Restricted Data Tokens (RDT)."""

	BASE_URI = "/tokens/2021-03-01"

	def create_restricted_data_token(
		self,
		restricted_resources: list[dict],
		target_application: str | None = None,
	) -> dict:
		"""Requests an RDT scoped to the given restrictedResources."""
		body = {
			"restrictedResources": restricted_resources,
			"targetApplication": target_application,
		}
		return self.make_request(
			method="POST",
			append_to_base_uri="/restrictedDataToken",
			json_data=body,
		)


class Finances(SPAPI):
	"""Amazon Finances API."""

	BASE_URI = "/finances/v0/"

	def list_financial_events_by_order_id(
		self, order_id: str, max_results: int | None = None, next_token: str | None = None
	) -> dict:
		append_to_base_uri = f"orders/{quote(str(order_id), safe='')}/financialEvents"
		data = dict(MaxResultsPerPage=max_results, NextToken=next_token)
		return self.make_request(append_to_base_uri=append_to_base_uri, params=data)

	def list_financial_events(
		self,
		max_results_per_page: int | None = None,
		posted_after: str | None = None,
		posted_before: str | None = None,
		next_token: str | None = None,
	) -> dict:
		data = dict(
			MaxResultsPerPage=max_results_per_page,
			PostedAfter=posted_after,
			PostedBefore=posted_before,
			NextToken=next_token,
		)
		return self.make_request(append_to_base_uri="financialEvents", params=data)

	def list_financial_event_groups(
		self,
		max_results_per_page: int | None = None,
		financial_event_group_started_before: str | None = None,
		financial_event_group_started_after: str | None = None,
		next_token: str | None = None,
	) -> dict:
		data = dict(
			MaxResultsPerPage=max_results_per_page,
			FinancialEventGroupStartedBefore=financial_event_group_started_before,
			FinancialEventGroupStartedAfter=financial_event_group_started_after,
			NextToken=next_token,
		)
		return self.make_request(append_to_base_uri="financialEventGroups", params=data)

	def list_financial_events_by_group_id(
		self,
		event_group_id: str,
		max_results_per_page: int | None = None,
		posted_after: str | None = None,
		posted_before: str | None = None,
		next_token: str | None = None,
	) -> dict:
		append_to_base_uri = f"financialEventGroups/{quote(str(event_group_id), safe='')}/financialEvents"
		data = dict(
			MaxResultsPerPage=max_results_per_page,
			PostedAfter=posted_after,
			PostedBefore=posted_before,
			NextToken=next_token,
		)
		return self.make_request(append_to_base_uri=append_to_base_uri, params=data)


class Orders(SPAPI):
	"""Amazon Orders API."""

	BASE_URI = "/orders/v0/orders"

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
		restricted_data_token: str | None = None,
	) -> dict:
		data = dict(
			CreatedAfter=created_after,
			CreatedBefore=created_before,
			LastUpdatedAfter=last_updated_after,
			LastUpdatedBefore=last_updated_before,
			BuyerEmail=buyer_email,
			SellerOrderId=seller_order_id,
			MaxResultsPerPage=max_results,
			NextToken=next_token,
			ActualFulfillmentSupplySourceId=actual_fulfillment_supply_source_id,
			IsISPU=is_ispu,
			StoreChainStoreId=store_chain_store_id,
		)

		if not marketplace_ids:
			marketplace_ids = [self.marketplace_id]

		self.list_to_dict("OrderStatuses", order_statuses, data)
		self.list_to_dict("MarketplaceIds", marketplace_ids, data)
		self.list_to_dict("FulfillmentChannels", fulfillment_channels, data)
		self.list_to_dict("PaymentMethods", payment_methods, data)
		self.list_to_dict("EasyShipShipmentStatuses", easyship_shipment_statuses, data)
		self.list_to_dict("AmazonOrderIds", amazon_order_ids, data)

		rdt_kwargs = {"restricted_data_token": restricted_data_token} if restricted_data_token else {}
		return self.make_request(params=data, **rdt_kwargs)

	def _get_order_subresource(
		self, order_id: str, suffix: str = "", restricted_data_token: str | None = None
	) -> dict:
		rdt_kwargs = {"restricted_data_token": restricted_data_token} if restricted_data_token else {}
		return self.make_request(
			append_to_base_uri=f"/{quote(str(order_id), safe='')}{suffix}",
			**rdt_kwargs,
		)

	def get_order(self, order_id: str, restricted_data_token: str | None = None) -> dict:
		return self._get_order_subresource(order_id, restricted_data_token=restricted_data_token)

	def get_order_buyer_info(self, order_id: str, restricted_data_token: str | None = None) -> dict:
		return self._get_order_subresource(
			order_id, suffix="/buyerInfo", restricted_data_token=restricted_data_token
		)

	def get_order_address(self, order_id: str, restricted_data_token: str | None = None) -> dict:
		return self._get_order_subresource(
			order_id, suffix="/address", restricted_data_token=restricted_data_token
		)

	def get_order_items(self, order_id: str, next_token: str | None = None) -> dict:
		append_to_base_uri = f"/{quote(str(order_id), safe='')}/orderItems"
		data = dict(NextToken=next_token)
		return self.make_request(append_to_base_uri=append_to_base_uri, params=data)

	def confirm_shipment(
		self,
		order_id: str,
		marketplace_id: str | None = None,
		package_detail: dict | None = None,
	) -> dict:
		if not marketplace_id:
			marketplace_id = self.marketplace_id

		detail = dict(package_detail or {})
		key_map = {
			"package_reference_id": "packageReferenceId",
			"carrier_code": "carrierCode",
			"carrier_name": "carrierName",
			"tracking_number": "trackingNumber",
			"ship_date": "shipDate",
			"order_items": "orderItems",
			"shipping_method": "shippingMethod",
			"ship_from_supply_source_id": "shipFromSupplySourceId",
		}
		for snake, camel in key_map.items():
			if snake in detail and camel not in detail:
				detail[camel] = detail.pop(snake)

		if "orderItems" in detail and isinstance(detail["orderItems"], list):
			normalized_items = []
			for item in detail["orderItems"]:
				if isinstance(item, dict):
					item_dict = dict(item)
					if "order_item_id" in item_dict and "orderItemId" not in item_dict:
						item_dict["orderItemId"] = item_dict.pop("order_item_id")
					if "transparency_codes" in item_dict and "transparencyCodes" not in item_dict:
						item_dict["transparencyCodes"] = item_dict.pop("transparency_codes")
					normalized_items.append(item_dict)
				else:
					normalized_items.append(item)
			detail["orderItems"] = normalized_items

		payload = {
			"marketplaceId": marketplace_id,
			"packageDetail": detail,
		}

		append_to_base_uri = f"/{quote(str(order_id), safe='')}/shipmentConfirmation"
		return self.make_request(
			method="POST",
			append_to_base_uri=append_to_base_uri,
			json_data=payload,
		)


class CatalogItems(SPAPI):
	"""Amazon Catalog Items API."""

	BASE_URI = "/catalog/v0"

	def get_catalog_item(self, asin: str, marketplace_id: str | None = None) -> dict:
		if not marketplace_id:
			marketplace_id = self.marketplace_id
		append_to_base_uri = f"/items/{asin}"
		return self.make_request(append_to_base_uri=append_to_base_uri, params={"MarketplaceId": marketplace_id})


class CatalogItemsV2(SPAPI):
	"""Amazon Catalog Items API (v2022-04-01)."""

	BASE_URI = "/catalog/2022-04-01"

	def get_catalog_item(
		self,
		asin: str,
		marketplace_ids: list | None = None,
		included_data: list | None = None,
	) -> dict:
		if not marketplace_ids:
			marketplace_ids = [self.marketplace_id]
		if not included_data:
			included_data = [
				"summaries",
				"classifications",
				"images",
				"dimensions",
				"identifiers",
				"productTypes",
			]

		append_to_base_uri = f"/items/{asin}"
		params = {
			"marketplaceIds": ",".join(marketplace_ids),
			"includedData": ",".join(included_data),
		}
		return self.make_request(append_to_base_uri=append_to_base_uri, params=params)


class Reports(SPAPI):
	"""Amazon Reports API (v2021-06-30)."""

	BASE_URI = "/reports/2021-06-30"

	def create_report(
		self,
		report_type: str,
		marketplace_ids: list | None = None,
		data_start_time: str | None = None,
		data_end_time: str | None = None,
		report_options: dict | None = None,
	) -> dict:
		body = {
			"reportType": report_type,
			"marketplaceIds": marketplace_ids or [self.marketplace_id],
			"dataStartTime": data_start_time,
			"dataEndTime": data_end_time,
			"reportOptions": report_options,
		}
		return self.make_request(method="POST", append_to_base_uri="/reports", json_data=body)

	def get_report(self, report_id: str) -> dict:
		return self.make_request(append_to_base_uri=f"/reports/{report_id}")

	def get_report_document(self, report_document_id: str) -> dict:
		return self.make_request(append_to_base_uri=f"/documents/{report_document_id}")

	def download_report_document(self, report_document: dict) -> str:
		url = report_document.get("url")
		if not url:
			raise SPAPIError(error="missing_report_url", error_description="Report document has no download URL.")

		parsed = urlparse(str(url).strip())
		hostname = (parsed.hostname or "").lower()
		if parsed.scheme not in ("http", "https") or not hostname or hostname in {
			"169.254.169.254",
			"100.100.100.200",
			"metadata.google.internal",
			"fd00:ec2::254",
		}:
			raise SPAPIError(error="invalid_report_url", error_description="Invalid or disallowed report download URL.")

		# Pre-signed S3/CloudFront URLs reject SP-API auth headers.
		response = request(method="GET", url=url, timeout=DEFAULT_SPAPI_TIMEOUT)
		if response.status_code >= 400:
			raise SPAPIError(error=str(response.status_code), error_description=response.text)

		raw_bytes = response.content
		if raw_bytes.startswith(b"\x1f\x8b"):
			raw_bytes = gzip.decompress(raw_bytes)

		try:
			return raw_bytes.decode("utf-8-sig")
		except UnicodeDecodeError:
			return raw_bytes.decode("cp1252", errors="replace")


class ListingsItems(SPAPI):
	"""Amazon Listings Items API (v2021-08-01)."""

	BASE_URI = "/listings/2021-08-01/items"

	@staticmethod
	def _normalize_marketplaces(marketplace_ids: list | str | None, fallback: str) -> str:
		if not marketplace_ids:
			return fallback
		if isinstance(marketplace_ids, (list, tuple, set)):
			return ",".join(marketplace_ids)
		return str(marketplace_ids)

	def get_listings_item(
		self,
		seller_id: str,
		sku: str,
		marketplace_ids: list | str | None = None,
		issue_locale: str | None = None,
		included_data: list | str | None = None,
	) -> dict:
		params = {"marketplaceIds": self._normalize_marketplaces(marketplace_ids, self.marketplace_id)}
		if issue_locale:
			params["issueLocale"] = issue_locale
		if included_data:
			params["includedData"] = (
				",".join(included_data) if isinstance(included_data, (list, tuple, set)) else str(included_data)
			)

		append_to_base_uri = f"/{quote(str(seller_id), safe='')}/{quote(str(sku), safe='')}"
		return self.make_request(append_to_base_uri=append_to_base_uri, params=params)

	def put_listings_item(
		self,
		seller_id: str,
		sku: str,
		marketplace_ids: list | str | None = None,
		body: dict | None = None,
		issue_locale: str | None = None,
	) -> dict:
		params = {"marketplaceIds": self._normalize_marketplaces(marketplace_ids, self.marketplace_id)}
		if issue_locale:
			params["issueLocale"] = issue_locale

		payload = dict(body or {})
		if not payload.get("requirements"):
			is_generic_product = str(payload.get("productType") or "").strip().upper() == "PRODUCT"
			payload["requirements"] = "LISTING_OFFER_ONLY" if is_generic_product else "LISTING"

		append_to_base_uri = f"/{quote(str(seller_id), safe='')}/{quote(str(sku), safe='')}"
		return self.make_request(
			method="PUT",
			append_to_base_uri=append_to_base_uri,
			params=params,
			json_data=payload,
		)

	def patch_listings_item(
		self,
		seller_id: str,
		sku: str,
		marketplace_ids: list | str | None = None,
		body: dict | None = None,
		issue_locale: str | None = None,
	) -> dict:
		params = {"marketplaceIds": self._normalize_marketplaces(marketplace_ids, self.marketplace_id)}
		if issue_locale:
			params["issueLocale"] = issue_locale

		append_to_base_uri = f"/{quote(str(seller_id), safe='')}/{quote(str(sku), safe='')}"
		return self.make_request(
			method="PATCH",
			append_to_base_uri=append_to_base_uri,
			params=params,
			json_data=body or {},
		)

	def delete_listings_item(
		self,
		seller_id: str,
		sku: str,
		marketplace_ids: list | str | None = None,
		issue_locale: str | None = None,
	) -> dict:
		params = {"marketplaceIds": self._normalize_marketplaces(marketplace_ids, self.marketplace_id)}
		if issue_locale:
			params["issueLocale"] = issue_locale

		append_to_base_uri = f"/{quote(str(seller_id), safe='')}/{quote(str(sku), safe='')}"
		return self.make_request(
			method="DELETE",
			append_to_base_uri=append_to_base_uri,
			params=params,
		)


class ProductTypeDefinitions(SPAPI):
	"""Amazon Product Type Definitions API (v2020-09-01)."""

	BASE_URI = "/definitions/2020-09-01/productTypes"

	def get_definitions_product_type(
		self,
		product_type: str,
		marketplace_ids: list | str | None = None,
		seller_id: str | None = None,
		product_type_version: str = "LATEST",
		requirements: str = "LISTING",
	) -> dict:
		if not marketplace_ids:
			marketplace_ids = [self.marketplace_id]
		if isinstance(marketplace_ids, (list, tuple, set)):
			marketplace_ids = ",".join(marketplace_ids)

		params = {
			"marketplaceIds": marketplace_ids,
			"productTypeVersion": product_type_version,
			"requirements": requirements,
		}
		if seller_id:
			params["sellerId"] = seller_id

		append_to_base_uri = f"/{quote(str(product_type), safe='')}"
		return self.make_request(append_to_base_uri=append_to_base_uri, params=params)


class Util:
	@staticmethod
	def get_marketplace(country_code):
		for selling_region in MARKETPLACES:
			for country in MARKETPLACES.get(selling_region):
				if country_code == country:
					return MARKETPLACES.get(selling_region)
		raise KeyError(f"Invalid Country Code: {country_code}")

	@staticmethod
	def get_marketplace_data(country_code, use_sandbox: bool = False):
		marketplace = Util.get_marketplace(country_code)
		region = marketplace.get("AWS Region")
		endpoint = marketplace.get("Endpoint")
		if use_sandbox and endpoint:
			endpoint = endpoint.replace("https://", "https://sandbox.")
		marketplace_id = marketplace.get(country_code)
		return region, endpoint, marketplace_id

	@staticmethod
	def remove_empty(data_dict):
		"""Remove keys whose values are None or empty containers/strings, preserving False and 0."""
		for key in list(data_dict):
			val = data_dict[key]
			is_empty = val is None or (
				not isinstance(val, bool)
				and isinstance(val, str | list | tuple | set | dict)
				and len(val) == 0
			)
			if is_empty:
				del data_dict[key]
		return data_dict

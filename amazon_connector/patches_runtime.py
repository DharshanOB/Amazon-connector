# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe

_PATCHES_APPLIED = False
_ORIGINAL_SCHEDULER = None


def _is_amazon_connector_installed() -> bool:
	try:
		if getattr(frappe.local, "site", None):
			return "amazon_connector" in frappe.get_installed_apps()
	except Exception:
		pass
	return True


def _noop_scheduler(*args, **kwargs):
	if not _is_amazon_connector_installed() and callable(_ORIGINAL_SCHEDULER):
		return _ORIGINAL_SCHEDULER(*args, **kwargs)
	return None


def _safe_create_tax_account():
	if not frappe.flags.in_test:
		return

	company = "Wind Power LLC"
	if not frappe.db.exists("Company", company):
		return

	account_name = "Output Tax GST"
	if frappe.db.exists("Account", {"account_name": account_name, "company": company}):
		return

	parent = (
		frappe.db.get_value("Account", {"company": company, "account_type": "Tax", "is_group": 1})
		or frappe.db.get_value("Account", {"company": company, "account_name": "Duties and Taxes"})
	)
	if not parent:
		return

	account = frappe.get_doc(
		{
			"doctype": "Account",
			"account_name": account_name,
			"account_type": "Tax",
			"company": company,
			"parent_account": parent,
		}
	)
	account.insert(ignore_permissions=True)


def apply_amazon_patches(*args, **kwargs):
	"""Idempotently bind upstream ecommerce_integrations references to amazon_connector once per process."""
	global _PATCHES_APPLIED
	if _PATCHES_APPLIED:
		return

	import ecommerce_integrations.amazon.doctype.amazon_sp_api_settings.amazon_repository as upstream_repo
	import ecommerce_integrations.amazon.doctype.amazon_sp_api_settings.amazon_sp_api as upstream_sp_api
	import ecommerce_integrations.amazon.doctype.amazon_sp_api_settings.amazon_sp_api_settings as upstream_settings
	import ecommerce_integrations.ecommerce_integrations.doctype.ecommerce_integration_log.ecommerce_integration_log as upstream_log

	from amazon_connector.amazon_ext import log_patch, repository, sp_api

	if not hasattr(upstream_repo, "_OriginalAmazonRepository"):
		upstream_repo._OriginalAmazonRepository = upstream_repo.AmazonRepository

	upstream_repo.AmazonRepository = repository.CustomAmazonRepository
	upstream_repo.get_orders = repository.get_orders
	upstream_repo.get_delivery_notes = repository.get_delivery_notes
	upstream_repo.get_listings = repository.get_listings
	upstream_repo.validate_amazon_sp_api_credentials = repository.validate_amazon_sp_api_credentials
	upstream_repo.resolve_custom_endpoint = repository.resolve_custom_endpoint

	for attr in (
		"AWSSigV4",
		"SPAPIError",
		"SPAPI",
		"Tokens",
		"Finances",
		"Orders",
		"CatalogItems",
		"CatalogItemsV2",
		"Reports",
		"ListingsItems",
		"ProductTypeDefinitions",
		"Util",
	):
		setattr(upstream_sp_api, attr, getattr(sp_api, attr))
	upstream_sp_api.__all__ = list(sp_api.__all__)

	# Disable upstream hourly order sync so hourly_long in amazon_connector runs without duplication.
	global _ORIGINAL_SCHEDULER
	if _ORIGINAL_SCHEDULER is None and upstream_settings.schedule_get_order_details is not _noop_scheduler:
		_ORIGINAL_SCHEDULER = upstream_settings.schedule_get_order_details
	upstream_settings.schedule_get_order_details = _noop_scheduler

	upstream_log.create_log = log_patch.create_log
	upstream_log._retry_job = log_patch._retry_job
	upstream_log.resync = log_patch.resync
	upstream_log.bulk_retry = log_patch.bulk_retry

	_PATCHES_APPLIED = True


def before_tests(*args, **kwargs):
	import ecommerce_integrations.utils.before_test as upstream_before_test

	apply_amazon_patches()
	upstream_before_test.create_tax_account = _safe_create_tax_account
	upstream_before_test.before_tests()

# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import dateutil
import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, now, strip_html, today

from amazon_connector.amazon_ext.log_patch import create_log

AMAZON_MODULE_DEF = "Amazon"


def get_marketplace_id_for_setting(amz_setting) -> str | None:
	from amazon_connector.amazon_ext.sp_api import Util

	try:
		_, _, marketplace_id = Util.get_marketplace_data(
			amz_setting.country, bool(amz_setting.get("use_sandbox"))
		)
		return marketplace_id
	except Exception:
		return None


def update_setting_sync_timestamp(amz_setting, fieldname: str) -> None:
	setting_name = getattr(amz_setting, "name", None)
	if setting_name and frappe.db.exists("Amazon SP API Settings", setting_name):
		if frappe.db.has_column("Amazon SP API Settings", fieldname):
			frappe.db.set_value(
				"Amazon SP API Settings", setting_name, fieldname, now(), update_modified=False
			)


def resolve_valid_posting_date(company: str, candidate_date=None, min_date=None) -> str:
	from erpnext.accounts.utils import FiscalYearError, get_fiscal_year

	target = getdate(candidate_date or today())
	if min_date and target < getdate(min_date):
		target = getdate(min_date)

	try:
		get_fiscal_year(target, company=company)
		return target.strftime("%Y-%m-%d")
	except FiscalYearError:
		fallback = getdate(today())
		if min_date and fallback < getdate(min_date):
			fallback = getdate(min_date)
		try:
			get_fiscal_year(fallback, company=company)
			return fallback.strftime("%Y-%m-%d")
		except FiscalYearError:
			fy = frappe.db.get_value(
				"Fiscal Year Company",
				{"company": company},
				"parent",
				order_by="creation desc",
			) or frappe.db.get_value("Fiscal Year", {"disabled": 0}, "name", order_by="year_end_date desc")
			if fy:
				year_end = frappe.db.get_value("Fiscal Year", fy, "year_end_date")
				if year_end:
					resolved = getdate(year_end)
					if min_date and resolved < getdate(min_date):
						resolved = getdate(min_date)
					return resolved.strftime("%Y-%m-%d")
		return target.strftime("%Y-%m-%d")


def _find_parent_account_group(
	company: str, root_type: str, preferred_parent: str | None = None, avoid_party_types: bool = True
) -> str | None:
	if preferred_parent:
		parent_info = frappe.db.get_value(
			"Account",
			preferred_parent,
			["company", "is_group", "root_type", "account_type"],
			as_dict=True,
		)
		if (
			parent_info
			and parent_info.company == company
			and parent_info.is_group
			and parent_info.root_type == root_type
			and (not avoid_party_types or parent_info.account_type not in ("Receivable", "Payable"))
		):
			return preferred_parent

	preferred_names = {
		"Asset": ("Bank Accounts", "Current Assets"),
		"Expense": ("Indirect Expenses", "Expenses"),
		"Income": ("Direct Income", "Indirect Income"),
		"Liability": ("Duties and Taxes", "Current Liabilities"),
	}.get(root_type, ())

	groups = frappe.get_all(
		"Account",
		filters={"company": company, "is_group": 1, "root_type": root_type},
		fields=["name", "account_name", "account_type"],
		order_by="lft asc",
	)
	for candidate in preferred_names:
		for g in groups:
			if g.account_name == candidate and (
				not avoid_party_types or g.account_type not in ("Receivable", "Payable")
			):
				return g.name

	for g in groups:
		if not avoid_party_types or g.account_type not in ("Receivable", "Payable"):
			return g.name

	return groups[0].name if groups else None


def get_or_create_leaf_account(
	company: str,
	account_name: str,
	root_type: str,
	account_type: str | None = None,
	preferred_parent: str | None = None,
) -> str:
	existing = frappe.db.get_value(
		"Account", {"account_name": account_name, "company": company, "is_group": 0}, "name"
	)
	if existing:
		return existing

	parent_account = _find_parent_account_group(company, root_type, preferred_parent=preferred_parent)
	if not parent_account:
		frappe.throw(
			_("Could not find a group {0} account for company {1}.").format(root_type, company)
		)

	acc = frappe.new_doc("Account")
	acc.account_name = account_name
	acc.company = company
	acc.parent_account = parent_account
	acc.root_type = root_type
	if account_type is not None:
		acc.account_type = account_type
	acc.insert(ignore_permissions=True)
	return acc.name


def create_sales_invoice_from_sales_order(repo, sales_order_name: str) -> str | None:
	from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

	so = frappe.get_doc("Sales Order", sales_order_name)
	if so.docstatus != 1 or cint(so.get("amazon_canceled")) or so.status == "Closed":
		return None

	amazon_order_id = so.get("amazon_order_id")
	if amazon_order_id:
		existing_si = frappe.db.get_value(
			"Sales Invoice",
			{"amazon_order_id": amazon_order_id, "is_return": 0, "docstatus": ["!=", 2]},
			"name",
		)
		if existing_si:
			return existing_si

	si = make_sales_invoice(sales_order_name)
	if not si.items:
		return frappe.db.get_value(
			"Sales Invoice Item", {"sales_order": sales_order_name, "docstatus": 1}, "parent"
		)

	si.set_posting_time = 1
	si.posting_date = resolve_valid_posting_date(so.company, so.transaction_date, min_date=so.transaction_date)
	si.update_stock = 0
	if frappe.db.has_column("Sales Invoice", "amazon_order_id"):
		si.amazon_order_id = amazon_order_id
	if frappe.db.has_column("Sales Invoice", "imported_from_amazon"):
		si.imported_from_amazon = 1
	if frappe.db.has_column("Sales Invoice", "marketplace_id"):
		si.marketplace_id = so.get("marketplace_id") or get_marketplace_id_for_setting(repo.amz_setting)

	si.flags.from_amazon_sync = True
	si.flags.ignore_permissions = True
	si.insert(ignore_permissions=True)
	si.submit()
	return si.name


def cancel_erpnext_order_from_amazon(repo, order: dict, sales_order_name: str) -> str | None:
	so = frappe.get_doc("Sales Order", sales_order_name)
	order_id = order.get("AmazonOrderId") or so.get("amazon_order_id")

	if frappe.db.has_column("Sales Order", "amazon_order_status"):
		so.db_set("amazon_order_status", "Canceled", update_modified=False)
	if frappe.db.has_column("Sales Order", "amazon_canceled"):
		so.db_set("amazon_canceled", 1, update_modified=False)

	if so.docstatus == 2:
		return so.name

	linked_draft_dns = frappe.get_all(
		"Delivery Note Item",
		filters={"against_sales_order": so.name, "docstatus": 0},
		pluck="parent",
		distinct=True,
	)
	for dn_name in linked_draft_dns:
		if frappe.db.get_value("Delivery Note", dn_name, "imported_from_amazon"):
			frappe.delete_doc("Delivery Note", dn_name, ignore_permissions=True)

	submitted_dn = frappe.db.get_value(
		"Delivery Note Item", {"against_sales_order": so.name, "docstatus": 1}, "parent"
	)
	submitted_si = frappe.db.get_value(
		"Sales Invoice Item", {"sales_order": so.name, "docstatus": 1}, "parent"
	)

	if submitted_dn or submitted_si or flt(so.per_delivered) > 0 or flt(so.per_billed) > 0:
		create_log(
			module_def=AMAZON_MODULE_DEF,
			status="Error",
			method="amazon_connector.amazon_ext.lifecycle.sync_cancellations",
			request_data={"amazon_order_id": order_id, "sales_order": so.name},
			message=(
				f"Cannot automatically cancel Sales Order {so.name} for canceled Amazon order {order_id}: "
				f"linked submitted Delivery Note ({submitted_dn or '-'}) or Sales Invoice ({submitted_si or '-'}) exists."
			),
			make_new=True,
		)
		return so.name

	if so.docstatus == 1:
		so.flags.from_amazon_sync = True
		so.flags.ignore_permissions = True
		so.cancel()
	elif so.docstatus == 0:
		frappe.delete_doc("Sales Order", so.name, ignore_permissions=True)
		return None

	return so.name


def handle_sales_order_cancel(doc, method=None):
	"""Keep Amazon custom status fields consistent when an ERPNext Sales Order is canceled."""
	if getattr(doc.flags, "from_amazon_sync", False):
		return
	if not doc.get("amazon_order_id"):
		return
	if frappe.db.has_column("Sales Order", "amazon_canceled"):
		doc.db_set("amazon_canceled", 1, update_modified=False)


def _to_iso_param(value) -> str | None:
	if not value:
		return None
	if isinstance(value, str) and "T" in value:
		return value
	parsed = dateutil.parser.parse(str(value))
	return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def _iter_financial_events(repo, posted_after=None, posted_before=None):
	finances = repo.get_finances_instance()
	iso_after = _to_iso_param(posted_after)
	iso_before = _to_iso_param(posted_before)
	next_token = None
	seen_tokens = set()

	while True:
		payload = repo.call_sp_api_method(
			sp_api_method=finances.list_financial_events,
			posted_after=iso_after,
			posted_before=iso_before,
			next_token=next_token,
		) or {}
		yield payload.get("FinancialEvents") or {}
		next_token = payload.get("NextToken")
		if not next_token or next_token in seen_tokens:
			break
		seen_tokens.add(next_token)


def _iter_financial_event_groups(repo, started_after=None, started_before=None):
	finances = repo.get_finances_instance()
	iso_after = _to_iso_param(started_after)
	iso_before = _to_iso_param(started_before)
	next_token = None
	seen_tokens = set()

	while True:
		payload = repo.call_sp_api_method(
			sp_api_method=finances.list_financial_event_groups,
			financial_event_group_started_after=iso_after,
			financial_event_group_started_before=iso_before,
			next_token=next_token,
		) or {}
		for group in payload.get("FinancialEventGroupList") or []:
			yield group
		next_token = payload.get("NextToken")
		if not next_token or next_token in seen_tokens:
			break
		seen_tokens.add(next_token)


def _fetch_group_financial_events(repo, event_group_id: str) -> list[dict]:
	finances = repo.get_finances_instance()
	events = []
	next_token = None
	seen_tokens = set()

	while True:
		payload = repo.call_sp_api_method(
			sp_api_method=finances.list_financial_events_by_group_id,
			event_group_id=event_group_id,
			next_token=next_token,
		) or {}
		events.append(payload.get("FinancialEvents") or {})
		next_token = payload.get("NextToken")
		if not next_token or next_token in seen_tokens:
			break
		seen_tokens.add(next_token)

	return events


def _build_return_id(refund_event: dict) -> str:
	order_id = refund_event.get("AmazonOrderId") or "UNKNOWN"
	posted_date = str(refund_event.get("PostedDate") or "").strip()
	item_keys = [
		str(item.get("OrderAdjustmentItemId") or item.get("SellerSKU") or "")
		for item in (refund_event.get("ShipmentItemAdjustmentList") or [])
	]
	suffix = "-".join([k for k in item_keys if k]) or "REF"
	raw = f"{order_id}:{posted_date}:{suffix}"
	return raw[:140]


def _maybe_create_return_delivery_note(repo, order_id: str, sku_qtys: dict[str, float], posting_date: str) -> str | None:
	from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_return as make_dn_return

	submitted_dn = frappe.db.get_value(
		"Delivery Note",
		{"amazon_order_id": order_id, "is_return": 0, "docstatus": 1},
		"name",
		order_by="creation desc",
	)
	if not submitted_dn:
		return None

	already_returned = frappe.db.exists(
		"Delivery Note",
		{"return_against": submitted_dn, "is_return": 1, "docstatus": ["!=", 2]},
	)
	if already_returned:
		return None

	dn_return = make_dn_return(submitted_dn)
	filtered_items = []
	for row in dn_return.items:
		target_qty = sku_qtys.get(row.item_code) or sku_qtys.get(row.item_name)
		if target_qty is not None and target_qty > 0:
			row.qty = -min(abs(flt(row.qty)), flt(target_qty))
			filtered_items.append(row)
		elif not sku_qtys:
			filtered_items.append(row)

	if not filtered_items:
		return None
	dn_return.items = filtered_items
	dn_return.set_posting_time = 1
	dn_return.posting_date = posting_date
	dn_return.amazon_order_id = order_id
	dn_return.imported_from_amazon = 1
	if frappe.db.has_column("Delivery Note", "fulfillment_channel"):
		dn_return.fulfillment_channel = frappe.db.get_value(
			"Delivery Note", submitted_dn, "fulfillment_channel"
		)

	prev_flag = getattr(frappe.flags, "from_amazon_fba_sync", False)
	frappe.flags.from_amazon_fba_sync = True
	try:
		dn_return.flags.from_amazon_import = True
		dn_return.flags.ignore_permissions = True
		dn_return.insert(ignore_permissions=True)
		dn_return.submit()
	finally:
		frappe.flags.from_amazon_fba_sync = prev_flag
	return dn_return.name


def _create_credit_note_from_refund(repo, refund_event: dict) -> str | None:
	from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return as make_si_return

	order_id = refund_event.get("AmazonOrderId")
	if not order_id:
		return None

	return_id = _build_return_id(refund_event)
	existing = frappe.db.get_value(
		"Sales Invoice", {"amazon_return_id": return_id, "docstatus": ["!=", 2]}, "name"
	)
	if existing:
		return existing

	original_si_name = frappe.db.get_value(
		"Sales Invoice",
		{"amazon_order_id": order_id, "is_return": 0, "docstatus": 1},
		"name",
		order_by="creation desc",
	)
	if not original_si_name:
		so_name = frappe.db.get_value(
			"Sales Order", {"amazon_order_id": order_id, "docstatus": 1}, "name"
		)
		if so_name:
			original_si_name = create_sales_invoice_from_sales_order(repo, so_name)
	if not original_si_name:
		return None

	original_si = frappe.get_doc("Sales Invoice", original_si_name)
	adj_items = refund_event.get("ShipmentItemAdjustmentList") or []
	sku_qtys: dict[str, float] = {}
	sku_amounts: dict[str, float] = {}
	for adj in adj_items:
		sku = adj.get("SellerSKU")
		qty = abs(flt(adj.get("QuantityShipped")))
		principal = sum(
			abs(flt(c.get("ChargeAmount", {}).get("CurrencyAmount")))
			for c in (adj.get("ItemChargeAdjustmentList") or [])
			if c.get("ChargeType") == "Principal"
		)
		if sku:
			sku_qtys[sku] = sku_qtys.get(sku, 0.0) + (qty or 1.0)
			if principal > 0:
				sku_amounts[sku] = sku_amounts.get(sku, 0.0) + principal

	credit_note = make_si_return(original_si_name)
	matched_rows = []
	for row in credit_note.items:
		match_qty = sku_qtys.get(row.item_code) or sku_qtys.get(row.item_name)
		if match_qty:
			row.qty = -min(abs(flt(row.qty)), flt(match_qty))
			matched_rows.append(row)
		elif not sku_qtys:
			matched_rows.append(row)

	if not matched_rows:
		matched_rows = list(credit_note.items)
	credit_note.items = matched_rows

	posted_dt = refund_event.get("PostedDate")
	candidate_date = dateutil.parser.parse(posted_dt).date() if posted_dt else today()
	posting_date = resolve_valid_posting_date(
		original_si.company, candidate_date, min_date=original_si.posting_date
	)

	_maybe_create_return_delivery_note(repo, order_id, sku_qtys, posting_date)

	credit_note.set_posting_time = 1
	credit_note.posting_date = posting_date
	credit_note.update_stock = 0
	if frappe.db.has_column("Sales Invoice", "amazon_order_id"):
		credit_note.amazon_order_id = order_id
	if frappe.db.has_column("Sales Invoice", "amazon_return_id"):
		credit_note.amazon_return_id = return_id
	if frappe.db.has_column("Sales Invoice", "imported_from_amazon"):
		credit_note.imported_from_amazon = 1
	if frappe.db.has_column("Sales Invoice", "marketplace_id"):
		credit_note.marketplace_id = original_si.get("marketplace_id") or get_marketplace_id_for_setting(
			repo.amz_setting
		)

	credit_note.flags.from_amazon_sync = True
	credit_note.flags.ignore_permissions = True
	credit_note.insert(ignore_permissions=True)
	credit_note.submit()
	return credit_note.name


def sync_returns(amz_setting_name: str, posted_after=None, posted_before=None) -> list[str]:
	from amazon_connector.amazon_ext.repository import (
		CustomAmazonRepository as AmazonRepository,
	)

	repo = AmazonRepository(amz_setting_name)
	after = posted_after or repo.amz_setting.get("last_returns_sync") or repo.amz_setting.after_date
	created_credit_notes = []
	errors = []

	for fin_events in _iter_financial_events(repo, posted_after=after, posted_before=posted_before):
		for refund_event in fin_events.get("RefundEventList") or []:
			order_id = refund_event.get("AmazonOrderId") or "Unknown"
			frappe.db.savepoint("amazon_return_sync")
			try:
				cn_name = _create_credit_note_from_refund(repo, refund_event)
				if cn_name:
					created_credit_notes.append(cn_name)
			except Exception as e:
				frappe.db.rollback(save_point="amazon_return_sync")
				errors.append({"order_id": order_id, "error": strip_html(str(e))})
				frappe.log_error(
					message=frappe.get_traceback(),
					title=f"Amazon Return Sync failed for {order_id}",
				)

	update_setting_sync_timestamp(repo.amz_setting, "last_returns_sync")
	create_log(
		module_def=AMAZON_MODULE_DEF,
		status="Error" if (errors and not created_credit_notes) else "Success",
		method="amazon_connector.amazon_ext.lifecycle.sync_returns",
		request_data={"amz_setting_name": amz_setting_name, "posted_after": str(after or "")},
		response_data={"credit_notes": created_credit_notes, "errors": errors},
		message=f"Amazon returns sync: {len(created_credit_notes)} credit notes synced, {len(errors)} failed.",
		make_new=True,
	)
	return created_credit_notes


def _sum_group_events(event_pages: list[dict]) -> dict[str, flt | dict]:
	gross_sales_charges = 0.0
	fees_by_type: dict[str, float] = {}
	refund_total = 0.0
	adjustments_total = 0.0

	def _record_fee(fee_type: str, raw_amount: float):
		# In SP-API Finances, seller fees are typically negative (e.g. -150), while fee reversals are positive.
		# Track net fee expense magnitude (positive = expense debit).
		fees_by_type[fee_type] = fees_by_type.get(fee_type, 0.0) + (-flt(raw_amount))

	for page in event_pages:
		for ship_event in page.get("ShipmentEventList") or []:
			for order_charge in ship_event.get("OrderChargeList") or []:
				gross_sales_charges += flt(order_charge.get("ChargeAmount", {}).get("CurrencyAmount"))
			for order_fee in ship_event.get("OrderFeeList") or []:
				_record_fee(
					order_fee.get("FeeType") or "OrderFee",
					order_fee.get("FeeAmount", {}).get("CurrencyAmount"),
				)
			for ship_item in ship_event.get("ShipmentItemList") or []:
				for c in ship_item.get("ItemChargeList") or []:
					gross_sales_charges += flt(c.get("ChargeAmount", {}).get("CurrencyAmount"))
				for f in ship_item.get("ItemFeeList") or []:
					_record_fee(
						f.get("FeeType") or "SellerFee",
						f.get("FeeAmount", {}).get("CurrencyAmount"),
					)
				for p in ship_item.get("PromotionList") or []:
					gross_sales_charges += flt(p.get("PromotionAmount", {}).get("CurrencyAmount"))
				for w in ship_item.get("ItemTaxWithheldList") or []:
					for tc in w.get("TaxesWithheld") or []:
						gross_sales_charges += flt(tc.get("ChargeAmount", {}).get("CurrencyAmount"))

		for refund_event in page.get("RefundEventList") or []:
			for adj_item in refund_event.get("ShipmentItemAdjustmentList") or []:
				for c in adj_item.get("ItemChargeAdjustmentList") or []:
					amt = flt(c.get("ChargeAmount", {}).get("CurrencyAmount"))
					refund_total += abs(amt) if amt < 0 else -amt
				for f in adj_item.get("ItemFeeAdjustmentList") or []:
					_record_fee(
						f.get("FeeType") or "RefundFeeAdjustment",
						f.get("FeeAmount", {}).get("CurrencyAmount"),
					)

		for svc_fee in page.get("ServiceFeeEventList") or []:
			for f in svc_fee.get("FeeList") or []:
				_record_fee(
					f.get("FeeType") or "ServiceFee",
					f.get("FeeAmount", {}).get("CurrencyAmount"),
				)

		for adj_event in page.get("AdjustmentEventList") or []:
			adjustments_total += flt(adj_event.get("AdjustmentAmount", {}).get("CurrencyAmount"))

	return {
		"gross_sales": flt(gross_sales_charges, 2),
		"fees_by_type": fees_by_type,
		"refund_total": flt(refund_total, 2),
		"adjustments_total": flt(adjustments_total, 2),
	}


def _create_settlement_journal_entry(repo, group: dict) -> str | None:
	group_id = group.get("FinancialEventGroupId")
	if not group_id:
		return None

	existing = frappe.db.get_value(
		"Journal Entry", {"amazon_financial_event_group_id": group_id, "docstatus": ["!=", 2]}, "name"
	)
	if existing:
		return existing

	pages = _fetch_group_financial_events(repo, group_id)
	totals = _sum_group_events(pages)
	company = repo.amz_setting.company

	group_total_dict = group.get("OriginalTotal") or group.get("ConvertedTotal") or {}
	has_group_total = group_total_dict.get("CurrencyAmount") is not None
	total_fees = sum(totals["fees_by_type"].values())
	computed_payout = flt(
		totals["gross_sales"] - total_fees - totals["refund_total"] + totals["adjustments_total"], 2
	)
	payout_amount = flt(group_total_dict.get("CurrencyAmount"), 2) if has_group_total else computed_payout

	if not any((payout_amount, totals["gross_sales"], total_fees, totals["refund_total"], totals["adjustments_total"])):
		return None

	bank_account = get_or_create_leaf_account(
		company,
		"Amazon Marketplace Bank",
		root_type="Asset",
		account_type="Bank",
		preferred_parent=repo.amz_setting.get("market_place_account_group"),
	)
	clearing_account = get_or_create_leaf_account(
		company, "Amazon Marketplace Clearing", root_type="Asset", account_type=""
	)
	refund_account = get_or_create_leaf_account(
		company, "Amazon Refunds Clearing", root_type="Asset", account_type=""
	)
	adjustment_account = get_or_create_leaf_account(
		company, "Amazon Settlement Adjustments", root_type="Expense", account_type=""
	)

	end_raw = group.get("FinancialEventGroupEnd") or group.get("FundTransferDate") or group.get("FinancialEventGroupStart")
	candidate_date = dateutil.parser.parse(end_raw).date() if end_raw else today()
	posting_date = resolve_valid_posting_date(company, candidate_date)

	je = frappe.new_doc("Journal Entry")
	je.voucher_type = "Journal Entry"
	je.company = company
	je.posting_date = posting_date
	if frappe.db.has_column("Journal Entry", "amazon_financial_event_group_id"):
		je.amazon_financial_event_group_id = group_id
	je.user_remark = (
		f"Amazon Settlement {group_id} ({group.get('ProcessingStatus') or 'Closed'}, "
		f"Transfer: {group.get('FundTransferStatus') or 'Unknown'})"
	)

	def _add_row(account: str, signed_debit: float, remark: str):
		amount = flt(signed_debit, 2)
		if amount == 0:
			return
		je.append(
			"accounts",
			{
				"account": account,
				"debit_in_account_currency": amount if amount > 0 else 0.0,
				"credit_in_account_currency": abs(amount) if amount < 0 else 0.0,
				"user_remark": remark,
			},
		)

	_add_row(bank_account, payout_amount, f"Net payout for {group_id}")
	for fee_type, fee_amount in sorted(totals["fees_by_type"].items()):
		fee_acc = get_or_create_leaf_account(company, f"Amazon {fee_type}", root_type="Expense", account_type="")
		_add_row(fee_acc, fee_amount, f"{fee_type} for {group_id}")
	_add_row(refund_account, totals["refund_total"], f"Customer refunds for {group_id}")
	_add_row(clearing_account, -totals["gross_sales"], f"Gross marketplace sales clearing for {group_id}")
	_add_row(adjustment_account, -totals["adjustments_total"], f"Marketplace adjustments for {group_id}")

	total_debit = sum(flt(r.debit_in_account_currency) for r in je.accounts)
	total_credit = sum(flt(r.credit_in_account_currency) for r in je.accounts)
	imbalance = flt(total_credit - total_debit, 2)
	if imbalance != 0:
		frappe.logger("amazon_connector").warning(
			f"Amazon settlement discrepancy detected for group {group_id}: imbalance={imbalance}. "
			f"Posted balancing entry to {adjustment_account}."
		)
		frappe.log_error(
			title=f"Amazon Settlement Discrepancy ({group_id})",
			message=(
				f"FinancialEventGroupId {group_id} has balancing difference of {imbalance}. "
				f"Posted to {adjustment_account}. Total Debit={total_debit}, Total Credit={total_credit}."
			),
		)
		_add_row(adjustment_account, imbalance, f"Settlement balancing difference for {group_id}")

	if not je.accounts:
		return None

	je.flags.ignore_permissions = True
	je.insert(ignore_permissions=True)
	je.submit()
	return je.name


def sync_settlements(
	amz_setting_name: str, started_after=None, started_before=None, include_open: bool = False
) -> list[str]:
	from amazon_connector.amazon_ext.repository import (
		CustomAmazonRepository as AmazonRepository,
	)

	repo = AmazonRepository(amz_setting_name)
	after = started_after or repo.amz_setting.get("last_settlements_sync") or repo.amz_setting.after_date
	created_jes = []
	errors = []

	for group in _iter_financial_event_groups(
		repo, started_after=after, started_before=started_before
	):
		group_id = group.get("FinancialEventGroupId") or "Unknown"
		if not include_open and group.get("ProcessingStatus") == "Open":
			continue

		frappe.db.savepoint("amazon_settlement_sync")
		try:
			je_name = _create_settlement_journal_entry(repo, group)
			if je_name:
				created_jes.append(je_name)
		except Exception as e:
			frappe.db.rollback(save_point="amazon_settlement_sync")
			errors.append({"group_id": group_id, "error": strip_html(str(e))})
			frappe.log_error(
				message=frappe.get_traceback(),
				title=f"Amazon Settlement Sync failed for {group_id}",
			)

	update_setting_sync_timestamp(repo.amz_setting, "last_settlements_sync")
	create_log(
		module_def=AMAZON_MODULE_DEF,
		status="Error" if (errors and not created_jes) else "Success",
		method="amazon_connector.amazon_ext.lifecycle.sync_settlements",
		request_data={"amz_setting_name": amz_setting_name, "started_after": str(after or "")},
		response_data={"journal_entries": created_jes, "errors": errors},
		message=f"Amazon settlement sync: {len(created_jes)} journal entries posted, {len(errors)} failed.",
		make_new=True,
	)
	return created_jes

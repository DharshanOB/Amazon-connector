# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import inspect
import json

import frappe
from frappe.utils import cstr, strip_html

ALLOWED_LOG_METHOD_PREFIXES = ("ecommerce_integrations.", "amazon_connector.")


def create_log(
	module_def=None,
	status="Error",
	response_data=None,
	request_data=None,
	exception=None,
	rollback=False,
	method=None,
	message=None,
	make_new=False,
	commit=True,
):
	make_new = make_new or not bool(frappe.flags.request_id)
	in_savepoint = bool(getattr(frappe.flags, "in_amazon_sync_savepoint", False))

	if rollback and not in_savepoint and not getattr(frappe.db, "_disable_transaction_control", 0):
		frappe.db.rollback()

	if make_new:
		log = frappe.get_doc({"doctype": "Ecommerce Integration Log", "integration": cstr(module_def)})
		log.insert(ignore_permissions=True)
	else:
		log = frappe.get_doc("Ecommerce Integration Log", frappe.flags.request_id)

	if response_data and not isinstance(response_data, str):
		response_data = json.dumps(response_data, sort_keys=True, indent=4, default=str)

	if request_data and not isinstance(request_data, str):
		request_data = json.dumps(request_data, sort_keys=True, indent=4, default=str)

	if callable(method):
		method = f"{method.__module__}.{method.__qualname__}"

	log.message = strip_html(cstr(message)) if message else _get_message(exception)
	log.method = log.method or method
	log.response_data = response_data or log.response_data
	log.request_data = request_data or log.request_data
	log.traceback = frappe.get_traceback()
	log.status = status
	log.save(ignore_permissions=True)

	if (
		commit
		and not in_savepoint
		and not frappe.flags.in_test
		and not getattr(frappe.db, "_disable_transaction_control", 0)
	):
		frappe.db.commit()

	return log


def _get_message(exception):
	if hasattr(exception, "message"):
		return strip_html(exception.message)
	if hasattr(exception, "__str__"):
		return strip_html(cstr(exception))
	return "Something went wrong while syncing"


@frappe.whitelist()
def resync(method, name, request_data):
	frappe.has_permission("Ecommerce Integration Log", "write", throw=True)
	_retry_job(name)


def _build_retry_enqueue_kwargs(method_path: str, request_data: str | None, request_id: str) -> dict:
	fn = frappe.get_attr(method_path)
	parsed_data = json.loads(request_data) if request_data else {}
	if parsed_data is None:
		parsed_data = {}
	elif not isinstance(parsed_data, dict):
		parsed_data = {"payload": parsed_data}

	sig = inspect.signature(fn)
	accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())

	if "payload" in sig.parameters and "payload" not in parsed_data:
		enqueue_kwargs = {"payload": parsed_data}
	elif accepts_var_kw:
		enqueue_kwargs = dict(parsed_data)
	else:
		enqueue_kwargs = {k: v for k, v in parsed_data.items() if k in sig.parameters}

	if "request_id" in sig.parameters or accepts_var_kw:
		enqueue_kwargs.setdefault("request_id", request_id)

	return enqueue_kwargs


def _retry_job(job: str):
	frappe.only_for("System Manager")

	doc = frappe.get_doc("Ecommerce Integration Log", job)
	if not doc.method or not doc.method.startswith(ALLOWED_LOG_METHOD_PREFIXES) or doc.status != "Error":
		return

	enqueue_kwargs = _build_retry_enqueue_kwargs(doc.method, doc.request_data, doc.name)

	doc.db_set("status", "Queued", update_modified=False)
	doc.db_set("traceback", "", update_modified=False)

	frappe.enqueue(
		method=doc.method,
		queue="short",
		timeout=300,
		is_async=True,
		now=bool(frappe.flags.in_test),
		enqueue_after_commit=not frappe.flags.in_test,
		**enqueue_kwargs,
	)


@frappe.whitelist()
def bulk_retry(names):
	frappe.has_permission("Ecommerce Integration Log", "write", throw=True)
	if isinstance(names, str):
		names = json.loads(names)
	for name in names:
		_retry_job(name)

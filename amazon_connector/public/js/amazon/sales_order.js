// Copyright (c) 2026, Frappe and contributors
// For license information, please see license.txt

function set_amazon_so_banner(frm) {
	const orderId = frappe.utils.escape_html(frm.doc.amazon_order_id || "");
	const status = frappe.utils.escape_html(frm.doc.amazon_order_status || "");
	const channel = frappe.utils.escape_html(frm.doc.fulfillment_channel || "");

	if (frm.doc.amazon_canceled) {
		frm.dashboard.set_headline_alert(
			__("Amazon Order {0} is marked Canceled ({1}).", [orderId, status || "Canceled"]),
			"red"
		);
		return;
	}

	if (status || channel) {
		const meta = [status, channel].filter(Boolean).join(" / ");
		frm.dashboard.set_headline_alert(
			__("Amazon Order {0} ({1}).", [orderId, meta]),
			"blue"
		);
	}
}

function run_sync_amazon_order(frm, opts) {
	frappe.call({
		method: "amazon_connector.overrides.amazon_sp_api_settings.sync_amazon_order",
		args: {
			sales_order_name: frm.doc.name,
			sync_delivery_notes: opts.sync_delivery_notes ? 1 : 0,
			sync_sales_invoice: opts.sync_sales_invoice ? 1 : 0,
		},
		freeze: true,
		freeze_message: opts.freeze_message,
		callback: function (r) {
			if (!r.exc && r.message) {
				const res = r.message;
				const dns = res.delivery_notes || [];
				let msg = __("Amazon Order {0}: {1}", [
					res.amazon_order_id || frm.doc.amazon_order_id,
					res.order_status || __("Synced"),
				]);
				if (opts.sync_delivery_notes) {
					msg = dns.length
						? __("Synced Delivery Notes for {0}: {1}", [frm.doc.amazon_order_id, dns.join(", ")])
						: __("No new Delivery Notes to import for {0} (Status: {1}).", [
								frm.doc.amazon_order_id,
								res.order_status || "Unknown",
						  ]);
				}
				frappe.show_alert({
					message: msg,
					indicator: res.canceled ? "orange" : "green",
				});
				frm.reload_doc();
			}
		},
	});
}

frappe.ui.form.on("Sales Order", {
	refresh(frm) {
		if (frm.is_new() || !frm.doc.amazon_order_id) return;

		set_amazon_so_banner(frm);

		frm.add_custom_button(
			__("Fetch Delivery Notes"),
			function () {
				run_sync_amazon_order(frm, {
					sync_delivery_notes: true,
					sync_sales_invoice: false,
					freeze_message: __("Fetching Delivery Notes from Amazon SP-API..."),
				});
			},
			__("Amazon SP-API")
		);

		frm.add_custom_button(
			__("Sync Order Status"),
			function () {
				run_sync_amazon_order(frm, {
					sync_delivery_notes: false,
					sync_sales_invoice: false,
					freeze_message: __("Syncing Amazon order status..."),
				});
			},
			__("Amazon SP-API")
		);
	},
});

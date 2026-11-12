// Copyright (c) 2026, Frappe and contributors
// For license information, please see license.txt

function set_amazon_dn_banner(frm) {
	const orderId = frappe.utils.escape_html(frm.doc.amazon_order_id || "");
	const status = frm.doc.amazon_shipment_status || "";
	const errorText = frappe.utils.escape_html(frm.doc.amazon_last_sync_error || "");
	const carrier = frappe.utils.escape_html(frm.doc.amazon_carrier || frm.doc.transporter_name || "");
	const tracking = frappe.utils.escape_html(frm.doc.amazon_tracking_number || frm.doc.lr_no || "");

	if (frm.doc.imported_from_amazon) {
		frm.dashboard.set_headline_alert(
			__("Amazon Order {0}: Imported from Amazon (outbound shipment confirmation skipped).", [orderId]),
			"blue"
		);
		return;
	}

	if (frm.doc.fulfillment_channel === "AFN") {
		frm.dashboard.set_headline_alert(
			__("Amazon Order {0}: FBA / AFN order fulfilled by Amazon.", [orderId]),
			"blue"
		);
		return;
	}

	if (status === "Confirmed") {
		const details = [carrier, tracking].filter(Boolean).join(" / ");
		frm.dashboard.set_headline_alert(
			details
				? __("Amazon Shipment Confirmed ({0}).", [details])
				: __("Amazon Shipment Confirmed for Order {0}.", [orderId]),
			"green"
		);
	} else if (status === "Failed") {
		frm.dashboard.set_headline_alert(
			errorText
				? __("Amazon Shipment Failed: {0}", [errorText])
				: __("Amazon Shipment Confirmation Failed for Order {0}.", [orderId]),
			"red"
		);
	} else if (status === "Pending") {
		frm.dashboard.set_headline_alert(
			errorText
				? __("Amazon Shipment Pending: {0}", [errorText])
				: __("Amazon Shipment Confirmation Pending for Order {0}.", [orderId]),
			"orange"
		);
	} else if (status === "Cancelled") {
		frm.dashboard.set_headline_alert(
			errorText
				? __("Amazon Shipment Cancelled: {0}", [errorText])
				: __("Amazon Shipment Cancelled for Order {0}.", [orderId]),
			"orange"
		);
	} else if (status === "Skipped") {
		frm.dashboard.set_headline_alert(
			__("Amazon Shipment Confirmation Skipped for Order {0}.", [orderId]),
			"blue"
		);
	}
}

frappe.ui.form.on("Delivery Note", {
	refresh(frm) {
		if (frm.is_new() || !frm.doc.amazon_order_id) return;

		set_amazon_dn_banner(frm);

		const canConfirm =
			frm.doc.docstatus === 1 &&
			!frm.doc.imported_from_amazon &&
			frm.doc.fulfillment_channel !== "AFN" &&
			frm.doc.amazon_shipment_status !== "Confirmed" &&
			frm.doc.amazon_shipment_status !== "Skipped";

		if (canConfirm) {
			frm.add_custom_button(
				__("Confirm / Retry Amazon Shipment"),
				function () {
					frappe.call({
						method: "amazon_connector.amazon_ext.fulfillment.confirm_delivery_note_shipment_now",
						args: { delivery_note_name: frm.doc.name },
						freeze: true,
						freeze_message: __("Confirming shipment with Amazon SP-API..."),
						callback: function (r) {
							if (!r.exc && r.message) {
								const ok = r.message.status === "Confirmed";
								frappe.show_alert({
									message: ok
										? __("Amazon shipment confirmed ({0})", [r.message.tracking_number || ""])
										: __("Amazon shipment confirmation {0}: {1}", [
												r.message.status || "Failed",
												r.message.error || __("Check error log"),
										  ]),
									indicator: ok ? "green" : "red",
								});
								frm.reload_doc();
							}
						},
					});
				},
				__("Amazon SP-API")
			);
		}
	},
});

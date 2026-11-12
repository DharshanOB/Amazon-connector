// Copyright (c) 2026, Frappe and contributors
// For license information, please see license.txt

function show_amazon_sync_alert(item_code, action_label, result) {
	const status = (result && result.status) || "ACCEPTED";
	const issues = (result && result.issues) || [];
	const reason = (result && (result.reason || result.message)) || "";

	if (status === "INVALID" || status === "ERROR" || status === "FAILED") {
		const details = issues.length
			? issues.map((i) => i.message || JSON.stringify(i)).join("; ")
			: reason || __("Validation failed on Amazon.");
		frappe.msgprint({
			title: __("Amazon {0} Failed ({1})", [action_label, status]),
			indicator: "red",
			message: frappe.utils.escape_html(details),
		});
		return;
	}

	if (status === "SKIPPED") {
		frappe.show_alert({
			message: __("{0} skipped for {1}: {2}", [
				action_label,
				item_code,
				reason || __("No changes to sync"),
			]),
			indicator: "orange",
		});
		return;
	}

	frappe.show_alert({
		message: __("{0} completed for {1} (Status: {2})", [action_label, item_code, status]),
		indicator: "green",
	});
}

frappe.ui.form.on("Item", {
	refresh(frm) {
		if (frm.is_new()) return;

		// Group buttons under "Amazon SP-API" dropdown in Item Desk Form
		frm.add_custom_button(
			__("Push Listing to Amazon"),
			function () {
				frappe.call({
					method: "amazon_connector.overrides.amazon_sp_api_settings.sync_outbound_item",
					args: { item_code: frm.doc.name },
					freeze: true,
					freeze_message: __("Pushing Listing to Amazon SP-API..."),
					callback: function (r) {
						if (!r.exc) {
							show_amazon_sync_alert(frm.doc.name, __("Listing Push"), r.message);
							frm.reload_doc();
						}
					},
				});
			},
			__("Amazon SP-API")
		);

		frm.add_custom_button(
			__("Sync Stock to Amazon"),
			function () {
				frappe.call({
					method: "amazon_connector.overrides.amazon_sp_api_settings.sync_outbound_item_stock",
					args: { item_code: frm.doc.name },
					freeze: true,
					freeze_message: __("Updating stock on Amazon..."),
					callback: function (r) {
						if (!r.exc) {
							show_amazon_sync_alert(frm.doc.name, __("Stock Sync"), r.message);
						}
					},
				});
			},
			__("Amazon SP-API")
		);

		frm.add_custom_button(
			__("Sync Price to Amazon"),
			function () {
				frappe.call({
					method: "amazon_connector.overrides.amazon_sp_api_settings.sync_outbound_item_price",
					args: { item_code: frm.doc.name },
					freeze: true,
					freeze_message: __("Updating price on Amazon..."),
					callback: function (r) {
						if (!r.exc) {
							show_amazon_sync_alert(frm.doc.name, __("Price Sync"), r.message);
						}
					},
				});
			},
			__("Amazon SP-API")
		);

		frm.add_custom_button(
			__("Fetch Amazon Listing"),
			function () {
				frappe.call({
					method: "amazon_connector.amazon_ext.outbound.fetch_amazon_listing",
					args: { item_code: frm.doc.name },
					freeze: true,
					freeze_message: __("Fetching listing from Amazon SP-API..."),
					callback: function (r) {
						if (!r.exc && r.message) {
							const data = r.message;
							const sku = frappe.utils.escape_html(data.sku || frm.doc.name);
							const status = frappe.utils.escape_html(
								data.status || (data.summaries && data.summaries[0] && data.summaries[0].status && data.summaries[0].status.join(", ")) || "FETCHED"
							);
							const summary_json = frappe.utils.escape_html(JSON.stringify(data, null, 2));
							frappe.msgprint({
								title: __("Amazon Listing: {0} ({1})", [sku, status]),
								indicator: data.status === "INVALID" ? "red" : "blue",
								message: `<pre style="max-height:360px;overflow:auto;">${summary_json}</pre>`,
							});
							frm.reload_doc();
						}
					},
				});
			},
			__("Amazon SP-API")
		);

		frm.add_custom_button(
			__("Delete Amazon Listing"),
			function () {
				frappe.confirm(
					__("Delete Amazon listing for Item {0}?", [frm.doc.name]),
					function () {
						frappe.call({
							method: "amazon_connector.amazon_ext.outbound.delete_amazon_listing",
							args: { item_code: frm.doc.name, remove_mapping: 1 },
							freeze: true,
							freeze_message: __("Deleting listing on Amazon SP-API..."),
							callback: function (r) {
								if (!r.exc) {
									show_amazon_sync_alert(frm.doc.name, __("Listing Delete"), r.message);
									frm.reload_doc();
								}
							},
						});
					}
				);
			},
			__("Amazon SP-API")
		);
	},
});

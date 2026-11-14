// Copyright (c) 2026, Frappe and contributors
// For license information, please see license.txt

frappe.ui.form.on("Amazon SP API Settings", {
	refresh(frm) {
		frm.trigger("set_ext_queries");

		if (frm.doc.is_active && !frm.doc.__islocal) {
			frm.add_custom_button(__("Sync Amazon Categories"), function () {
				frappe.call({
					method: "amazon_connector.amazon_ext.outbound.sync_amazon_product_types",
					args: { amz_setting_name: frm.doc.name },
					freeze: true,
					freeze_message: __("Syncing categories from Amazon SP-API..."),
					callback: function (r) {
						if (!r.exc && r.message) {
							frappe.msgprint({
								title: __("Categories Synced"),
								indicator: "green",
								message: __("Successfully synced {0} Amazon product categories.", [r.message.synced || r.message.total]),
							});
						}
					},
				});
			});

			frm.add_custom_button(__("Push Item to Amazon"), function () {
				frappe.prompt(
					[
						{
							fieldname: "item_code",
							label: __("Item Code"),
							fieldtype: "Link",
							options: "Item",
							reqd: 1,
						},
					],
					function (values) {
						frappe.call({
							method: "amazon_connector.overrides.amazon_sp_api_settings.sync_outbound_item",
							args: {
								item_code: values.item_code,
								amz_setting_name: frm.doc.name,
							},
							freeze: true,
							freeze_message: __("Pushing Item to Amazon SP-API..."),
							callback: function (r) {
								if (!r.exc) {
									frappe.msgprint(
										__("Item {0} pushed to Amazon. Status: {1}", [
											values.item_code,
											(r.message && r.message.status) || "ACCEPTED",
										])
									);
								}
							},
						});
					},
					__("Push Item to Amazon"),
					__("Push")
				);
			});
		}
	},

	set_ext_queries(frm) {
		frm.set_query("warehouse", () => {
			return {
				filters: {
					company: frm.doc.company,
				},
			};
		});

		if (frm.fields_dict.fba_warehouse) {
			frm.set_query("fba_warehouse", () => {
				return {
					filters: {
						is_group: 0,
						company: frm.doc.company,
					},
				};
			});
		}
	},
});

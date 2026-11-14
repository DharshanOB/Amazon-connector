// Copyright (c) 2026, Frappe and contributors
// For license information, please see license.txt

function set_amazon_product_type_query(frm) {
	if (!frm.fields_dict.amazon_product_type) return;
	frm.set_query("amazon_product_type", function () {
		return {
			query: "amazon_connector.amazon_connector.doctype.amazon_product_type.amazon_product_type.get_product_types_list",
		};
	});
}

function preload_item_group_amazon_template(frm, force = false) {
	const pt = frm.doc.amazon_product_type;
	if (!pt || pt === "PRODUCT") return;

	const existing = (frm.doc.amazon_attributes_template || "").trim();
	const has_template = existing !== "" && existing !== "{}";
	if (!force && has_template) return;

	frappe.call({
		method: "amazon_connector.amazon_ext.outbound.get_category_attributes_template",
		args: { product_type: pt },
		freeze: force,
		freeze_message: force ? __("Loading Amazon attributes template...") : undefined,
		callback: function (r) {
			if (!r.exc && r.message && r.message.json) {
				frm.set_value("amazon_attributes_template", r.message.json);
				frappe.show_alert({
					message: __("Preloaded Amazon attributes template for {0}.", [pt]),
					indicator: "green",
				});
			}
		},
	});
}

frappe.ui.form.on("Item Group", {
	setup(frm) {
		set_amazon_product_type_query(frm);
	},

	refresh(frm) {
		set_amazon_product_type_query(frm);

		if (!frm.fields_dict.amazon_product_type) return;
		frm.add_custom_button(
			__("Preload Attributes Template"),
			function () {
				if (!frm.doc.amazon_product_type) {
					frappe.msgprint(__("Please select a Default Amazon Product Type first."));
					return;
				}
				preload_item_group_amazon_template(frm, true);
			},
			__("Amazon SP-API")
		);
	},

	amazon_product_type(frm) {
		preload_item_group_amazon_template(frm, false);
	},
});

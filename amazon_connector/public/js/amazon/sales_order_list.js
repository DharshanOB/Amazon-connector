// Copyright (c) 2026, Frappe and contributors
// For license information, please see license.txt

(function () {
	function ensure_amazon_spin_style() {
		if (!document.getElementById("amazon-spin-style")) {
			const style = document.createElement("style");
			style.id = "amazon-spin-style";
			style.textContent =
				"@keyframes amazon-spin { 100% { transform: rotate(360deg); } } " +
				".amazon-spinning { animation: amazon-spin 0.8s linear infinite !important; display: inline-block; }";
			document.head.appendChild(style);
		}
	}

	let is_syncing = false;
	let last_sync_time = 0;

	function trigger_amazon_so_sync(listview) {
		const now = Date.now();
		if (is_syncing || now - last_sync_time < 3000) {
			return;
		}
		is_syncing = true;
		last_sync_time = now;
		ensure_amazon_spin_style();

		const $wrapper = listview && listview.page ? listview.page.wrapper : null;
		const $icon = $wrapper
			? $wrapper.find(".page-icon-group svg, .page-icon-group .icon, button[data-original-title*='Reload'] svg, button[title*='Reload'] svg")
			: $(".page-icon-group svg");
		$icon.addClass("amazon-spinning");

		frappe.show_alert(
			{
				message: __("Checking Amazon for new orders..."),
				indicator: "blue",
			},
			3
		);

		frappe.call({
			method: "amazon_connector.overrides.amazon_sp_api_settings.sync_from_listview",
			args: { doctype: "Sales Order" },
			callback: function (r) {
				is_syncing = false;
				$icon.removeClass("amazon-spinning");
				if (r && r.message) {
					frappe.show_alert(
						{
							message: r.message.message || __("Amazon sync complete"),
							indicator: r.message.status === "error" ? "red" : "green",
						},
						5
					);
				}
				const lv = listview || window.cur_list;
				if (lv) {
					lv.last_args = null;
					lv.refresh();
				}
			},
			error: function () {
				is_syncing = false;
				$icon.removeClass("amazon-spinning");
				const lv = listview || window.cur_list;
				if (lv) {
					lv.last_args = null;
					lv.refresh();
				}
			},
		});
	}

	function attach_amazon_refresh_hook(listview) {
		if (!listview || !listview.page || !listview.page.wrapper) return;
		ensure_amazon_spin_style();

		const $wrapper = listview.page.wrapper;

		// 1. Direct binding to listview.refresh_button if present
		if (listview.refresh_button && !listview.refresh_button.data("amazon-so-hooked")) {
			listview.refresh_button.data("amazon-so-hooked", true);
			listview.refresh_button.on("click.amazon_so_sync", function () {
				trigger_amazon_so_sync(listview);
			});
		}

		// 2. Delegated binding on the listview page wrapper
		$wrapper.off("click.amazon_so_sync").on(
			"click.amazon_so_sync",
			".page-icon-group button, .page-icon-group .icon-btn, button[data-original-title*='Reload'], button[data-bs-original-title*='Reload'], button[title*='Reload'], button.icon-btn:has([*|href*='refresh-cw']), button.icon-btn:has(use[href*='refresh-cw'])",
			function () {
				trigger_amazon_so_sync(listview);
			}
		);
	}

	// 3. Global document delegated listener as bulletproof safety net
	$(document).off("click.amazon_so_global").on(
		"click.amazon_so_global",
		".page-icon-group button, .page-icon-group .icon-btn, button[data-original-title*='Reload'], button[title*='Reload']",
		function () {
			const route = frappe.get_route ? frappe.get_route() : null;
			if (route && route[0] === "List" && route[1] === "Sales Order") {
				const lv = window.cur_list;
				trigger_amazon_so_sync(lv);
			}
		}
	);

	const existing_settings = frappe.listview_settings["Sales Order"] || {};
	const original_onload = existing_settings.onload;
	const original_refresh = existing_settings.refresh;

	existing_settings.onload = function (listview) {
		if (original_onload) {
			original_onload.call(this, listview);
		}
		attach_amazon_refresh_hook(listview);
	};

	existing_settings.refresh = function (listview) {
		if (original_refresh) {
			original_refresh.call(this, listview);
		}
		attach_amazon_refresh_hook(listview);
	};

	frappe.listview_settings["Sales Order"] = existing_settings;
})();

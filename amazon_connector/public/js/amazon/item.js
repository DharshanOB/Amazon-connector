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

const VARIANT_TO_AMAZON_ALIASES = {
	colour: "color",
	color_name: "color",
	size_name: "size",
	apparel_size: "size",
	book_binding: "binding",
	format: "binding",
	pages: "number_of_pages",
	page_count: "number_of_pages",
	pages_count: "number_of_pages",
	author_name: "author",
	model: "model_name",
	storage_capacity: "memory_storage_capacity",
	storage: "memory_storage_capacity",
	pack_size: "number_of_items",
	flavour: "flavor",
	material_type: "material",
	edition_name: "edition",
	style_name: "style",
};

function map_variant_attributes_to_controls(frm, notify = false) {
	const variant_rows = frm.doc.attributes || [];
	if (!variant_rows.length || !frm._amz_controls) return 0;

	let mapped_count = 0;
	const mapped_names = [];

	variant_rows.forEach((row) => {
		if (!row.attribute || row.attribute_value === undefined || row.attribute_value === null) return;
		const raw_name = String(row.attribute)
			.toLowerCase()
			.replace(/[^a-z0-9_]/g, "_")
			.replace(/^_+|_+$/g, "");
		const val = String(row.attribute_value).trim();
		if (!val) return;

		const target_keys = [raw_name, VARIANT_TO_AMAZON_ALIASES[raw_name] || null].filter(Boolean);
		for (const key of target_keys) {
			if (frm._amz_controls[key]) {
				const ctrl = frm._amz_controls[key].control;
				ctrl.set_value(val);
				mapped_count++;
				mapped_names.push(`${row.attribute} ➔ ${key}`);
				break;
			}
		}
	});

	if (mapped_count > 0 && frm._amz_sync_fn) {
		frm._amz_sync_fn();
		if (notify) {
			frappe.show_alert({
				message: __("Mapped {0} attributes from Variants tab: {1}", [
					mapped_count,
					mapped_names.join(", "),
				]),
				indicator: "green",
			});
		}
	} else if (notify) {
		frappe.show_alert({
			message: __("No matching variant attributes found on Variants tab to map."),
			indicator: "orange",
		});
	}
	return mapped_count;
}

function ensure_amazon_attribute_styles() {
	if (document.getElementById("amz-attr-styles")) return;
	$(`<style id="amz-attr-styles">
		.amz-attr-toolbar {
			display: flex;
			justify-content: space-between;
			align-items: center;
			gap: 12px;
			flex-wrap: wrap;
			padding-bottom: 12px;
			margin-bottom: 16px;
			border-bottom: 1px solid var(--border-color);
		}
		.amz-attr-toolbar-title {
			display: flex;
			align-items: center;
			gap: 8px;
			flex-wrap: wrap;
		}
		.amz-attr-pt-code {
			font-weight: 600;
			font-size: 13px;
			color: var(--heading-color, var(--text-color));
		}
		.amz-attr-pill {
			display: inline-flex;
			align-items: center;
			padding: 2px 8px;
			border-radius: var(--border-radius-full, 999px);
			font-size: 11px;
			font-weight: 500;
			background: var(--control-bg);
			color: var(--text-color);
			border: 1px solid var(--border-color);
		}
		.amz-attr-pill--muted {
			color: var(--text-muted);
		}
		.amz-attr-actions {
			display: flex;
			align-items: center;
			gap: 6px;
			flex-wrap: wrap;
		}
		.amz-attr-section {
			margin-bottom: 22px;
		}
		.amz-attr-section-header {
			display: flex;
			align-items: center;
			gap: 8px;
			margin-bottom: 12px;
			font-size: 12px;
			font-weight: 600;
			text-transform: uppercase;
			letter-spacing: 0.04em;
			color: var(--heading-color, var(--text-color));
		}
		.amz-attr-section-header--optional {
			color: var(--text-muted);
		}
		.amz-attr-grid {
			display: grid;
			grid-template-columns: repeat(2, minmax(0, 1fr));
			gap: 14px 24px;
			align-items: start;
		}
		.amz-attr-cell--full {
			grid-column: 1 / -1;
		}
		.amz-attr-grid .frappe-control {
			margin-bottom: 0;
		}
		.amz-attr-grid .frappe-control[data-fieldtype="Small Text"] textarea.form-control {
			min-height: 76px;
			resize: vertical;
		}
		.amz-dimensions-row {
			display: grid;
			grid-template-columns: repeat(3, minmax(0, 1fr)) minmax(120px, 1.1fr);
			gap: 8px;
			align-items: end;
		}
		.amz-unit-count-row {
			display: grid;
			grid-template-columns: minmax(90px, 0.8fr) minmax(120px, 1.2fr);
			gap: 8px;
			align-items: end;
		}
		.amz-subinput-label {
			display: block;
			font-size: 11px;
			color: var(--text-muted);
			margin-bottom: 4px;
		}
		@media (max-width: 767px) {
			.amz-attr-grid {
				grid-template-columns: 1fr;
			}
			.amz-dimensions-row {
				grid-template-columns: repeat(2, minmax(0, 1fr));
			}
		}
	</style>`).appendTo("head");
}

function build_safe_defaults_payload(fields_meta) {
	const payload = {};
	(fields_meta || []).forEach((meta) => {
		if (meta.is_safe_default && meta.default !== undefined && meta.default !== null && meta.default !== "") {
			payload[meta.fieldname] = meta.default;
		}
	});
	return payload;
}

function extract_control_display_value(raw_val, meta) {
	if (raw_val === undefined || raw_val === null) return null;

	const first_item = Array.isArray(raw_val) ? raw_val[0] : raw_val;

	if (meta.widget_type === "bullet_list") {
		const list = Array.isArray(raw_val) ? raw_val : [raw_val];
		return list
			.map((entry) => (entry && typeof entry === "object" ? entry.value : entry))
			.filter((v) => v !== undefined && v !== null && String(v).trim() !== "")
			.join("\n");
	}

	if (meta.widget_type === "dimensions") {
		const obj = first_item && typeof first_item === "object" ? first_item : null;
		if (!obj) return null;
		const len = obj.length && typeof obj.length === "object" ? obj.length.value : "";
		const wid = obj.width && typeof obj.width === "object" ? obj.width.value : "";
		const hgt = obj.height && typeof obj.height === "object" ? obj.height.value : "";
		const unit =
			(obj.length && obj.length.unit) ||
			(obj.width && obj.width.unit) ||
			(obj.height && obj.height.unit) ||
			"centimeters";
		return { length: len, width: wid, height: hgt, unit: unit };
	}

	if (meta.widget_type === "unit_count") {
		if (first_item && typeof first_item === "object") {
			const unit_type =
				first_item.type && typeof first_item.type === "object"
					? first_item.type.value
					: first_item.type || "count";
			return {
				value: first_item.value !== undefined ? first_item.value : "",
				unit_type: unit_type || "count",
			};
		}
		return { value: first_item, unit_type: "count" };
	}

	if (Array.isArray(raw_val)) {
		if (!raw_val.length) return "";
		if (raw_val.length > 1) {
			return raw_val
				.map((entry) => (entry && typeof entry === "object" && entry.value !== undefined ? entry.value : entry))
				.filter((v) => v !== undefined && v !== null && String(v).trim() !== "")
				.join(", ");
		}
		return first_item && typeof first_item === "object" && first_item.value !== undefined
			? first_item.value
			: first_item;
	}

	if (typeof raw_val === "object") {
		return raw_val.value !== undefined ? raw_val.value : JSON.stringify(raw_val);
	}

	return raw_val;
}

function serialize_control_value(val, meta) {
	if (meta.widget_type === "dimensions") {
		if (!val || typeof val !== "object") return null;
		const l = parseFloat(val.length);
		const w = parseFloat(val.width);
		const h = parseFloat(val.height);
		if (isNaN(l) && isNaN(w) && isNaN(h)) return null;
		const unit = val.unit || "centimeters";
		const out = {};
		if (!isNaN(l)) out.length = { unit: unit, value: l };
		if (!isNaN(w)) out.width = { unit: unit, value: w };
		if (!isNaN(h)) out.height = { unit: unit, value: h };
		return out;
	}

	if (meta.widget_type === "unit_count") {
		if (!val || typeof val !== "object") return null;
		const raw_num = val.value;
		if (raw_num === "" || raw_num === null || raw_num === undefined) return null;
		const num = Number(raw_num);
		if (isNaN(num)) return null;
		const unit_type = String(val.unit_type || "count").trim() || "count";
		return {
			value: num,
			type: { language_tag: "en_IN", value: unit_type },
		};
	}

	if (meta.widget_type === "bullet_list") {
		const lines = String(val || "")
			.split(/\r?\n/)
			.map((line) => line.trim())
			.filter(Boolean);
		if (!lines.length) return null;
		return lines.map((line) => ({ language_tag: "en_IN", value: line }));
	}

	if (meta.widget_type === "language_obj") {
		const text = String(val || "").trim();
		if (!text) return null;
		return { type: "published", value: text };
	}

	if (val === "" || val === null || val === undefined) {
		return null;
	}

	if (meta.fieldtype === "Check") {
		return Boolean(val);
	}
	if (meta.fieldtype === "Int") {
		const parsed = parseInt(val, 10);
		return isNaN(parsed) ? null : parsed;
	}
	if (meta.fieldtype === "Float") {
		const parsed = parseFloat(val);
		return isNaN(parsed) ? null : parsed;
	}
	if (meta.fieldtype === "Date" || meta.fieldtype === "Datetime") {
		return [{ value: String(val).trim() }];
	}
	if (meta.has_lang) {
		return [{ language_tag: "en_IN", value: String(val).trim() }];
	}
	if (meta.unit) {
		const num_val = Number(val);
		return [{ unit: meta.unit, value: isNaN(num_val) ? val : num_val }];
	}
	return [{ value: String(val).trim() }];
}

function create_composite_dimensions_control($parent, meta, on_change) {
	const units =
		Array.isArray(meta.unit_options) && meta.unit_options.length
			? meta.unit_options
			: ["centimeters", "millimeters", "inches", "meters"];
	const req_cls = meta.reqd ? "reqd" : "";
	const options_html = units
		.map((u) => `<option value="${frappe.utils.escape_html(u)}">${frappe.utils.escape_html(u)}</option>`)
		.join("");

	const $wrap = $(`
		<div class="frappe-control">
			<div class="form-group">
				<div class="clearfix">
					<label class="control-label ${req_cls}">${frappe.utils.escape_html(__(meta.label || meta.fieldname))}</label>
				</div>
				<div class="amz-dimensions-row">
					<div>
						<span class="amz-subinput-label">${__("Length")}</span>
						<input type="number" step="any" min="0" class="form-control input-sm amz-dim-l" placeholder="10" />
					</div>
					<div>
						<span class="amz-subinput-label">${__("Width")}</span>
						<input type="number" step="any" min="0" class="form-control input-sm amz-dim-w" placeholder="10" />
					</div>
					<div>
						<span class="amz-subinput-label">${__("Height")}</span>
						<input type="number" step="any" min="0" class="form-control input-sm amz-dim-h" placeholder="5" />
					</div>
					<div>
						<span class="amz-subinput-label">${__("Unit")}</span>
						<select class="form-control input-sm amz-dim-unit">${options_html}</select>
					</div>
				</div>
			</div>
		</div>
	`).appendTo($parent);

	const $l = $wrap.find(".amz-dim-l");
	const $w = $wrap.find(".amz-dim-w");
	const $h = $wrap.find(".amz-dim-h");
	const $u = $wrap.find(".amz-dim-unit");
	$u.val(units.includes("centimeters") ? "centimeters" : units[0]);

	$wrap.find("input, select").on("change", on_change);

	return {
		get_value() {
			return {
				length: $l.val(),
				width: $w.val(),
				height: $h.val(),
				unit: $u.val(),
			};
		},
		set_value(val) {
			if (!val || typeof val !== "object") {
				$l.val("");
				$w.val("");
				$h.val("");
				return;
			}
			$l.val(val.length !== undefined && val.length !== null ? val.length : "");
			$w.val(val.width !== undefined && val.width !== null ? val.width : "");
			$h.val(val.height !== undefined && val.height !== null ? val.height : "");
			if (val.unit) $u.val(val.unit);
		},
	};
}

function create_composite_unit_count_control($parent, meta, on_change) {
	const req_cls = meta.reqd ? "reqd" : "";
	const $wrap = $(`
		<div class="frappe-control">
			<div class="form-group">
				<div class="clearfix">
					<label class="control-label ${req_cls}">${frappe.utils.escape_html(__(meta.label || meta.fieldname))}</label>
				</div>
				<div class="amz-unit-count-row">
					<div>
						<span class="amz-subinput-label">${__("Count")}</span>
						<input type="number" step="any" min="0" class="form-control input-sm amz-uc-val" placeholder="1" />
					</div>
					<div>
						<span class="amz-subinput-label">${__("Unit Type")}</span>
						<input type="text" class="form-control input-sm amz-uc-type" placeholder="count" />
					</div>
				</div>
			</div>
		</div>
	`).appendTo($parent);

	const $val = $wrap.find(".amz-uc-val");
	const $type = $wrap.find(".amz-uc-type");
	$wrap.find("input").on("change", on_change);

	return {
		get_value() {
			return { value: $val.val(), unit_type: $type.val() };
		},
		set_value(val) {
			if (!val || typeof val !== "object") {
				$val.val(val !== undefined && val !== null ? val : "");
				$type.val("count");
				return;
			}
			$val.val(val.value !== undefined && val.value !== null ? val.value : "");
			$type.val(val.unit_type || "count");
		},
	};
}

function create_attribute_control($parent, meta, on_change) {
	if (meta.widget_type === "dimensions") {
		return create_composite_dimensions_control($parent, meta, on_change);
	}
	if (meta.widget_type === "unit_count") {
		return create_composite_unit_count_control($parent, meta, on_change);
	}

	const is_bullets = meta.widget_type === "bullet_list";
	const base_label = __(meta.label || meta.fieldname);
	const label = is_bullets
		? __("{0} (one per line)", [base_label])
		: base_label + (meta.unit ? ` (${meta.unit})` : "");

	return frappe.ui.form.make_control({
		parent: $parent,
		df: {
			fieldtype: meta.fieldtype || "Data",
			fieldname: "dyn_amz_" + meta.fieldname,
			label: label,
			reqd: meta.reqd || 0,
			options: meta.options || null,
			placeholder: meta.placeholder || (meta.reqd ? __("Required by Amazon") : __("Optional")),
			description: "",
			change: on_change,
		},
		render_input: true,
	});
}

function render_amazon_dynamic_attributes(frm, force_reload = false) {
	const field = frm.fields_dict.amazon_dynamic_attributes_html;
	if (!field || !field.$wrapper) return;

	ensure_amazon_attribute_styles();

	const $wrapper = field.$wrapper;
	const pt = frm.doc.amazon_product_type;

	if (!pt || pt === "PRODUCT") {
		$wrapper.html(`
			<div style="padding: 24px; text-align: center; background: var(--control-bg); border-radius: 8px; border: 1px dashed var(--border-color); margin: 10px 0;">
				<div style="font-size: 14px; font-weight: 600; color: var(--text-color); margin-bottom: 6px;">
					${__("No Amazon Product Type Selected")}
				</div>
				<div style="color: var(--text-muted); font-size: 13px; margin-bottom: 14px; max-width: 520px; margin-left: auto; margin-right: auto;">
					${__("Select an Amazon Product Type in the <strong>Details</strong> tab to load its schema fields here.")}
				</div>
				<button class="btn btn-sm btn-primary btn-amazon-goto-details">
					${__("Go to Details Tab")}
				</button>
			</div>
		`);
		$wrapper.find(".btn-amazon-goto-details").on("click", function (e) {
			e.preventDefault();
			frm.scroll_to_field("amazon_product_type");
		});
		frm._amz_controls = {};
		return;
	}

	if (!force_reload && frm._amz_rendered_pt === pt && frm._amz_fields_meta) {
		build_attribute_controls(frm, $wrapper, frm._amz_fields_meta);
		return;
	}

	$wrapper.html(`
		<div style="padding: 20px; text-align: center; color: var(--text-muted);">
			<i class="fa fa-spinner fa-spin mr-2"></i> ${__("Loading schema attributes for {0}...", [frappe.utils.escape_html(pt)])}
		</div>
	`);

	frappe.call({
		method: "amazon_connector.amazon_ext.outbound.get_category_attributes_template",
		args: { product_type: pt },
		callback: function (r) {
			if (!r.exc && r.message) {
				frm._amz_fields_meta = r.message.fields_meta || [];
				frm._amz_rendered_pt = pt;

				const has_attrs =
					frm.doc.amazon_attributes &&
					frm.doc.amazon_attributes.trim() !== "" &&
					frm.doc.amazon_attributes.trim() !== "{}";
				if (!has_attrs) {
					const safe_defaults = build_safe_defaults_payload(frm._amz_fields_meta);
					if (Object.keys(safe_defaults).length > 0) {
						frm.set_value("amazon_attributes", JSON.stringify(safe_defaults, null, 2));
					}
				}

				build_attribute_controls(frm, $wrapper, frm._amz_fields_meta);
			} else {
				$wrapper.html(`
					<div class="alert alert-warning">
						${__("Could not load Amazon category attributes for {0}.", [frappe.utils.escape_html(pt)])}
					</div>
				`);
			}
		},
	});
}

function build_attribute_controls(frm, $wrapper, fields_meta) {
	$wrapper.empty();
	frm._amz_controls = {};

	let current_attrs = {};
	try {
		current_attrs = frm.doc.amazon_attributes ? JSON.parse(frm.doc.amazon_attributes) : {};
	} catch (e) {
		current_attrs = {};
	}

	const pt = frm.doc.amazon_product_type || "";
	const is_variant = Boolean(frm.doc.variant_of);
	const req_count = fields_meta.filter((m) => m.reqd).length;
	const opt_count = fields_meta.length - req_count;

	if (is_variant) {
		$(`
			<div class="alert alert-info py-2 px-3 mb-3 small d-flex justify-content-between align-items-center" style="border-radius: 6px;">
				<div>
					<i class="fa fa-code-fork mr-1"></i>
					${__("This item is a <strong>Variant</strong> of {0}. Template defaults are inherited automatically, and values on the <strong>Variants</strong> tab override variation fields.", [
						`<code>${frappe.utils.escape_html(frm.doc.variant_of)}</code>`,
					])}
				</div>
				<button class="btn btn-xs btn-primary btn-pull-variant-attrs ml-2">
					<i class="fa fa-refresh mr-1"></i> ${__("Sync from Variants")}
				</button>
			</div>
		`).appendTo($wrapper);

		$wrapper.find(".btn-pull-variant-attrs").on("click", function (e) {
			e.preventDefault();
			map_variant_attributes_to_controls(frm, true);
		});
	}

	const $header = $(`
		<div class="amz-attr-toolbar">
			<div class="amz-attr-toolbar-title">
				<span class="amz-attr-pt-code">${frappe.utils.escape_html(pt)}</span>
				<span class="amz-attr-pill">${req_count} ${__("Required")}</span>
				<span class="amz-attr-pill amz-attr-pill--muted">${opt_count} ${__("Optional")}</span>
			</div>
			<div class="amz-attr-actions">
				<button class="btn btn-xs btn-default btn-pull-variant-tab" title="${__("Map Color, Size, etc. from the Variants tab")}">
					<i class="fa fa-tags mr-1"></i> ${__("Pull from Variants")}
				</button>
				<button class="btn btn-xs btn-default btn-fill-amz-samples" title="${__("Populate all fields with schema sample values")}">
					<i class="fa fa-magic mr-1"></i> ${__("Fill Samples")}
				</button>
				<button class="btn btn-xs btn-default btn-reset-amz-defaults">
					<i class="fa fa-refresh mr-1"></i> ${__("Reset Defaults")}
				</button>
				<button class="btn btn-xs btn-default btn-clear-amz-inputs">
					<i class="fa fa-eraser mr-1"></i> ${__("Clear All")}
				</button>
			</div>
		</div>
	`).appendTo($wrapper);

	if (!fields_meta || fields_meta.length === 0) {
		$wrapper.append(`
			<div class="text-muted small p-3">
				${__("No category-specific required attributes found for this product type. General listing identifiers will be used.")}
			</div>
		`);
		return;
	}

	let is_syncing = false;
	function sync_dynamic_controls_to_doc() {
		if (is_syncing) return;
		is_syncing = true;
		try {
			let payload = {};
			try {
				if (frm.doc.amazon_attributes) {
					payload = JSON.parse(frm.doc.amazon_attributes);
				}
			} catch (e) {
				payload = {};
			}

			Object.keys(frm._amz_controls || {}).forEach((key) => {
				const item = frm._amz_controls[key];
				const serialized = serialize_control_value(item.control.get_value(), item.meta);
				if (serialized === null) {
					delete payload[key];
				} else {
					payload[key] = serialized;
				}
			});

			const json_str = Object.keys(payload).length > 0 ? JSON.stringify(payload, null, 2) : "{}";
			if (frm.doc.amazon_attributes !== json_str) {
				frm.set_value("amazon_attributes", json_str);
			}
		} finally {
			is_syncing = false;
		}
	}

	$header.find(".btn-pull-variant-tab").on("click", function (e) {
		e.preventDefault();
		map_variant_attributes_to_controls(frm, true);
	});

	$header.find(".btn-fill-amz-samples").on("click", function (e) {
		e.preventDefault();
		Object.keys(frm._amz_controls || {}).forEach((k) => {
			const entry = frm._amz_controls[k];
			const display_val = extract_control_display_value(entry.meta.default, entry.meta);
			if (display_val !== null && display_val !== undefined && display_val !== "") {
				entry.control.set_value(display_val);
			}
		});
		sync_dynamic_controls_to_doc();
		frappe.show_alert({ message: __("Populated schema sample values."), indicator: "blue" });
	});

	$header.find(".btn-reset-amz-defaults").on("click", function (e) {
		e.preventDefault();
		frappe.confirm(__("Reset to safe category defaults and reload schema?"), function () {
			const safe_defaults = build_safe_defaults_payload(frm._amz_fields_meta);
			frm.set_value(
				"amazon_attributes",
				Object.keys(safe_defaults).length ? JSON.stringify(safe_defaults, null, 2) : "{}"
			);
			render_amazon_dynamic_attributes(frm, true);
		});
	});

	$header.find(".btn-clear-amz-inputs").on("click", function (e) {
		e.preventDefault();
		frappe.confirm(__("Clear all Amazon attribute fields?"), function () {
			Object.keys(frm._amz_controls || {}).forEach((k) => {
				const ctrl = frm._amz_controls[k].control;
				const ft = frm._amz_controls[k].meta.fieldtype;
				ctrl.set_value(ft === "Check" ? 0 : "");
			});
			frm.set_value("amazon_attributes", "{}");
		});
	});

	const req_fields = fields_meta.filter((m) => m.reqd);
	const opt_fields = fields_meta.filter((m) => !m.reqd);

	function render_section_fields(container_title, fields, is_required_section) {
		if (!fields.length) return;

		const header_cls = is_required_section ? "" : "amz-attr-section-header--optional";
		const $section = $(`
			<div class="amz-attr-section">
				<div class="amz-attr-section-header ${header_cls}">
					<span>${container_title}</span>
					<span class="amz-attr-pill amz-attr-pill--muted">${fields.length}</span>
				</div>
				<div class="amz-attr-grid"></div>
			</div>
		`).appendTo($wrapper);

		const $grid = $section.find(".amz-attr-grid");

		fields.forEach((meta) => {
			const is_full =
				meta.widget_type === "dimensions" ||
				meta.widget_type === "bullet_list" ||
				meta.fieldtype === "Small Text";
			const $cell = $(`<div class="amz-attr-cell ${is_full ? "amz-attr-cell--full" : ""}"></div>`).appendTo(
				$grid
			);

			const control = create_attribute_control($cell, meta, sync_dynamic_controls_to_doc);

			let initial_val = extract_control_display_value(current_attrs[meta.fieldname], meta);
			if (initial_val === null && meta.is_safe_default) {
				initial_val = extract_control_display_value(meta.default, meta);
			}

			if (initial_val !== undefined && initial_val !== null && initial_val !== "") {
				control.set_value(initial_val);
			}

			frm._amz_controls[meta.fieldname] = { control: control, meta: meta };
		});
	}

	render_section_fields(__("Mandatory Category Attributes"), req_fields, true);
	render_section_fields(__("Additional Specifications (Optional)"), opt_fields, false);

	frm._amz_sync_fn = sync_dynamic_controls_to_doc;

	// Automatically map any existing variant attributes from Variants tab
	map_variant_attributes_to_controls(frm, false);
}

frappe.ui.form.on("Item", {
	setup(frm) {
		frm.set_query("amazon_product_type", function () {
			return {
				query: "amazon_connector.amazon_connector.doctype.amazon_product_type.amazon_product_type.get_product_types_list",
			};
		});
	},

	refresh(frm) {
		frm.set_query("amazon_product_type", function () {
			return {
				query: "amazon_connector.amazon_connector.doctype.amazon_product_type.amazon_product_type.get_product_types_list",
			};
		});

		// If this is a variant and has no product type, inherit from template
		if (frm.doc.variant_of && !frm.doc.amazon_product_type) {
			frappe.db.get_value(
				"Item",
				frm.doc.variant_of,
				["amazon_product_type", "amazon_attributes"],
				function (r) {
					if (r && r.amazon_product_type) {
						frm.set_value("amazon_product_type", r.amazon_product_type);
						if (r.amazon_attributes && (!frm.doc.amazon_attributes || frm.doc.amazon_attributes === "{}")) {
							frm.set_value("amazon_attributes", r.amazon_attributes);
						}
					}
					render_amazon_dynamic_attributes(frm);
				}
			);
		} else {
			render_amazon_dynamic_attributes(frm);
		}

		// Preload Attributes button works for both new and saved items
		frm.add_custom_button(
			__("Preload Attributes Template"),
			function () {
				const pt = frm.doc.amazon_product_type;
				if (!pt) {
					frappe.msgprint(__("Please select an Amazon Product Type in the Details tab first."));
					return;
				}
				render_amazon_dynamic_attributes(frm, true);
			},
			__("Amazon SP-API")
		);

		if (frm.is_new()) return;

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
								data.status ||
									(data.summaries &&
										data.summaries[0] &&
										data.summaries[0].status &&
										data.summaries[0].status.join(", ")) ||
									"FETCHED"
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

	amazon_product_type(frm) {
		render_amazon_dynamic_attributes(frm, true);
	},

	variant_of(frm) {
		if (frm.doc.variant_of) {
			frappe.db.get_value(
				"Item",
				frm.doc.variant_of,
				["amazon_product_type", "amazon_attributes"],
				function (r) {
					if (r) {
						if (r.amazon_product_type && !frm.doc.amazon_product_type) {
							frm.set_value("amazon_product_type", r.amazon_product_type);
						}
						if (r.amazon_attributes && (!frm.doc.amazon_attributes || frm.doc.amazon_attributes === "{}")) {
							frm.set_value("amazon_attributes", r.amazon_attributes);
						}
						render_amazon_dynamic_attributes(frm, true);
					}
				}
			);
		}
	},

	item_group(frm) {
		if (!frm.doc.item_group) return;
		frappe.db.get_value(
			"Item Group",
			frm.doc.item_group,
			["amazon_product_type", "amazon_attributes_template"],
			function (r) {
				if (r) {
					if (r.amazon_product_type && !frm.doc.amazon_product_type) {
						frm.set_value("amazon_product_type", r.amazon_product_type);
					}
					const has_attrs =
						frm.doc.amazon_attributes &&
						frm.doc.amazon_attributes.trim() !== "" &&
						frm.doc.amazon_attributes.trim() !== "{}";
					if (!has_attrs && r.amazon_attributes_template) {
						frm.set_value("amazon_attributes", r.amazon_attributes_template);
					}
					render_amazon_dynamic_attributes(frm, true);
				}
			}
		);
	},

	before_save(frm) {
		if (frm._amz_sync_fn) {
			frm._amz_sync_fn();
		}
	},
});

// Real-time synchronization when user modifies the Variant Attributes child table
frappe.ui.form.on("Item Variant Attribute", {
	attribute_value(frm) {
		map_variant_attributes_to_controls(frm, false);
	},
	attribute(frm) {
		map_variant_attributes_to_controls(frm, false);
	},
});

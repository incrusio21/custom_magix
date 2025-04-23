// Copyright (c) 2025, DAS and Contributors
// License: GNU General Public License v3. See license.txt

frappe.ui.form.on("Sales Order", {
    refresh: function (frm) {
		if (frm.doc.docstatus === 1) {
			if (
				frm.doc.status !== "Closed" &&
				flt(frm.doc.per_billed) == 0 &&
				frm.has_perm("write")
			) {
				frm.add_custom_button(__("Update Tax"), () => {
					erpnext.utils.update_tax_items({
						frm: frm,
						child_docname: "taxes",
						child_doctype: "Sales Taxes and Charges",
						cannot_add_row: false,
					});
				});
            }
        }
    }
})

erpnext.utils.update_tax_items = function (opts) {
	const frm = opts.frm;
	const cannot_add_row = typeof opts.cannot_add_row === "undefined" ? true : opts.cannot_add_row;
	const child_docname = typeof opts.cannot_add_row === "undefined" ? "taxes" : opts.child_docname;
	const child_doctype = typeof opts.child_doctype === "undefined" ? "Sales Taxes and Charges" : opts.child_doctype;
	const child_meta = frappe.get_meta(child_doctype);
	const get_precision = (fieldname) => child_meta.fields.find((f) => f.fieldname == fieldname).precision;

	this.data = frm.doc[opts.child_docname].map((d) => {
		return {
			docname: d.name,
			name: d.name,
			charge_type: d.charge_type,
			account_head: d.account_head,
			description: d.description,
			rate: d.rate,
			net_amount: d.net_amount,
			tax_amount: d.tax_amount,
			total: d.total,
		};
	});

	const fields = [
		{
			fieldtype: "Data",
			fieldname: "docname",
			read_only: 1,
			hidden: 1,
		},
		{
			fieldtype: "Select",
			fieldname: "charge_type",
			label: __("Type"),
			options: [
				"",
				"Actual",
				"On Net Total",
				"On Previous Row Amount",
				"On Previous Row Total",
				"On Item Quantity"
			],
			reqd: 1,
			in_list_view: 1,
		},
		{
			fieldtype: "Link",
			fieldname: "account_head",
			options: "Account",
			label: __("Account Head"),
			reqd: 1,
			in_list_view: 1,
			onchange: function () {
				const me = this;

				if(!me.doc.charge_type && me.doc.account_head){
					frappe.msgprint(__("Please select Charge Type first"));
					me.doc.account_head = ""
				} else if (me.doc.account_head) {
					frappe.call({
						type:"GET",
						method: "erpnext.controllers.accounts_controller.get_tax_rate",
						args: {"account_head": me.doc.account_head},
						callback: function(r) {
							if (r.message) {
								const {
									account_name,
									tax_rate,
								} = r.message;
									let rate = me.doc.rate
									if (me.doc.charge_type!=="Actual") {
										rate = tax_rate || 0
									}
									
									Object.assign(me.doc, {
										description: account_name,
										rate: rate,
									});
							}
						}
					})
				}
			}
		},
		{
			fieldtype: "Column Break",
			fieldname: "",
		},
		{
			fieldtype: "Small Text",
			fieldname: "description",
			options: "Account",
			label: __("Description"),
			reqd: 1,
		},
		{
			fieldtype: "Check",
			fieldname: "included_in_print_rate",
			label: __("Is this Tax included in Basic Rate?"),
			description: "If checked, the tax amount will be considered as already included in the Print Rate / Print Amount",
		},
		{
			fieldtype: "Section Break",
			fieldname: "",
		},
		{
			fieldtype: "Float",
			fieldname: "rate",
			read_only: 0,
			in_list_view: 1,
			// columns: 1,
			label: __("Tax Rate"),
			precision: get_precision("rate"),
		},
		// {
		// 	fieldtype: "Currency",
		// 	fieldname: "net_amount",
		// 	options: "currency",
		// 	read_only: 1,
		// 	in_list_view: 1,
		// 	label: __("Net Amount"),
		// 	precision: get_precision("net_amount"),
		// },
		{
			fieldtype: "Currency",
			fieldname: "tax_amount",
			options: "currency",
			read_only: 0,
			in_list_view: 1,
			label: __("Amount"),
			precision: get_precision("tax_amount"),
		},
		// {
		// 	fieldtype: "Currency",
		// 	fieldname: "total",
		// 	options: "currency",
		// 	read_only: 1,
		// 	in_list_view: 1,
		// 	label: __("Total"),
		// 	precision: get_precision("total"),
		// },
	];

	let dialog = new frappe.ui.Dialog({
		title: __("Update Tax"),
		size: "extra-large",
		fields: [
			{
				fieldname: "trans_items",
				fieldtype: "Table",
				label: "Taxes",
				cannot_add_rows: cannot_add_row,
				in_place_edit: false,
				reqd: 1,
				data: this.data,
				get_data: () => {
					return this.data;
				},
				fields: fields,
			},
		],
		primary_action: function () {
			var me = this
			const trans_items = this.get_values()["trans_items"].filter((item) => !!(item.charge_type || item.account_head));
			frappe.call({
				method: "custom_magix.controllers.accounts_controller.update_child_rate",
				freeze: true,
				args: {
					parent_doctype: frm.doc.doctype,
					trans_items: trans_items,
					parent_doctype_name: frm.doc.name,
					child_doctype: child_doctype,
					child_docname: child_docname,
				},
				callback: function () {
					frm.reload_doc();
					me.hide();
				},
			});
			refresh_field(child_docname);
		},
		primary_action_label: __("Update"),
		on_page_show: () => {
			
			// dialog.fields_dict.trans_items.grid.grid_rows.forEach((grid_row) => {
			// 	if(grid_row) {
			// 		if(grid_row.doc.charge_type==="Actual") {
			// 			grid_row.toggle_editable("tax_amount", true);
			// 			grid_row.toggle_reqd("tax_amount", true);
			// 			grid_row.toggle_editable("rate", false);
			// 			grid_row.toggle_reqd("rate", false);
			// 		} else {
			// 			grid_row.toggle_editable("rate", true);
			// 			grid_row.toggle_reqd("rate", true);
			// 			grid_row.toggle_editable("tax_amount", false);
			// 			grid_row.toggle_reqd("tax_amount", false);
			// 		}
			// 	}
			// });

			// dialog.fields_dict.trans_items.grid.refresh();
			// console.log(dialog.fields_dict.trans_items.grid)
		}
	});

	dialog.show();
};
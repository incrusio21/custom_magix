# Copyright (c) 2025, DAS and contributors
# For license information, please see license.txt

import frappe
from frappe import _, qb, throw
from frappe.query_builder.functions import Sum
from frappe.utils import cstr, flt, formatdate, get_link_to_form, getdate, nowdate

import erpnext
from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import get_accounting_dimensions
from erpnext.accounts.deferred_revenue import validate_service_stop_date
from erpnext.accounts.doctype.sales_invoice.sales_invoice import (
	get_total_in_party_account_currency,
	is_overdue,
	validate_inter_company_party,
)
from erpnext.accounts.doctype.tax_withholding_category.tax_withholding_category import (
	get_party_tax_withholding_details,
)
from erpnext.accounts.general_ledger import (
	get_round_off_account_and_cost_center,
	merge_similar_entries,
)
from erpnext.accounts.party import get_due_date, get_party_account
from erpnext.accounts.utils import get_account_currency
from erpnext.assets.doctype.asset.asset import is_cwip_accounting_enabled
from erpnext.assets.doctype.asset_category.asset_category import get_asset_category_account
from erpnext.controllers.accounts_controller import validate_account_head
from erpnext.controllers.buying_controller import BuyingController
from erpnext.stock import get_warehouse_account_map


class WarehouseMissingError(frappe.ValidationError):
	pass


class PettyCash(BuyingController):
	
	def onload(self):
		super().onload()
		supplier_tds = frappe.db.get_value("Supplier", self.supplier, "tax_withholding_category")
		self.set_onload("supplier_tds", supplier_tds)

		if self.is_new():
			self.set("tax_withheld_vouchers", [])

	def before_save(self):
		if not self.on_hold:
			self.release_date = ""

	def invoice_is_blocked(self):
		return self.on_hold and (not self.release_date or self.release_date > getdate(nowdate()))

	def validate(self):
		if not self.is_opening:
			self.is_opening = "No"

		self.validate_posting_time()

		super().validate()

		# validate service stop date to lie in between start and end date
		validate_service_stop_date(self)

		self.validate_release_date()
		self.check_conversion_rate()
		self.validate_credit_to_acc()
		self.clear_unallocated_advances("Purchase Invoice Advance", "advances")
		self.validate_uom_is_integer("uom", "qty")
		self.validate_uom_is_integer("stock_uom", "stock_qty")
		self.set_expense_account(for_validate=True)
		self.validate_expense_account()
		self.set_against_expense_account()
		self.set_status()
		validate_inter_company_party(
			self.doctype, self.supplier, self.company, self.inter_company_invoice_reference
		)
		self.reset_default_field_value("set_warehouse", "items", "warehouse")
		self.reset_default_field_value("rejected_warehouse", "items", "rejected_warehouse")
		self.reset_default_field_value("set_from_warehouse", "items", "from_warehouse")

	def validate_release_date(self):
		if self.release_date and getdate(nowdate()) >= getdate(self.release_date):
			frappe.throw(_("Release date must be in the future"))

	def validate_cash(self):
		if not self.cash_bank_account and flt(self.paid_amount):
			frappe.throw(_("Cash or Bank Account is mandatory for making payment entry"))

		if flt(self.paid_amount) + flt(self.write_off_amount) - flt(
			self.get("rounded_total") or self.grand_total
		) > 1 / (10 ** (self.precision("base_grand_total") + 1)):
			frappe.throw(_("""Paid amount + Write Off Amount can not be greater than Grand Total"""))

	def create_remarks(self):
		if not self.remarks:
			if self.bill_no:
				self.remarks = _("Against Supplier Invoice {0}").format(self.bill_no)
				if self.bill_date:
					self.remarks += " " + _("dated {0}").format(formatdate(self.bill_date))

			else:
				self.remarks = _("No Remarks")

	def set_missing_values(self, for_validate=False):
		if not self.credit_to:
			self.credit_to = get_party_account("Supplier", self.supplier, self.company)
			self.party_account_currency = frappe.get_cached_value(
				"Account", self.credit_to, "account_currency"
			)
		if not self.due_date:
			self.due_date = get_due_date(
				self.posting_date, "Supplier", self.supplier, self.company, self.bill_date
			)

		tds_category = frappe.db.get_value("Supplier", self.supplier, "tax_withholding_category")
		if tds_category and not for_validate:
			self.apply_tds = 1
			self.tax_withholding_category = tds_category
			self.set_onload("supplier_tds", tds_category)

		super().set_missing_values(for_validate)

	def validate_credit_to_acc(self):
		if not self.credit_to:
			self.credit_to = get_party_account("Supplier", self.supplier, self.company)
			if not self.credit_to:
				self.raise_missing_debit_credit_account_error("Supplier", self.supplier)

		account = frappe.get_cached_value(
			"Account", self.credit_to, ["account_type", "report_type", "account_currency"], as_dict=True
		)

		self.party_account_currency = account.account_currency

	def validate_item_code(self):
		for d in self.get("items"):
			if not d.item_code:
				frappe.msgprint(_("Item Code required at Row No {0}").format(d.idx), raise_exception=True)

	def set_expense_account(self, for_validate=False):
		auto_accounting_for_stock = erpnext.is_perpetual_inventory_enabled(self.company)

		if auto_accounting_for_stock:
			stock_not_billed_account = self.get_company_default("stock_received_but_not_billed")
			stock_items = self.get_stock_items()

		self.asset_received_but_not_billed = None

		for item in self.get("items"):
			# in case of auto inventory accounting,
			# expense account is always "Stock Received But Not Billed" for a stock item
			# except opening entry, drop-ship entry and fixed asset items
			if (
				auto_accounting_for_stock
				and item.item_code in stock_items
				and self.is_opening == "No"
				and not item.is_fixed_asset
				and (
					not item.po_detail
					or not frappe.db.get_value("Purchase Order Item", item.po_detail, "delivered_by_supplier")
				)
			):
				if self.update_stock and item.warehouse and (not item.from_warehouse):
					if (
						for_validate
						and item.expense_account
						and item.expense_account != warehouse_account[item.warehouse]["account"]
					):
						msg = _(
							"Row {0}: Expense Head changed to {1} because account {2} is not linked to warehouse {3} or it is not the default inventory account"
						).format(
							item.idx,
							frappe.bold(warehouse_account[item.warehouse]["account"]),
							frappe.bold(item.expense_account),
							frappe.bold(item.warehouse),
						)
						frappe.msgprint(msg, title=_("Expense Head Changed"))
					item.expense_account = warehouse_account[item.warehouse]["account"]
				else:
					# check if 'Stock Received But Not Billed' account is credited in Purchase receipt or not
					if item.purchase_receipt:
						negative_expense_booked_in_pr = frappe.db.sql(
							"""select name from `tabGL Entry`
							where voucher_type='Purchase Receipt' and voucher_no=%s and account = %s""",
							(item.purchase_receipt, stock_not_billed_account),
						)

						if negative_expense_booked_in_pr:
							if (
								for_validate
								and item.expense_account
								and item.expense_account != stock_not_billed_account
							):
								msg = _(
									"Row {0}: Expense Head changed to {1} because expense is booked against this account in Purchase Receipt {2}"
								).format(
									item.idx,
									frappe.bold(stock_not_billed_account),
									frappe.bold(item.purchase_receipt),
								)
								frappe.msgprint(msg, title=_("Expense Head Changed"))

							item.expense_account = stock_not_billed_account
					else:
						# If no purchase receipt present then book expense in 'Stock Received But Not Billed'
						# This is done in cases when Purchase Invoice is created before Purchase Receipt
						if (
							for_validate
							and item.expense_account
							and item.expense_account != stock_not_billed_account
						):
							msg = _(
								"Row {0}: Expense Head changed to {1} as no Purchase Receipt is created against Item {2}."
							).format(
								item.idx, frappe.bold(stock_not_billed_account), frappe.bold(item.item_code)
							)
							msg += "<br>"
							msg += _(
								"This is done to handle accounting for cases when Purchase Receipt is created after Purchase Invoice"
							)
							frappe.msgprint(msg, title=_("Expense Head Changed"))

						item.expense_account = stock_not_billed_account
			elif item.is_fixed_asset:
				account = None
				if not item.pr_detail and item.po_detail:
					receipt_item = frappe.get_cached_value(
						"Purchase Receipt Item",
						{
							"purchase_order": item.purchase_order,
							"purchase_order_item": item.po_detail,
							"docstatus": 1,
						},
						["name", "parent"],
						as_dict=1,
					)
					if receipt_item:
						item.pr_detail = receipt_item.name
						item.purchase_receipt = receipt_item.parent

				if item.pr_detail:
					if not self.asset_received_but_not_billed:
						self.asset_received_but_not_billed = self.get_company_default(
							"asset_received_but_not_billed"
						)

					# check if 'Asset Received But Not Billed' account is credited in Purchase receipt or not
					arbnb_booked_in_pr = frappe.db.get_value(
						"GL Entry",
						{
							"voucher_type": "Purchase Receipt",
							"voucher_no": item.purchase_receipt,
							"account": self.asset_received_but_not_billed,
						},
						"name",
					)
					if arbnb_booked_in_pr:
						account = self.asset_received_but_not_billed

				if not account:
					account_type = (
						"capital_work_in_progress_account"
						if is_cwip_accounting_enabled(item.asset_category)
						else "fixed_asset_account"
					)
					account = get_asset_category_account(
						account_type, item=item.item_code, company=self.company
					)
					if not account:
						form_link = get_link_to_form("Asset Category", item.asset_category)
						throw(
							_("Please set Fixed Asset Account in {} against {}.").format(
								form_link, self.company
							),
							title=_("Missing Account"),
						)
				item.expense_account = account
			elif not item.expense_account and for_validate:
				throw(_("Expense account is mandatory for item {0}").format(item.item_code or item.item_name))

	def validate_expense_account(self):
		for item in self.get("items"):
			validate_account_head(item.idx, item.expense_account, self.company, _("Expense"))

	def set_against_expense_account(self, force=False):
		against_accounts = []
		for item in self.get("items"):
			if item.expense_account and (item.expense_account not in against_accounts):
				against_accounts.append(item.expense_account)

		self.against_expense_account = ",".join(against_accounts)

	def force_set_against_expense_account(self):
		self.set_against_expense_account()
		frappe.db.set_value(self.doctype, self.name, "against_expense_account", self.against_expense_account)

	def before_submit(self):
		self.create_remarks()

	def on_submit(self):
		super().on_submit()

		# this sequence because outstanding may get -negative
		self.make_journal_entries()

	def on_cancel(self):
		super().on_cancel()

		self.db_set("status", "Cancelled")
		frappe.flags.remove_journal_entry = 1
		for jv in frappe.get_all("Journal Entry", filters={"petty_cash": self.name, "docstatus": 1 }, pluck="name"):
			doc = frappe.get_doc("Journal Entry", jv)
			doc.cancel()
			doc.delete()

	def make_journal_entries(self):
		if self.docstatus == 1:
			gl_entries = self.get_gl_entries()
			if not gl_entries:
				return
			
			doc = frappe.new_doc("Journal Entry")
			doc.update({
				"company": self.company,
				"sales_order": self.sales_order,
				"posting_date": self.posting_date,
				"petty_cash": self.name,
			})

			for gl in gl_entries:
				account_je = {
					"account": gl.account,
					"party_type": gl.party_type,
					"party": gl.party,
					"debit": gl.debit,
					"credit": gl.credit,
					"debit_in_account_currency": gl.debit_in_account_currency,
					"credit_in_account_currency": gl.credit_in_account_currency,
					"cost_center": gl.cost_center,
					"user_remark": gl.get("user_remark")
				}

				accounting_dimensions = get_accounting_dimensions()
				dimension_dict = {}

				for dimension in accounting_dimensions:
					dimension_dict[dimension] = self.get(dimension)
					if gl.get(dimension):
						dimension_dict[dimension] = gl.get(dimension)

				account_je.update(dimension_dict)

				doc.append("accounts", account_je)

			doc.submit()

	def get_gl_entries(self, warehouse_account=None):
		self.auto_accounting_for_stock = erpnext.is_perpetual_inventory_enabled(self.company)

		if self.auto_accounting_for_stock:
			self.stock_received_but_not_billed = self.get_company_default("stock_received_but_not_billed")
		else:
			self.stock_received_but_not_billed = None

		self.negative_expense_to_be_booked = 0.0
		gl_entries = []

		self.make_supplier_gl_entry(gl_entries)
		self.make_item_gl_entries(gl_entries)
		self.make_precision_loss_gl_entry(gl_entries)

		self.make_tax_gl_entries(gl_entries)
		self.make_gl_entries_for_tax_withholding(gl_entries)

		gl_entries = merge_similar_entries(gl_entries)

		self.make_gle_for_rounding_adjustment(gl_entries)
		self.set_transaction_currency_and_rate_in_gl_map(gl_entries)
		return gl_entries
	
	def make_supplier_gl_entry(self, gl_entries):
		# Checked both rounding_adjustment and rounded_total
		# because rounded_total had value even before introduction of posting GLE based on rounded total
		grand_total = (
			self.rounded_total if (self.rounding_adjustment and self.rounded_total) else self.grand_total
		)
		base_grand_total = flt(
			self.base_rounded_total
			if (self.base_rounding_adjustment and self.base_rounded_total)
			else self.base_grand_total,
			self.precision("base_grand_total"),
		)

		if grand_total:
			self.add_supplier_gl_entry(gl_entries, base_grand_total, grand_total)

	def add_supplier_gl_entry(
		self, gl_entries, base_grand_total, grand_total, against_account=None, remarks=None, skip_merge=False
	):
		against_voucher = self.name
		if self.is_return and self.return_against and not self.update_outstanding_for_self:
			against_voucher = self.return_against

		# Did not use base_grand_total to book rounding loss gle
		gl = {
			"account": self.credit_to,
			# "party_type": "Supplier",
			# "party": self.supplier,
			"due_date": self.due_date,
			"against": against_account or self.against_expense_account,
			"credit": base_grand_total,
			"credit_in_account_currency": base_grand_total
			if self.party_account_currency == self.company_currency
			else grand_total,
			"credit_in_transaction_currency": grand_total,
			"against_voucher": against_voucher,
			"against_voucher_type": self.doctype,
			"project": self.project,
			"cost_center": self.cost_center,
			"_skip_merge": skip_merge,
		}

		if remarks:
			gl["remarks"] = remarks

		gl_entries.append(self.get_gl_dict(gl, self.party_account_currency, item=self))

	def make_item_gl_entries(self, gl_entries):
		# item gl entries
		for item in self.get("items"):
			if flt(item.base_net_amount):
				if item.item_code:
					frappe.get_cached_value("Item", item.item_code, "asset_category")

				expense_account = (
					item.expense_account
					if (not item.enable_deferred_expense or self.is_return)
					else item.deferred_expense_account
				)

				account_currency = get_account_currency(expense_account)
				amount, base_amount = self.get_amount_and_base_amount(item, None)

				if not self.is_internal_transfer():
					gl_entries.append(
						self.get_gl_dict(
							{
								"account": expense_account,
								"against": self.supplier,
								"debit": base_amount,
								"debit_in_transaction_currency": amount,
								"cost_center": item.cost_center,
								"project": item.project or self.project,
							},
							account_currency,
							item=item,
						)
					)

	def make_precision_loss_gl_entry(self, gl_entries):
		(
			round_off_account,
			round_off_cost_center,
			round_off_for_opening,
		) = get_round_off_account_and_cost_center(
			self.company, "Purchase Invoice", self.name, self.use_company_roundoff_cost_center
		)

		precision_loss = self.get("base_net_total") - flt(
			self.get("net_total") * self.conversion_rate, self.precision("net_total")
		)

		if precision_loss:
			gl_entries.append(
				self.get_gl_dict(
					{
						"account": round_off_account,
						"against": self.supplier,
						"credit": precision_loss,
						"cost_center": round_off_cost_center
						if self.use_company_roundoff_cost_center
						else self.cost_center or round_off_cost_center,
						"remarks": _("Net total calculation precision loss"),
					}
				)
			)

	def make_tax_gl_entries(self, gl_entries):
		# tax table gl entries
		valuation_tax = {}

		for tax in self.get("taxes"):
			amount, base_amount = self.get_tax_amounts(tax, None)
			if tax.category in ("Total", "Valuation and Total") and flt(base_amount):
				account_currency = get_account_currency(tax.account_head)

				dr_or_cr = "debit" if tax.add_deduct_tax == "Add" else "credit"

				gl_entries.append(
					self.get_gl_dict(
						{
							"account": tax.account_head,
							"against": self.supplier,
							dr_or_cr: base_amount,
							dr_or_cr + "_in_account_currency": base_amount
							if account_currency == self.company_currency
							else amount,
							dr_or_cr + "_in_transaction_currency": amount,
							"cost_center": tax.cost_center,
						},
						account_currency,
						item=tax,
					)
				)
			# accumulate valuation tax
			if (
				self.is_opening == "No"
				and tax.category in ("Valuation", "Valuation and Total")
				and flt(base_amount)
				and not self.is_internal_transfer()
			):
				if self.auto_accounting_for_stock and not tax.cost_center:
					frappe.throw(
						_("Cost Center is required in row {0} in Taxes table for type {1}").format(
							tax.idx, _(tax.category)
						)
					)
				valuation_tax.setdefault(tax.name, 0)
				valuation_tax[tax.name] += (tax.add_deduct_tax == "Add" and 1 or -1) * flt(base_amount)

		if self.is_opening == "No" and self.negative_expense_to_be_booked and valuation_tax:
			# credit valuation tax amount in "Expenses Included In Valuation"
			# this will balance out valuation amount included in cost of goods sold

			total_valuation_amount = sum(valuation_tax.values())
			amount_including_divisional_loss = self.negative_expense_to_be_booked
			i = 1
			for tax in self.get("taxes"):
				if valuation_tax.get(tax.name):
					if i == len(valuation_tax):
						applicable_amount = amount_including_divisional_loss
					else:
						applicable_amount = self.negative_expense_to_be_booked * (
							valuation_tax[tax.name] / total_valuation_amount
						)
						amount_including_divisional_loss -= applicable_amount

					gl_entries.append(
						self.get_gl_dict(
							{
								"account": tax.account_head,
								"cost_center": tax.cost_center,
								"against": self.supplier,
								"credit": applicable_amount,
								"credit_in_transaction_currency": flt(
									applicable_amount / self.conversion_rate,
									frappe.get_precision("Petty Cash Item", "item_tax_amount"),
								),
								"remarks": self.remarks or _("Accounting Entry for Stock"),
							},
							item=tax,
						)
					)

					i += 1
					
	def make_gl_entries_for_tax_withholding(self, gl_entries):
		"""
		Tax withholding amount is not part of supplier invoice.
		Separate supplier GL Entry for correct reporting.
		"""
		if not self.apply_tds:
			return

		for row in self.get("taxes"):
			if not row.is_tax_withholding_account or not row.tax_amount:
				continue

			base_tds_amount = row.base_tax_amount_after_discount_amount
			tds_amount = row.tax_amount_after_discount_amount

			self.add_supplier_gl_entry(gl_entries, base_tds_amount, tds_amount)
			self.add_supplier_gl_entry(
				gl_entries,
				-base_tds_amount,
				-tds_amount,
				against_account=row.account_head,
				remarks=_("TDS Deducted"),
				skip_merge=True,
			)

	def make_gle_for_rounding_adjustment(self, gl_entries):
		# if rounding adjustment in small and conversion rate is also small then
		# base_rounding_adjustment may become zero due to small precision
		# eg: rounding_adjustment = 0.01 and exchange rate = 0.05 and precision of base_rounding_adjustment is 2
		# 	then base_rounding_adjustment becomes zero and error is thrown in GL Entry
		if self.rounding_adjustment and self.base_rounding_adjustment:
			(
				round_off_account,
				round_off_cost_center,
				round_off_for_opening,
			) = get_round_off_account_and_cost_center(
				self.company, "Petty Cash", self.name, self.use_company_roundoff_cost_center
			)

			if self.is_opening == "Yes" and self.rounding_adjustment:
				if not round_off_for_opening:
					frappe.throw(
						_(
							"Opening Invoice has rounding adjustment of {0}.<br><br> '{1}' account is required to post these values. Please set it in Company: {2}.<br><br> Or, '{3}' can be enabled to not post any rounding adjustment."
						).format(
							frappe.bold(self.rounding_adjustment),
							frappe.bold("Round Off for Opening"),
							get_link_to_form("Company", self.company),
							frappe.bold("Disable Rounded Total"),
						)
					)
				else:
					round_off_account = round_off_for_opening

			gl_entries.append(
				self.get_gl_dict(
					{
						"account": round_off_account,
						"against": self.supplier,
						"debit_in_account_currency": self.rounding_adjustment,
						"debit": self.base_rounding_adjustment,
						"cost_center": round_off_cost_center
						if self.use_company_roundoff_cost_center
						else (self.cost_center or round_off_cost_center),
					},
					item=self,
				)
			)

	def update_project(self):
		projects = frappe._dict()
		for d in self.items:
			if d.project:
				if self.docstatus == 1:
					projects[d.project] = projects.get(d.project, 0) + d.base_net_amount
				elif self.docstatus == 2:
					projects[d.project] = projects.get(d.project, 0) - d.base_net_amount

		pj = frappe.qb.DocType("Project")
		for proj, value in projects.items():
			res = frappe.qb.from_(pj).select(pj.total_purchase_cost).where(pj.name == proj).for_update().run()
			current_purchase_cost = res and res[0][0] or 0
			# frappe.db.set_value("Project", proj, "total_purchase_cost", current_purchase_cost + value)
			project_doc = frappe.get_doc("Project", proj)
			project_doc.total_purchase_cost = current_purchase_cost + value
			project_doc.calculate_gross_margin()
			project_doc.db_update()

	def block_invoice(self, hold_comment=None, release_date=None):
		self.db_set("on_hold", 1)
		self.db_set("hold_comment", cstr(hold_comment))
		self.db_set("release_date", release_date)

	def unblock_invoice(self):
		self.db_set("on_hold", 0)
		self.db_set("release_date", None)

	def set_tax_withholding(self):
		self.set("advance_tax", [])
		self.set("tax_withheld_vouchers", [])

		if not self.apply_tds:
			return

		if self.apply_tds and not self.get("tax_withholding_category"):
			self.tax_withholding_category = frappe.db.get_value(
				"Supplier", self.supplier, "tax_withholding_category"
			)

		if not self.tax_withholding_category:
			return

		tax_withholding_details, advance_taxes, voucher_wise_amount = get_party_tax_withholding_details(
			self, self.tax_withholding_category
		)

		# Adjust TDS paid on advances
		self.allocate_advance_tds(tax_withholding_details, advance_taxes)

		if not tax_withholding_details:
			return

		accounts = []
		for d in self.taxes:
			if d.account_head == tax_withholding_details.get("account_head"):
				d.update(tax_withholding_details)

			accounts.append(d.account_head)

		if not accounts or tax_withholding_details.get("account_head") not in accounts:
			self.append("taxes", tax_withholding_details)

		to_remove = [
			d
			for d in self.taxes
			if not d.tax_amount and d.account_head == tax_withholding_details.get("account_head")
		]

		for d in to_remove:
			self.remove(d)

		## Add pending vouchers on which tax was withheld
		for row in voucher_wise_amount:
			self.append(
				"tax_withheld_vouchers",
				{
					"voucher_name": row.voucher_name,
					"voucher_type": row.voucher_type,
					"taxable_amount": row.taxable_amount,
				},
			)

		# calculate totals again after applying TDS
		self.calculate_taxes_and_totals()

	def allocate_advance_tds(self, tax_withholding_details, advance_taxes):
		for tax in advance_taxes:
			allocated_amount = 0
			pending_amount = flt(tax.tax_amount - tax.allocated_amount)
			if flt(tax_withholding_details.get("tax_amount")) >= pending_amount:
				tax_withholding_details["tax_amount"] -= pending_amount
				allocated_amount = pending_amount
			elif (
				flt(tax_withholding_details.get("tax_amount"))
				and flt(tax_withholding_details.get("tax_amount")) < pending_amount
			):
				allocated_amount = tax_withholding_details["tax_amount"]
				tax_withholding_details["tax_amount"] = 0

			self.append(
				"advance_tax",
				{
					"reference_type": "Payment Entry",
					"reference_name": tax.parent,
					"reference_detail": tax.name,
					"account_head": tax.account_head,
					"allocated_amount": allocated_amount,
				},
			)

	def update_advance_tax_references(self, cancel=0):
		for tax in self.get("advance_tax"):
			at = frappe.qb.DocType("Advance Taxes and Charges").as_("at")

			if cancel:
				frappe.qb.update(at).set(
					at.allocated_amount, at.allocated_amount - tax.allocated_amount
				).where(at.name == tax.reference_detail).run()
			else:
				frappe.qb.update(at).set(
					at.allocated_amount, at.allocated_amount + tax.allocated_amount
				).where(at.name == tax.reference_detail).run()

	def set_status(self, update=False, status=None, update_modified=True):
		if self.is_new():
			if self.get("amended_from"):
				self.status = "Draft"
			return

		outstanding_amount = flt(self.outstanding_amount, self.precision("outstanding_amount"))
		total = get_total_in_party_account_currency(self)

		if not status:
			if self.docstatus == 2:
				status = "Cancelled"
			elif self.docstatus == 1:
				if is_overdue(self, total):
					self.status = "Overdue"
				elif 0 < outstanding_amount < total:
					self.status = "Partly Paid"
				elif outstanding_amount > 0 and getdate(self.due_date) >= getdate():
					self.status = "Unpaid"
				# Check if outstanding amount is 0 due to debit note issued against invoice
				elif self.is_return == 0 and frappe.db.get_value(
					"Petty", {"is_return": 1, "return_against": self.name, "docstatus": 1}
				):
					self.status = "Debit Note Issued"
				elif self.is_return == 1:
					self.status = "Return"
				elif outstanding_amount <= 0:
					self.status = "Paid"
				else:
					self.status = "Submitted"
			else:
				self.status = "Draft"

		if update:
			self.db_set("status", self.status, update_modified=update_modified)

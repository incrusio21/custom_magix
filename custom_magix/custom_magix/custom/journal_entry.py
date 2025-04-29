# Copyright (c) 2025, DAS and contributors
# For license information, please see license.txt

import frappe

def validate_petty_cash(self, method):
    if self.petty_cash and not frappe.flags.remove_journal_entry:
        frappe.throw("Journal entries can only be canceled from Petty Cash.")
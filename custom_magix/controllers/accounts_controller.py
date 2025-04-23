# Copyright (c) 2025, DAS and Contributors
# License: GNU General Public License v3. See license.txt

import json
import frappe
from frappe import _
from frappe.utils import flt

@frappe.whitelist()
def update_child_rate(parent_doctype, trans_items, parent_doctype_name, child_doctype, child_docname="taxes"):
    def check_doc_permissions(doc, perm_type="create"):
        try:
            doc.check_permission(perm_type)
        except frappe.PermissionError:
            actions = {"create": "add", "write": "update"}

            frappe.throw(
                _("You do not have permissions to {} items in a {}.").format(
                    actions[perm_type], parent_doctype
                ),
                title=_("Insufficient Permissions"),
            )

    def get_new_child_item(item_row):
        child_item = frappe.new_doc(child_doctype, parent_doc=parent, parentfield=child_docname)
        return child_item
    
    data = json.loads(trans_items)

    items_added_or_removed = False  # updated to true if any new item is added or removed

    parent = frappe.get_doc(parent_doctype, parent_doctype_name)

    check_doc_permissions(parent, "write")
    _removed_items = validate_and_delete_children(parent, data)
    items_added_or_removed |= _removed_items
    
    for d in data:
        new_child_flag = False

        if not (d.get("charge_type") or d.get("account_head")):
            # ignore empty rows
            continue

        if not d.get("docname"):
            new_child_flag = True
            items_added_or_removed = True
            check_doc_permissions(parent, "create")
            child_item = get_new_child_item(d)
        else:
            check_doc_permissions(parent, "write")
            child_item = frappe.get_doc(child_doctype, d.get("docname"))
            
            prev_charge_type, new_charge_type = child_item.get("charge_type"), d.get("charge_type")
            prev_account_head, new_account_head = child_item.get("account_head"), d.get("account_head")
            prev_tax_amount, new_tax_amount = flt(child_item.get("tax_amount")), flt(d.get("tax_amount"))
            prev_rate, new_rate = flt(child_item.get("rate")), flt(d.get("rate"))
            prev_description, new_description = child_item.get("description"), d.get("description")
            prev_included_in_print_rate, new_included_in_print_rate = child_item.get("included_in_print_rate"), d.get("included_in_print_rate")

            rate_unchanged = prev_rate == new_rate
            tax_amount_unchanged = prev_tax_amount == new_tax_amount
            account_head_unchanged = prev_account_head == new_account_head
            charge_type_unchanged = prev_charge_type == new_charge_type
            description_unchanged = prev_description == new_description
            included_in_print_rate_unchanged = prev_included_in_print_rate == new_included_in_print_rate

            if (
                rate_unchanged
                and tax_amount_unchanged
                and account_head_unchanged
                and charge_type_unchanged
                and description_unchanged
                and included_in_print_rate_unchanged
            ):
                continue
        
        for fieldname, value in d.items():
            if fieldname in ["name", "docname"]:
                continue
            
            if child_item.meta.get_field(fieldname) and value is not None:
                child_item.set(fieldname, value)

        child_item.flags.ignore_validate_update_after_submit = True
        if new_child_flag:
            parent.load_from_db()
            child_item.idx = len(parent.items) + 1
            child_item.insert()
        else:
            child_item.save()
    
    parent.reload()
    parent.flags.ignore_validate_update_after_submit = True
    parent.calculate_taxes_and_totals()
    parent.set_total_in_words()

    parent.set_payment_schedule()

    if parent_doctype == "Sales Order":
        parent.check_credit_limit()

    # reset index of child table
    for idx, row in enumerate(parent.get(child_docname), start=1):
        row.idx = idx

    parent.save()

    parent.update_billing_percentage()
    parent.set_status()

def validate_and_delete_children(parent, data) -> bool:
    deleted_children = []
    updated_item_names = [d.get("docname") for d in data]
    
    for item in parent.taxes:
        if item.name not in updated_item_names:
            deleted_children.append(item)

    for d in deleted_children:
        d.cancel()
        d.delete()

    
    return bool(deleted_children)
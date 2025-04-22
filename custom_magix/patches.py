import frappe

@frappe.whitelist()
def repair_gl_entry_v14(doctype, docname):
	docu = frappe.get_doc(doctype, docname)
	# Tambahan : Delete Payment Ledger Entry Untuk V14 agar outstanding tidak double
	delete_pl = frappe.db.sql(""" DELETE FROM `tabPayment Ledger Entry` WHERE voucher_no = "{}" """.format(docname))
	delete_gl = frappe.db.sql(""" DELETE FROM `tabGL Entry` WHERE voucher_no = "{}" """.format(docname))	

	docu.make_gl_entries()
	frappe.db.commit()
     
def patch_tax():
    doclist = frappe.db.sql("""
            SELECT name
            FROM `tabSales Invoice`
            WHERE YEAR(posting_date) = "2025"
            AND docstatus = 1
        """, as_dict=1)
    print(len(doclist))
    for d in doclist:
        doc = frappe.get_doc("Sales Invoice", d.name)
        print(doc.name)
        for row in doc.taxes:
            # SINV
            if row.account_head == "1110.002 - PPN MASUKAN - MMM":
                row.account_head = "2102.006 - PPN TERHUTANG - MMM"
             
            #  PINV
            # if row.account_head == "2102.006 - PPN TERHUTANG - MMM":
            #     row.account_head = "1110.002 - PPN MASUKAN - MMM"

            # if row.account_head == "6202.002 - PPH 23 - MMM":
            #     row.account_head = "2102.002 - PPH 23 TERHUTANG - MMM"

        doc.db_update()
        doc.update_children()
        repair_gl_entry_v14(doc.doctype, doc.name)


def pe():
    doc = frappe.get_doc("Payment Entry", "ACC-PAY-2025-03006")
    doc.update_advance_paid()
    print(doc.name)

def test():
    doc = frappe.get_doc("Employee Advance", "HR-EAD-2025-00001")
    doc.set_total_advance_paid()
    print(doc.name)
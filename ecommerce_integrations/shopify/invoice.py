import frappe
from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice
from frappe.utils import cint, cstr, getdate, nowdate

from ecommerce_integrations.shopify.constants import (
	ORDER_ID_FIELD,
	ORDER_NUMBER_FIELD,
	SETTING_DOCTYPE,
)
from ecommerce_integrations.shopify.utils import create_shopify_log


def prepare_sales_invoice(payload, request_id=None):
	from ecommerce_integrations.shopify.order import get_sales_order

	order = payload

	frappe.set_user("Administrator")
	setting = frappe.get_doc(SETTING_DOCTYPE)
	frappe.flags.request_id = request_id

	try:
		sales_order = get_sales_order(cstr(order["id"]))
		if sales_order:
			create_sales_invoice(order, setting, sales_order)
			create_shopify_log(status="Success")
		else:
			create_shopify_log(status="Invalid", message="Sales Order not found for syncing sales invoice.")
	except Exception as e:
		create_shopify_log(status="Error", exception=e, rollback=True)


def create_sales_invoice(shopify_order, setting, so):
	if (
		not frappe.db.get_value("Sales Invoice", {ORDER_ID_FIELD: shopify_order.get("id")}, "name")
		and so.docstatus == 1
		and not so.per_billed
		and cint(setting.sync_sales_invoice)
	):

		posting_date = getdate(shopify_order.get("created_at")) or nowdate()

		sales_invoice = make_sales_invoice(so.name, ignore_permissions=True)
		sales_invoice.set(ORDER_ID_FIELD, str(shopify_order.get("id")))
		sales_invoice.set(ORDER_NUMBER_FIELD, shopify_order.get("name"))
		sales_invoice.set_posting_time = 1
		sales_invoice.posting_date = posting_date
		sales_invoice.due_date = posting_date
		sales_invoice.naming_series = setting.sales_invoice_series or "SI-Shopify-"
		sales_invoice.flags.ignore_mandatory = True
		set_cost_center(sales_invoice.items, setting.cost_center)
		sales_invoice.insert(ignore_mandatory=True)
		sales_invoice.submit()
		if sales_invoice.grand_total > 0:
			make_payament_entry_against_sales_invoice(sales_invoice, setting, posting_date)

		if shopify_order.get("note"):
			sales_invoice.add_comment(text=f"Order Note: {shopify_order.get('note')}")


def set_cost_center(items, cost_center):
	for item in items:
		item.cost_center = cost_center


def make_payament_entry_against_sales_invoice(doc, setting, posting_date=None):
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	payment_entry = get_payment_entry(doc.doctype, doc.name, bank_account=setting.cash_bank_account)
	payment_entry.flags.ignore_mandatory = True
	payment_entry.reference_no = doc.name
	payment_entry.posting_date = posting_date or nowdate()
	payment_entry.reference_date = posting_date or nowdate()
	payment_entry.insert(ignore_permissions=True)
	payment_entry.submit()

def create_sales_invoice_with_location_mapping(shopify_order, setting, so):
	"""
	Create sales invoice with location-based cost center and naming series.
	"""
	from frappe.utils import getdate, nowdate
	
	# Check if sales invoice already exists
	if frappe.db.get_value("Sales Invoice", {ORDER_ID_FIELD: shopify_order.get("id")}, "name"):
		return
	
	if so.docstatus != 1 or so.per_billed:
		return
	
	# Extract location information from fulfillments
	location_mappings = get_location_mappings_from_fulfillments(shopify_order.get("fulfillments", []), setting)
	
	# Determine primary location for naming and cost center
	primary_location_mapping = get_primary_location_mapping(location_mappings, setting)
	
	posting_date = getdate(shopify_order.get("created_at")) or nowdate()
	
	sales_invoice = make_sales_invoice(so.name, ignore_permissions=True)
	sales_invoice.set(ORDER_ID_FIELD, str(shopify_order.get("id")))
	sales_invoice.set(ORDER_NUMBER_FIELD, shopify_order.get("name"))
	sales_invoice.set_posting_time = 1
	sales_invoice.posting_date = posting_date
	sales_invoice.due_date = posting_date
	sales_invoice.shopify_coupon_code = so.shopify_coupon_code
	
	# Set location-based naming series and cost center
	warehouse_name = primary_location_mapping.get("warehouse_name", "Default")
	cost_center = primary_location_mapping.get("cost_center", setting.cost_center)
	name_series = primary_location_mapping.get("name_series")
	create_shopify_log(
		status="Info",
		message=f"Creating Sales Invoice for Shopify Order {shopify_order.get('id')} with Warehouse: {warehouse_name}, Cost Center: {cost_center}, Naming Series: {name_series}"
	)
 
	# Use the name_series from the warehouse mapping if available, else fallback to default logic
	if name_series:
		sales_invoice.naming_series = name_series
	else:
		base_series = setting.sales_invoice_series or "SI-Shopify-"
		if base_series.endswith("-"):
			location_series = f"SI-{warehouse_name}-"
		else:
			location_series = f"SI-{warehouse_name}-####"
		sales_invoice.naming_series = location_series
	# Set cost center for the sales invoice header
	sales_invoice.cost_center = cost_center
	
	# Set cost center for all items
	set_cost_center_for_invoice_items(sales_invoice.items, cost_center)
	
	sales_invoice.flags.ignore_mandatory = True
	sales_invoice.insert(ignore_mandatory=True)
	sales_invoice.submit()
	
	if sales_invoice.grand_total > 0:
		make_payament_entry_against_sales_invoice(sales_invoice, setting, posting_date)
	
	if shopify_order.get("note"):
		sales_invoice.add_comment(text=f"Order Note: {shopify_order.get('note')}")


def get_location_mappings_from_fulfillments(fulfillments, setting):
	"""
	Extract location mappings from fulfillments and return warehouse/cost center mappings.
	"""
	location_mappings = {}
	
	for fulfillment in fulfillments:
		location_id = str(fulfillment.get("location_id", ""))
		if location_id:
			# Find matching warehouse mapping
			for mapping in setting.shopify_warehouse_mapping:
				if str(mapping.shopify_location_id) == location_id:
					# Get warehouse name from the warehouse doctype
					warehouse_name = frappe.db.get_value("Warehouse", mapping.erpnext_warehouse, "warehouse_name") or mapping.erpnext_warehouse
					
					location_mappings[location_id] = {
						"warehouse": mapping.erpnext_warehouse,
						"warehouse_name": warehouse_name,
						"cost_center": mapping.erpnext_cost_center,
						"location_name": mapping.shopify_location_name,
      			"name_series": mapping.name_series
					}
					break
			
			# Fallback to default if no mapping found
			if location_id not in location_mappings:
				# Get default warehouse name
				default_warehouse_name = frappe.db.get_value("Warehouse", setting.warehouse, "warehouse_name") or setting.warehouse if setting.warehouse else "Default"
				
				location_mappings[location_id] = {
					"warehouse": setting.warehouse,
					"warehouse_name": default_warehouse_name,
					"cost_center": setting.cost_center,
					"location_name": "Default"
				}
	
	return location_mappings


def get_primary_location_mapping(location_mappings, setting):
	"""
	Get the primary location mapping. If multiple locations, use the first one.
	"""
	if location_mappings:
		return next(iter(location_mappings.values()))
	
	# Fallback to default settings with warehouse name
	default_warehouse_name = frappe.db.get_value("Warehouse", setting.warehouse, "warehouse_name") or setting.warehouse if setting.warehouse else "Default"
	
	return {
		"warehouse": setting.warehouse,
		"warehouse_name": default_warehouse_name,
		"cost_center": setting.cost_center,
		"location_name": "Default"
	}


def set_cost_center_for_invoice_items(items, cost_center):
	"""
	Set cost center for all invoice items.
	"""
	for item in items:
		item.cost_center = cost_center
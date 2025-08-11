import frappe
import requests
import json

from ecommerce_integrations.shopify.customer import get_customer_email
from ecommerce_integrations.shopify.utils import create_shopify_log, get_shopify_prefs, shopify_url_to_admin_url

@frappe.whitelist(allow_guest=True)
def sync_sales_invoice_to_shopify(doc, method):
    """
    Syncs a submitted Sales Invoice to Shopify if the 'sync_sales_invoice'
    field in 'Shopify Setting' is checked.
    """
    shopify_setting, headers = get_shopify_prefs()
    
    if not shopify_setting:
        frappe.log_error("Shopify settings not found. Sales Invoice not synced.", "Shopify Sync Error")
        frappe.throw("Shopify settings are not configured. Cannot sync Sales Invoice.")
        return

    if not getattr(shopify_setting, 'sync_sales_invoice_to_shopify', False):
        return
    create_shopify_log(
        message=f"Syncing Sales Invoice {doc.name} to Shopify",
        status="Information"
    )

    # Ensure doc is a Document object
    if isinstance(doc, str):
        doc = frappe.get_doc("Sales Invoice", doc)
    elif isinstance(doc, dict):
        doc = frappe.get_doc("Sales Invoice", doc.get("name", ""))

    if doc.docstatus != 1:        
        return

    if getattr(doc, 'shopify_order_id', None):
        frappe.msgprint(f"Sales Invoice {doc.name} already synced to Shopify (Order ID: {doc.shopify_order_id}). Skipping sync.", indicator='blue')
        return

    try:
        shopify_order_payload = build_shopify_order_payload_from_invoice(doc)
        if not shopify_order_payload:
            create_shopify_log(
                message=f"Failed to build Shopify order payload for Sales Invoice: {doc.name}. Skipping sync.",
                status="Error"
            )
            frappe.throw(f"Failed to build Shopify order payload for Sales Invoice: {doc.name}.")
            return

        response = send_order_to_shopify(shopify_order_payload, shopify_setting)

        if response and response.get("order"):
            shopify_order = response["order"]
            
            # Method 1: Update doc object and save with flags to avoid validation
            doc.shopify_order_id = str(shopify_order.get("id"))
            doc.shopify_order_number = str(shopify_order.get("order_number"))
            doc.shopify_sync_status = "Success"
            doc.db_update()  # Direct database update without validation
            
            create_shopify_log(
                message=f"Successfully synced Sales Invoice {doc.name} to Shopify. Shopify Order ID: {shopify_order.get('id')}, Order Number: {shopify_order.get('order_number')}",
                status="Success"
            )
            frappe.msgprint(f"Sales Invoice synced to Shopify. Shopify Order ID: {shopify_order.get('id')}")
        else:
            error_message = f"Failed to sync Sales Invoice {doc.name} to Shopify. Response: {response}"
            create_shopify_log(message=error_message, status="Error")
            
            # Update doc object for failed status
            doc.shopify_sync_status = "Failed"
            doc.db_update()
            
            frappe.throw(error_message)

    except Exception as e:
        # Update doc object for failed status
        try:
            doc.shopify_sync_status = "Failed"
            doc.db_update()
        except:
            # Fallback to frappe.db.set_value if doc update fails
            frappe.db.set_value("Sales Invoice", doc.name, "shopify_sync_status", "Failed")
            frappe.db.commit()
            
        error_message = f"An error occurred while syncing Sales Invoice {doc.name} to Shopify. Error: {str(e)}"
        create_shopify_log(message=error_message, status="Error", exception=e)
        frappe.throw(error_message)

def build_shopify_order_payload_from_invoice(sales_invoice_doc):
    """
    Builds the JSON payload for creating an order in Shopify from an ERPNext Sales Invoice.
    """
    customer = frappe.get_doc("Customer", sales_invoice_doc.customer)
    shipping_address = frappe.get_doc("Address", sales_invoice_doc.shipping_address_name) if getattr(sales_invoice_doc, 'shipping_address_name', None) else None
    billing_address = frappe.get_doc("Address", sales_invoice_doc.customer_address) if sales_invoice_doc.customer_address else None

    line_items = []
    for item in sales_invoice_doc.items:
        line_items.append({
            "variant_id": None,
            "sku": item.item_code,
            "quantity": int(item.qty),
            "price": float(item.rate),
            "title": item.item_name
        })

    # Collect coupon code (now a single Link field)
    discount_codes = []
    if getattr(sales_invoice_doc, "shopify_coupon_code", None):
        shopify_coupon_doc = frappe.get_doc("Shopify Coupon Code", sales_invoice_doc.shopify_coupon_code)
        if shopify_coupon_doc and shopify_coupon_doc.shopify_price_rule_id:
            discount_codes.append({
                "amount": "{:.2f}".format(float(shopify_coupon_doc.percent_value or 0)),
                "code": shopify_coupon_doc.coupon_code_name,
                "type": "percentage"
            })
        else:
            create_shopify_log(
                message=f"Shopify Coupon Code '{sales_invoice_doc.shopify_coupon_code}' not found or missing shopify_price_rule_id. Skipping discount for Sales Invoice: {sales_invoice_doc.name}.",
                status="Warning"
            )

    # Basic customer info (Shopify might create a new customer or link to existing)
    name_parts = customer.customer_name.strip().split(" ", 1)
    first_name = name_parts[0]
    last_name = name_parts[1] if len(name_parts) > 1 else ""

    customer_email = get_customer_email(customer.name)

    customer_payload = {
        "first_name": first_name,
        "last_name": last_name,
        "email": customer_email,
    }

    shipping_address_payload = {}
    if shipping_address:
        shipping_address_payload = {
            "first_name": shipping_address.address_title,
            "address1": shipping_address.address_line1,
            "address2": shipping_address.address_line2,
            "city": shipping_address.city,
            "province": shipping_address.state,
            "zip": shipping_address.pincode,
            "country": shipping_address.country,
            "phone": shipping_address.phone or customer.mobile_no,
        }

    billing_address_payload = {}
    if billing_address:
        billing_address_payload = {
            "first_name": billing_address.address_title,
            "address1": billing_address.address_line1,
            "address2": billing_address.address_line2,
            "city": billing_address.city,
            "province": billing_address.state,
            "zip": billing_address.pincode,
            "country": billing_address.country,
            "phone": billing_address.phone or customer.mobile_no,
        }
    
    financial_status = "paid"
    transactions = [
        {
            "kind": "sale",
            "status": "success",
            "amount": float(sales_invoice_doc.grand_total),
            "gateway": "manual"
        }
    ]

    shipping_amount = 0
    for tax in getattr(sales_invoice_doc, "taxes", []):
        if "shipping" in (tax.description or "").lower():
            shipping_amount += tax.tax_amount
    for item in getattr(sales_invoice_doc, "items", []):
        if item.item_code == "YOUR_SHIPPING_ITEM_CODE":
            shipping_amount += item.amount

    shipping_lines = []
    if shipping_amount:
        shipping_lines.append({
            "title": "Shipping",
            "price": shipping_amount
        })

    payload = {
        "order": {
            "line_items": line_items,
            "customer": customer_payload,
            "email": customer.email_id,
            "transactions": transactions,
            "financial_status": financial_status,
            "current_total_price": float(sales_invoice_doc.grand_total),
            "total_tax": float(sales_invoice_doc.total_taxes_and_charges),
            "shipping_lines": shipping_lines,
            "source_name": "erpnext-ecommerce-integration",
            "note": f"ERPNext Sales Invoice: {sales_invoice_doc.name}",
            "tags": "erpnext_invoice_synced",
        }
    }

    if shipping_address_payload:
        payload["order"]["shipping_address"] = shipping_address_payload
    if billing_address_payload:
        payload["order"]["billing_address"] = billing_address_payload
    if discount_codes:
        payload["order"]["discount_codes"] = discount_codes

    return payload

def send_order_to_shopify(payload, shopify_setting):
    """
    Sends the order payload to Shopify's Orders API.
    """
    
    shopify_setting, headers = get_shopify_prefs()
    
    shopify_api_version = "2025-04"
    url = f"https://{shopify_setting.shopify_url}/admin/api/{shopify_api_version}/orders.json"

    try:
        response = requests.post(url, headers=headers, data=json.dumps(payload))
        order_name = payload.get("order", {}).get("note", "")
        create_shopify_log(
            status="Information",
            message=f"Syncing sales invoice to shopify {order_name}",
            request_data=json.dumps(payload)
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as errh:
        error_message = f"HTTP Error syncing invoice to Shopify: {errh} - {response.text}"
        create_shopify_log(message=error_message, status="Error")
        frappe.throw(error_message)
    except requests.exceptions.ConnectionError as errc:
        error_message = f"Error Connecting to Shopify: {errc}"
        create_shopify_log(message=error_message, status="Error")
        frappe.throw(error_message)
    except requests.exceptions.Timeout as errt:
        error_message = f"Timeout Error syncing invoice to Shopify: {errt}"
        create_shopify_log(message=error_message, status="Error")
        frappe.throw(error_message)
    except requests.exceptions.RequestException as err:
        error_message = f"Unexpected Error syncing invoice to Shopify"
        create_shopify_log(message=error_message, status="Error")
        frappe.throw(error_message)

@frappe.whitelist(allow_guest=True)
def test_shopify_sales_invoice_sync(invoice_json):
    import json

    if isinstance(invoice_json, str):
        invoice_data = json.loads(invoice_json)
    else:
        invoice_data = invoice_json

    invoice_data.pop("name", None)

    si = frappe.get_doc({"doctype": "Sales Invoice", **invoice_data})
    si.insert(ignore_permissions=True)
    si.submit()  # This triggers on_submit and your hook

    si_name = si.name
    shopify_order_id = getattr(si, "shopify_order_id", None)

    si.cancel()
    si.delete(ignore_permissions=True)

    return {
        "message": f"Test Sales Invoice {si_name} created, synced, cancelled, and deleted.",
        "shopify_order_id": shopify_order_id
    }
import frappe
import requests
import json

from ecommerce_integrations.shopify.customer import get_customer_email
from ecommerce_integrations.shopify.utils import create_shopify_log, get_shopify_prefs

def sync_sales_order_to_shopify(doc, method):
    """
    Syncs a submitted Sales Order to Shopify if the 'sync_sales_order'
    field in 'Shopify Setting' is checked.
    """
    shopify_setting, headers = get_shopify_prefs()
    
    if not shopify_setting:
        frappe.log_error("Shopify settings not found. Sales Order not synced.", "Shopify Sync Error")
        frappe.throw("Shopify settings are not configured. Cannot sync Sales Order.")
        return

    if not shopify_setting.sync_sales_order:
        return

    # Ensure doc is a Document object
    if isinstance(doc, str):
        doc = frappe.get_doc("Sales Order", doc)
    elif isinstance(doc, dict):
        doc = frappe.get_doc("Sales Order", doc.get("name", ""))

    if doc.docstatus != 1:        
        return
    
    if doc.do_not_sync_to_shopify:
        # get the shopify coupon code and set the status to 'Need Review'
        if doc.shopify_coupon_code:
            frappe.db.set_value("Shopify Coupon Code", doc.shopify_coupon_code, "status", "Need Review")
        frappe.msgprint(f"Sales Order {doc.name} is not to be synced to Shopify. COUPON CODE has marked as 'Need Review'. Skipping sync.\nPlease visit shopify dashbaord and manually deactivate the coupon code.", indicator='blue')
        return

    if doc.shopify_order_id:
        frappe.msgprint(f"Sales Order {doc.name} already synced to Shopify (Order ID: {doc.shopify_order_id}). Skipping sync.", indicator='blue')
        return

    try:
        shopify_order_payload = build_shopify_order_payload(doc)
        if not shopify_order_payload:
            create_shopify_log(
                message=f"Failed to build Shopify order payload for Sales Order: {doc.name}. Skipping sync.",
                status="Error"
            )
            frappe.throw(f"Failed to build Shopify order payload for Sales Order: {doc.name}.")
            return

        response = send_order_to_shopify(shopify_order_payload, shopify_setting)

        if response and response.get("order"):
            shopify_order = response["order"]
            # Use frappe.db.set_value to avoid UpdateAfterSubmitError
            frappe.db.set_value("Sales Order", doc.name, "shopify_order_id", str(shopify_order.get("id")))
            frappe.db.set_value("Sales Order", doc.name, "shopify_order_number", str(shopify_order.get("order_number")))
            create_shopify_log(
                message=f"Successfully synced Sales Order {doc.name} to Shopify. Shopify Order ID: {shopify_order.get('id')}, Order Number: {shopify_order.get('order_number')}",
                status="Success"
            )
            frappe.msgprint(f"Sales Order synced to Shopify. Shopify Order ID: {shopify_order.get('id')}")
        else:
            error_message = f"Failed to sync Sales Order {doc.name} to Shopify. Response: {response}"
            create_shopify_log(message=error_message, status="Error")
            frappe.throw(error_message)

    except Exception as e:
        frappe.db.set_value("Sales Order", doc.name, "do_not_sync_to_shopify", 1)
        # doc.refresh()
        error_message = f"An error occurred while syncing Sales Order {doc.name} to Shopify. The sales order has been marked to not sync to Shopify. Submitting again will not sync it to Shopify. Error: {str(e)}"
        create_shopify_log(message=error_message, status="Error", exception=e)
        frappe.throw(error_message)

def build_shopify_order_payload(sales_order_doc):
    """
    Builds the JSON payload for creating an order in Shopify from an ERPNext Sales Order.
    """
    customer = frappe.get_doc("Customer", sales_order_doc.customer)
    shipping_address = frappe.get_doc("Address", sales_order_doc.shipping_address_name) if sales_order_doc.shipping_address_name else None
    billing_address = frappe.get_doc("Address", sales_order_doc.customer_address) if sales_order_doc.customer_address else None

    line_items = []
    for item in sales_order_doc.items:
        line_items.append({
            "variant_id": None,
            "sku": item.item_code,
            "quantity": int(item.qty),
            "price": float(item.rate),
            "title": item.item_name
        })

    # Collect coupon code (now a single Link field)
    discount_codes = []
    if getattr(sales_order_doc, "shopify_coupon_code", None):
        shopify_coupon_doc = frappe.get_doc("Shopify Coupon Code", sales_order_doc.shopify_coupon_code)
        if shopify_coupon_doc and shopify_coupon_doc.shopify_price_rule_id:
            discount_codes.append({
                "amount": "{:.2f}".format(float(shopify_coupon_doc.percent_value or 0)),
                "code": shopify_coupon_doc.coupon_code_name,
                "type": "percentage"
            })
        else:
            create_shopify_log(
                message=f"Shopify Coupon Code '{sales_order_doc.shopify_coupon_code}' not found or missing shopify_price_rule_id. Skipping discount for Sales Order: {sales_order_doc.name}.",
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
    
    financial_status = "pending"
    transactions = [
        {
            "kind": "sale",
            "status": "pending",
            "amount": float(sales_order_doc.grand_total),
        }
    ]

    if sales_order_doc.mark_shopify_order_paid:
        financial_status = "paid"
        transactions[0]["status"] = "success"
        transactions[0]["gateway"] = "manual" 

    shipping_amount = 0
    for tax in getattr(sales_order_doc, "taxes", []):
        if "shipping" in (tax.description or "").lower():
            shipping_amount += tax.tax_amount
    for item in getattr(sales_order_doc, "items", []):
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
            "transactions": [
                {
                    "kind": "sale",
                    "status": "pending",
                    "amount": float(sales_order_doc.grand_total),
                }
            ],
            "financial_status": financial_status,
            "current_total_price": float(sales_order_doc.grand_total),
            "total_tax": float(sales_order_doc.total_taxes_and_charges),
            "shipping_lines": shipping_lines,
            "source_name": "erpnext-ecommerce-integration",
            "note": f"ERPNext Sales Order: {sales_order_doc.name}",
            "tags": "erpnext_synced",
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
            message=f"Syncing sales order to shopify {order_name}",
            request_data=json.dumps(payload)
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as errh:
        error_message = f"HTTP Error syncing order to Shopify: {errh} - {response.text}"
        create_shopify_log(message=error_message, status="Error")
        frappe.throw(error_message)
    except requests.exceptions.ConnectionError as errc:
        error_message = f"Error Connecting to Shopify: {errc}"
        create_shopify_log(message=error_message, status="Error")
        frappe.throw(error_message)
    except requests.exceptions.Timeout as errt:
        error_message = f"Timeout Error syncing order to Shopify: {errt}"
        create_shopify_log(message=error_message, status="Error")
        frappe.throw(error_message)
    except requests.exceptions.RequestException as err:
        error_message = f"Unexpected Error syncing order to Shopify"
        create_shopify_log(message=error_message, status="Error")
        frappe.throw(error_message)

@frappe.whitelist(allow_guest=True)
def test_shopify_sales_order_sync(order_json):
    import json

    if isinstance(order_json, str):
        order_data = json.loads(order_json)
    else:
        order_data = order_json

    # Remove name if present to avoid conflicts
    order_data.pop("name", None)

    # Create and insert Sales Order
    so = frappe.get_doc({"doctype": "Sales Order", **order_data})
    so.insert(ignore_permissions=True)
    so.submit()  # This triggers on_submit and your hook

    # Optionally, call sync_sales_order_to_shopify explicitly (not needed if hooked)
    # sync_sales_order_to_shopify(so.name, "on_submit")

    # Capture info for response
    so_name = so.name
    shopify_order_id = so.shopify_order_id if hasattr(so, "shopify_order_id") else None

    # Cancel before delete
    so.cancel()
    so.delete(ignore_permissions=True)

    return {
        "message": f"Test Sales Order {so_name} created, synced, cancelled, and deleted.",
        "shopify_order_id": shopify_order_id
    }
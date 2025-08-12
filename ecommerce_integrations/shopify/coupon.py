import frappe
from datetime import datetime, timedelta
import frappe
from datetime import datetime, timedelta
import json
from ecommerce_integrations.shopify.utils import create_shopify_log, get_shopify_prefs, shopify_url_to_admin_url
from ecommerce_integrations.shopify.constants import SETTING_DOCTYPE
import re
import requests
from dateutil import parser
import pytz

def clean_datetime(dt_str):
    # Parse and convert to UTC, then format without timezone
    dt = parser.isoparse(dt_str)
    dt_utc = dt.astimezone(pytz.UTC)
    return dt_utc.strftime('%Y-%m-%d %H:%M:%S')
    
def create_coupon_from_shopify_discount(payload, request_id):
    """
    Receives a Shopify Discount Code webhook payload and 
    fetches the full Price Rule details from Shopify,


    Args:
        payload (dict): The JSON payload from the Shopify Discount Code webhook.

    Returns:
        dict: A status dictionary indicating overall success or failure.
    """

    if not isinstance(payload, dict) or "admin_graphql_api_id" not in payload:
        frappe.log_error(
            "Invalid Shopify Discount Code webhook payload: missing 'admin_graphql_api_id' key.",
            "Shopify Discount Code Webhook Error"
        )
        return {"status": "error", "message": "Invalid payload format. Expected {'admin_graphql_api_id': '...'}"}

    admin_graphql_id = payload.get("admin_graphql_api_id")
    

    match = re.search(r'\d+$', admin_graphql_id)
    if not match:
        frappe.log_error(
            f"Could not extract numeric ID from admin_graphql_api_id: {admin_graphql_id}",
            "Shopify Discount Code Webhook Error"
        )
        return {"status": "error", "message": f"Could not parse ID from '{admin_graphql_id}'"}

    shopify_price_rule_id = match.group(0)
    shopify_price_rule_payload = get_shopify_price_rule(shopify_price_rule_id)
    process_shopify_price_rule_webhook(shopify_price_rule_payload) 

def get_shopify_price_rule(shopify_price_rule_id):
    try:
        shopify_setting, headers = get_shopify_prefs()
        if not shopify_setting.shopify_url or not shopify_setting.get_password("password"):
            frappe.throw("Shopify API credentials (Shop URL or Access Token) are not configured in 'Shopify Settings'.")

        
        shopify_api_version = "2025-04"
        shopify_api_endpoint = f"https://{shopify_setting.shopify_url}/admin/api/{shopify_api_version}/price_rules/{shopify_price_rule_id}.json"
        

        response = requests.get(shopify_api_endpoint, headers=headers)
        response.raise_for_status()

        shopify_price_rule_payload = response.json()
        
        if "price_rule" not in shopify_price_rule_payload:
            frappe.log_error(
                f"Shopify API response for Price Rule ID {shopify_price_rule_id} missing 'price_rule' key. Response: {shopify_price_rule_payload}",
                "Shopify Discount Code Webhook Error"
            )
            frappe.throw("Shopify API response did not contain expected price_rule data.")
        return shopify_price_rule_payload
        

    except requests.exceptions.RequestException as e:
        error_message = f"Shopify API Request Error while fetching Price Rule ID {shopify_price_rule_id}: {e}"
        frappe.log_error(error_message, "Shopify Discount Code Webhook Error")
        return {"status": "error", "message": error_message}
    except Exception as e:
        error_message = f"An unexpected error occurred while processing Discount Code for ID {shopify_price_rule_id}: {frappe.utils.get_traceback()}"
        frappe.log_error(error_message, "Shopify Discount Code Webhook Error")
        return {"status": "error", "message": error_message}

def process_shopify_price_rule_webhook(payload):
    """
    Receives a Shopify price rule webhook payload and creates or updates
    Shopify Coupon Code doctypes in ERPNext.

    Args:
        payload (dict): The JSON payload from the Shopify price rule webhook.

    Returns:
        dict: A status dictionary indicating overall success or failure, and
              a summary of the processed coupon code (created/updated/failed).
    """
    
    if not isinstance(payload, dict) or "price_rule" not in payload:
        frappe.log_error(
            "Invalid Shopify webhook payload: missing 'price_rule' key. Expected payload for single price rule.",
            "Shopify Coupon Webhook Error"
        )
        return {"status": "error", "message": "Invalid payload format. Expected {'price_rule': {...}}"}

    processed_coupons_summary = []

    price_rule = payload.get("price_rule")
    price_rule_id = price_rule.get("id")
    price_rule_title = price_rule.get("title", f"Price Rule ID {price_rule_id}") # Get title for coupon name
    
    try:
        existing_coupon = frappe.get_all(
            "Shopify Coupon Code",
            filters={"coupon_code_name": price_rule_title},
            limit=1,
            pluck="name"
        )

        coupon_doc = None
        action_type = ""

        if existing_coupon:
            coupon_doc = frappe.get_doc("Shopify Coupon Code", existing_coupon[0])
            action_type = "Updated"
            create_shopify_log(
                message=f"Updating existing Shopify Coupon Code '{existing_coupon[0]}' for Price Rule ID: {price_rule_id}",
                status="Information"
            )
        else:
            coupon_doc = frappe.new_doc("Shopify Coupon Code")
            coupon_doc.shopify_price_rule_id = price_rule_id  # <-- Set the field here
            action_type = "Created"
            create_shopify_log(
                message=f"Creating new Shopify Coupon Code for Price Rule ID: {price_rule_id} (Title: '{price_rule_title}')",
                status="Information"
            )

        coupon_doc.coupon_code_name = price_rule_title
        coupon_doc.status = "Active"
        shopify_value_type = price_rule.get("value_type")
        shopify_value = float(price_rule.get("value", 0.0))

        if shopify_value_type == "percentage":
            coupon_doc.discount_type = "Percentage"
            coupon_doc.percent_value = abs(shopify_value)
            coupon_doc.fixed_value = 0.0
        elif shopify_value_type == "fixed_amount":
            coupon_doc.discount_type = "Value"
            coupon_doc.fixed_value = abs(shopify_value)
            coupon_doc.percent_value = 0.0
        else:
            create_shopify_log(status="Invalid",message=f"Unrecognized Shopify 'value_type': '{shopify_value_type}' for Price Rule ID: {price_rule_id}. Defaulting 'discount_type' to 'Percentage'."
            )
            coupon_doc.discount_type = "Percentage"
            coupon_doc.percent_value = abs(shopify_value)

        starts_at_str = price_rule.get("starts_at")
        ends_at_str = price_rule.get("ends_at")

        if starts_at_str:
            coupon_doc.start_date_time = clean_datetime(price_rule.get("starts_at"))
        else:
            coupon_doc.start_date_time = datetime.now()

        # if ends_at_str:
        #     start_dt = frappe.utils.get_datetime(starts_at_str) if starts_at_str else datetime.now()
        #     end_dt = frappe.utils.get_datetime(ends_at_str)
        #     delta = end_dt - start_dt
        #     coupon_doc.duration = format_timedelta_to_frappe_duration(delta)
        # else:
        #     coupon_doc.duration = None
        coupon_doc.duration = None
        if action_type == "Created":
            coupon_doc.insert(ignore_permissions=True)
        else: # action_type == "Updated"
            coupon_doc.save(ignore_permissions=True)

        frappe.db.commit() # Commit the transaction to save changes to the database
        
        # Add the result of this price rule processing to the summary
        processed_coupons_summary.append({
            "price_rule_id": price_rule_id,
            "coupon_name": coupon_doc.coupon_code_name,
            "erpnext_coupon_name": coupon_doc.name, # The ERPNext DocType's actual name/ID
            "status": "success",
            "action": action_type # "Created" or "Updated"
        })
        create_shopify_log(
            message=f"Successfully {action_type.lower()} Shopify Coupon Code: '{coupon_doc.name}' for Price Rule ID: {price_rule_id}",
            status="Success"
        )

    except Exception as e:
        frappe.db.rollback() # Rollback any partial database changes if an error occurs
        error_message = f"Error processing Shopify Price Rule ID {price_rule_id} ('{price_rule_title}'): {frappe.utils.get_traceback()}"
        frappe.log_error(error_message, "Shopify Coupon Webhook Error")
        # Record the failure in the summary
        processed_coupons_summary.append({
            "price_rule_id": price_rule_id,
            "coupon_name": price_rule_title,
            "status": "failed",
            "error": str(e) # Store the error message
        })

    # Return the overall status and a detailed summary of processed coupons
    return {"status": "success", "processed_coupons": processed_coupons_summary}


@frappe.whitelist()
def create_shopify_price_rule_from_erpnext_coupon(coupon_code_name: str):
    """
    Fetches a Shopify Coupon Code from ERPNext and creates a corresponding
    Price Rule in Shopify via their API.

    Upon successful creation in Shopify, it updates the ERPNext Shopify Coupon Code
    DocType with the newly generated `shopify_price_rule_id`.

    Args:
        coupon_code_name (str): The 'name' (primary identifier) of the Shopify Coupon Code
                                DocType in ERPNext.

    Returns:
        dict: A status dictionary indicating success or failure, and details of the operation.
    """
    if not coupon_code_name:
        frappe.throw("Coupon Code Name is required to create a Shopify Price Rule.")

    try:
        # 1. Fetch the Shopify Coupon Code DocType from ERPNext
        coupon_doc = frappe.get_doc("Shopify Coupon Code", coupon_code_name)
        create_shopify_log(message=f"Fetched Shopify Coupon Code: {coupon_code_name}")

        # Check if price rule already exists in Shopify
        if coupon_doc.shopify_price_rule_id:
            frappe.throw(f"Shopify Price Rule already exists for this coupon: {coupon_doc.shopify_price_rule_id}")

        # --- Configure Shopify API Access ---
        # IMPORTANT: Replace these with your actual Shopify shop URL and API access token.
        # In a real-world scenario, these should be securely fetched from a DocType like
        # 'Shopify Settings' or site_config.
        shopify_shop_url = frappe.db.get_single_value("Shopify Settings", "shop_url")
        shopify_access_token = frappe.db.get_single_value("Shopify Settings", "access_token")

        if not shopify_shop_url or not shopify_access_token:
            frappe.throw("Shopify API credentials (Shop URL or Access Token) are not configured.")

        shopify_api_version = "2023-10" # Use a stable API version
        shopify_api_endpoint = f"https://{shopify_shop_url}/admin/api/{shopify_api_version}/price_rules.json"
        
        headers = {
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": shopify_access_token
        }

        # --- Construct Shopify Price Rule Payload ---
        price_rule_payload = {
            "price_rule": {
                "title": coupon_doc.coupon_code_name,
                "customer_selection": "all",
                "target_type": "line_item",
                "target_selection": "all",
                "allocation_method": "each",
                "once_per_customer": False,
                "usage_limit": None,
                "entitled_product_ids": [],
                "entitled_variant_ids": [],
                "entitled_collection_ids": [],
                "prerequisite_product_ids": [],
                "prerequisite_variant_ids": [],
                "prerequisite_collection_ids": [],
                "prerequisite_to_entitlement_quantity_ratio": {},
                "prerequisite_to_entitlement_purchase": {}
            }
        }

        if coupon_doc.discount_type == "Percentage":
            price_rule_payload["price_rule"]["value_type"] = "percentage"
            price_rule_payload["price_rule"]["value"] = f"-{coupon_doc.percent_value}"
        elif coupon_doc.discount_type == "Value":
            price_rule_payload["price_rule"]["value_type"] = "fixed_amount"
            price_rule_payload["price_rule"]["value"] = f"-{coupon_doc.fixed_value}"
        else:
            frappe.throw(f"Unsupported Discount Type in ERPNext: {coupon_doc.discount_type}")

        if coupon_doc.start_date_time:
            start_dt = frappe.utils.get_datetime(coupon_doc.start_date_time)
            price_rule_payload["price_rule"]["starts_at"] = start_dt.isoformat(timespec='seconds') + "Z"
        else:
            current_time = datetime.now()
            price_rule_payload["price_rule"]["starts_at"] = current_time.isoformat(timespec='seconds') + "Z"
            frappe.log_warn("ERPNext Coupon has no Start Date/Time. Using current time for Shopify Price Rule.",
                           "Create Shopify Price Rule Warning")

        if coupon_doc.duration:
            duration_str = coupon_doc.duration
            if duration_str and isinstance(start_dt, datetime):
                days = 0
                hours = 0
                minutes = 0
                for part in duration_str.split():
                    if 'd' in part:
                        days = int(part.replace('d', ''))
                    elif 'h' in part:
                        hours = int(part.replace('h', ''))
                    elif 'm' in part:
                        minutes = int(part.replace('m', ''))
                
                end_dt = start_dt + timedelta(days=days, hours=hours, minutes=minutes)
                price_rule_payload["price_rule"]["ends_at"] = end_dt.isoformat(timespec='seconds') + "Z"
            else:
                price_rule_payload["price_rule"]["ends_at"] = None # No end date if no duration
        else:
            price_rule_payload["price_rule"]["ends_at"] = None # No end date if no duration


        # 2. Make the API call to Shopify
        create_shopify_log(f"Sending payload to Shopify: {price_rule_payload}")
        response = requests.post(shopify_api_endpoint, headers=headers, json=price_rule_payload)
        response.raise_for_status() # Raise an HTTPError for bad responses (4xx or 5xx)

        shopify_response_data = response.json()
        new_price_rule = shopify_response_data.get("price_rule")

        if new_price_rule and new_price_rule.get("id"):
            shopify_id = new_price_rule.get("id")
            create_shopify_log(f"Shopify Price Rule created successfully with ID: {shopify_id}")

            # 3. Update the ERPNext Shopify Coupon Code with the new Shopify Price Rule ID
            coupon_doc.shopify_price_rule_id = str(shopify_id) # Store as string
            coupon_doc.save(ignore_permissions=True)
            frappe.db.commit() # Commit the transaction

            return {
                "status": "success",
                "message": "Shopify Price Rule created and ERPNext coupon updated.",
                "erpnext_coupon_name": coupon_doc.name,
                "shopify_price_rule_id": shopify_id
            }
        else:
            frappe.log_error(f"Shopify API did not return a valid price rule ID. Response: {shopify_response_data}", "Shopify API Error")
            frappe.throw("Shopify API did not return a valid price rule ID.")

    except frappe.exceptions.ValidationError as e:
        frappe.db.rollback()
        error_message = f"Validation Error: {e.message}"
        frappe.log_error(error_message, "Create Shopify Price Rule Error")
        return {"status": "error", "message": error_message}
    except requests.exceptions.RequestException as e:
        frappe.db.rollback()
        error_message = f"Shopify API Request Error: {e}"
        frappe.log_error(error_message, "Create Shopify Price Rule Error")
        return {"status": "error", "message": error_message}
    except Exception as e:
        error_message = f"An unexpected error occurred: {frappe.utils.get_traceback()}"
        frappe.log_error(error_message, "Create Shopify Price Rule Error")
        return {"status": "error", "message": error_message}


# Helper function to format timedelta into Frappe's Duration field format
def format_timedelta_to_frappe_duration(td: timedelta):
    """
    Converts a timedelta object into a string format compatible with Frappe's Duration field
    (e.g., "1d 5h 30m").

    Args:
        td (timedelta): The timedelta object representing the duration.

    Returns:
        str: A formatted duration string, or None if the duration is non-positive.
    """
    if td.total_seconds() <= 0:
        return None # Return None for non-positive durations

    total_seconds = int(td.total_seconds())

    days = total_seconds // (24 * 3600)
    total_seconds %= (24 * 3600)
    hours = total_seconds // 3600
    total_seconds %= 3600
    minutes = total_seconds // 60
    # Seconds are generally not included in Frappe's default Duration display for longer periods

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    
    # If the duration is less than a minute but positive, represent it as "0m"
    if not parts and td.total_seconds() > 0:
        return "0m"

    return " ".join(parts) if parts else None # Return None if no parts (e.g., 0 duration or very small)

@frappe.whitelist()
def import_shopify_coupons():
    try:
        shopify_setting, headers = get_shopify_prefs()
        shopify_api_version = "2025-04"
        shopify_api_endpoint = f"https://{shopify_setting.shopify_url}/admin/api/{shopify_api_version}/price_rules.json"
        response = requests.get(shopify_api_endpoint, headers=headers)
        response.raise_for_status()
        data = response.json()
        price_rules = data.get("price_rules", [])
        imported = 0
        skipped = 0
        for price_rule in price_rules:
            exists = frappe.db.exists("Shopify Coupon Code", {"coupon_code_name": price_rule.get("title")})
            if exists:
                skipped += 1
                continue
            process_shopify_price_rule_webhook({"price_rule": price_rule})
            imported += 1
        return {
            "status": "success",
            "imported_count": imported,
            "skipped_count": skipped
        }
    except Exception as e:
        frappe.log_error(frappe.utils.get_traceback(), "Shopify Import Coupons Error")
        return {"status": "error", "error": str(e)}

@frappe.whitelist()
def get_active_shopify_coupons(doctype, txt, searchfield, start, page_len, filters):
    return frappe.db.get_values(
        "Shopify Coupon Code",
        filters={"status": "Active"},
        fieldname=["name", "coupon_code_name"],
        as_dict=False
    )

@frappe.whitelist()
def get_shopify_coupon_code_percent_value(coupon_code):
    """
    Accepts a single coupon code (Link field).
    Returns the percent_value for the selected coupon code.
    Args:
        coupon_code (str): The name of the Shopify Coupon Code DocType.
    Returns:
        dict: {"percent_value": float}
    """
    percent_value = 0.0
    if coupon_code:
        percent_value = frappe.db.get_value("Shopify Coupon Code", coupon_code, "percent_value") or 0.0
    return {
        "percent_value": float(percent_value)
    }

@frappe.whitelist()
def update_shopify_coupon_code(coupon_code_name):
    try:
        coupon_doc = frappe.get_doc("Shopify Coupon Code", coupon_code_name)
        if not coupon_doc.shopify_price_rule_id:
            frappe.throw(f"Shopify Price Rule ID is not set for Coupon Code: {coupon_code_name}")

        status = get_shopify_discount_status(coupon_doc.shopify_price_rule_id)
        if status:
            if status == "ACTIVE": 
                frappe.db.set_value("Shopify Coupon Code", coupon_code_name, "status", "Active")
            else:
                frappe.db.set_value("Shopify Coupon Code", coupon_code_name, "status", "Expired")
    except Exception as e:
        frappe.log_error(frappe.utils.get_traceback(), "Update Shopify Coupon Code Error")
        return {"status": "error", "message": str(e)}
    return {"status": "success", "message": f"Coupon Code '{coupon_code_name}' updated successfully."}
            

def get_shopify_discount_status(shopify_price_rule_id):
    """
    Fetch the discount status from Shopify using GraphQL API given a price rule ID.

    Args:
        shopify_price_rule_id (str or int): The Shopify price rule ID.
        shopify_url (str): Your Shopify store URL (e.g., 'development-togglehead.myshopify.com').
        access_token (str): Shopify Admin API access token.

    Returns:
        str: Discount status (e.g., "ACTIVE", "EXPIRED", etc.) or None if not found.
    """
    shopify_setting, headers = get_shopify_prefs()
    
    graphql_url = f"https://{shopify_setting.shopify_url}/admin/api/2025-04/graphql.json"
    gid = f"gid://shopify/DiscountCodeNode/{shopify_price_rule_id}"
    query = """
    query {
      discountNode(id: "%s") {
        id
        discount {
          ...DiscountFields
        }
      }
    }

    fragment DiscountFields on Discount {
      ... on DiscountCodeApp {
        status
      }
      ... on DiscountCodeBasic {
        status
      }
      ... on DiscountCodeBxgy {
        status
      }
      ... on DiscountCodeFreeShipping {
        status
      }
    }
    """ % gid

    response = requests.post(graphql_url, headers=headers, json={"query": query})
    response.raise_for_status()

    data = response.json()
    discount_node = data.get("data", {}).get("discountNode")

    if discount_node:
        return discount_node.get("discount", {}).get("status")
    
    return None

def expire_coupon_code_on_sales_order_submit(doc, method):
    if doc.shopify_coupon_code:
        frappe.db.set_value("Shopify Coupon Code", doc.shopify_coupon_code, "status", "Expired")
        
        # Create URL-safe link to the coupon code document
        # from urllib.parse import quote
        # encoded_name = quote(doc.shopify_coupon_code, safe='')
        # coupon_url = f"/app/shopify-coupon-code/{encoded_name}"
        if doc.shopify_sync_status == "Failed":
            price_rule_id = frappe.db.get_value("Shopify Coupon Code", doc.shopify_coupon_code, "shopify_price_rule_id")
            
            shop_url = frappe.db.get_value(SETTING_DOCTYPE, None, "shopify_url")
            shopify_admin_url = shopify_url_to_admin_url(shop_url)
            discount_url = f"{shopify_admin_url}discounts/{price_rule_id}"
            
            frappe.throw(
                f"Visit shopify admin page to expire the coupon <a href=\"{discount_url}\" target=\"_blank\">{doc.shopify_coupon_code}</a>."
            )


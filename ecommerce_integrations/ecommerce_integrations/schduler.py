import frappe
from frappe.utils import now_datetime,get_datetime
from ecommerce_integrations.shopify.product import upload_item_to_shopify

BATCH_SIZE = 500

@frappe.whitelist()
def cron_sync_items_to_shopify():
    """Sync only items updated after last sync, in batches of 500"""

    setting = frappe.get_doc("Shopify Setting")
    if not setting.enable_shopify:
        return

    # If first run, set an old default timestamp
    last_sync_time = get_datetime(setting.last_updated_erpnext_to_shopify or "2025-11-01 00:00:00")

    # Get only items modified after last sync
    items = frappe.get_all(
        "Item",
        filters={"modified": (">", last_sync_time), "disabled": 0},
        pluck="name",
        order_by="modified asc"
    )

    total_items = len(items)
    if total_items == 0:
        frappe.log_error("Sync Items To Shopify", "No Records Found For SHopify")
        return

    latest_modified = last_sync_time

    # Process in batches
    for start in range(0, total_items, BATCH_SIZE):
        batch = items[start : start + BATCH_SIZE]

        for item_code in batch:
            try:
                upload_item_to_shopify(item_code)

                # Track last modified timestamp
                item_modified = frappe.db.get_value("Item", item_code, "modified")
                item_modified = get_datetime(item_modified)

                if item_modified and item_modified > latest_modified:
                    latest_modified = item_modified

            except Exception:
                frappe.log_error(
                    frappe.get_traceback(),
                    f"Shopify Sync Failed for Item {item_code}"
                )

        # Commit after each batch
        frappe.db.commit()

        # API safety delay
        frappe.sleep(2)

    # Update last sync time after full process
    frappe.db.set_value(
        "Shopify Setting",
        setting.name,
        "last_updated_erpnext_to_shopify",
        latest_modified
    )
    frappe.db.commit()


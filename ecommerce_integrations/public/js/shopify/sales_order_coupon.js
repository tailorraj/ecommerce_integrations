frappe.ui.form.on('Sales Order', {
    onload: function (frm) {
        frm.set_query("shopify_coupon_code", function () {
            return {
                filters: {
                    "status": "Active",
                }
            };
        });
    },
    shopify_coupon_code: function (frm) {
        if (frm.doc.shopify_coupon_code) {
            frappe.call({
                method: "ecommerce_integrations.shopify.coupon.get_shopify_coupon_code_percent_value",
                args: { coupon_code: frm.doc.shopify_coupon_code },
                callback: function (r) {
                    if (r.message) {
                        frm.set_value('additional_discount_percentage', r.message.percent_value || 0);
                    }
                }
            });
        } else {
            frm.set_value('additional_discount_percentage', 0);
        }
    }

});
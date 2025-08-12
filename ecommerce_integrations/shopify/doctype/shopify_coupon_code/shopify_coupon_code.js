// Copyright (c) 2025, Frappe and contributors
// For license information, please see license.txt

frappe.ui.form.on('Shopify Coupon Code', {
	refresh: function(frm) {
		frm.add_custom_button(__("Update Coupon"), function () {
			frappe.call({
				method: "ecommerce_integrations.shopify.coupon.update_shopify_coupon_code",
				args: {
					coupon_code_name: frm.doc.name
				},
				freeze: true,
				callback: function (r) {
					if (r.message && r.message.status === "success") {
						frappe.msgprint(__("Coupon updated."));
					} else {
						frappe.msgprint({
							title: __("Error"),
							message: r.message && r.message.error ? r.message.error : __("Failed to update coupons."),
							indicator: "red"
						});
					}
				}
			});
		});

	}
});
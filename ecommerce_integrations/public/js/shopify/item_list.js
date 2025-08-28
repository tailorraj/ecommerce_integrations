frappe.provide('ecommerce_integrations.shopify');

frappe.listview_settings['Item'] = {
    onload: function(listview) {
        listview.page.add_inner_button(__('Batch Update Price'), function() {
            const d = new frappe.ui.Dialog({
                title: 'Batch Update Price',
                fields: [
                    {fieldtype: 'Small Text', fieldname: 'csv', label: 'CSV (Item Code,Shopify Selling Rate)', reqd: 1},
                    {fieldtype: 'HTML', fieldname: 'progress_html', label: 'Progress'}
                ],
                primary_action_label: 'Start',
                primary_action: function() {
                    const csv = d.get_value('csv');
                    if (!csv) return;
                    // kick off background job
                    frappe.call({
                        method: 'ecommerce_integrations.shopify.product.batch_update_prices_from_csv',
                        args: { csv: csv },
                        freeze: true,
                        callback: function(r) {
                            frappe.msgprint(__('Batch update started. Progress will be shown here.'));
                        }
                    });
                }
            });

            d.show();

            const messages = [];
            const handler = function(data) {
                if (!data) return;
                const total = data.total || 0;
                const progress = data.progress || 0;
                const current = data.current || '';
                const status = data.status || '';
                const result = data.result || '';
                const message = data.message || '';

                if (message) {
                    messages.push(`${result.toUpperCase()}: ${frappe.utils.escape_html(message)}`);
                }

                const pct = total ? Math.round((progress / total) * 100) : 0;
                const html = `\n+                    <div style="margin: 10px 0;">\n+                        <div><strong>${progress}/${total}</strong> — ${frappe.utils.escape_html(current)} — ${status}</div>\n+                        <div style="background:#f1f1f1;border-radius:3px;height:10px;width:100%;margin-top:6px;">\n+                            <div style="width:${pct}%;background:#5b8ef8;height:100%;border-radius:3px"></div>\n+                        </div>\n+                        <div style=\"margin-top:8px;max-height:200px;overflow:auto;padding:6px;background:#fff;border:1px solid #eee;border-radius:4px\">${messages.join('<br>')}</div>\n+                    </div>`;

                d.fields_dict.progress_html.$wrapper.html(html);

                if (status === 'done' || progress >= total) {
                    frappe.realtime.off('shopify_price_batch', handler);
                }
            };

            frappe.realtime.on('shopify_price_batch', handler);

            d.onhide = function() { frappe.realtime.off('shopify_price_batch', handler); };
        });
    }
};

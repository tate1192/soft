from odoo import fields, models, api

class CrmLead(models.Model):
    _inherit = 'crm.lead'

    product_id = fields.Many2one('product.product', string='Requested Product')
    requested_quantity = fields.Float(string='Requested Quantity', default=1.0)
    # requested_price = fields.Float(string='Requested Price')
    customer_description = fields.Text(string='Customer Description')

    @api.model
    def create_quote_request(self, partner_id, product_id, qty, requested_price, description):
        """Create a CRM lead from website quote request."""
        partner = self.env['res.partner'].browse(partner_id)
        product = self.env['product.product'].browse(product_id)
        lead = self.create({
            'name': f"Quote Request for {product.name}",
            'partner_id': partner.id,
            'product_id': product.id,
            'requested_quantity': qty,
            # 'requested_price': requested_price,
            'customer_description': description,
            'type': 'lead',
        })
        return lead